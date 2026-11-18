"""Text-to-speech engines.

* :class:`MeloEngine` — the vendored MeloTTS (EN_NEWEST). On the RTX 5050 with
  torch cu128 a sentence takes ~0.1 s (CPU: ~0.8 s), so streamed chunks are
  ready long before the previous one finishes playing.
* :class:`SapiEngine` — Windows' built-in speech synthesizer via PowerShell.
  Robotic, but it has no dependencies; used only if MeloTTS fails.

:class:`Synthesizer` owns the fallback order and a small cache for short
phrases SID says often ("Done.", "Yes?") so they cost nothing.

MeloTTS output is shaped into SID's voice by :mod:`sugar.audio.voice_profile`
(``tts.voice_profile``; ``natural`` turns it off).
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

from sugar.audio.voice_profile import VoiceProfile, apply_profile, get_profile, length_scale_factor
from sugar.audio.voices import profile_for
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

    def __init__(self, settings: TTSSettings, models_dir: Path | None = None) -> None:
        self._settings = settings
        self._models_dir = models_dir
        self._converter = None  # OpenVoice tone colour converter for cloned profiles
        self._converter_failed = False
        self._tts = None
        self._speaker_id = 0
        self._extra: dict[str, object] = {}  # other MeloTTS language models (the blue voice), loaded on first use
        self._lock = threading.Lock()
        self.device = "cpu"
        self.sample_rate = OUTPUT_RATE
        self.delivery = None  # () -> (pace multiplier, pause scale) from the active personality

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
        if self.profile().clone:
            self._clone_converter()
        if self.profile().model:
            self._model_for(self.profile())
        self.synthesize("Okay.")  # first CUDA call compiles kernels (~2 s)
        log.info("MeloTTS ready on %s in %.1fs (voice %s)", self.device, time.perf_counter() - started,
                 self._settings.voice)

    def _create(self, language: str = "EN_NEWEST"):
        from melo.api import TTS

        return TTS(language=language, device=self.device)

    def profile(self) -> VoiceProfile:
        return get_profile(profile_for(self._settings.voice_profile))

    def _model_for(self, profile: VoiceProfile):
        """(model, speaker id) for a profile: the main EN_NEWEST model, or another MeloTTS model on demand."""
        if not profile.model or profile.model == "EN_NEWEST":
            return self._tts, self._speaker_id
        model = self._extra.get(profile.model)
        if model is None:
            started = time.perf_counter()
            model = self._create(profile.model)
            if int(model.hps.data.sampling_rate) != self.sample_rate:
                raise RuntimeError(f"MeloTTS {profile.model} runs at a different sample rate")
            self._extra[profile.model] = model
            log.info("MeloTTS %s loaded in %.1fs for the %s voice", profile.model, time.perf_counter() - started,
                     profile.name)
        speakers = dict(model.hps.data.spk2id.items())
        return model, speakers.get(profile.speaker or "", next(iter(speakers.values())))

    def synthesize(self, text: str) -> np.ndarray:
        import torch

        from melo import utils as melo_utils

        if self._tts is None:
            raise RuntimeError("MeloTTS is not loaded")
        text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
        s = self._settings
        profile = self.profile()
        pace = max(0.5, s.speed * profile.pace * (self.delivery()[0] if self.delivery else 1.0))
        with self._lock, torch.inference_mode():
            tts, speaker_id = self._model_for(profile)
            bert, ja_bert, phones, tones, lang_ids = melo_utils.get_text_for_tts_infer(
                text, tts.language, tts.hps, tts.device, tts.symbol_to_id
            )
            device = tts.device
            x = phones.to(device).unsqueeze(0)
            audio = tts.model.infer(
                x,
                torch.LongTensor([phones.size(0)]).to(device),
                torch.LongTensor([speaker_id]).to(device),
                tones.to(device).unsqueeze(0),
                lang_ids.to(device).unsqueeze(0),
                bert.to(device).unsqueeze(0),
                ja_bert.to(device).unsqueeze(0),
                sdp_ratio=s.sdp_ratio if profile.sdp_ratio is None else profile.sdp_ratio,
                noise_scale=s.noise_scale if profile.noise_scale is None else profile.noise_scale,
                noise_scale_w=s.noise_scale_w if profile.noise_scale_w is None else profile.noise_scale_w,
                length_scale=length_scale_factor(profile) / pace,
            )[0][0, 0]
            raw = audio.float().cpu().numpy().astype(np.float32)
        if profile.clone:
            converter = self._clone_converter()
            if converter is not None:
                try:
                    return self._offset(converter.convert(raw, self.sample_rate, air_db=profile.clone_air_db))
                except Exception:
                    log.exception("voice conversion failed; using the DSP approximation for this sentence")
            profile = get_profile("sid_dsp")  # same voice, approximated without the converter
        return self._offset(apply_profile(raw, self.sample_rate, profile))

    def _offset(self, audio: np.ndarray) -> np.ndarray:
        """The user's pitch adjustment ("deeper", "higher"), on top of any voice."""
        offset = float(getattr(self._settings, "pitch_offset", 0.0) or 0.0)
        if not offset or audio.size == 0:
            return audio
        from sugar.audio.voice_profile import _psola

        return _psola(audio, self.sample_rate, 2 ** (offset / 12.0))

    def _clone_converter(self):
        """Load the OpenVoice converter once (models on D:, see voice_clone.py); None if unavailable."""
        if self._converter is not None or self._converter_failed:
            return self._converter
        from sugar.audio.voice_clone import ToneConverter
        from sugar.config.settings import ROOT_DIR

        models = self._models_dir or Path(r"D:\Sugar_Models")
        reference = Path(self._settings.voice_reference)
        if not reference.is_absolute():
            reference = ROOT_DIR / reference
        converter = ToneConverter(models / "OpenVoiceV2", reference, tau=self._settings.clone_tau,
                                  device=self._settings.device)
        try:
            converter.load()
            self._converter = converter
        except Exception as exc:
            self._converter_failed = True
            log.warning("voice cloning unavailable (%s); using the DSP approximation", exc)
        return self._converter


class SapiEngine:
    """Windows SAPI through PowerShell. Text goes through stdin, never the command line."""

    name = "sapi"
    sample_rate = 22050
    _SCRIPT = (
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        "$s.Rate = {rate}; "
        "$s.SetOutputToWaveFile($env:SID_TTS_OUT); "
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
                env={**os.environ, "SID_TTS_OUT": path},
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


# Pause after a chunk, by how the chunk ends: what a speaker would leave between those words.
PAUSE_AFTER_S = {"sentence": 0.16, "clause": 0.07, "none": 0.03}


def trim_silence(audio: np.ndarray, rate: int, threshold_db: float = -42.0, margin_s: float = 0.012) -> np.ndarray:
    """Cut the leading and trailing silence each MeloTTS call adds (the "robotic gaps" between chunks)."""
    if audio.size == 0:
        return audio
    frame = max(1, int(rate * 0.01))
    usable = audio[: audio.size - audio.size % frame] if audio.size >= frame else audio
    if usable.size < frame:
        return audio
    rms = np.sqrt(np.mean(usable.reshape(-1, frame).astype(np.float64) ** 2, axis=1))
    peak = float(rms.max()) or 1.0
    loud = np.nonzero(20 * np.log10(np.maximum(rms, 1e-9) / peak) > threshold_db)[0]
    if loud.size == 0:
        return audio
    margin = int(rate * margin_s)
    start = max(0, loud[0] * frame - margin)
    end = min(audio.size, (loud[-1] + 1) * frame + margin)
    return audio[start:end]


def tighten_pauses(audio: np.ndarray, rate: int, longest_s: float = 0.17, threshold_db: float = -42.0) -> np.ndarray:
    """Shorten MeloTTS's long internal pauses (at full stops it often leaves ~0.3 s) to ``longest_s``."""
    frame = int(rate * 0.01)
    if audio.size < frame * 4:
        return audio
    n = audio.size // frame
    rms = np.sqrt(np.mean(audio[: n * frame].reshape(n, frame).astype(np.float64) ** 2, axis=1))
    quiet = 20 * np.log10(np.maximum(rms, 1e-9) / (float(rms.max()) or 1.0)) < threshold_db
    keep_frames = int(longest_s / 0.01)
    keep = np.ones(audio.size, dtype=bool)
    run_start = None
    for i in range(n + 1):
        if i < n and quiet[i]:
            if run_start is None:
                run_start = i
            continue
        if run_start is not None and i - run_start > keep_frames:
            # keep the edges of the pause, drop its middle
            cut_from = (run_start + keep_frames // 2) * frame
            cut_to = (i - keep_frames // 2) * frame
            keep[cut_from:cut_to] = False
        run_start = None
    return audio[keep]


def join_smoothly(audio: np.ndarray, text: str, rate: int, pause_scale: float = 1.0) -> np.ndarray:
    """Trim a chunk's own silence, fade its edges and end it with a natural pause for its punctuation.

    Chunks are played back to back, so this sets the rhythm between them: a breath after a sentence, a
    short catch after a comma, and nothing that sounds like the voice stalled.
    """
    if audio.size == 0:
        return audio
    clip = tighten_pauses(trim_silence(audio, rate).astype(np.float32, copy=True), rate, 0.17 * pause_scale)
    fade = min(clip.size // 4, int(rate * 0.006))
    if fade > 1:
        ramp = np.linspace(0.0, 1.0, fade, dtype=np.float32)
        clip[:fade] *= ramp
        clip[-fade:] *= ramp[::-1]
    ending = text.rstrip()[-1:] if text.strip() else ""
    kind = "sentence" if ending in ".!?" else "clause" if ending in ",;:-" else "none"
    pause = np.zeros(int(rate * PAUSE_AFTER_S[kind] * pause_scale), dtype=np.float32)
    return np.concatenate([clip, pause])


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
        self.delivery = None  # () -> (pace multiplier, pause scale): the personality's delivery
        self.profile_pauses = None  # () -> the voice profile's pause scale

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
        pace, pause_scale = self.delivery() if self.delivery else (1.0, 1.0)
        pause_scale *= self.profile_pauses() if self.profile_pauses else 1.0
        key = f"{pace:.2f}|{pause_scale:.2f}|{text.lower()}"
        if len(text) <= self.CACHE_MAX_CHARS:
            with self._cache_lock:
                cached = self._cache.get(key)
                if cached is not None:
                    self._cache.move_to_end(key)
                    return cached
        audio = join_smoothly(self._synthesize_with_fallback(text), text, self.output_rate, pause_scale)
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


def build_synthesizer(settings: TTSSettings, temp_dir: Path, models_dir: Path | None = None) -> Synthesizer:
    engines: list[TTSEngine] = []
    if settings.engine == "melo":
        engines.append(MeloEngine(settings, models_dir))
    if settings.engine == "sapi" or settings.fallback:
        engines.append(SapiEngine(settings, temp_dir))
    synth = Synthesizer(engines)
    synth.profile_pauses = lambda: get_profile(profile_for(settings.voice_profile)).pause_scale
    return synth
