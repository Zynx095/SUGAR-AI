"""Speech recognition (faster-whisper / CTranslate2).

Two models share one GPU worker thread:

* **fast** (``small.en``, ~0.09 s per call on the RTX 5050) — partial
  transcripts while the user talks, wake-word checks, and the quick
  transcript used to judge whether a pause ends the turn;
* **final** (``large-v3-turbo`` int8_float16, ~0.26 s) — the transcript that is
  acted on. It is started speculatively when the user pauses, so it usually
  finishes inside the end-of-turn silence window.

Without a GPU both roles fall back to smaller CPU models (int8).

Recognition is pinned to English and biased with a small vocabulary
(assistant name, project names) via Whisper's hotwords prompt. A filter
rejects classic Whisper hallucinations ("Thank you.", "Thanks for
watching!") unless the audio evidence says the user really spoke.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from sugar.config.settings import STTSettings

log = logging.getLogger(__name__)

HALLUCINATIONS = {
    "thank you", "thank you very much", "thanks for watching", "thank you for watching",
    "thanks for listening", "please subscribe", "subscribe", "like and subscribe", "you", "bye", "bye bye",
    "the end", "music", "applause", "silence", "subtitles by the amara org community", "so", "uh",
    "um", "hmm", "mm", "oh", "ah", "i", "a", "the", "and", "to be continued", "see you next time",
}
_BRACKETED = re.compile(r"[\[(\*♪][^\])\*♪]*[\])\*♪]")
# Whisper's favourite inventions on noise: caption credits and web addresses.
_CAPTION_RE = re.compile(r"www\.|\.com\b|\.tv\b|\bcaption|\bsubtitle|amara|transcribed by|translated by", re.I)
_NON_LATIN = re.compile(r"[^\x00-\x7FÀ-ɏ‘-‟…]")


@dataclass
class Transcript:
    text: str
    model: str
    latency_ms: int
    duration_s: float
    avg_logprob: float = 0.0
    no_speech_prob: float = 0.0
    compression_ratio: float = 0.0
    rejected: str | None = None
    final: bool = True

    @property
    def ok(self) -> bool:
        return bool(self.text) and self.rejected is None


@dataclass
class Evidence:
    """What the VAD saw — used to tell real speech from hallucinations."""

    voiced_ms: int = 1000
    mean_prob: float = 0.9


@dataclass
class _Model:
    name: str
    compute_type: str
    model: object = field(repr=False, default=None)


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", text.lower()).strip()


def filter_transcript(text: str, *, avg_logprob: float, no_speech_prob: float, compression_ratio: float,
                      evidence: Evidence, language: str = "en") -> tuple[str, str | None]:
    """Return (cleaned text, rejection reason or None)."""
    cleaned = _BRACKETED.sub(" ", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if language == "en" and len(_NON_LATIN.findall(cleaned)) > max(2, len(cleaned) // 5):
        return cleaned, "non_english"
    if not re.search(r"[A-Za-z0-9]", cleaned):
        return "", "empty"
    if compression_ratio > 2.6:
        return cleaned, "repetition"
    weak_audio = evidence.voiced_ms < 600 or evidence.mean_prob < 0.75
    unsure_model = no_speech_prob > 0.45 or avg_logprob < -0.9
    if _normalize(cleaned) in HALLUCINATIONS and (weak_audio or unsure_model):
        return cleaned, "hallucination"
    if _CAPTION_RE.search(cleaned) and (weak_audio or avg_logprob < -0.5):
        return cleaned, "hallucination"
    if weak_audio and avg_logprob < -0.8:
        return cleaned, "low_confidence"
    if no_speech_prob > 0.8 and avg_logprob < -0.7:
        return cleaned, "no_speech"
    return cleaned, None


class SpeechRecognizer:
    def __init__(self, settings: STTSettings, download_root: Path) -> None:
        self._settings = settings
        self._download_root = download_root
        self._lock = threading.Lock()  # one GPU job at a time
        self.device = "cpu"
        self.fast: _Model | None = None
        self.final: _Model | None = None
        self.vocabulary: list[str] = list(settings.vocabulary)
        self.ready = False

    # ------------------------------------------------------------------ loading

    def _resolve_device(self) -> str:
        wanted = self._settings.device
        if wanted == "cpu":
            return "cpu"
        try:
            import torch  # noqa: F401 — puts torch's CUDA 12.8 cuBLAS on the DLL path first
        except ImportError:
            pass
        try:
            import ctranslate2

            if ctranslate2.get_cuda_device_count() > 0:
                return "cuda"
        except Exception:
            log.debug("CUDA probe failed", exc_info=True)
        if wanted == "cuda":
            log.warning("CUDA requested for STT but unavailable; using CPU")
        return "cpu"

    def _load_model(self, name: str, compute_types: list[str]) -> _Model:
        from faster_whisper import WhisperModel

        last_error: Exception | None = None
        for compute_type in compute_types:
            try:
                model = WhisperModel(
                    name, device=self.device, compute_type=compute_type, download_root=str(self._download_root)
                )
                silence = np.zeros(16000, dtype=np.float32)
                list(model.transcribe(silence, language=self._settings.language, beam_size=1,
                                      without_timestamps=True)[0])
                return _Model(name, compute_type, model)
            except Exception as exc:  # unsupported compute type on this GPU, missing model…
                last_error = exc
                log.warning("could not load %s (%s): %s", name, compute_type, exc)
        raise RuntimeError(f"failed to load Whisper model {name}: {last_error}")

    def load(self) -> None:
        started = time.perf_counter()
        self.device = self._resolve_device()
        s = self._settings
        if self.device == "cuda":
            self.final = self._load_model(s.final_model, ["int8_float16", "float16", "int8"])
            self.fast = (self.final if s.fast_model == s.final_model
                         else self._load_model(s.fast_model, ["float16", "int8_float16", "int8"]))
        else:
            self.final = self._load_model(s.cpu_final_model, ["int8", "float32"])
            self.fast = (self.final if s.cpu_fast_model == s.cpu_final_model
                         else self._load_model(s.cpu_fast_model, ["int8", "float32"]))
        self.ready = True
        log.info("STT ready on %s: final=%s/%s fast=%s/%s in %.1fs", self.device, self.final.name,
                 self.final.compute_type, self.fast.name, self.fast.compute_type, time.perf_counter() - started)

    # ------------------------------------------------------------------ inference

    def set_vocabulary(self, words: list[str]) -> None:
        seen: dict[str, None] = {}
        for word in [*self._settings.vocabulary, *words]:
            if word and word.lower() not in (w.lower() for w in seen):
                seen[word] = None
        self.vocabulary = list(seen)[:40]

    def transcribe(self, audio: np.ndarray, *, final: bool, evidence: Evidence | None = None) -> Transcript:
        """Blocking — call from the STT worker thread."""
        holder = self.final if final else self.fast
        if holder is None or holder.model is None:
            raise RuntimeError("speech recognizer is not loaded")
        duration = len(audio) / 16000.0
        started = time.perf_counter()
        # Hotword prompting biases toward names but makes Whisper hallucinate them on noise
        # ("Code Vibes" from "VS Code"), so it is opt-in; names are fuzzy-matched downstream.
        hotwords = " ".join(self.vocabulary) if self._settings.hotwords and self.vocabulary else None
        with self._lock:
            segments, _info = holder.model.transcribe(
                audio.astype(np.float32, copy=False),
                language=self._settings.language,
                task="transcribe",
                beam_size=1,
                temperature=[0.0, 0.2],
                vad_filter=False,
                without_timestamps=True,
                condition_on_previous_text=False,
                hotwords=hotwords,
                no_speech_threshold=0.6,
                log_prob_threshold=-1.0,
                compression_ratio_threshold=2.4,
            )
            segments = list(segments)
        latency = int((time.perf_counter() - started) * 1000)
        text = "".join(segment.text for segment in segments).strip()
        if segments:
            total = sum(max(1, len(s.tokens)) for s in segments)
            avg_logprob = sum(s.avg_logprob * max(1, len(s.tokens)) for s in segments) / total
            no_speech = max(s.no_speech_prob for s in segments)
            compression = max(s.compression_ratio for s in segments)
        else:
            avg_logprob, no_speech, compression = -2.0, 1.0, 0.0
        cleaned, rejected = filter_transcript(
            text, avg_logprob=avg_logprob, no_speech_prob=no_speech, compression_ratio=compression,
            evidence=evidence or Evidence(), language=self._settings.language,
        )
        return Transcript(
            text=cleaned, model=holder.name, latency_ms=latency, duration_s=duration, avg_logprob=avg_logprob,
            no_speech_prob=no_speech, compression_ratio=compression, rejected=rejected, final=final,
        )
