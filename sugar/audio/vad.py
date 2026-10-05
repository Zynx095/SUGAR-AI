"""Streaming voice-activity detection.

Primary: Silero VAD v6 — the ONNX model that ships inside faster-whisper,
run one 32 ms window (512 samples at 16 kHz) at a time with its recurrent
state carried across calls (~0.1 ms per window on CPU). It returns a speech
probability, which is far more robust to noise than WebRTC VAD's binary
decision and lets the endpointer use hysteresis.

Fallback: an adaptive energy detector, used only if onnxruntime or the model
file is unavailable.
"""

from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import Protocol

import numpy as np

log = logging.getLogger(__name__)

FRAME_SAMPLES = 512
CONTEXT_SAMPLES = 64
SAMPLE_RATE = 16000


class VoiceDetector(Protocol):
    name: str

    def __call__(self, frame: np.ndarray) -> float: ...

    def reset(self) -> None: ...


def _silero_model_path() -> Path | None:
    try:
        from faster_whisper.utils import get_assets_path
    except ImportError:
        return None
    assets = Path(get_assets_path())
    for candidate in sorted(assets.glob("silero_vad*.onnx"), reverse=True):
        return candidate
    return None


class SileroVAD:
    name = "silero"

    def __init__(self, model_path: Path | None = None) -> None:
        import onnxruntime

        path = model_path or _silero_model_path()
        if path is None or not path.exists():
            raise FileNotFoundError("Silero VAD model not found in faster-whisper assets")
        options = onnxruntime.SessionOptions()
        options.inter_op_num_threads = 1
        options.intra_op_num_threads = 1
        options.log_severity_level = 4
        self._session = onnxruntime.InferenceSession(
            str(path), providers=["CPUExecutionProvider"], sess_options=options
        )
        self.reset()

    def reset(self) -> None:
        self._h = np.zeros((1, 1, 128), dtype=np.float32)
        self._c = np.zeros((1, 1, 128), dtype=np.float32)
        self._context = np.zeros((1, CONTEXT_SAMPLES), dtype=np.float32)

    def __call__(self, frame: np.ndarray) -> float:
        if frame.shape[0] != FRAME_SAMPLES:
            raise ValueError(f"Silero VAD expects {FRAME_SAMPLES} samples, got {frame.shape[0]}")
        window = frame.astype(np.float32, copy=False).reshape(1, -1)
        batch = np.concatenate([self._context, window], axis=1)
        output, self._h, self._c = self._session.run(None, {"input": batch, "h": self._h, "c": self._c})
        self._context = window[:, -CONTEXT_SAMPLES:]
        return float(np.asarray(output).reshape(-1)[0])


class EnergyVAD:
    """Adaptive-threshold energy detector (fallback only)."""

    name = "energy"

    def __init__(self, margin_db: float = 12.0) -> None:
        self._margin_db = margin_db
        self.reset()

    def reset(self) -> None:
        self._noise_db = -60.0

    def __call__(self, frame: np.ndarray) -> float:
        rms = float(np.sqrt(np.mean(np.square(frame), dtype=np.float64)) + 1e-9)
        level_db = 20.0 * math.log10(rms)
        # Track the noise floor: fall fast, rise slowly.
        if level_db < self._noise_db:
            self._noise_db = 0.7 * self._noise_db + 0.3 * level_db
        else:
            self._noise_db = 0.995 * self._noise_db + 0.005 * level_db
        excess = level_db - (self._noise_db + self._margin_db)
        return 1.0 / (1.0 + math.exp(-excess / 2.0))


def create_vad() -> VoiceDetector:
    try:
        return SileroVAD()
    except Exception as exc:  # missing onnxruntime / model
        log.warning("Silero VAD unavailable (%s); using energy VAD", exc)
        return EnergyVAD()


def rms_dbfs(frame: np.ndarray) -> float:
    rms = float(np.sqrt(np.mean(np.square(frame), dtype=np.float64)))
    return 20.0 * math.log10(rms + 1e-9)
