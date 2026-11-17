"""SID's synthetic voice: an original profile built on top of MeloTTS.

MeloTTS ``EN_NEWEST`` ships one English speaker and no pitch control, so the
character is shaped after synthesis, with a short chain of cheap, vectorised
DSP stages (a few milliseconds per sentence on the CPU):

1. **Register** — the voice is synthesised slightly *faster* and then
   resampled *down*, which lowers pitch and formants together while restoring
   the original duration. The result is a deeper, larger-bodied voice with an
   unmistakably synthetic timbre (formant and pitch move as one, which no
   human voice does).
2. **Resonance** — a short feedback comb filter adds a faint metallic ring,
   like a voice heard through a hard, small chamber.
3. **Texture** — a very shallow amplitude modulation adds an electronic grain
   without making words wobble.
4. **Tone** — a low-shelf lift for weight, a gentle presence dip to remove
   harshness, and a low-pass that keeps sibilants from hissing.
5. **Dynamics** — an RMS compressor for the calm, even delivery, then a soft
   limiter.

Every stage is deliberately mild; the profile is tuned so Whisper still
transcribes the output correctly (see ``tests/test_voice_profile.py``).
This is an original voice — nothing is cloned from any recording.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

import numpy as np


@dataclass(frozen=True)
class VoiceProfile:
    name: str
    register: float = 1.0  # < 1 lowers pitch and formants (duration is preserved)
    pitch_steps: float = 0.0  # further pitch change in semitones, formants untouched (phase vocoder)
    resonance: float = 0.0  # comb-filter wet mix, 0–0.5
    resonance_ms: float = 2.6  # comb delay; sets where the metallic ring sits
    resonance_feedback: float = 0.35
    texture: float = 0.0  # amplitude-modulation depth, 0–0.2
    texture_hz: float = 48.0
    low_shelf_db: float = 0.0  # weight below ~200 Hz
    presence_db: float = 0.0  # negative softens 2.5–4.5 kHz
    lowpass_hz: float = 0.0  # 0 = off
    compression: float = 0.0  # 0–1: ratio from 1:1 to 4:1
    gain_db: float = 0.0

    @property
    def neutral(self) -> bool:
        return (self.register == 1.0 and not self.pitch_steps and not self.resonance and not self.texture and not self.low_shelf_db
                and not self.presence_db and not self.lowpass_hz and not self.compression and not self.gain_db)


PROFILES: dict[str, VoiceProfile] = {
    # SID: deep, controlled, a little metallic. Paired with a measured speaking rate (tts.speed ≈ 0.95).
    # Measured on the RTX 5050 over 12 synthesised sentences (samples in data/voice_samples): median F0
    # 127 Hz (raw MeloTTS 203 Hz); Whisper small.en word error rate 1.0% (raw MeloTTS 1.6%); ~40 ms of
    # DSP per ~5 s sentence. At -6.5 semitones F0 reached 118 Hz but Whisper began mishearing words.
    "sid": VoiceProfile("sid", register=0.86, pitch_steps=-5.5, resonance=0.14, resonance_ms=2.2,
                        resonance_feedback=0.3, texture=0.04, texture_hz=48.0, low_shelf_db=3.0, presence_db=-1.0,
                        lowpass_hz=9500.0, compression=0.55),
    # The same register without the metallic ring and grain.
    "sid_clean": VoiceProfile("sid_clean", register=0.86, pitch_steps=-5.5, low_shelf_db=2.0, lowpass_hz=10000.0,
                              compression=0.4),
    # MeloTTS exactly as synthesised.
    "natural": VoiceProfile("natural"),
}


def get_profile(name: str | None) -> VoiceProfile:
    return PROFILES.get((name or "natural").lower(), PROFILES["natural"])


# --------------------------------------------------------------------------- filters (RBJ biquads)


def _biquad(kind: str, freq: float, rate: int, gain_db: float = 0.0, q: float = 0.707) -> tuple[np.ndarray, np.ndarray]:
    a_gain = 10 ** (gain_db / 40)
    w0 = 2 * np.pi * freq / rate
    cos_w0, sin_w0 = np.cos(w0), np.sin(w0)
    alpha = sin_w0 / (2 * q)
    if kind == "lowpass":
        b = [(1 - cos_w0) / 2, 1 - cos_w0, (1 - cos_w0) / 2]
        a = [1 + alpha, -2 * cos_w0, 1 - alpha]
    elif kind == "lowshelf":
        sq = 2 * np.sqrt(a_gain) * alpha
        b = [a_gain * ((a_gain + 1) - (a_gain - 1) * cos_w0 + sq),
             2 * a_gain * ((a_gain - 1) - (a_gain + 1) * cos_w0),
             a_gain * ((a_gain + 1) - (a_gain - 1) * cos_w0 - sq)]
        a = [(a_gain + 1) + (a_gain - 1) * cos_w0 + sq,
             -2 * ((a_gain - 1) + (a_gain + 1) * cos_w0),
             (a_gain + 1) + (a_gain - 1) * cos_w0 - sq]
    elif kind == "peak":
        b = [1 + alpha * a_gain, -2 * cos_w0, 1 - alpha * a_gain]
        a = [1 + alpha / a_gain, -2 * cos_w0, 1 - alpha / a_gain]
    else:  # pragma: no cover - programming error
        raise ValueError(kind)
    b_arr, a_arr = np.asarray(b, dtype=np.float64), np.asarray(a, dtype=np.float64)
    return b_arr / a_arr[0], a_arr / a_arr[0]


@lru_cache(maxsize=32)
def _filters(profile: VoiceProfile, rate: int) -> list[tuple[np.ndarray, np.ndarray]]:
    stages = []
    if profile.low_shelf_db:
        stages.append(_biquad("lowshelf", 200.0, rate, profile.low_shelf_db))
    if profile.presence_db:
        stages.append(_biquad("peak", 3400.0, rate, profile.presence_db, q=0.9))
    if profile.lowpass_hz and profile.lowpass_hz < rate / 2 - 500:
        stages.append(_biquad("lowpass", profile.lowpass_hz, rate))
    return stages


def _compress(audio: np.ndarray, rate: int, amount: float) -> np.ndarray:
    from scipy.signal import lfilter

    ratio = 1.0 + 3.0 * amount
    threshold = 0.12
    # RMS envelope with ~12 ms smoothing.
    coeff = np.exp(-1.0 / (0.012 * rate))
    power = lfilter([1 - coeff], [1, -coeff], audio.astype(np.float64) ** 2)
    envelope = np.sqrt(np.maximum(power, 1e-12))
    gain = np.ones_like(envelope)
    over = envelope > threshold
    gain[over] = (threshold / envelope[over]) ** (1 - 1 / ratio)
    makeup = (1 / threshold) ** ((1 - 1 / ratio) * 0.35)
    return (audio * gain * makeup).astype(np.float32)


def length_scale_factor(profile: VoiceProfile) -> float:
    """How much faster to synthesise so the register shift leaves the duration unchanged."""
    return profile.register


def apply_profile(audio: np.ndarray, rate: int, profile: VoiceProfile) -> np.ndarray:
    """Shape raw TTS output (float32 mono at ``rate``) into the profile's voice. Returns the same rate.

    The work happens at half of MeloTTS's 44.1 kHz: the profile low-passes speech below ~10 kHz anyway,
    and the phase vocoder costs half as much there.
    """
    if audio.size == 0 or profile.neutral:
        return audio.astype(np.float32, copy=False)
    import soxr
    from scipy.signal import lfilter

    x = audio.astype(np.float32, copy=False)
    peak_in = float(np.max(np.abs(x))) or 1.0
    work = rate // 2 if rate > 32000 and profile.lowpass_hz and profile.lowpass_hz < rate / 4 - 400 else rate
    final_rate = rate
    rate = work

    # 1. register: the same samples played at a lower rate (resampled up) → lower pitch and formants.
    #    Resampling straight to the working rate folds the decimation into the same pass.
    if profile.register != 1.0 or work != final_rate:
        x = soxr.resample(x, final_rate, work / profile.register).astype(np.float32)
    if profile.pitch_steps:
        import librosa

        x = librosa.effects.pitch_shift(x, sr=work, n_steps=profile.pitch_steps, n_fft=1024 if work < 32000 else 2048,
                                        res_type="soxr_hq").astype(np.float32)

    # 2. resonance: y[n] = x[n] + g·y[n−D]
    if profile.resonance:
        delay = max(1, int(round(profile.resonance_ms / 1000 * rate)))
        denominator = np.zeros(delay + 1)
        denominator[0], denominator[delay] = 1.0, -profile.resonance_feedback
        ringing = lfilter([1.0], denominator, x).astype(np.float32)
        ringing *= peak_in / (float(np.max(np.abs(ringing))) or 1.0)
        x = (1 - profile.resonance) * x + profile.resonance * ringing

    # 3. texture: shallow amplitude modulation
    if profile.texture:
        t = np.arange(x.size, dtype=np.float32) / rate
        x = x * (1 - profile.texture + profile.texture * np.cos(2 * np.pi * profile.texture_hz * t)).astype(np.float32)

    # 4. tone
    for b, a in _filters(profile, rate):
        x = lfilter(b, a, x).astype(np.float32)

    # 5. dynamics
    if profile.compression:
        x = _compress(x, rate, profile.compression)
    if profile.gain_db:
        x = x * np.float32(10 ** (profile.gain_db / 20))
    # Keep the loudness where the raw voice was, then soft-limit any peaks.
    if work != final_rate:
        x = soxr.resample(x, work, final_rate).astype(np.float32)
    peak = float(np.max(np.abs(x))) or 1.0
    x = x * np.float32(min(peak_in, 0.95) / peak)
    return np.tanh(x * 1.1).astype(np.float32) / np.float32(np.tanh(1.1))
