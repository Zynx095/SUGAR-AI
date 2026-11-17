"""Fit the cloned SID voice (OpenVoice conversion of MeloTTS) to the reference recording.

Only two things are fitted, everything else is the converted voice as-is:
  * pace   — MeloTTS speaking rate so the reference sentence lasts as long as in the recording;
  * treble — the level of the >9.5 kHz band (the converter itself stops at ~11 kHz).

Writes the "clone" section of sugar/audio/sid_voice_match.json.

    venv\\Scripts\\python.exe tools\\fit_clone.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("HF_HUB_OFFLINE", "1")

from sugar.audio.tts import MeloEngine  # noqa: E402
from sugar.audio.voice_clone import CROSSOVER_HZ, ToneConverter  # noqa: E402
from sugar.config.settings import TTSSettings, load_settings  # noqa: E402

MATCH = ROOT / "sugar" / "audio" / "sid_voice_match.json"
RATE = 44100


def ltas(y: np.ndarray) -> np.ndarray:
    import librosa

    spectrum = np.abs(librosa.stft(y, n_fft=4096, hop_length=1024)) ** 2
    energy = spectrum.sum(0)
    return 10 * np.log10(spectrum[:, energy > np.percentile(energy, 40)].mean(1) + 1e-12)


def main() -> int:
    import librosa

    settings = load_settings(use_env=False)
    match = json.loads(MATCH.read_text(encoding="utf-8"))
    reference_path = ROOT / settings.tts.voice_reference
    reference, _ = librosa.load(str(reference_path), sr=RATE, mono=True)
    text = match["reference_text"]
    converter = ToneConverter(settings.paths.models_dir / "OpenVoiceV2", reference_path, tau=settings.tts.clone_tau)
    converter.load()
    engine = MeloEngine(TTSSettings(voice_profile="natural"))
    engine.load()
    base_speed = TTSSettings().speed

    takes = [engine.synthesize(text) for _ in range(4)]
    pace = float(np.mean([t.size / RATE for t in takes]) / (reference.size / RATE))
    engine._settings = TTSSettings(voice_profile="natural", speed=base_speed * pace)
    takes = [engine.synthesize(text) for _ in range(4)]
    converted = [converter.convert(t, RATE, air_db=0.0) for t in takes]

    freqs = librosa.fft_frequencies(sr=RATE, n_fft=4096)
    diff = ltas(reference) - np.mean([ltas(c) for c in converted], axis=0)
    diff -= np.median(diff[(freqs > 300) & (freqs < 3000)])
    air = float(np.clip(np.mean(diff[(freqs > CROSSOVER_HZ + 500) & (freqs < 16000)]), -24, 24))
    duration = np.mean([c.size / RATE for c in converted])
    match["clone"] = {"pace": round(pace, 3), "air_db": round(air, 1), "tau": settings.tts.clone_tau,
                      "source": "en-newest"}
    MATCH.write_text(json.dumps(match, indent=1), encoding="utf-8")
    print(f"pace {pace:.3f} (sentence {duration:.2f} s vs {reference.size / RATE:.2f} s), treble {air:+.1f} dB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
