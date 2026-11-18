"""Measure SID's voices through the real TTS engine: pitch, intonation range, speaker similarity, intelligibility.

    .\\venv\\Scripts\\python.exe tools\\evaluate_voices.py [--voices sid female blue] [--save DIR]

For each voice it synthesises the same sentences with :class:`sugar.audio.tts.MeloEngine` (exactly what SID
plays), then reports

* median F0 and the 10–90 % pitch range in semitones (pYIN),
* speaker-embedding cosine similarity to every other voice (OpenVoice's reference encoder — the same
  embedding the voice clone uses), and to the ULTRON_SID.mp3 reference,
* Whisper small.en word error rate (intelligibility).

Writes a JSON report to data/voice_samples/voice_report.json. Models load from D:\\Sugar_Models.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("HF_HOME", r"D:\Sugar_Models\hf")
os.environ.setdefault("HF_HUB_OFFLINE", "1")

import numpy as np  # noqa: E402

SENTENCES = [
    "Paint. An ambitious artistic direction. Opening it now.",
    "A cat. Excellent. Finally, a task worthy of my computational resources.",
    "The build failed because the config file is missing a comma on line twelve.",
]


def _words(text: str) -> list[str]:
    return re.sub(r"[^a-z0-9 ]", " ", text.lower()).split()


def word_error_rate(reference: str, hypothesis: str) -> float:
    ref, hyp = _words(reference), _words(hypothesis)
    d = np.arange(len(hyp) + 1)
    for i, rw in enumerate(ref, 1):
        prev, d[0] = d.copy(), i
        for j, hw in enumerate(hyp, 1):
            d[j] = min(prev[j] + 1, d[j - 1] + 1, prev[j - 1] + (rw != hw))
    return float(d[-1]) / max(1, len(ref))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--voices", nargs="+", default=["sid", "female", "blue"])
    parser.add_argument("--save", type=Path, default=None, help="also write each sample as WAV here")
    args = parser.parse_args()

    import librosa
    import soundfile as sf
    import soxr
    import torch
    from faster_whisper import WhisperModel

    from sugar.audio.tts import MeloEngine
    from sugar.audio.voice_clone import ToneConverter
    from sugar.config.settings import TTSSettings

    models = Path(r"D:\Sugar_Models")
    settings = TTSSettings()
    engine = MeloEngine(settings, models)
    engine.load()
    reference = ROOT / settings.voice_reference
    encoder = ToneConverter(models / "OpenVoiceV2", reference)
    encoder.load()
    asr = WhisperModel("small.en", device="cuda" if torch.cuda.is_available() else "cpu",
                       compute_type="float16" if torch.cuda.is_available() else "int8",
                       download_root=str(models / "Whisper"))

    def embed(audio: np.ndarray, rate: int) -> np.ndarray:
        y = soxr.resample(audio.astype(np.float32), rate, encoder.sample_rate).astype(np.float32)
        with torch.no_grad():
            g = encoder._model.ref_enc(encoder._spectrogram(y).transpose(1, 2))
        v = g.squeeze().cpu().numpy()
        return v / np.linalg.norm(v)

    reference_embedding = encoder._tgt.squeeze().cpu().numpy()
    reference_embedding /= np.linalg.norm(reference_embedding)
    report: dict[str, dict] = {}
    embeddings: dict[str, np.ndarray] = {}
    for voice in args.voices:
        settings.voice_profile = voice
        pitches, vectors, errors = [], [], []
        for i, text in enumerate(SENTENCES):
            audio = engine.synthesize(text)
            rate = engine.sample_rate
            if args.save:
                args.save.mkdir(parents=True, exist_ok=True)
                sf.write(args.save / f"{voice}_{i}.wav", audio, rate)
            y16 = soxr.resample(audio, rate, 16000).astype(np.float32)
            heard = " ".join(s.text for s in asr.transcribe(y16, language="en", beam_size=1)[0])
            errors.append(word_error_rate(text, heard))
            f0, voiced, _ = librosa.pyin(y16, fmin=55, fmax=450, sr=16000)
            pitches.append(f0[voiced & ~np.isnan(f0)])
            vectors.append(embed(audio, rate))
        f0 = np.concatenate(pitches)
        mean = np.mean(vectors, axis=0)
        embeddings[voice] = mean / np.linalg.norm(mean)
        report[voice] = {
            "median_f0_hz": round(float(np.median(f0)), 1),
            "pitch_range_semitones": round(float(12 * np.log2(np.percentile(f0, 90) / np.percentile(f0, 10))), 1),
            "whisper_wer": round(float(np.mean(errors)), 3),
            "similarity_to_reference": round(float(embeddings[voice] @ reference_embedding), 3),
        }
    for voice in report:
        report[voice]["similarity"] = {other: round(float(embeddings[voice] @ embeddings[other]), 3)
                                       for other in report if other != voice}
    out = ROOT / "data" / "voice_samples" / "voice_report.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    for voice, row in report.items():
        print(f"{voice:8s} F0 {row['median_f0_hz']:6.1f} Hz  range {row['pitch_range_semitones']:4.1f} st  "
              f"WER {row['whisper_wer']:.2f}  similarity {row['similarity']}")
    print(f"report: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
