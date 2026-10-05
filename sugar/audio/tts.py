"""Text-to-speech engines.

* :class:`MeloEngine` — the vendored MeloTTS (EN_NEWEST). On the RTX 5050 with
  torch cu128 a sentence takes ~0.1 s (CPU: ~0.8 s), so streamed chunks are
  ready long before the previous one finishes playing.
* :class:`SapiEngine` — Windows' built-in speech synthesizer via PowerShell.
  Robotic, but it has no dependencies; used only if MeloTTS fails.

:class:`Synthesizer` owns the fallback order and a small cache for short
phrases Sugar says often ("Done.", "Yeah?") so they cost nothing.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import tempfile
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Protocol

import numpy as np

from sugar.config.settings import TTSSettings
from sugar.core.processes import CREATE_NO_WINDOW

log = logging.getLogger(__name__)

OUTPUT_RATE = 44100


class TTSEngine(Protocol):
    name: str
    sample_rate: int

    def load(self) -> None: ...

    def synthesize(self, text: str) -> np.ndarray: ...


def _resample(audio: np.ndarray, source_rate: int, target_rate: int) -> np.ndarray:
    if source_rate == target_rate or audio.size == 0:
        return audio.astype(np.float32, copy=False)
    import soxr

    return soxr.resample(audio.astype(np.float32, copy=False), source_rate, target_rate).astype(np.float32)


class MeloEngine:
    name = "melo"

    def __init__(self, settings: TTSSettings) -> None:
        self._settings = settings
        self._tts = None
        self._speaker_id = 0
        self._lock = threading.Lock()
        self.device = "cpu"
        self.sample_rate = OUTPUT_RATE

    def _resolve_device(self) -> str:
        import torch

        if self._settings.device == "cpu":
            return "cpu"
        if torch.cuda.is_available():
            try:
                probe = torch.ones(8, device="cuda")
                float((probe * 2).sum())  # fails here if the build lacks kernels for this GPU
                return "cuda"
            except Exception as exc:
                log.warning("CUDA present but unusable for TTS (%s); using CPU", exc)
        return "cpu"

    def load(self) -> None:
        started = time.perf_counter()
        self.device = self._resolve_device()
        offline_before = os.environ.get("HF_HUB_OFFLINE")
        os.environ["HF_HUB_OFFLINE"] = "1"  # models are cached; skip slow network checks
        try:
            self._tts = self._create()
        except Exception:
            log.info("cached MeloTTS model not found; downloading")
            if offline_before is None:
                os.environ.pop("HF_HUB_OFFLINE", None)
            else:
                os.environ["HF_HUB_OFFLINE"] = offline_before
            self._tts = self._create()
        speakers = dict(self._tts.hps.data.spk2id.items())
        self._speaker_id = speakers.get(self._settings.voice, next(iter(speakers.values())))
        self.sample_rate = int(self._tts.hps.data.sampling_rate)
        self.voices = list(speakers.keys())
        self.synthesize("Okay.")  # first CUDA call compiles kernels (~2 s)
        log.info("MeloTTS ready on %s in %.1fs (voice %s)", self.device, time.perf_counter() - started,
                 self._settings.voice)

    def _create(self):
        from melo.api import TTS

        return TTS(language="EN_NEWEST", device=self.device)

    def synthesize(self, text: str) -> np.ndarray:
        import torch

        from melo import utils as melo_utils

        tts = self._tts
        if tts is None:
            raise RuntimeError("MeloTTS is not loaded")
        text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
        s = self._settings
        with self._lock, torch.inference_mode():
            bert, ja_bert, phones, tones, lang_ids = melo_utils.get_text_for_tts_infer(
                text, tts.language, tts.hps, tts.device, tts.symbol_to_id
            )
            device = tts.device
            x = phones.to(device).unsqueeze(0)
            audio = tts.model.infer(
                x,
                torch.LongTensor([phones.size(0)]).to(device),
                torch.LongTensor([self._speaker_id]).to(device),
                tones.to(device).unsqueeze(0),
                lang_ids.to(device).unsqueeze(0),
                bert.to(device).unsqueeze(0),
                ja_bert.to(device).unsqueeze(0),
                sdp_ratio=s.sdp_ratio,
                noise_scale=s.noise_scale,
                noise_scale_w=s.noise_scale_w,
                length_scale=1.0 / max(0.5, s.speed),
            )[0][0, 0]
            return audio.float().cpu().numpy().astype(np.float32)


class SapiEngine:
    """Windows SAPI through PowerShell. Text goes through stdin, never the command line."""

    name = "sapi"
    sample_rate = 22050
    _SCRIPT = (
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        "$s.Rate = {rate}; "
        "$s.SetOutputToWaveFile($env:SUGAR_TTS_OUT); "
        "$s.Speak([Console]::In.ReadToEnd()); $s.Dispose()"
    )

    def __init__(self, settings: TTSSettings, temp_dir: Path) -> None:
        self._settings = settings
        self._temp_dir = temp_dir

    def load(self) -> None:
        if os.name != "nt":
            raise RuntimeError("SAPI is Windows-only")

    def synthesize(self, text: str) -> np.ndarray:
        import soundfile

        rate = max(-10, min(10, int(round((self._settings.speed - 1.0) * 10))))
        self._temp_dir.mkdir(parents=True, exist_ok=True)
        handle, path = tempfile.mkstemp(suffix=".wav", dir=self._temp_dir)
        os.close(handle)
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", self._SCRIPT.format(rate=rate)],
                input=text.encode("utf-8"),
                env={**os.environ, "SUGAR_TTS_OUT": path},
                capture_output=True,
                timeout=30,
                check=True,
                creationflags=CREATE_NO_WINDOW,
            )
            audio, rate_read = soundfile.read(path, dtype="float32")
            if audio.ndim > 1:
                audio = audio.mean(axis=1)
            self.sample_rate = int(rate_read)
            return audio
        finally:
            try:
                os.remove(path)
            except OSError:
                pass


class Synthesizer:
    """Fallback chain + phrase cache. Always returns float32 mono at ``OUTPUT_RATE``."""

    CACHE_MAX_CHARS = 48

    def __init__(self, engines: list[TTSEngine], cache_size: int = 96) -> None:
        self._engines = engines
        self._active: TTSEngine | None = None
        self._cache: OrderedDict[str, np.ndarray] = OrderedDict()
        self._cache_size = cache_size
        self._cache_lock = threading.Lock()
        self.output_rate = OUTPUT_RATE

    @property
    def engine_name(self) -> str:
        return self._active.name if self._active else "none"

    def load(self) -> None:
        errors = []
        for engine in self._engines:
            try:
                engine.load()
                self._active = engine
                return
            except Exception as exc:
                log.exception("TTS engine %s failed to load", engine.name)
                errors.append(f"{engine.name}: {exc}")
        raise RuntimeError("no TTS engine available: " + "; ".join(errors))

    def synthesize(self, text: str) -> np.ndarray:
        text = text.strip()
        if not text:
            return np.zeros(0, dtype=np.float32)
        key = text.lower()
        if len(text) <= self.CACHE_MAX_CHARS:
            with self._cache_lock:
                cached = self._cache.get(key)
                if cached is not None:
                    self._cache.move_to_end(key)
                    return cached
        audio = self._synthesize_with_fallback(text)
        if len(text) <= self.CACHE_MAX_CHARS and audio.size:
            with self._cache_lock:
                self._cache[key] = audio
                while len(self._cache) > self._cache_size:
                    self._cache.popitem(last=False)
        return audio

    def _synthesize_with_fallback(self, text: str) -> np.ndarray:
        if self._active is None:
            raise RuntimeError("TTS not loaded")
        try:
            raw = self._active.synthesize(text)
            return _resample(raw, self._active.sample_rate, self.output_rate)
        except Exception:
            log.exception("TTS engine %s failed on %r", self._active.name, text[:60])
        # Strip anything unusual and retry once on the same engine (g2p chokes on odd glyphs).
        ascii_text = re.sub(r"[^\x20-\x7E]", " ", text).strip()
        if ascii_text and ascii_text != text:
            try:
                return _resample(self._active.synthesize(ascii_text), self._active.sample_rate, self.output_rate)
            except Exception:
                pass
        index = self._engines.index(self._active)
        for engine in self._engines[index + 1:]:
            try:
                engine.load()
                self._active = engine
                log.warning("switched TTS to fallback engine %s", engine.name)
                return _resample(engine.synthesize(text), engine.sample_rate, self.output_rate)
            except Exception:
                log.exception("fallback TTS engine %s failed", engine.name)
        return np.zeros(0, dtype=np.float32)

    def prewarm(self, phrases: list[str]) -> None:
        for phrase in phrases:
            try:
                self.synthesize(phrase)
            except Exception:
                log.debug("prewarm failed for %r", phrase, exc_info=True)


def build_synthesizer(settings: TTSSettings, temp_dir: Path) -> Synthesizer:
    engines: list[TTSEngine] = []
    if settings.engine == "melo":
        engines.append(MeloEngine(settings))
    if settings.engine == "sapi" or settings.fallback:
        engines.append(SapiEngine(settings, temp_dir))
    return Synthesizer(engines)
