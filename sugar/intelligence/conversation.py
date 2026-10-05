"""The conversation manager: where hearing, thinking and speaking meet.

Responsibilities
  * consume utterance events from the audio front end;
  * schedule transcription: partials while the user talks, a quick transcript
    at each pause to judge whether the turn is over, and the final transcript
    started *speculatively* at the pause so it is ready when the turn ends;
  * wake word / engagement, hallucination and echo rejection;
  * spoken yes/no answers to pending permission questions;
  * one foreground turn at a time — new input supersedes (or, if the user was
    still mid-thought, *merges with*) the previous turn;
  * barge-in: stop speaking within one audio block, cancel the turn;
  * proactive announcements (Claude Code progress/results) that never talk
    over the user;
  * the assistant state machine, derived from the facts above.
"""

from __future__ import annotations

import asyncio
import difflib
import logging
import re
import time
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
from typing import Any

from sugar.agent.permissions import PermissionManager, PermissionRequest
from sugar.audio.endpointing import COMPLETE, EndpointEvent
from sugar.audio.speech import SpeechHandle, SpeechOutput
from sugar.audio.stt import Evidence, SpeechRecognizer, Transcript
from sugar.coding.sessions import CodingSessionManager
from sugar.config.settings import Settings
from sugar.core.events import Event, EventBus
from sugar.core.logging import log_event
from sugar.core.metrics import MetricsRecorder, TurnTrace
from sugar.core.state import AssistantState, StateMachine
from sugar.intelligence.fastpath import FastPath, Intent, normalize
from sugar.intelligence.orchestrator import Orchestrator
from sugar.intelligence.response import SpeechChunker
from sugar.intelligence.turns import assess_completeness
from sugar.intelligence.working import WorkingMemory
from sugar.memory.store import MemoryStore

log = logging.getLogger(__name__)
S = AssistantState

YES = {"yes", "yeah", "yep", "yup", "sure", "ok", "okay", "do it", "go ahead", "go for it", "allow it", "allow",
       "approve", "approved", "confirm", "confirmed", "please do", "absolutely", "of course", "yes please",
       "that's fine", "fine", "proceed", "yes do it", "yeah do it", "sure go ahead", "affirmative", "correct"}
NO = {"no", "nope", "nah", "don't", "do not", "cancel", "stop", "never mind", "nevermind", "deny", "negative",
      "not now", "no thanks", "don't do it", "leave it", "no don't", "hold off", "wait no", "no wait"}


def parse_yes_no(text: str) -> bool | None:
    phrase = normalize(text)
    phrase = re.sub(r"^(?:sugar|hey sugar)\s+", "", phrase)
    if phrase in YES:
        return True
    if phrase in NO:
        return False
    words = phrase.split()
    if words and words[0] in {"yes", "yeah", "yep", "sure", "okay", "ok"} and len(words) <= 4 and "not" not in words:
        return True
    if words and words[0] in {"no", "nope", "nah", "don't", "dont"} and len(words) <= 4:
        return False
    return None


def split_wake_word(text: str, wake_words: list[str]) -> tuple[bool, str]:
    """Detect and remove the wake word at the start (or end) of an utterance."""
    tokens = text.strip().split()
    if not tokens:
        return False, ""

    def is_wake(token: str) -> bool:
        word = re.sub(r"[^a-z]", "", token.lower())
        if not word:
            return False
        if word in wake_words:
            return True
        return len(word) >= 4 and max(difflib.SequenceMatcher(None, word, w).ratio() for w in wake_words) >= 0.8

    for index in range(min(3, len(tokens))):
        if is_wake(tokens[index]):
            prefix = [re.sub(r"[^a-z]", "", t.lower()) for t in tokens[:index]]
            if all(p in {"hey", "hi", "ok", "okay", "yo", "oh", "so", "", "um", "uh"} for p in prefix):
                remainder = " ".join(tokens[index + 1:]).lstrip(" ,.!?:;-")
                return True, remainder
    if is_wake(tokens[-1]):
        return True, " ".join(tokens[:-1]).rstrip(" ,.!?:;-")
    return False, text.strip()


@dataclass
class UtteranceState:
    id: int
    trace: TurnTrace
    during_playback: bool
    engaged: bool
    partial_task: asyncio.Task | None = None
    partial_len: int = 0
    pause_text: str | None = None
    speculative: asyncio.Future | None = None
    speculative_len: int = 0
    resumed: bool = False
    ended: bool = False
    barge_in: bool = False


@dataclass
class ActiveTurn:
    """Implements the orchestrator's TurnOutput: streams display text and speech."""

    manager: ConversationManager
    text: str
    source: str
    trace: TurnTrace
    handle: SpeechHandle
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    started: float = field(default_factory=time.perf_counter)
    task: asyncio.Task | None = None
    display: list[str] = field(default_factory=list)
    chunker: SpeechChunker = field(default_factory=SpeechChunker)
    spoke: bool = False
    used_tools: bool = False
    in_tool: bool = False
    cancelled: bool = False

    def stream(self, delta: str) -> None:
        if self.cancelled or not delta:
            return
        self.display.append(delta)
        self.manager._bus.publish("assistant.delta", turn_id=self.id, text=delta)
        for chunk in self.chunker.feed(delta):
            self._speak(chunk)

    def say(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        if self.display and not "".join(self.display).endswith(("\n", " ")):
            self.stream(" ")
        self.stream(text + "\n")

    def show(self, markdown: str) -> None:
        if self.cancelled or not markdown:
            return
        block = "\n\n" + markdown.strip() + "\n\n"
        self.display.append(block)
        self.manager._bus.publish("assistant.delta", turn_id=self.id, text=block, display_only=True)

    def ack(self, text: str) -> None:
        if not self.cancelled:
            self._speak(text)

    def _speak(self, chunk: str) -> None:
        if not self.spoke:
            self.spoke = True
            self.trace.mark("tts_first_chunk")
        self.manager._speech.say(self.handle, chunk)

    def finish(self) -> None:
        if not self.cancelled:
            for chunk in self.chunker.flush():
                self._speak(chunk)
        self.manager._speech.close(self.handle)

    @property
    def display_text(self) -> str:
        return "".join(self.display).strip()


class ConversationManager:
    def __init__(
        self,
        settings: Settings,
        bus: EventBus,
        state: StateMachine,
        metrics: MetricsRecorder,
        speech: SpeechOutput,
        orchestrator: Orchestrator,
        fastpath: FastPath,
        permissions: PermissionManager,
        memory: MemoryStore,
        working: WorkingMemory,
        sessions: CodingSessionManager,
        shared_state: dict[str, Any],
        stt: SpeechRecognizer | None = None,
        pipeline: Any | None = None,
    ) -> None:
        self._settings = settings
        self._bus = bus
        self._state = state
        self._metrics = metrics
        self._speech = speech
        self._orchestrator = orchestrator
        self._fastpath = fastpath
        self._permissions = permissions
        self._memory = memory
        self._working = working
        self._sessions = sessions
        self._shared = shared_state
        self._stt = stt
        self._pipeline = pipeline
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stt_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sugar-stt")
        self._stt_jobs = 0
        self._utterances: dict[int, UtteranceState] = {}
        self._turn: ActiveTurn | None = None
        self._engaged_until = 0.0
        self._announcements: deque[tuple[str, str, float]] = deque(maxlen=10)
        self._recent_speech: deque[str] = deque(maxlen=8)
        self._last_reply_spoken = ""
        self._transcribing = 0
        self.conversation_id = self._pick_conversation()
        permissions.on_request = self._ask_permission
        sessions.on_announce = self.announce
        bus.subscribe("speech.*", self._on_speech_event)
        bus.subscribe("tool.*", self._on_tool_event)

    # ------------------------------------------------------------------ lifecycle

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def shutdown(self) -> None:
        self._stt_pool.shutdown(wait=False, cancel_futures=True)

    def _pick_conversation(self) -> str:
        last = self._memory.last_conversation()
        if last:
            turns = self._memory.recent_turns(last, 1)
            if turns and time.time() - turns[-1]["ts"] < 30 * 60:
                return last  # continue a conversation interrupted by a restart
        return datetime.now().strftime("%Y%m%d-%H%M%S")

    def new_conversation(self) -> None:
        self.conversation_id = datetime.now().strftime("%Y%m%d-%H%M%S")
        self._bus.publish("conversation.reset", id=self.conversation_id)

    # ------------------------------------------------------------------ engagement

    def is_engaged(self) -> bool:
        return self._settings.conversation.engagement == "always" or time.monotonic() < self._engaged_until

    def engage(self, seconds: float | None = None) -> None:
        self._engaged_until = time.monotonic() + (seconds or self._settings.conversation.engagement_timeout_s)
        self._bus.publish("conversation.engaged", engaged=True)

    def disengage(self) -> None:
        self._engaged_until = 0.0
        self._bus.publish("conversation.engaged", engaged=False)

    # ------------------------------------------------------------------ state

    def _settle_state(self, reason: str = "") -> None:
        if self._pipeline is not None and self._pipeline.paused:
            target = S.PAUSED
        elif any(not u.ended for u in self._utterances.values() if u.engaged or u.during_playback):
            target = S.LISTENING
        elif self._transcribing:
            target = S.TRANSCRIBING
        elif self._speech.speaking:
            target = S.SPEAKING
        elif self._turn is not None:
            target = S.TOOL_EXECUTION if self._turn.in_tool else S.THINKING
        else:
            target = S.IDLE
        if self._state.state == S.PAUSED and target != S.PAUSED:
            self._state.transition(S.IDLE, "resumed")
        if not self._state.transition(target, reason):
            self._state.force(target, f"settle: {reason}")
        if target == S.IDLE:
            self._drain_announcements()

    def _on_speech_event(self, event: Event) -> None:
        if event.type == "speech.finished":
            heard = event.data.get("heard")
            if heard:
                self._last_reply_spoken = heard
            if self._turn is None:
                self.engage()  # keep the conversation open after Sugar finishes talking
        if event.type == "speech.segment":
            self._recent_speech.append(event.data.get("text", ""))
        self._settle_state(event.type)

    def _on_tool_event(self, event: Event) -> None:
        turn = self._turn
        if turn is None:
            return
        if event.type == "tool.start":
            turn.in_tool = True
            turn.used_tools = True
        elif event.type in ("tool.complete", "tool.denied"):
            turn.in_tool = False
        self._settle_state(event.type)

    # ------------------------------------------------------------------ STT plumbing

    async def _transcribe(self, audio, *, final: bool, evidence: Evidence) -> Transcript:
        assert self._stt is not None
        loop = asyncio.get_running_loop()
        self._stt_jobs += 1
        try:
            return await loop.run_in_executor(self._stt_pool, partial(self._stt.transcribe, audio, final=final,
                                                                      evidence=evidence))
        finally:
            self._stt_jobs -= 1

    # ------------------------------------------------------------------ voice events (loop thread)

    def on_voice_event(self, event: EndpointEvent) -> None:
        kind = event.kind
        if kind == "start":
            self._on_start(event)
        elif kind == "pause":
            state = self._utterances.get(event.utterance_id)
            if state is not None:
                asyncio.ensure_future(self._on_pause(state, event))
        elif kind == "resume":
            state = self._utterances.get(event.utterance_id)
            if state is not None:
                state.resumed = True
                state.speculative = None
                state.pause_text = None
        elif kind == "end":
            state = self._utterances.get(event.utterance_id)
            if state is not None:
                state.ended = True
                asyncio.ensure_future(self._on_end(state, event))
        elif kind == "discard":
            state = self._utterances.pop(event.utterance_id, None)
            if state is not None:
                state.ended = True
                if state.partial_task:
                    state.partial_task.cancel()
            self._bus.publish("vad.discarded", utterance=event.utterance_id, voiced_ms=event.voiced_ms)
            self._settle_state("discarded")

    def _on_start(self, event: EndpointEvent) -> None:
        trace = TurnTrace(source="voice")
        trace.mark("speech_start", event.speech_start_t)
        during_playback = self._speech.speaking
        state = UtteranceState(event.utterance_id, trace, during_playback, self.is_engaged())
        self._utterances[event.utterance_id] = state
        log_event("VOICE_DETECTED", utterance=event.utterance_id, playback=during_playback, engaged=state.engaged)
        self._bus.publish("vad.speech_started", utterance=event.utterance_id, engaged=state.engaged)
        if state.engaged and self._settings.stt.partials and self._stt is not None:
            state.partial_task = asyncio.ensure_future(self._partials(state))
        if not during_playback:
            self._settle_state("speech started")

    async def _partials(self, state: UtteranceState) -> None:
        interval = self._settings.stt.partial_interval_ms / 1000.0
        try:
            while not state.ended:
                await asyncio.sleep(interval)
                if state.ended or self._stt_jobs > 0 or self._pipeline is None:
                    continue
                audio = self._pipeline.endpointer.snapshot(state.id)
                if audio is None or len(audio) - state.partial_len < 16000 * 0.4:
                    continue
                state.partial_len = len(audio)
                transcript = await self._transcribe(audio, final=False, evidence=Evidence())
                if not state.ended and transcript.text and transcript.rejected is None:
                    log_event("STT_PARTIAL", utterance=state.id, text=transcript.text, ms=transcript.latency_ms)
                    self._bus.publish("stt.partial", utterance=state.id, text=transcript.text)
        except asyncio.CancelledError:
            pass

    async def _on_pause(self, state: UtteranceState, event: EndpointEvent) -> None:
        if self._stt is None or event.audio is None:
            return
        evidence = Evidence(event.voiced_ms, event.mean_prob)
        quick = await self._transcribe(event.audio, final=False, evidence=evidence)
        if state.ended or state.resumed:
            return
        state.pause_text = quick.text
        wake, remainder = split_wake_word(quick.text, self._settings.conversation.wake_words)
        addressed = state.engaged or wake
        if quick.text and quick.rejected is None and addressed:
            self._bus.publish("stt.partial", utterance=state.id, text=quick.text)
        if not addressed:
            completeness = COMPLETE  # background speech: end it quickly and ignore it
        else:
            completeness = assess_completeness(remainder if wake else quick.text, self._fastpath.is_command)
        if self._pipeline is not None:
            self._pipeline.endpointer.set_hint(state.id, completeness)
        if addressed and quick.rejected is None:
            state.speculative_len = len(event.audio)
            state.speculative = asyncio.ensure_future(self._transcribe(event.audio, final=True, evidence=evidence))

    async def _on_end(self, state: UtteranceState, event: EndpointEvent) -> None:
        trace = state.trace
        trace.mark("speech_end", event.speech_end_t)
        trace.mark("endpoint", event.t)
        if state.partial_task:
            state.partial_task.cancel()
        self._transcribing += 1
        self._settle_state("utterance ended")
        try:
            evidence = Evidence(event.voiced_ms, event.mean_prob)
            transcript: Transcript | None = None
            speculative = state.speculative
            if speculative is not None and not state.resumed and len(event.audio) - state.speculative_len < 16000 * 0.3:
                try:
                    transcript = await speculative
                    trace.info["speculative_stt"] = True
                except Exception:
                    transcript = None
            if transcript is None:
                transcript = await self._transcribe(event.audio, final=True, evidence=evidence)
            trace.mark("stt_final")
            log_event("STT_FINAL", utterance=state.id, text=transcript.text, ms=transcript.latency_ms,
                      rejected=transcript.rejected, model=transcript.model)
            self._bus.publish("stt.final", utterance=state.id, text=transcript.text, rejected=transcript.rejected,
                              latency_ms=transcript.latency_ms)
        except Exception:
            log.exception("transcription failed")
            transcript = None
        finally:
            self._transcribing -= 1
            self._utterances.pop(state.id, None)
        if transcript is None:
            self._bus.publish("error", source="stt", message="Speech recognition failed.")
            self._settle_state("stt error")
            return
        await self._handle_transcript(state, transcript)

    async def _handle_transcript(self, state: UtteranceState, transcript: Transcript) -> None:
        if not transcript.ok:
            self._settle_state("rejected transcript")
            return
        text = transcript.text
        if state.during_playback and not state.barge_in and self._looks_like_echo(text):
            log_event("ECHO_REJECTED", text=text)
            self._bus.publish("stt.echo", text=text)
            self._settle_state("echo")
            return
        wake, remainder = split_wake_word(text, self._settings.conversation.wake_words)
        if not wake and not self.is_engaged() and not state.engaged:
            self._bus.publish("stt.ignored", text=text)
            self._settle_state("not addressed")
            return
        self.engage()
        if not remainder.strip():
            await self.quick_reply("Yeah?")
            return
        await self.submit(remainder, source="voice", trace=state.trace)

    def _looks_like_echo(self, text: str) -> bool:
        heard = " ".join(self._recent_speech).lower()
        candidate = re.sub(r"[^a-z0-9 ]", "", text.lower()).strip()
        if not candidate or not heard:
            return False
        if candidate in re.sub(r"[^a-z0-9 ]", "", heard):
            return True
        ratio = difflib.SequenceMatcher(None, candidate, re.sub(r"[^a-z0-9 ]", "", heard)[-len(candidate) * 2:]).ratio()
        return ratio > 0.7

    # ------------------------------------------------------------------ barge-in

    def on_barge_in_candidate(self, utterance_id: int) -> None:
        if self._speech.speaking:
            self._speech_player().duck(0.3)
            self._bus.publish("barge_in.candidate", utterance=utterance_id)

    def on_barge_in(self, utterance_id: int, detected_at: float) -> None:
        if not self._speech.speaking:
            return
        state = self._utterances.get(utterance_id)
        if state is not None:
            state.barge_in = True
            state.engaged = True
        self._speech.cancel()
        silent_at = time.perf_counter()
        player = self._speech_player()
        player.unduck(ramp_ms=1)
        latency = int((silent_at - detected_at) * 1000)
        log_event("INTERRUPTION", utterance=utterance_id, ms=latency)
        self._bus.publish("barge_in", utterance=utterance_id, latency_ms=latency)
        if state is not None:
            state.trace.mark("interrupt_detect", detected_at)
            state.trace.mark("interrupt_silent", silent_at)
        self._state.transition(S.INTERRUPTED, "user interrupted")
        turn = self._turn
        if turn is not None:
            self._cancel_turn(turn, "interrupted")
        self.engage()
        self._settle_state("barge-in")

    def on_barge_in_rejected(self, utterance_id: int) -> None:
        self._speech_player().unduck()

    def _speech_player(self):
        return self._speech._player  # the shared AudioPlayer

    # ------------------------------------------------------------------ input entry points

    async def handle_text(self, text: str) -> None:
        """Typed input from the UI."""
        text = text.strip()
        if not text:
            return
        self.engage()
        trace = TurnTrace(source="text")
        trace.mark("received")
        await self.submit(text, source="text", trace=trace)

    async def submit(self, text: str, *, source: str, trace: TurnTrace) -> None:
        trace.mark("received")
        self._bus.publish("user.message", text=text, source=source)
        decision = parse_yes_no(text)
        pending = self._permissions.pending
        if pending is not None:
            if decision is not None:
                self._permissions.resolve(decision, pending.id)
                if not decision:
                    self._speech_now("Okay, I won't.")
                return
            self._permissions.resolve(False, pending.id)
        waiting = [s for s in self._sessions.all()
                   if s.status == "waiting_permission" and time.time() - s.last_activity < 300]
        if waiting and decision is not None:
            await self._start_turn(text, source, trace, coding_permission=decision)
            return
        intent = self._fastpath.match(text)
        if intent is not None and intent.domain == "control":
            await self._control(intent, trace)
            return
        await self._start_turn(text, source, trace)

    # ------------------------------------------------------------------ control intents

    async def _control(self, intent: Intent, trace: TurnTrace) -> None:
        name = intent.name
        turn = self._turn
        was_busy = turn is not None and (turn.used_tools or time.perf_counter() - turn.started > 1.5)
        if name in ("control.stop", "control.wait"):
            self._speech.cancel()
            if turn is not None:
                self._cancel_turn(turn, "user said " + name.split(".")[1])
            self._permissions.cancel_pending()
            if name == "control.wait":
                await self.quick_reply("Yeah?")
            elif was_busy:
                await self.quick_reply("Stopped.")
            else:
                self._settle_state("stopped")
        elif name == "control.sleep":
            self._speech.cancel()
            if turn is not None:
                self._cancel_turn(turn, "sleep")
            await self.quick_reply("Standing by.")
            self.disengage()
        elif name == "control.privacy":
            handle = await self._speech.speak("Mic's off.", wait=False)
            await handle.done.wait()
            if self._pipeline is not None:
                self._pipeline.pause()
            self._settle_state("privacy")
        elif name == "control.repeat":
            await self.quick_reply(self._last_reply_spoken or "I haven't said anything yet.")
        trace.info["route"] = "control"
        self._metrics.finish(trace)

    async def quick_reply(self, text: str) -> None:
        """Short spoken response outside of a full turn ("Yeah?", "Stopped.")."""
        handle = await self._speech.speak(text, wait=False)
        self._bus.publish("assistant.message", turn_id=None, text=text, quick=True)
        self._settle_state("quick reply")
        await handle.done.wait()

    def _speech_now(self, text: str) -> None:
        asyncio.ensure_future(self._speech.speak(text))

    # ------------------------------------------------------------------ turns

    async def _start_turn(self, text: str, source: str, trace: TurnTrace, coding_permission: bool | None = None) -> None:
        previous = self._turn
        if previous is not None and previous.task is not None and not previous.task.done():
            mid_thought = (not previous.spoke and not previous.used_tools and source == previous.source == "voice"
                           and time.perf_counter() - previous.started < 8.0)
            if mid_thought:
                text = f"{previous.text.rstrip('.')} {text}"  # the user wasn't finished: merge the two
                trace.info["merged"] = True
            self._cancel_turn(previous, "superseded")
        handle = self._speech.begin()
        turn = ActiveTurn(self, text, source, trace, handle,
                          chunker=SpeechChunker(max_spoken_chars=self._settings.tts.max_spoken_chars))
        handle.turn_id = turn.id
        self._turn = turn
        turn.task = asyncio.ensure_future(self._run_turn(turn, coding_permission))
        self._settle_state("turn started")

    def _cancel_turn(self, turn: ActiveTurn, reason: str) -> None:
        turn.cancelled = True
        self._speech.cancel(turn.handle)
        if turn.task is not None and not turn.task.done():
            turn.task.cancel()
        log_event("TURN_CANCELLED", turn=turn.id, reason=reason)
        self._bus.publish("turn.cancelled", turn_id=turn.id, reason=reason)

    async def _run_turn(self, turn: ActiveTurn, coding_permission: bool | None) -> None:
        log_event("TURN_START", turn=turn.id, source=turn.source, text=turn.text)
        self._bus.publish("turn.started", turn_id=turn.id, text=turn.text, source=turn.source)
        self._memory.add_turn(self.conversation_id, "user", turn.text, meta={"source": turn.source})
        summary = None
        try:
            if coding_permission is not None:
                summary = await self._orchestrator.answer_coding_permission(coding_permission, turn)
            else:
                summary = await self._orchestrator.handle(turn.text, turn)
            turn.finish()
            await turn.handle.done.wait()
        except asyncio.CancelledError:
            turn.cancelled = True
            turn.finish()
        except Exception as exc:
            log.exception("turn failed")
            self._bus.publish("error", source="turn", message=str(exc))
            turn.say("Something went wrong on my end. Check the developer panel for details.")
            turn.finish()
        finally:
            heard = turn.handle.spoken_text
            display = turn.display_text
            if display:
                self._memory.add_turn(self.conversation_id, "assistant", display, heard=heard,
                                      meta={"route": summary.route if summary else None,
                                            "interrupted": turn.cancelled})
                self._shared["last_reply"] = display
                self._working.last_assistant_text = display
            first_audio = turn.handle.first_audio_at
            if first_audio is not None:
                turn.trace.mark("first_audio", first_audio)
            if summary is not None:
                turn.trace.info.setdefault("route", summary.route)
                turn.trace.info["tools"] = summary.tools
            turn.trace.info["interrupted"] = turn.cancelled
            record = self._metrics.finish(turn.trace)
            log_event("TURN_COMPLETE", turn=turn.id, interrupted=turn.cancelled, **record["metrics"])
            self._bus.publish("assistant.message", turn_id=turn.id, text=display, heard=heard,
                              interrupted=turn.cancelled, route=summary.route if summary else None,
                              provider=summary.provider if summary else None,
                              model=summary.model if summary else None)
            if self._turn is turn:
                self._turn = None
            self.engage()
            self._settle_state("turn finished")

    # ------------------------------------------------------------------ permissions & announcements

    async def _ask_permission(self, request: PermissionRequest) -> None:
        question = f"I need your OK to {request.action}. Should I go ahead?"
        turn = self._turn
        if turn is not None and not turn.cancelled:
            turn.say(question)
        else:
            await self._speech.speak(question)
        self.engage(self._settings.permissions.confirmation_timeout_s + 5)

    async def announce(self, text: str, kind: str) -> None:
        """Proactive speech (e.g. Claude Code finished). Never talks over the user."""
        self._announcements.append((text, kind, time.monotonic()))
        self._bus.publish("announcement", text=text, kind=kind)
        if kind == "coding.finished":
            self._memory.add_turn(self.conversation_id, "assistant", text, meta={"kind": kind})
        self._drain_announcements()

    def _drain_announcements(self) -> None:
        if not self._announcements or self._state.state != S.IDLE or self._speech.speaking or self._turn is not None:
            return
        if any(not u.ended for u in self._utterances.values()):
            return
        text, kind, created = self._announcements.popleft()
        if kind == "coding.progress" and time.monotonic() - created > 60:
            self._drain_announcements()  # stale progress isn't worth saying
            return
        self.engage()
        asyncio.ensure_future(self._speech.speak(text))
