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

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np


@dataclass(frozen=True)
class VoiceProfile:
    name: str
    register: float = 1.0  # < 1 lowers pitch and formants (duration is preserved by faster synthesis)
    pitch_steps: float = 0.0  # further pitch change in semitones
    pitch_method: str = "vocoder"  # "psola" keeps formants and treble; "vocoder" squeezes the spectrum (v2)
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
    eq: tuple[tuple[float, float], ...] = ()  # (Hz, dB) curve fitted to a reference voice (tools/fit_voice.py)
    clone: bool = False  # convert MeloTTS speech to the reference speaker (sugar/audio/voice_clone.py)
    clone_air_db: float = 0.0  # level of the >9.5 kHz band kept from the original synthesis (treble)
    # base speaker: MeloTTS language model and speaker id (None = tts.voice / EN_NEWEST)
    model: str | None = None
    speaker: str | None = None
    # character (the blue voice): intonation and synthetic colour
    contour: float = 1.0  # > 1 exaggerates the pitch movement around the speaker's median (livelier delivery)
    quantize: float = 0.0  # 0–1: pull each pitch toward the nearest semitone (a playful, tuned robot)
    ring_hz: float = 0.0  # ring-modulation carrier (Hz) for a light synthetic buzz
    ring_depth: float = 0.0  # 0–0.5
    comb: float = 0.0  # level of a short "small metal speaker" resonance, 0–0.4
    comb_ms: float = 2.2
    pause_scale: float = 1.0  # > 1 keeps longer pauses between sentences (timing for jokes)

    @property
    def neutral(self) -> bool:
        return (self.register == 1.0 and not self.pitch_steps and not self.sub_octave and not self.edge
                and not self.low_shelf_db and not self.presence_db and not self.air_db and not self.compression
                and not self.eq and not self.character)

    @property
    def character(self) -> bool:
        return self.contour != 1.0 or bool(self.quantize or self.ring_depth or self.comb)


MATCH_FILE = Path(__file__).with_name("sid_voice_match.json")


def _matched_profile() -> VoiceProfile | None:
    """The profile fitted to the user's reference recording, if tools/fit_voice.py has produced one."""
    try:
        match = json.loads(MATCH_FILE.read_text(encoding="utf-8"))
        eq = tuple(zip(match["eq_hz"], match["eq_db"], strict=True))
        return VoiceProfile("sid_dsp", register=float(match["register"]), pitch_steps=float(match["pitch_steps"]),
                            pitch_method="psola",
                            compression=0.4, sdp_ratio=0.2, noise_scale=0.45, noise_scale_w=0.55,
                            pace=float(match["pace"]), eq=eq)
    except (OSError, ValueError, KeyError, TypeError):
        return None


PROFILES: dict[str, VoiceProfile] = {
    # v2: deep, mechanical, controlled. ~110 Hz fundamental, male-sized formants, crisp consonants.
    "sid_v2": VoiceProfile("sid_v2", register=0.84, pitch_steps=-7.0, sub_octave=0.16, edge=0.18, edge_ms=0.55,
                        low_shelf_db=4.0, presence_db=2.5, air_db=1.5, compression=0.6,
                        sdp_ratio=0.2, noise_scale=0.45, noise_scale_w=0.55, pace=0.95),
    # The v2 voice without the metallic edge and sub-layer.
    "sid_clean": VoiceProfile("sid_clean", register=0.84, pitch_steps=-7.0, low_shelf_db=3.0, presence_db=2.5,
                              air_db=1.5, compression=0.5, sdp_ratio=0.2, noise_scale=0.45, noise_scale_w=0.55,
                              pace=0.95),
    # MeloTTS exactly as synthesised.
    "natural": VoiceProfile("natural"),
    # The female voice: MeloTTS's own EN-Newest speaker (a genuine female voice, not a pitch-shifted male),
    # lightly levelled and brightened.
    "female": VoiceProfile("female", presence_db=0.5, air_db=1.0, compression=0.3),
    # The old calm mid-register voice (the EN-Newest speaker 3 semitones down). Kept for reference only:
    # it measured 0.86 speaker similarity to the female voice, so it no longer backs any identity.
    "neutral": VoiceProfile("neutral", register=0.95, pitch_steps=-3.0, pitch_method="psola", low_shelf_db=1.0,
                            compression=0.4),
    # The blue voice: a different base speaker (MeloTTS English v1, Australian, naturally lively), a smaller
    # "tin" resonance, semitone-tuned and slightly exaggerated intonation, and a light ring-modulated buzz —
    # a playful robot, not a re-pitched female voice. Chosen by measurement (tools/evaluate_voices.py):
    # speaker similarity 0.39 to the female voice and 0.41 to SID, Whisper WER 2 %.
    "blue": VoiceProfile("blue", model="EN", speaker="EN-AU", register=0.92, pitch_steps=-4.0, contour=1.12,
                         quantize=0.6, ring_hz=80.0, ring_depth=0.25, comb=0.3, comb_ms=1.6, low_shelf_db=-2.0,
                         presence_db=3.0, air_db=1.0, compression=0.5, sdp_ratio=0.6, noise_scale=0.6,
                         noise_scale_w=0.9, pace=1.07, pause_scale=1.6),
}


def _clone_profile() -> VoiceProfile | None:
    """The cloned voice (tools/fit_clone.py): the reference speaker's timbre; only pace and treble fitted."""
    try:
        clone = json.loads(MATCH_FILE.read_text(encoding="utf-8"))["clone"]
        return VoiceProfile("sid", clone=True, pace=float(clone["pace"]), clone_air_db=float(clone["air_db"]))
    except (OSError, ValueError, KeyError, TypeError):
        return None


# DSP-only approximation of ULTRON_SID.mp3 (pitch, pace, spectrum) — also the fallback when the
# converter's model files are missing.
PROFILES["sid_dsp"] = _matched_profile() or PROFILES["sid_v2"]
# "sid": the voice cloned from data/voice_samples/ULTRON_SID.mp3.
PROFILES["sid"] = _clone_profile() or PROFILES["sid_dsp"]


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


def _yin_f0(x: np.ndarray, rate: int, hop: int) -> tuple[np.ndarray, np.ndarray]:
    """Fast F0 track (YIN at 16 kHz) and a voicing mask, one value per ``hop`` samples of ``x``."""
    import librosa
    import soxr

    y16 = soxr.resample(x, rate, 16000).astype(np.float32)
    hop16 = max(1, int(round(hop * 16000 / rate)))
    f0 = librosa.yin(y16, fmin=50, fmax=400, sr=16000, frame_length=1024, hop_length=hop16, trough_threshold=0.15)
    rms = librosa.feature.rms(y=y16, frame_length=1024, hop_length=hop16)[0]
    n = min(f0.size, rms.size)
    f0, rms = f0[:n], rms[:n]
    # YIN returns fmax/fmin when it finds no period; real speech sits well inside the range.
    voiced = (f0 > 55) & (f0 < 390) & (rms > 0.08 * (rms.max() or 1.0))
    return f0, voiced


def _psola(x: np.ndarray, rate: int, ratio, *, f0_track: tuple[np.ndarray, np.ndarray] | None = None) -> np.ndarray:
    """TD-PSOLA: multiply pitch by ``ratio`` keeping duration, formants and the full bandwidth.

    ``ratio`` is a number, or a function ``(f0, voiced) -> per-frame ratios`` (frames of 256 samples) for
    pitch that moves differently over time (exaggerated or tuned intonation).

    Voiced speech is cut into two-period grains centred on pitch marks (aligned to waveform peaks) and
    re-spaced at the new period; unvoiced sounds (s, t, f — the treble that carries articulation) are
    overlap-added at their own spacing, untouched.
    """
    hop = 256
    f0, voiced = f0_track if f0_track is not None else _yin_f0(x, rate, hop)
    n = x.size
    ratios = np.asarray(ratio(f0, voiced), dtype=np.float64) if callable(ratio) else np.full(f0.size, float(ratio))

    def frame_of(t: float) -> int:
        return min(f0.size - 1, max(0, int(t / hop)))

    unvoiced_period = int(rate * 0.005)
    marks, periods, flags = [], [], []
    t = 0.0
    while t < n:
        i = frame_of(t)
        if voiced[i]:
            period = rate / f0[i]
            lo, hi = int(t - period / 3), int(t + period / 3)
            lo, hi = max(0, lo), min(n, max(hi, lo + 1))
            centre = lo + int(np.argmax(x[lo:hi]))  # lock onto the glottal peak
            if marks and centre <= marks[-1]:
                centre = int(marks[-1] + period)
            marks.append(centre)
            periods.append(int(period))
            flags.append(True)
            t = centre + period
        else:
            marks.append(int(t))
            periods.append(unvoiced_period)
            flags.append(False)
            t += unvoiced_period
    marks_arr = np.asarray(marks)

    out = np.zeros(n + 4 * rate // 50, dtype=np.float64)
    weight = np.zeros_like(out)
    position = float(marks[0]) if marks else 0.0
    while position < n:
        k = int(np.clip(np.searchsorted(marks_arr, position), 0, len(marks) - 1))
        if k > 0 and abs(marks_arr[k - 1] - position) < abs(marks_arr[k] - position):
            k -= 1
        period, centre = periods[k], marks[k]
        window = np.hanning(2 * period + 1)
        lo, hi = centre - period, centre + period + 1
        grain = np.zeros(2 * period + 1)
        src_lo, src_hi = max(0, lo), min(n, hi)
        grain[src_lo - lo: src_hi - lo] = x[src_lo:src_hi]
        start = int(round(position)) - period
        if start >= 0 and start + grain.size <= out.size:
            out[start:start + grain.size] += grain * window
            weight[start:start + grain.size] += window
        position += period / ratios[frame_of(position)] if flags[k] else period
    # Where grains overlap more than a Hann pair would, scale back; sparse (lowered) voiced grains stay as is.
    out = out[:n] / np.maximum(weight[:n], 1.0)
    return out.astype(np.float32)


def _pitch_shift(x: np.ndarray, rate: int, steps: float) -> np.ndarray:
    """Pitch by ``steps`` semitones and duration kept: time-scale, then resample back.

    Note: the resample moves the formants and squeezes the spectrum too (the v2 voice's muffling);
    the matched "sid" voice uses :func:`_psola` instead.
    """
    import soxr

    ratio = 2 ** (steps / 12.0)
    scaled = _time_scale(x, ratio)  # shorter (ratio < 1) at the same pitch
    shifted = soxr.resample(scaled, rate, rate / ratio)  # stretched back to length → pitch × ratio
    if shifted.size >= x.size:
        return shifted[: x.size].astype(np.float32)
    return np.pad(shifted, (0, x.size - shifted.size)).astype(np.float32)


def _character_ratios(profile: VoiceProfile):
    """Per-frame pitch ratios: exaggerate the intonation around the median, then pull toward semitones."""
    shift = 2 ** (profile.pitch_steps / 12.0)

    def ratios(f0: np.ndarray, voiced: np.ndarray) -> np.ndarray:
        out = np.full(f0.size, shift)
        if not voiced.any():
            return out
        median = float(np.median(f0[voiced]))
        target = shift * median * (f0[voiced] / median) ** profile.contour
        if profile.quantize:
            semis = 12 * np.log2(target / 440.0)
            semis = semis + profile.quantize * (np.round(semis) - semis)
            target = 440.0 * 2 ** (semis / 12)
        out[voiced] = target / f0[voiced]
        # a 3-frame median keeps single mis-tracked frames from jumping
        padded = np.pad(out, 1, mode="edge")
        return np.median(np.stack([padded[:-2], padded[1:-1], padded[2:]]), axis=0)

    return ratios


def _ring(x: np.ndarray, rate: int, hz: float, depth: float) -> np.ndarray:
    t = np.arange(x.size, dtype=np.float64) / rate
    return (x * (1.0 - depth + depth * np.cos(2 * np.pi * hz * t))).astype(np.float32)


def _comb(x: np.ndarray, rate: int, level: float, ms: float) -> np.ndarray:
    """A short feedback comb: the resonance of a small metal speaker (tinny, not echoing)."""
    from scipy.signal import lfilter

    delay = max(1, int(round(ms / 1000 * rate)))
    a = np.zeros(delay + 1)
    a[0], a[delay] = 1.0, -level
    return ((1 - level) * lfilter([1.0], a, x)).astype(np.float32)


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


@lru_cache(maxsize=8)
def _eq_filter(eq: tuple[tuple[float, float], ...], rate: int, taps: int = 2047) -> np.ndarray:
    """Linear-phase FIR that follows a (Hz, dB) curve (frequency sampling + window)."""
    hz = np.array([p[0] for p in eq])
    db = np.array([p[1] for p in eq])
    n_fft = 8192
    freqs = np.fft.rfftfreq(n_fft, 1 / rate)
    gains = 10 ** (np.interp(np.log(np.maximum(freqs, 1.0)), np.log(hz), db) / 20)
    impulse = np.fft.irfft(gains, n=n_fft)
    impulse = np.roll(impulse, taps // 2)[:taps] * np.hanning(taps)
    return impulse.astype(np.float32)


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
    # 2. pitch with formants kept (the blue voice: exaggerated, semitone-tuned intonation)
    if profile.contour != 1.0 or profile.quantize:
        x = _psola(x, rate, _character_ratios(profile))
    elif profile.pitch_steps:
        if profile.pitch_method == "psola":
            x = _psola(x, rate, 2 ** (profile.pitch_steps / 12.0))
        else:
            x = _pitch_shift(x, rate, profile.pitch_steps)
    # 3. sub-octave layer
    if profile.sub_octave:
        x = x + np.float32(profile.sub_octave) * _pitch_shift(x, rate, -12.0)
    # 4. mechanical edge: a static sub-millisecond doubling (fine comb across the upper spectrum)
    if profile.edge:
        delay = max(1, int(round(profile.edge_ms / 1000 * rate)))
        doubled = np.concatenate([np.zeros(delay, dtype=np.float32), x[:-delay]])
        x = (1 - profile.edge) * x + profile.edge * doubled
    # 4b. synthetic colour: small-speaker resonance and a light ring-modulated buzz
    if profile.comb:
        x = _comb(x, rate, profile.comb, profile.comb_ms)
    if profile.ring_depth and profile.ring_hz:
        x = _ring(x, rate, profile.ring_hz, profile.ring_depth)
    # 5. tone
    for b, a in _filters(profile, rate):
        x = lfilter(b, a, x).astype(np.float32)
    if profile.eq:
        from scipy.signal import oaconvolve

        x = oaconvolve(x, _eq_filter(profile.eq, rate), mode="same").astype(np.float32)
    # 6. dynamics and level
    if profile.compression:
        x = _compress(x, rate, profile.compression)
    peak = float(np.max(np.abs(x))) or 1.0
    x = x * np.float32(min(peak_in, 0.9) / peak)
    return (np.tanh(x * 1.2) / np.tanh(1.2)).astype(np.float32)
