"""Keeping Sugar from hearing (and interrupting) itself.

There is no acoustic echo canceller available to PortAudio on Windows, so
the guard uses what it can measure:

* the **reference level** — RMS of every block the speaker plays;
* the **echo coupling** — how loud that playback is at the microphone,
  learned continuously as the median mic/reference ratio while Sugar talks
  (≈0 with a headset, larger with laptop speakers);
* a **tail** after playback stops, covering output latency and room reverb.

While Sugar is speaking (or within the tail), microphone activity only
counts as speech if it is clearly louder than the predicted echo
(``margin_db`` above it). Everything else is treated as silence by the
endpointer, so echo can neither start an utterance nor interrupt Sugar.
The conversation layer adds a second check: transcripts that match what
Sugar just said are discarded.
"""

from __future__ import annotations

import math
import threading
from collections import deque


class EchoGuard:
    def __init__(self, mode: str = "auto", margin_db: float = 9.0, tail_s: float = 0.35,
                 window_s: float = 0.3, initial_coupling: float = 1.0) -> None:
        self.mode = mode
        self._margin = 10 ** (margin_db / 20.0)
        self._tail_s = tail_s
        self._window_s = window_s
        self._reference: deque[tuple[float, float]] = deque(maxlen=256)
        self._ratios: deque[float] = deque(maxlen=96)  # ~3 s of 32 ms frames
        self._lock = threading.Lock()
        self.coupling = initial_coupling
        self._last_playback = -1e9
        self.noise_rms = 1e-4

    # -------------------------------------------------------------- output side

    def note_playback(self, rms: float, t: float) -> None:
        """Called from the speaker callback for every output block."""
        if rms > 1e-4:
            with self._lock:
                self._reference.append((t, rms))
                self._last_playback = t

    def playing(self, now: float) -> bool:
        return now - self._last_playback < self._tail_s

    def reference_level(self, now: float) -> float:
        with self._lock:
            levels = [rms for t, rms in self._reference if now - t <= self._window_s]
        return max(levels) if levels else 0.0

    # -------------------------------------------------------------- input side

    def gate(self, mic_rms: float, prob: float, now: float) -> float:
        """Return the speech probability the endpointer should see."""
        if self.mode == "off":
            return prob
        if not self.playing(now):
            if prob < 0.2:  # learn the room's noise floor while nobody talks
                self.noise_rms = 0.98 * self.noise_rms + 0.02 * max(mic_rms, 1e-5)
            return prob
        reference = self.reference_level(now)
        if reference > 1e-4:
            self._ratios.append(mic_rms / reference)
            if len(self._ratios) >= 10:
                ordered = sorted(self._ratios)
                self.coupling = ordered[len(ordered) // 2]
        expected_echo = self.coupling * max(reference, self._recent_peak(now))
        if mic_rms > max(expected_echo * self._margin, self.noise_rms * 4):
            return prob
        return 0.0

    def _recent_peak(self, now: float) -> float:
        with self._lock:
            levels = [rms for t, rms in self._reference if now - t <= self._tail_s + self._window_s]
        return max(levels) if levels else 0.0

    def stats(self) -> dict[str, float]:
        return {
            "coupling_db": round(20 * math.log10(max(self.coupling, 1e-6)), 1),
            "noise_dbfs": round(20 * math.log10(max(self.noise_rms, 1e-6)), 1),
        }
