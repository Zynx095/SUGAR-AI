"""SID's synthetic voice: an original deep, mechanical profile built on top of MeloTTS.

MeloTTS ``EN_NEWEST`` ships one (higher-pitched) English speaker and no pitch control, so the
character is shaped after synthesis. Version 2 replaces the first profile, which users found muffled
and strange: that one low-passed at 9.5 kHz, worked at 22 kHz, used librosa's plain phase vocoder (its
"underwater" smear) and added a comb ring and amplitude grain. Now:

1. **Register** — synthesised slightly faster, then resampled down: pitch *and* formants drop together,
   from a female-sized vocal tract to a male-sized one. No artefacts (it is a plain resample).
2. **Pitch** — a further drop with formants kept, through a phase vocoder with *identity phase locking*
   (Laroche & Dolson), which keeps the harmonics of each formant coherent instead of smearing them.
3. **Sub-layer** — a quiet copy one octave lower underneath the voice: weight and menace without mud.
4. **Mechanical edge** — a very short static doubling (sub-millisecond) that puts a fine metallic comb
   on the upper spectrum, the "machine speaking through metal" quality, without ringing.
5. **Tone** — chest lift, presence and air shelves (no low-pass: articulation stays crisp).
6. **Dynamics** — RMS compression for a calm, controlled delivery, then a soft limiter.

The profile also asks MeloTTS for steadier, less sing-song prosody (lower noise and SDP ratio) and a
measured pace. Everything is an original transformation — nothing is cloned from any recording.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

import numpy as np


@dataclass(frozen=True)
class VoiceProfile:
    name: str
    register: float = 1.0  # < 1 lowers pitch and formants (duration is preserved by faster synthesis)
    pitch_steps: float = 0.0  # further pitch change in semitones with formants kept
    sub_octave: float = 0.0  # level of the octave-down layer, 0–0.4
    edge: float = 0.0  # level of the short metallic doubling, 0–0.4
    edge_ms: float = 0.55
    low_shelf_db: float = 0.0  # chest, below ~160 Hz
    presence_db: float = 0.0  # articulation around 3 kHz
    air_db: float = 0.0  # high shelf above ~8 kHz
    compression: float = 0.0  # 0–1: ratio from 1:1 to 4:1
    # prosody asked of MeloTTS (None = the tts.* settings)
    sdp_ratio: float | None = None
    noise_scale: float | None = None
    noise_scale_w: float | None = None
    pace: float = 1.0  # multiplies tts.speed

    @property
    def neutral(self) -> bool:
        return (self.register == 1.0 and not self.pitch_steps and not self.sub_octave and not self.edge
                and not self.low_shelf_db and not self.presence_db and not self.air_db and not self.compression)


PROFILES: dict[str, VoiceProfile] = {
    # Deep, mechanical, controlled. ~110 Hz fundamental, male-sized formants, crisp consonants.
    "sid": VoiceProfile("sid", register=0.84, pitch_steps=-7.0, sub_octave=0.16, edge=0.18, edge_ms=0.55,
                        low_shelf_db=4.0, presence_db=2.5, air_db=1.5, compression=0.6,
                        sdp_ratio=0.2, noise_scale=0.45, noise_scale_w=0.55, pace=0.95),
    # The same deep voice without the metallic edge and sub-layer.
    "sid_clean": VoiceProfile("sid_clean", register=0.84, pitch_steps=-7.0, low_shelf_db=3.0, presence_db=2.5,
                              air_db=1.5, compression=0.5, sdp_ratio=0.2, noise_scale=0.45, noise_scale_w=0.55,
                              pace=0.95),
    # MeloTTS exactly as synthesised.
    "natural": VoiceProfile("natural"),
}


def get_profile(name: str | None) -> VoiceProfile:
    return PROFILES.get((name or "natural").lower(), PROFILES["natural"])


def length_scale_factor(profile: VoiceProfile) -> float:
    """How much faster to synthesise so the register shift leaves the duration unchanged."""
    return profile.register


# --------------------------------------------------------------------------- phase-locked vocoder


@lru_cache(maxsize=4)
def _window(n_fft: int) -> np.ndarray:
    return np.hanning(n_fft + 1)[:-1].astype(np.float64)


def _time_scale(x: np.ndarray, factor: float, n_fft: int = 2048, hop_out: int = 256) -> np.ndarray:
    """Change duration by ``factor`` (output length ≈ len(x)·factor) keeping pitch, with identity phase locking."""
    if abs(factor - 1.0) < 1e-6 or x.size < n_fft:
        return x.astype(np.float32, copy=False)
    window = _window(n_fft)
    hop_in = hop_out / factor
    padded = np.concatenate([np.zeros(n_fft // 2), x.astype(np.float64), np.zeros(n_fft)])
    frames = int((padded.size - n_fft) / hop_in) + 1
    bins = n_fft // 2 + 1
    omega = 2 * np.pi * np.arange(bins) * hop_in / n_fft  # expected phase advance per analysis hop
    scale = hop_out / hop_in
    out = np.zeros(frames * hop_out + n_fft)
    norm = np.zeros_like(out)
    window_sq = window ** 2
    previous_phase = None
    synth_phase = np.zeros(bins)
    index = np.arange(bins)
    for k in range(frames):
        start = int(round(k * hop_in))
        spectrum = np.fft.rfft(padded[start:start + n_fft] * window)
        magnitude = np.abs(spectrum)
        phase = np.angle(spectrum)
        if previous_phase is None:
            synth_phase = phase.copy()
        else:
            delta = phase - previous_phase - omega
            delta -= 2 * np.pi * np.round(delta / (2 * np.pi))
            # peaks: local maxima of the magnitude spectrum
            peak = np.zeros(bins, dtype=bool)
            peak[1:-1] = (magnitude[1:-1] > magnitude[:-2]) & (magnitude[1:-1] >= magnitude[2:])
            peaks = index[peak]
            if peaks.size == 0:
                synth_phase = synth_phase + (omega + delta) * scale
            else:
                advanced = synth_phase[peaks] + (omega[peaks] + delta[peaks]) * scale
                # every bin follows the peak whose region it belongs to (midpoints between peaks)
                edges = (peaks[:-1] + peaks[1:]) / 2
                owner = peaks[np.searchsorted(edges, index)]
                peak_phase = np.zeros(bins)
                peak_phase[peaks] = advanced
                synth_phase = peak_phase[owner] + (phase - phase[owner])
        previous_phase = phase
        frame = np.fft.irfft(magnitude * np.exp(1j * synth_phase), n=n_fft) * window
        position = k * hop_out
        out[position:position + n_fft] += frame
        norm[position:position + n_fft] += window_sq
    out = out / np.maximum(norm, 1e-3)
    out = out[n_fft // 2:]
    length = int(round(x.size * factor))
    return out[:length].astype(np.float32)


def _pitch_shift(x: np.ndarray, rate: int, steps: float) -> np.ndarray:
    """Pitch by ``steps`` semitones, formants and duration kept: time-scale, then resample back."""
    import soxr

    ratio = 2 ** (steps / 12.0)
    scaled = _time_scale(x, ratio)  # shorter (ratio < 1) at the same pitch
    shifted = soxr.resample(scaled, rate, rate / ratio)  # stretched back to length → pitch × ratio
    if shifted.size >= x.size:
        return shifted[: x.size].astype(np.float32)
    return np.pad(shifted, (0, x.size - shifted.size)).astype(np.float32)


# --------------------------------------------------------------------------- filters (RBJ biquads)


def _biquad(kind: str, freq: float, rate: int, gain_db: float = 0.0, q: float = 0.707) -> tuple[np.ndarray, np.ndarray]:
    a_gain = 10 ** (gain_db / 40)
    w0 = 2 * np.pi * freq / rate
    cos_w0, sin_w0 = np.cos(w0), np.sin(w0)
    alpha = sin_w0 / (2 * q)
    sq = 2 * np.sqrt(a_gain) * alpha
    if kind == "lowshelf":
        b = [a_gain * ((a_gain + 1) - (a_gain - 1) * cos_w0 + sq), 2 * a_gain * ((a_gain - 1) - (a_gain + 1) * cos_w0),
             a_gain * ((a_gain + 1) - (a_gain - 1) * cos_w0 - sq)]
        a = [(a_gain + 1) + (a_gain - 1) * cos_w0 + sq, -2 * ((a_gain - 1) + (a_gain + 1) * cos_w0),
             (a_gain + 1) + (a_gain - 1) * cos_w0 - sq]
    elif kind == "highshelf":
        b = [a_gain * ((a_gain + 1) + (a_gain - 1) * cos_w0 + sq), -2 * a_gain * ((a_gain - 1) + (a_gain + 1) * cos_w0),
             a_gain * ((a_gain + 1) + (a_gain - 1) * cos_w0 - sq)]
        a = [(a_gain + 1) - (a_gain - 1) * cos_w0 + sq, 2 * ((a_gain - 1) - (a_gain + 1) * cos_w0),
             (a_gain + 1) - (a_gain - 1) * cos_w0 - sq]
    elif kind == "peak":
        b = [1 + alpha * a_gain, -2 * cos_w0, 1 - alpha * a_gain]
        a = [1 + alpha / a_gain, -2 * cos_w0, 1 - alpha / a_gain]
    elif kind == "highpass":
        b = [(1 + cos_w0) / 2, -(1 + cos_w0), (1 + cos_w0) / 2]
        a = [1 + alpha, -2 * cos_w0, 1 - alpha]
    else:  # pragma: no cover - programming error
        raise ValueError(kind)
    b_arr, a_arr = np.asarray(b, dtype=np.float64), np.asarray(a, dtype=np.float64)
    return b_arr / a_arr[0], a_arr / a_arr[0]


@lru_cache(maxsize=32)
def _filters(profile: VoiceProfile, rate: int) -> list[tuple[np.ndarray, np.ndarray]]:
    stages = [_biquad("highpass", 55.0, rate)]  # rumble from the octave layer never reaches the speakers
    if profile.low_shelf_db:
        stages.append(_biquad("lowshelf", 160.0, rate, profile.low_shelf_db))
    if profile.presence_db:
        stages.append(_biquad("peak", 3000.0, rate, profile.presence_db, q=0.8))
    if profile.air_db:
        stages.append(_biquad("highshelf", 8000.0, rate, profile.air_db))
    return stages


def _compress(audio: np.ndarray, rate: int, amount: float) -> np.ndarray:
    from scipy.signal import lfilter

    ratio = 1.0 + 3.0 * amount
    threshold = 0.12
    coeff = np.exp(-1.0 / (0.012 * rate))  # ~12 ms RMS envelope
    power = lfilter([1 - coeff], [1, -coeff], audio.astype(np.float64) ** 2)
    envelope = np.sqrt(np.maximum(power, 1e-12))
    gain = np.ones_like(envelope)
    over = envelope > threshold
    gain[over] = (threshold / envelope[over]) ** (1 - 1 / ratio)
    return (audio * gain).astype(np.float32)


# --------------------------------------------------------------------------- the chain


def apply_profile(audio: np.ndarray, rate: int, profile: VoiceProfile) -> np.ndarray:
    """Shape raw TTS output (float32 mono at ``rate``) into the profile's voice. Returns the same rate.

    The output is ``1/register`` times longer than the input; MeloTTS is asked for
    ``length_scale_factor`` faster speech so the final pace is unchanged.
    """
    if audio.size == 0 or profile.neutral:
        return audio.astype(np.float32, copy=False)
    import soxr
    from scipy.signal import lfilter

    x = audio.astype(np.float32, copy=False)
    peak_in = float(np.max(np.abs(x))) or 1.0

    # 1. register: resample → lower pitch and formants together
    if profile.register != 1.0:
        x = soxr.resample(x, rate, rate / profile.register).astype(np.float32)
    # 2. pitch with formants kept
    if profile.pitch_steps:
        x = _pitch_shift(x, rate, profile.pitch_steps)
    # 3. sub-octave layer
    if profile.sub_octave:
        x = x + np.float32(profile.sub_octave) * _pitch_shift(x, rate, -12.0)
    # 4. mechanical edge: a static sub-millisecond doubling (fine comb across the upper spectrum)
    if profile.edge:
        delay = max(1, int(round(profile.edge_ms / 1000 * rate)))
        doubled = np.concatenate([np.zeros(delay, dtype=np.float32), x[:-delay]])
        x = (1 - profile.edge) * x + profile.edge * doubled
    # 5. tone
    for b, a in _filters(profile, rate):
        x = lfilter(b, a, x).astype(np.float32)
    # 6. dynamics and level
    if profile.compression:
        x = _compress(x, rate, profile.compression)
    peak = float(np.max(np.abs(x))) or 1.0
    x = x * np.float32(min(peak_in, 0.9) / peak)
    return (np.tanh(x * 1.2) / np.tanh(1.2)).astype(np.float32)
