"""Fit SID's voice profile to a reference recording (data/voice_samples/ULTRON_SID.mp3).

What can be matched without a voice-conversion model, and is matched here:
  * pitch      — register shift + formant-preserving pitch step chosen so the median F0 equals the reference;
  * pace       — speaking rate scaled so the reference sentence takes as long as in the recording;
  * timbre     — a smoothed long-term spectrum match (an EQ curve on a 1/6-octave grid), fitted by
                 synthesising the reference sentence itself and comparing like with like;
  * treble     — part of the same curve, with an optional extra high-shelf trim (--treble-db).

The result is written to sugar/audio/sid_voice_match.json and used by the "sid" profile.

    venv\\Scripts\\python.exe tools\\fit_voice.py [--reference PATH] [--treble-db 0]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("HF_HUB_OFFLINE", "1")

from sugar.audio import voice_profile as vp  # noqa: E402
from sugar.audio.tts import MeloEngine  # noqa: E402
from sugar.config.settings import TTSSettings  # noqa: E402

OUT = ROOT / "sugar" / "audio" / "sid_voice_match.json"
RATE = 44100
N_FFT = 4096


def ltas(y: np.ndarray) -> np.ndarray:
    import librosa

    spectrum = np.abs(librosa.stft(y, n_fft=N_FFT, hop_length=1024)) ** 2
    energy = spectrum.sum(0)
    spectrum = spectrum[:, energy > np.percentile(energy, 40)]  # voiced/active frames only
    return 10 * np.log10(spectrum.mean(1) + 1e-12)


def median_f0(y: np.ndarray) -> float:
    import librosa

    f0, _, _ = librosa.pyin(librosa.resample(y, orig_sr=RATE, target_sr=16000), fmin=45, fmax=400, sr=16000)
    return float(np.nanmedian(f0))


def smooth_octave(freqs: np.ndarray, values: np.ndarray, fraction: float = 6.0) -> np.ndarray:
    out = np.empty_like(values)
    for i, f in enumerate(freqs):
        if f <= 0:
            out[i] = values[i]
            continue
        lo, hi = f * 2 ** (-0.5 / fraction), f * 2 ** (0.5 / fraction)
        out[i] = values[(freqs >= lo) & (freqs <= hi)].mean()
    return out


def transcribe(model, y: np.ndarray) -> str:
    import librosa

    segments, _ = model.transcribe(librosa.resample(y, orig_sr=RATE, target_sr=16000), language="en", beam_size=5)
    return " ".join(s.text for s in segments).strip()


def words(text: str) -> list[str]:
    return re.sub(r"[^a-z' ]", " ", text.lower()).split()


def wer(ref: str, hyp: str) -> float:
    r, h = words(ref), words(hyp)
    d = np.zeros((len(r) + 1, len(h) + 1), int)
    d[:, 0], d[0, :] = range(len(r) + 1), range(len(h) + 1)
    for i in range(1, len(r) + 1):
        for j in range(1, len(h) + 1):
            d[i, j] = min(d[i - 1, j] + 1, d[i, j - 1] + 1, d[i - 1, j - 1] + (r[i - 1] != h[j - 1]))
    return d[-1, -1] / max(1, len(r))


def main() -> int:
    import librosa
    from faster_whisper import WhisperModel

    parser = argparse.ArgumentParser()
    parser.add_argument("--reference", default=str(ROOT / "data" / "voice_samples" / "ULTRON_SID.mp3"))
    parser.add_argument("--treble-db", type=float, default=0.0, help="extra high-shelf trim above 6 kHz")
    parser.add_argument("--takes", type=int, default=4)
    args = parser.parse_args()

    reference, _ = librosa.load(args.reference, sr=RATE, mono=True)
    stt = WhisperModel("small.en", device="cuda", compute_type="float16",
                       download_root=r"D:\Sugar_Models\Whisper")
    text = transcribe(stt, reference)
    ref_f0, ref_duration, ref_ltas = median_f0(reference), len(reference) / RATE, ltas(reference)
    print(f"reference: '{text}'  F0 {ref_f0:.0f} Hz  {ref_duration:.2f} s")

    settings = TTSSettings(voice_profile="natural")
    engine = MeloEngine(settings)
    engine.load()
    base = vp.PROFILES["sid_v2"]
    natural_f0 = np.median([median_f0(engine.synthesize(text)) for _ in range(2)])

    best = None
    for register in (0.9, 0.82):
        steps = 12 * np.log2(ref_f0 / (natural_f0 * register))
        candidate = vp.VoiceProfile("fit", register=register, pitch_steps=float(steps), pitch_method="psola",
                                    compression=0.4,
                                    sdp_ratio=base.sdp_ratio, noise_scale=base.noise_scale,
                                    noise_scale_w=base.noise_scale_w)
        vp.PROFILES["fit"] = candidate
        engine._settings = TTSSettings(voice_profile="fit")
        takes = [engine.synthesize(text) for _ in range(args.takes)]
        duration = float(np.mean([len(t) / RATE for t in takes]))
        pace = duration / ref_duration
        errors = float(np.mean([wer(text, transcribe(stt, t)) for t in takes]))
        f0 = float(np.median([median_f0(t) for t in takes]))
        freqs = librosa.fft_frequencies(sr=RATE, n_fft=N_FFT)
        diff = ref_ltas - np.mean([ltas(t) for t in takes], axis=0)
        mid = (freqs > 300) & (freqs < 3000)
        diff -= np.median(diff[mid])
        distance = float(np.mean(np.abs(diff[(freqs > 80) & (freqs < 12000)])))
        print(f"register {register}: steps {steps:+.1f}  F0 {f0:.0f} Hz  WER {errors:.3f}  pace {pace:.2f}  "
              f"spectral distance {distance:.1f} dB")
        score = errors * 40 + distance
        if best is None or score < best[0]:
            best = (score, candidate, pace, diff, freqs, errors)

    _, candidate, pace, diff, freqs, errors = best
    # EQ curve: 1/6-octave smoothing, limited to ±15 dB, flattened outside 70 Hz–16 kHz.
    curve = smooth_octave(freqs, diff)
    curve = np.clip(curve, -15, np.where(freqs > 8000, 24, 15))  # treble may need more lift than the body
    curve[freqs < 70] = curve[np.argmin(np.abs(freqs - 70))]
    curve[freqs > 16000] = curve[np.argmin(np.abs(freqs - 16000))]
    grid = np.geomspace(40, 20000, 96)
    gains = np.interp(grid, freqs, curve)
    if args.treble_db:
        gains += args.treble_db * np.clip((np.log2(grid / 6000)) / 1.0, 0, 1)

    match = {
        "reference": Path(args.reference).name,
        "reference_text": text,
        "register": candidate.register,
        "pitch_steps": round(candidate.pitch_steps, 2),
        "pace": round(float(pace), 3),
        "eq_hz": [round(float(f), 1) for f in grid],
        "eq_db": [round(float(g), 2) for g in gains],
        "treble_db": args.treble_db,
    }
    OUT.write_text(json.dumps(match, indent=1), encoding="utf-8")
    print(f"wrote {OUT}  (register {candidate.register}, pitch {candidate.pitch_steps:+.1f} st, pace {pace:.2f}, "
          f"WER {errors:.3f})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
