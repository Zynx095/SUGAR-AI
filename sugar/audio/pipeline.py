"""The always-on audio front end.

One thread owns the microphone loop:

    mic frame (32 ms) → Silero VAD → echo guard → endpointer
                                                    ├─ start / pause / resume / end / discard
                                                    └─ barge-in candidate / confirmed / rejected

Events are handed to a listener (the conversation manager) on the asyncio
loop. Nothing here blocks on models: transcription happens elsewhere, so the
microphone keeps flowing while Sugar thinks or speaks — which is what makes
interruption possible.

Barge-in: while Sugar is talking, an utterance that gets past the echo guard
first *ducks* the reply (instant feedback), and once it has lasted
``barge_in_ms`` with a strong speech probability it is confirmed and the
reply is stopped. If it fizzles out before that, the volume comes back.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Protocol

import numpy as np

from sugar.audio.capture import MicrophoneStream
from sugar.audio.echo import EchoGuard
from sugar.audio.endpointing import EndpointConfig, Endpointer, EndpointEvent
from sugar.audio.playback import AudioPlayer
from sugar.audio.vad import VoiceDetector, rms_dbfs
from sugar.config.settings import Settings
from sugar.core.events import EventBus
from sugar.core.logging import log_event

log = logging.getLogger(__name__)


class VoiceListener(Protocol):
    def on_voice_event(self, event: EndpointEvent) -> None: ...

    def on_barge_in_candidate(self, utterance_id: int) -> None: ...

    def on_barge_in(self, utterance_id: int, detected_at: float) -> None: ...

    def on_barge_in_rejected(self, utterance_id: int) -> None: ...


def endpoint_config_from(settings: Settings) -> EndpointConfig:
    v = settings.vad
    return EndpointConfig(
        frame_ms=settings.audio.block_size / settings.audio.sample_rate * 1000.0,
        threshold=v.threshold,
        negative_threshold=v.negative_threshold,
        start_ms=v.start_ms,
        pause_ms=v.pause_ms,
        preroll_ms=settings.audio.preroll_ms,
        min_utterance_ms=v.min_utterance_ms,
        endpoint_complete_ms=v.endpoint_complete_ms,
        endpoint_default_ms=v.endpoint_default_ms,
        endpoint_incomplete_ms=v.endpoint_incomplete_ms,
        max_utterance_s=v.max_utterance_s,
    )


class VoicePipeline:
    LEVEL_INTERVAL_S = 1 / 15

    def __init__(
        self,
        settings: Settings,
        bus: EventBus,
        mic: MicrophoneStream,
        vad: VoiceDetector,
        echo: EchoGuard,
        player: AudioPlayer,
    ) -> None:
        self._settings = settings
        self._bus = bus
        self.mic = mic
        self.vad = vad
        self.echo = echo
        self.player = player
        self.endpointer = Endpointer(endpoint_config_from(settings))
        self._listener: VoiceListener | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._running = threading.Event()
        self._paused = threading.Event()
        self._last_level = 0.0
        self._barge: dict[str, object] = {}
        self._restart_backoff = 1.0
        self._next_restart = 0.0
        self.frames = 0
        player.on_reference = echo.note_playback

    # ------------------------------------------------------------------ lifecycle

    def attach(self, listener: VoiceListener, loop: asyncio.AbstractEventLoop) -> None:
        self._listener = listener
        self._loop = loop

    def start(self) -> None:
        self.mic.start()
        self._running.set()
        self._thread = threading.Thread(target=self._run, name="sugar-audio", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running.clear()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self.mic.stop()

    @property
    def paused(self) -> bool:
        return self._paused.is_set()

    def pause(self) -> None:
        """Privacy mode: the microphone is closed, not just ignored."""
        if self._paused.is_set():
            return
        self._paused.set()
        self.endpointer.cancel()
        self.mic.stop()
        self._bus.publish("audio.mic", status="paused")

    def resume(self) -> None:
        if not self._paused.is_set():
            return
        self.mic.flush()
        self.vad.reset()
        try:
            self.mic.start()
            self._paused.clear()
            self._bus.publish("audio.mic", status="listening", device=self.mic.device_name)
        except Exception as exc:
            self._bus.publish("audio.mic", status="error", error=str(exc))

    def cancel_utterance(self) -> None:
        self.endpointer.cancel()
        self._barge.clear()

    # ------------------------------------------------------------------ loop

    def _run(self) -> None:
        frame_s = self._settings.audio.block_size / self._settings.audio.sample_rate
        while self._running.is_set():
            if self._paused.is_set():
                time.sleep(0.05)
                continue
            frame = self.mic.read(timeout=0.5)
            now = time.perf_counter()
            if frame is None:
                self._watchdog(now)
                continue
            self.frames += 1
            try:
                self._process(frame, now, frame_s)
            except Exception:  # never let one bad frame kill the microphone thread
                log.exception("audio frame processing failed")

    def _process(self, frame: np.ndarray, now: float, frame_s: float) -> None:
        prob = self.vad(frame)
        mic_rms = float(np.sqrt(np.mean(frame * frame)))
        gated = self.echo.gate(mic_rms, prob, now)
        events = self.endpointer.process(frame, gated, now)

        if now - self._last_level >= self.LEVEL_INTERVAL_S:
            self._last_level = now
            level = max(0.0, min(1.0, (rms_dbfs(frame) + 60.0) / 50.0))
            self._bus.publish("audio.level", mic=round(level, 3), vad=round(prob, 2),
                              out=round(min(1.0, self.player.output_level * 4), 3))

        for event in events:
            self._post(self._deliver, event)
        self._track_barge_in(events, now)

    def _track_barge_in(self, events: list[EndpointEvent], now: float) -> None:
        v = self._settings.vad
        for event in events:
            if event.kind == "start" and self.echo.playing(now):
                self._barge = {"id": event.utterance_id, "confirmed": False}
                self._post(self._candidate, event.utterance_id)
            elif event.kind in ("end", "discard") and self._barge.get("id") == event.utterance_id:
                if not self._barge.get("confirmed"):
                    self._post(self._rejected, event.utterance_id)
                self._barge = {}
        barge_id = self._barge.get("id")
        if barge_id is None or self._barge.get("confirmed"):
            return
        if self.endpointer.active_utterance != barge_id:
            return
        if self.endpointer.voiced_ms >= v.barge_in_ms and self.endpointer.mean_prob >= v.barge_in_threshold:
            self._barge["confirmed"] = True
            log_event("BARGE_IN", utterance=barge_id, voiced_ms=self.endpointer.voiced_ms)
            self._post(self._confirmed, barge_id, now)

    def _watchdog(self, now: float) -> None:
        if not self.mic.stalled(2.5) and self.mic.active:
            return
        if now < self._next_restart:
            return
        log.warning("microphone stalled or disconnected; reopening")
        self._bus.publish("audio.mic", status="reconnecting")
        try:
            self.mic.restart()
            self._restart_backoff = 1.0
            self._bus.publish("audio.mic", status="listening", device=self.mic.device_name)
        except Exception as exc:
            self._next_restart = now + self._restart_backoff
            self._restart_backoff = min(10.0, self._restart_backoff * 2)
            self._bus.publish("audio.mic", status="error", error=str(exc))
        if not self.player.stream_ok:
            try:
                self.player.restart()
            except Exception:
                log.exception("speaker restart failed")

    # ------------------------------------------------------------------ dispatch to the loop thread

    def _post(self, fn, *args) -> None:
        loop = self._loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(fn, *args)

    def _deliver(self, event: EndpointEvent) -> None:
        if self._listener is not None:
            self._listener.on_voice_event(event)

    def _candidate(self, utterance_id: int) -> None:
        if self._listener is not None:
            self._listener.on_barge_in_candidate(utterance_id)

    def _confirmed(self, utterance_id: int, detected_at: float) -> None:
        if self._listener is not None:
            self._listener.on_barge_in(utterance_id, detected_at)

    def _rejected(self, utterance_id: int) -> None:
        if self._listener is not None:
            self._listener.on_barge_in_rejected(utterance_id)
