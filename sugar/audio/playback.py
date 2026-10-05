"""Low-latency, interruptible audio output.

A single PortAudio output stream stays open for the whole session and plays
silence when idle, so starting speech costs nothing and stopping is
immediate: :meth:`AudioPlayer.stop` fades the current block out over ~10 ms
(no click) and drops everything queued. :meth:`duck` lowers the volume while
a possible interruption is being verified.

Every output block's RMS is reported through ``on_reference`` — the echo
guard uses it to tell Sugar's own voice (picked up by the microphone) from
the user's.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import sounddevice as sd

from sugar.audio.capture import resolve_device

log = logging.getLogger(__name__)


@dataclass
class _Segment:
    samples: np.ndarray
    meta: dict[str, Any] = field(default_factory=dict)
    position: int = 0
    started: bool = False


class AudioPlayer:
    def __init__(
        self,
        sample_rate: int = 44100,
        device: str | int | None = None,
        block_size: int = 512,
        on_reference: Callable[[float, float], None] | None = None,
        on_segment: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.sample_rate = sample_rate
        self._device_spec = device
        self._block = block_size
        self._segments: deque[_Segment] = deque()
        self._lock = threading.Lock()
        self._stream: sd.OutputStream | None = None
        self._gain = 1.0
        self._target_gain = 1.0
        self._gain_step = 0.0
        self.volume = 1.0
        self._fade_out = 0  # samples left in a stop fade
        self._fade_total = 1
        self.on_reference = on_reference
        self.on_segment = on_segment
        self.last_audio_time = 0.0
        self.output_level = 0.0
        self.device_name: str | None = None
        self.underruns = 0

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        if self._stream is not None:
            return
        index = resolve_device(self._device_spec, "output")
        info = sd.query_devices(index if index is not None else sd.default.device[1], "output")
        self.device_name = info["name"]
        self._stream = sd.OutputStream(
            device=index,
            samplerate=self.sample_rate,
            channels=1,
            dtype="float32",
            blocksize=self._block,
            latency="low",
            callback=self._callback,
        )
        self._stream.start()
        log.info("speaker open: %s @ %d Hz", self.device_name, self.sample_rate)

    def stop_stream(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except sd.PortAudioError:
                pass

    def restart(self) -> None:
        self.stop_stream()
        self.start()

    @property
    def stream_ok(self) -> bool:
        return self._stream is not None and self._stream.active

    # ------------------------------------------------------------------ control

    def enqueue(self, samples: np.ndarray, meta: dict[str, Any] | None = None) -> None:
        if samples.size == 0:
            return
        with self._lock:
            self._segments.append(_Segment(samples.astype(np.float32, copy=False), meta or {}))

    def stop(self, fade_ms: float = 10.0) -> list[dict[str, Any]]:
        """Stop immediately (short fade). Returns metadata of segments that never finished."""
        with self._lock:
            dropped = [segment.meta for segment in self._segments]
            if self._segments:
                current = self._segments[0]
                self._segments.clear()
                if current.started:
                    fade = max(1, int(self.sample_rate * fade_ms / 1000))
                    remaining = current.samples[current.position:current.position + fade]
                    if remaining.size:
                        tail = _Segment(remaining.copy(), {"fade": True})
                        tail.started = True
                        self._segments.append(tail)
                        self._fade_out = remaining.size
                        self._fade_total = remaining.size
            self._target_gain = 1.0
            return dropped

    def duck(self, gain: float = 0.3, ramp_ms: float = 40.0) -> None:
        self._set_gain(gain, ramp_ms)

    def unduck(self, ramp_ms: float = 150.0) -> None:
        self._set_gain(1.0, ramp_ms)

    def _set_gain(self, target: float, ramp_ms: float) -> None:
        with self._lock:
            self._target_gain = target
            steps = max(1.0, self.sample_rate * ramp_ms / 1000.0)
            self._gain_step = abs(target - self._gain) / steps

    @property
    def is_active(self) -> bool:
        with self._lock:
            return bool(self._segments)

    def queued_seconds(self) -> float:
        with self._lock:
            samples = sum(s.samples.size - s.position for s in self._segments)
        return samples / self.sample_rate

    # ------------------------------------------------------------------ audio thread

    def _callback(self, outdata: np.ndarray, frames: int, time_info, status: sd.CallbackFlags) -> None:
        if status.output_underflow:
            self.underruns += 1
        out = np.zeros(frames, dtype=np.float32)
        events: list[tuple[str, dict[str, Any]]] = []
        with self._lock:
            filled = 0
            while filled < frames and self._segments:
                segment = self._segments[0]
                if not segment.started:
                    segment.started = True
                    if not segment.meta.get("fade"):
                        events.append(("start", segment.meta))
                take = min(frames - filled, segment.samples.size - segment.position)
                out[filled:filled + take] = segment.samples[segment.position:segment.position + take]
                segment.position += take
                filled += take
                if segment.position >= segment.samples.size:
                    self._segments.popleft()
                    if not segment.meta.get("fade"):
                        events.append(("end", segment.meta))
            gain_target, gain_step = self._target_gain, self._gain_step
            fade_left, fade_total = self._fade_out, self._fade_total
            if fade_left:
                self._fade_out = max(0, fade_left - frames)
        if filled:
            if fade_left:
                index = np.arange(frames, dtype=np.float32)
                ramp = np.clip((fade_left - index) / fade_total, 0.0, 1.0)
                out *= ramp
            gains = self._gain_curve(frames, gain_target, gain_step)
            out *= gains * self.volume
            np.clip(out, -1.0, 1.0, out=out)
            self.last_audio_time = time.perf_counter()
        outdata[:, 0] = out
        rms = float(np.sqrt(np.mean(out * out))) if filled else 0.0
        self.output_level = rms
        if self.on_reference is not None:
            self.on_reference(rms, time.perf_counter())
        if events and self.on_segment is not None:
            for kind, meta in events:
                self.on_segment(kind, meta)

    def _gain_curve(self, frames: int, target: float, step: float) -> np.ndarray | float:
        current = self._gain
        if current == target or step <= 0:
            self._gain = target
            return target
        direction = 1.0 if target > current else -1.0
        curve = current + direction * step * np.arange(1, frames + 1, dtype=np.float32)
        curve = np.minimum(curve, target) if direction > 0 else np.maximum(curve, target)
        self._gain = float(curve[-1])
        return curve
