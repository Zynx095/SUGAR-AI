"""SID's voice profile DSP: register, stability and cost (no TTS model needed)."""

from __future__ import annotations

import time

import numpy as np
import pytest

from sugar.audio.voice_profile import PROFILES, apply_profile, get_profile, length_scale_factor
from sugar.config.settings import TTSSettings

RATE = 44100


def voiced(f0: float = 200.0, seconds: float = 2.0) -> np.ndarray:
    """A harmonic, vowel-like test signal with a slow amplitude envelope."""
    t = np.arange(int(RATE * seconds)) / RATE
    signal = sum((0.6 / k) * np.sin(2 * np.pi * f0 * k * t) for k in range(1, 12))
    envelope = 0.5 + 0.5 * np.sin(2 * np.pi * 1.5 * t) ** 2
    return (0.4 * signal * envelope / np.max(np.abs(signal))).astype(np.float32)


def fundamental(audio: np.ndarray, fmin: float = 60.0, fmax: float = 400.0) -> float:
    frame = audio[RATE // 2: RATE // 2 + 4096].astype(np.float64)
    frame -= frame.mean()
    corr = np.correlate(frame, frame, mode="full")[frame.size - 1:]
    lo, hi = int(RATE / fmax), int(RATE / fmin)
    lag = lo + int(np.argmax(corr[lo:hi]))
    return RATE / lag


def test_default_voice_is_sid():
    assert TTSSettings().voice_profile == "sid" and get_profile("sid") is PROFILES["sid"]
    assert TTSSettings().voice_reference.endswith("ULTRON_SID.mp3")


def test_sid_is_cloned_from_the_reference_and_only_pace_and_treble_are_fitted():
    sid = PROFILES["sid"]
    assert sid.clone and sid.pace != 1.0
    assert sid.register == 1.0 and not sid.pitch_steps and not sid.eq and not sid.compression
    assert get_profile("unknown") is PROFILES["natural"]


def test_natural_profile_is_untouched():
    audio = voiced()
    assert np.array_equal(apply_profile(audio, RATE, PROFILES["natural"]), audio)


def test_dsp_fallback_lowers_the_voice_to_the_reference_pitch():
    shaped = apply_profile(voiced(200.0), RATE, PROFILES["sid_dsp"])
    f0 = fundamental(shaped)
    assert 75 <= f0 <= 92, f0  # MeloTTS EN-Newest speaks around 200 Hz; ULTRON_SID.mp3 sits at 83 Hz


def test_register_shift_is_compensated_by_faster_synthesis():
    profile = PROFILES["sid_dsp"]
    audio = voiced(seconds=2.0)
    shaped = apply_profile(audio, RATE, profile)
    # MeloTTS is asked for length_scale × register, so the stretched result is back to normal pace.
    assert shaped.size * length_scale_factor(profile) == pytest.approx(audio.size, rel=0.02)


@pytest.mark.parametrize("name", sorted(PROFILES))
def test_output_is_finite_bounded_and_not_silent(name):
    shaped = apply_profile(voiced(), RATE, PROFILES[name])
    assert np.all(np.isfinite(shaped)) and np.max(np.abs(shaped)) <= 1.0
    assert np.sqrt(np.mean(shaped ** 2)) > 0.02


def test_empty_audio_passes_through():
    assert apply_profile(np.zeros(0, dtype=np.float32), RATE, PROFILES["sid"]).size == 0


def test_processing_is_cheap_enough_for_streaming():
    profile = PROFILES["sid_dsp"]
    audio = voiced(seconds=5.0)
    apply_profile(audio[:RATE], RATE, profile)  # first call pays numba/FFT warm-up
    started = time.perf_counter()
    apply_profile(audio, RATE, profile)
    assert time.perf_counter() - started < 0.6  # ~0.2 s measured for a 5 s sentence


def test_dsp_fallback_is_fitted_to_the_reference_recording():
    from sugar.audio.voice_profile import MATCH_FILE

    sid = PROFILES["sid_dsp"]
    assert MATCH_FILE.exists() and sid.pitch_method == "psola" and len(sid.eq) > 50


def test_psola_halves_pitch_and_keeps_duration():
    from sugar.audio.voice_profile import _psola

    audio = voiced(200.0)
    shifted = _psola(audio, RATE, 0.5)
    assert shifted.size == audio.size
    assert 92 <= fundamental(shifted) <= 108


def test_psola_keeps_the_treble():
    """Unlike resample-based shifting, PSOLA must not squeeze energy out of the top octave."""
    from sugar.audio.voice_profile import _psola

    rng = np.random.default_rng(1)
    audio = voiced(200.0) + (0.02 * rng.standard_normal(RATE * 2)).astype(np.float32)
    shifted = _psola(audio, RATE, 0.5)

    def top_share(y):
        spectrum = np.abs(np.fft.rfft(y)) ** 2
        freqs = np.fft.rfftfreq(y.size, 1 / RATE)
        return spectrum[freqs > 8000].sum() / spectrum.sum()

    assert top_share(shifted) > 0.5 * top_share(audio)
