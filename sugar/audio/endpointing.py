"""Utterance segmentation and adaptive end-of-turn detection.

The endpointer turns a stream of (frame, speech-probability) pairs into
utterances. Unlike the old fixed 2.5 s silence timer it:

* keeps a **pre-roll** of audio from before the onset so first syllables are
  never clipped ("Lee Master of Puppets" → "Play Master of Puppets");
* distinguishes a **pause** (short silence — the user may continue) from the
  **end of turn**;
* decides how long a pause must last using a **semantic hint** supplied by
  the conversation layer once it has a quick transcript of what was said so
  far: a complete command ends quickly, "I want you to…" or "actually wait…"
  waits much longer;
* trims trailing silence before handing audio to Whisper (silence invites
  hallucinations like "Thank you.").

``process`` runs on the audio thread; ``set_hint``/``snapshot``/``cancel`` are
called from the asyncio thread, so shared state is guarded by a lock.
"""

from __future__ import annotations

import math
import threading
from collections import deque
from dataclasses import dataclass, field

import numpy as np

COMPLETE, INCOMPLETE, UNKNOWN = "complete", "incomplete", "unknown"


@dataclass
class EndpointConfig:
    frame_ms: float = 32.0
    threshold: float = 0.5
    negative_threshold: float = 0.35
    start_ms: int = 96
    pause_ms: int = 128
    preroll_ms: int = 400
    min_utterance_ms: int = 250
    endpoint_complete_ms: int = 380
    endpoint_default_ms: int = 750
    endpoint_incomplete_ms: int = 1700
    max_utterance_s: float = 45.0
    trailing_keep_ms: int = 200


@dataclass
class EndpointEvent:
    kind: str  # start | pause | resume | end | discard
    utterance_id: int
    t: float
    audio: np.ndarray | None = None
    voiced_ms: int = 0
    speech_start_t: float | None = None
    speech_end_t: float | None = None
    mean_prob: float = 0.0
    reason: str = ""
    extra: dict = field(default_factory=dict)


class Endpointer:
    SILENCE, SPEECH, PAUSE = "silence", "speech", "pause"

    def __init__(self, config: EndpointConfig | None = None) -> None:
        self.config = config or EndpointConfig()
        c = self.config
        self._frame_s = c.frame_ms / 1000.0
        self._start_frames = max(1, math.ceil(c.start_ms / c.frame_ms))
        self._pause_frames = max(1, math.ceil(c.pause_ms / c.frame_ms))
        self._keep_frames = max(0, math.ceil(c.trailing_keep_ms / c.frame_ms))
        self._max_frames = int(c.max_utterance_s * 1000 / c.frame_ms)
        self._preroll: deque[np.ndarray] = deque(maxlen=max(self._start_frames, math.ceil(c.preroll_ms / c.frame_ms)))
        self._lock = threading.Lock()
        self._next_id = 0
        self._reset()

    # ------------------------------------------------------------------ state

    def _reset(self) -> None:
        self.state = self.SILENCE
        self._utterance_id = 0
        self._frames: list[np.ndarray] = []
        self._run = 0
        self._silence_run = 0
        self._resume_run = 0
        self._voiced = 0
        self._prob_sum = 0.0
        self._last_voice_index = -1
        self._last_voice_t = 0.0
        self._speech_start_t = 0.0
        self._hint = UNKNOWN

    @property
    def active_utterance(self) -> int | None:
        return self._utterance_id if self.state != self.SILENCE else None

    @property
    def voiced_ms(self) -> int:
        return int(self._voiced * self.config.frame_ms)

    @property
    def mean_prob(self) -> float:
        return self._prob_sum / self._voiced if self._voiced else 0.0

    def required_silence_ms(self) -> int:
        c = self.config
        if self._hint == COMPLETE:
            return c.endpoint_complete_ms
        if self._hint == INCOMPLETE:
            return c.endpoint_incomplete_ms
        return c.endpoint_default_ms

    # ------------------------------------------------------------------ api

    def set_hint(self, utterance_id: int, completeness: str) -> None:
        with self._lock:
            if utterance_id == self._utterance_id and self.state != self.SILENCE:
                self._hint = completeness

    def snapshot(self, utterance_id: int | None = None) -> np.ndarray | None:
        """Copy of the current utterance's audio (speech plus a short tail)."""
        with self._lock:
            if self.state == self.SILENCE:
                return None
            if utterance_id is not None and utterance_id != self._utterance_id:
                return None
            return self._audio_until_last_voice()

    def cancel(self) -> None:
        with self._lock:
            self._reset()

    def process(self, frame: np.ndarray, prob: float, now: float) -> list[EndpointEvent]:
        with self._lock:
            if self.state == self.SILENCE:
                return self._in_silence(frame, prob, now)
            if self.state == self.SPEECH:
                return self._in_speech(frame, prob, now)
            return self._in_pause(frame, prob, now)

    # ------------------------------------------------------------------ transitions

    def _in_silence(self, frame: np.ndarray, prob: float, now: float) -> list[EndpointEvent]:
        c = self.config
        self._preroll.append(frame)
        if prob >= c.threshold:
            self._run += 1
            self._prob_sum += prob
        elif prob < c.negative_threshold:
            self._run = 0
            self._prob_sum = 0.0
        if self._run < self._start_frames:
            return []
        self._next_id += 1
        voiced, prob_sum = self._run, self._prob_sum
        self._reset()
        self.state = self.SPEECH
        self._utterance_id = self._next_id
        self._frames = list(self._preroll)
        self._preroll.clear()
        self._voiced = voiced
        self._prob_sum = prob_sum
        self._last_voice_index = len(self._frames) - 1
        self._last_voice_t = now
        self._speech_start_t = now - voiced * self._frame_s
        return [EndpointEvent("start", self._utterance_id, now, speech_start_t=self._speech_start_t,
                              voiced_ms=self.voiced_ms, mean_prob=self.mean_prob)]

    def _in_speech(self, frame: np.ndarray, prob: float, now: float) -> list[EndpointEvent]:
        c = self.config
        self._frames.append(frame)
        if prob >= c.threshold:
            self._mark_voice(prob, now)
            self._silence_run = 0
        elif prob < c.negative_threshold:
            self._silence_run += 1
        if len(self._frames) >= self._max_frames:
            return [self._finish(now, "max_length")]
        if self._silence_run >= self._pause_frames:
            self.state = self.PAUSE
            self._resume_run = 0
            return [EndpointEvent("pause", self._utterance_id, now, audio=self._audio_until_last_voice(),
                                  voiced_ms=self.voiced_ms, speech_start_t=self._speech_start_t,
                                  speech_end_t=self._last_voice_t, mean_prob=self.mean_prob)]
        return []

    def _in_pause(self, frame: np.ndarray, prob: float, now: float) -> list[EndpointEvent]:
        c = self.config
        self._frames.append(frame)
        if prob >= c.threshold:
            self._resume_run += 1
            if self._resume_run >= 2:
                self.state = self.SPEECH
                self._silence_run = 0
                self._hint = UNKNOWN  # new words change what "complete" means
                self._mark_voice(prob, now)
                return [EndpointEvent("resume", self._utterance_id, now, voiced_ms=self.voiced_ms)]
            return []
        self._resume_run = 0
        if len(self._frames) >= self._max_frames:
            return [self._finish(now, "max_length")]
        silence_ms = (now - self._last_voice_t) * 1000.0
        if silence_ms >= self.required_silence_ms():
            return [self._finish(now, f"silence:{self._hint}")]
        return []

    def _mark_voice(self, prob: float, now: float) -> None:
        self._voiced += 1
        self._prob_sum += prob
        self._last_voice_index = len(self._frames) - 1
        self._last_voice_t = now

    def _audio_until_last_voice(self) -> np.ndarray:
        end = min(len(self._frames), self._last_voice_index + 1 + self._keep_frames)
        if end <= 0:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(self._frames[:end]).astype(np.float32, copy=False)

    def _finish(self, now: float, reason: str) -> EndpointEvent:
        utterance_id = self._utterance_id
        voiced_ms = self.voiced_ms
        mean_prob = self.mean_prob
        start_t, end_t = self._speech_start_t, self._last_voice_t
        if voiced_ms < self.config.min_utterance_ms:
            event = EndpointEvent("discard", utterance_id, now, voiced_ms=voiced_ms, reason="too_short",
                                  mean_prob=mean_prob)
        else:
            event = EndpointEvent("end", utterance_id, now, audio=self._audio_until_last_voice(),
                                  voiced_ms=voiced_ms, speech_start_t=start_t, speech_end_t=end_t,
                                  mean_prob=mean_prob, reason=reason)
        self._reset()
        return event
