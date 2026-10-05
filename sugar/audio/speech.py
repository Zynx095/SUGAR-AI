"""Streaming speech output.

A :class:`SpeechHandle` is one spoken reply. Text chunks are pushed into it
as the model produces them; a single synthesis thread turns each chunk into
audio and queues it on the :class:`AudioPlayer`. The first chunk starts
playing while later ones are still being generated or synthesised.

``cancel`` (barge-in, "stop") marks the handle cancelled, drops its pending
chunks and stops playback with a short fade — within one audio block.
The handle records which chunks were actually heard so the conversation
history reflects what the user really heard, not what the model wrote.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from sugar.audio.playback import AudioPlayer
from sugar.audio.tts import Synthesizer
from sugar.core.events import EventBus
from sugar.core.logging import log_event
from sugar.intelligence.response import SpeechChunker

log = logging.getLogger(__name__)
_handle_ids = itertools.count(1)


@dataclass
class SpeechHandle:
    turn_id: int | None
    id: int = field(default_factory=lambda: next(_handle_ids))
    created: float = field(default_factory=time.perf_counter)
    cancelled: bool = False
    closed: bool = False
    queued: list[str] = field(default_factory=list)
    heard: list[str] = field(default_factory=list)
    first_chunk_at: float | None = None
    first_audio_at: float | None = None
    pending: int = 0  # chunks pushed but not finished playing
    done: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def spoken_text(self) -> str:
        return " ".join(self.heard)


class SpeechOutput:
    def __init__(self, synthesizer: Synthesizer, player: AudioPlayer, bus: EventBus) -> None:
        self._synth = synthesizer
        self._player = player
        self._bus = bus
        self._loop: asyncio.AbstractEventLoop | None = None
        self._jobs: queue.Queue[tuple[SpeechHandle, str] | None] = queue.Queue()
        self._handles: dict[int, SpeechHandle] = {}
        self._worker: threading.Thread | None = None
        self._busy = threading.Event()
        self.muted = False  # text-only mode / no TTS engine: complete chunks without audio
        player.on_segment = self._on_segment_threadsafe

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        if self._worker is None:
            self._worker = threading.Thread(target=self._run, name="sugar-tts", daemon=True)
            self._worker.start()

    def shutdown(self) -> None:
        self._jobs.put(None)

    # ------------------------------------------------------------------ handles

    def begin(self, turn_id: int | None = None) -> SpeechHandle:
        handle = SpeechHandle(turn_id)
        self._handles[handle.id] = handle
        return handle

    def say(self, handle: SpeechHandle, text: str) -> None:
        text = text.strip()
        if not text or handle.cancelled:
            return
        if handle.first_chunk_at is None:
            handle.first_chunk_at = time.perf_counter()
        handle.pending += 1
        handle.queued.append(text)
        if self.muted:
            meta = {"handle": handle.id, "text": text}
            self._on_segment("start", meta)
            self._on_segment("end", meta)
            return
        self._jobs.put((handle, text))

    def close(self, handle: SpeechHandle) -> None:
        handle.closed = True
        self._maybe_done(handle)

    def cancel(self, handle: SpeechHandle | None = None) -> list[SpeechHandle]:
        """Cancel one handle (or all active ones) and silence the speaker."""
        targets = [handle] if handle is not None else [h for h in self._handles.values() if not h.done.is_set()]
        cancelled = []
        for target in targets:
            if target is None or target.done.is_set():
                continue
            target.cancelled = True
            cancelled.append(target)
        if cancelled:
            self._player.stop()
            for target in cancelled:
                target.pending = 0
                self._finish(target, interrupted=True)
        return cancelled

    @property
    def speaking(self) -> bool:
        return self._player.is_active or self._busy.is_set() or any(
            not h.done.is_set() and h.pending > 0 for h in self._handles.values()
        )

    async def speak(self, text: str, turn_id: int | None = None, *, wait: bool = False) -> SpeechHandle:
        """Speak a complete text (chunked so it starts quickly)."""
        handle = self.begin(turn_id)
        chunker = SpeechChunker(max_spoken_chars=None)
        for chunk in [*chunker.feed(text), *chunker.flush()]:
            self.say(handle, chunk)
        self.close(handle)
        if wait:
            await handle.done.wait()
        return handle

    def speak_raw(self, handle: SpeechHandle, text: str) -> None:
        """Push already-speakable text (no markdown processing)."""
        self.say(handle, text)

    # ------------------------------------------------------------------ synthesis thread

    def _run(self) -> None:
        while True:
            job = self._jobs.get()
            if job is None:
                return
            handle, text = job
            if handle.cancelled:
                continue
            self._busy.set()
            started = time.perf_counter()
            try:
                audio = self._synth.synthesize(text)
            except Exception:
                log.exception("synthesis failed")
                audio = None
            finally:
                self._busy.clear()
            elapsed = int((time.perf_counter() - started) * 1000)
            if handle.cancelled:
                continue
            if audio is None or audio.size == 0:
                self._call(self._segment_done, handle, text, False)
                continue
            log_event("TTS_CHUNK", turn=handle.turn_id, ms=elapsed, chars=len(text))
            self._bus.publish("tts.chunk", turn_id=handle.turn_id, text=text, synth_ms=elapsed,
                              engine=self._synth.engine_name)
            meta = {"handle": handle.id, "text": text}
            if not self._player.stream_ok:
                # No working speaker (unplugged / headless): don't let the turn hang waiting for playback.
                self._bus.publish("audio.speaker", status="unavailable")
                self._call(self._on_segment, "start", meta)
                self._call(self._on_segment, "end", meta)
                continue
            self._player.enqueue(audio, meta)

    # ------------------------------------------------------------------ playback callbacks

    def _on_segment_threadsafe(self, kind: str, meta: dict[str, Any]) -> None:
        self._call(self._on_segment, kind, meta)

    def _call(self, fn, *args) -> None:
        loop = self._loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(fn, *args)

    def _on_segment(self, kind: str, meta: dict[str, Any]) -> None:
        handle = self._handles.get(meta.get("handle", -1))
        if handle is None or handle.cancelled:
            return
        text = meta.get("text", "")
        if kind == "start":
            if handle.first_audio_at is None:
                handle.first_audio_at = time.perf_counter()
                latency = int((handle.first_audio_at - (handle.first_chunk_at or handle.created)) * 1000)
                log_event("TTS_FIRST_AUDIO", turn=handle.turn_id, ms=latency)
                self._bus.publish("tts.first_audio", turn_id=handle.turn_id, latency_ms=latency)
                self._bus.publish("speech.started", turn_id=handle.turn_id)
            handle.heard.append(text)
            self._bus.publish("speech.segment", turn_id=handle.turn_id, text=text)
        elif kind == "end":
            self._segment_done(handle, text, True)

    def _segment_done(self, handle: SpeechHandle, text: str, played: bool) -> None:
        handle.pending = max(0, handle.pending - 1)
        self._maybe_done(handle)

    def _maybe_done(self, handle: SpeechHandle) -> None:
        if handle.closed and handle.pending == 0 and not handle.done.is_set():
            self._finish(handle, interrupted=False)

    def _finish(self, handle: SpeechHandle, interrupted: bool) -> None:
        if handle.done.is_set():
            return
        handle.done.set()
        self._handles.pop(handle.id, None)
        log_event("TTS_COMPLETE", turn=handle.turn_id, interrupted=interrupted, chunks=len(handle.heard))
        self._bus.publish("speech.finished", turn_id=handle.turn_id, interrupted=interrupted,
                          heard=handle.spoken_text)
