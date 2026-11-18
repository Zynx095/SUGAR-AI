"""TTS facade, playback and streaming speech output."""

from __future__ import annotations

import asyncio
import os

import numpy as np
import pytest
from conftest import run

from sugar.audio.playback import AudioPlayer
from sugar.audio.speech import SpeechOutput
from sugar.audio.tts import OUTPUT_RATE, PAUSE_AFTER_S, Synthesizer, join_smoothly, trim_silence
from sugar.config.settings import TTSSettings

MODEL_TESTS = os.environ.get("SUGAR_MODEL_TESTS") == "1"


class FakeEngine:
    def __init__(self, name, *, fail=False, rate=OUTPUT_RATE):
        self.name = name
        self.sample_rate = rate
        self.fail = fail
        self.calls = []

    def load(self):
        pass

    def synthesize(self, text):
        self.calls.append(text)
        if self.fail:
            raise RuntimeError("engine broke")
        return np.ones(int(self.sample_rate * 0.1), dtype=np.float32) * 0.1


def test_synthesizer_falls_back_when_primary_fails():
    primary, backup = FakeEngine("melo", fail=True), FakeEngine("sapi", rate=22050)
    synth = Synthesizer([primary, backup])
    synth.load()
    audio = synth.synthesize("Hello there.")
    assert synth.engine_name == "sapi"
    # resampled to the output rate, plus the natural pause after a sentence
    assert audio.size == pytest.approx(OUTPUT_RATE * (0.1 + PAUSE_AFTER_S["sentence"]), rel=0.03)


def test_short_phrases_are_cached():
    engine = FakeEngine("melo")
    synth = Synthesizer([engine])
    synth.load()
    synth.synthesize("Done.")
    synth.synthesize("done.")
    assert engine.calls == ["Done."]


def test_player_mixes_segments_reports_events_and_stops_with_fade():
    events = []
    references = []
    player = AudioPlayer(sample_rate=1000, block_size=100, on_reference=lambda rms, t: references.append(rms),
                         on_segment=lambda kind, meta: events.append((kind, meta.get("text"))))
    player.enqueue(np.full(150, 0.5, dtype=np.float32), {"text": "one"})
    player.enqueue(np.full(150, 0.5, dtype=np.float32), {"text": "two"})
    out = np.zeros((100, 1), dtype=np.float32)

    class Flags:
        output_underflow = False

    player._callback(out, 100, None, Flags())
    assert events == [("start", "one")]
    player._callback(out, 100, None, Flags())
    assert events == [("start", "one"), ("end", "one"), ("start", "two")]
    dropped = player.stop(fade_ms=20)
    assert [m.get("text") for m in dropped] == ["two"]
    player._callback(out, 100, None, Flags())
    assert abs(out[0, 0]) <= 0.5 and abs(out[-1, 0]) == 0.0  # faded, then silence
    assert not player.is_active
    assert references and references[0] > 0


def test_duck_ramps_gain_down():
    player = AudioPlayer(sample_rate=1000, block_size=100)
    player.enqueue(np.full(1000, 0.5, dtype=np.float32))
    player.duck(0.2, ramp_ms=50)
    out = np.zeros((100, 1), dtype=np.float32)

    class Flags:
        output_underflow = False

    player._callback(out, 100, None, Flags())
    assert out[0, 0] > out[-1, 0] == pytest.approx(0.1, abs=0.01)


class InstantSynth:
    engine_name = "fake"

    def synthesize(self, text):
        return np.full(44, 0.2, dtype=np.float32)


class _ActiveStream:
    active = True


def running_player() -> AudioPlayer:
    """A player whose callback the test drives by hand, reported as having a live stream."""
    player = AudioPlayer(sample_rate=1000, block_size=100)
    player._stream = _ActiveStream()
    return player


def test_speech_without_a_speaker_still_completes(bus):
    async def scenario():
        speech = SpeechOutput(InstantSynth(), AudioPlayer(sample_rate=1000), bus)
        speech.start(asyncio.get_running_loop())
        handle = await speech.speak("Hello there. How are you?")
        await asyncio.wait_for(handle.done.wait(), 1)
        speech.shutdown()
        return handle

    handle = run(scenario())
    assert handle.heard and not handle.cancelled


def test_speech_output_streams_and_records_what_was_heard(bus):
    async def scenario():
        player = running_player()
        speech = SpeechOutput(InstantSynth(), player, bus)
        speech.start(asyncio.get_running_loop())
        handle = speech.begin(turn_id=7)
        speech.say(handle, "First.")
        speech.say(handle, "Second.")
        speech.close(handle)

        class Flags:
            output_underflow = False

        out = np.zeros((100, 1), dtype=np.float32)
        for _ in range(20):
            await asyncio.sleep(0.01)
            player._callback(out, 100, None, Flags())
        await asyncio.wait_for(handle.done.wait(), 1)
        speech.shutdown()
        return handle

    handle = run(scenario())
    assert handle.heard == ["First.", "Second."]
    assert handle.first_audio_at is not None


def test_cancel_silences_immediately_and_keeps_only_heard_text(bus, recorder):
    async def scenario():
        player = running_player()
        speech = SpeechOutput(InstantSynth(), player, bus)
        speech.start(asyncio.get_running_loop())
        handle = speech.begin(turn_id=1)
        for text in ["One.", "Two.", "Three."]:
            speech.say(handle, text)

        class Flags:
            output_underflow = False

        out = np.zeros((30, 1), dtype=np.float32)
        await asyncio.sleep(0.05)
        player._callback(out, 30, None, Flags())  # only the first segment starts
        await asyncio.sleep(0.01)
        speech.cancel(handle)
        speech.shutdown()
        return handle, player

    handle, player = run(scenario())
    assert handle.cancelled and handle.done.is_set()
    assert handle.heard == ["One."]
    finished = recorder.of("speech.finished")
    assert finished and finished[0].data["interrupted"] is True


@pytest.mark.skipif(not MODEL_TESTS, reason="set SUGAR_MODEL_TESTS=1 to run model tests")
def test_melo_engine_synthesizes_quickly():
    import time

    from sugar.audio.tts import MeloEngine
    from sugar.audio.voice_profile import PROFILES, apply_profile

    engine = MeloEngine(TTSSettings(voice_profile="natural"))  # the synthesis itself
    engine.load()
    apply_profile(engine.synthesize("Okay."), engine.sample_rate, PROFILES["sid"])  # JIT warm-up of the DSP
    synth, dsp = [], []
    for _ in range(3):  # best of three: a laptop GPU in a low power state is slow on the first call
        started = time.perf_counter()
        audio = engine.synthesize("I found the problem.")
        synth.append(time.perf_counter() - started)
        started = time.perf_counter()
        apply_profile(audio, engine.sample_rate, PROFILES["sid"])  # SID's voice on top
        dsp.append(time.perf_counter() - started)
    assert audio.size > engine.sample_rate * 0.5
    assert min(synth) < (0.5 if engine.device == "cuda" else 3.0), synth
    assert min(dsp) < 0.3, dsp


@pytest.mark.skipif(not MODEL_TESTS, reason="set SUGAR_MODEL_TESTS=1 to run model tests")
def test_sid_voice_is_cloned_from_the_reference():
    """MeloTTS + OpenVoice conversion: the reference speaker's pitch and timbre, and Whisper still understands it."""
    import librosa
    import torch

    from sugar.audio.tts import MeloEngine
    from sugar.config.settings import ROOT_DIR

    reference = ROOT_DIR / TTSSettings().voice_reference
    if not reference.exists():
        pytest.skip("reference recording not present")
    engine = MeloEngine(TTSSettings(voice_profile="sid"))
    engine.load()
    converter = engine._converter
    assert converter is not None, "OpenVoice converter files missing from D:/Sugar_Models/OpenVoiceV2"
    audio = engine.synthesize("Hello, I am Sid, your personal AI assistant.")

    def embedding(y, rate):
        resampled = librosa.resample(y, orig_sr=rate, target_sr=converter.sample_rate)
        with torch.no_grad():
            return converter._model.ref_enc(converter._spectrogram(resampled).transpose(1, 2)).squeeze().cpu().numpy()

    ref_audio, _ = librosa.load(str(reference), sr=engine.sample_rate)
    a, b = embedding(audio, engine.sample_rate), embedding(ref_audio, engine.sample_rate)
    assert float(a @ b / np.linalg.norm(a) / np.linalg.norm(b)) > 0.8
    f0, _, _ = librosa.pyin(librosa.resample(audio, orig_sr=engine.sample_rate, target_sr=16000), fmin=45, fmax=400,
                            sr=16000)
    assert 70 <= float(np.nanmedian(f0)) <= 95


@pytest.mark.skipif(not MODEL_TESTS, reason="set SUGAR_MODEL_TESTS=1 to run model tests")
def test_blue_voice_is_a_different_speaker_from_the_female_voice():
    """The blue voice must not be a re-pitched female voice: a different speaker embedding, pitch and intonation,
    and Whisper must still understand every word."""
    import librosa
    import torch
    from faster_whisper import WhisperModel

    from sugar.audio.tts import MeloEngine

    settings = TTSSettings(voice_profile="female")
    engine = MeloEngine(settings)
    engine.load()
    engine._settings.voice_profile = "sid"
    converter = engine._clone_converter()
    if converter is None:
        pytest.skip("OpenVoice files missing: no speaker encoder")
    text = "A cat. Excellent. Finally, a task worthy of my computational resources."

    def render(voice):
        engine._settings.voice_profile = voice
        audio = engine.synthesize(text)
        resampled = librosa.resample(audio, orig_sr=engine.sample_rate, target_sr=converter.sample_rate)
        with torch.no_grad():
            vector = converter._model.ref_enc(converter._spectrogram(resampled).transpose(1, 2)).squeeze().cpu().numpy()
        y16 = librosa.resample(audio, orig_sr=engine.sample_rate, target_sr=16000)
        f0, voiced, _ = librosa.pyin(y16, fmin=55, fmax=450, sr=16000)
        return vector / np.linalg.norm(vector), f0[voiced & ~np.isnan(f0)], y16

    female, female_f0, _ = render("female")
    blue, blue_f0, blue_audio = render("blue")
    assert float(female @ blue) < 0.6  # the old "neutral" third voice measured 0.86
    assert abs(12 * np.log2(np.median(female_f0) / np.median(blue_f0))) > 3  # more than 3 semitones apart
    spread = 12 * np.log2(np.percentile(blue_f0, 90) / np.percentile(blue_f0, 10))
    assert spread > 8  # lively, tuned intonation (the female voice is ~3.5 semitones)
    asr = WhisperModel("small.en", device="cuda" if torch.cuda.is_available() else "cpu",
                       compute_type="float16" if torch.cuda.is_available() else "int8",
                       download_root=r"D:\Sugar_Models\Whisper")
    heard = " ".join(s.text for s in asr.transcribe(blue_audio, language="en", beam_size=1)[0]).lower()
    assert "task worthy" in heard and "computational" in heard


def test_chunks_lose_their_padding_and_get_a_natural_pause():
    rate = 44100
    speech = (0.4 * np.sin(np.linspace(0, 2 * np.pi * 220, rate // 2))).astype(np.float32)
    padded = np.concatenate([np.zeros(rate // 3, np.float32), speech, np.zeros(rate // 3, np.float32)])
    trimmed = trim_silence(padded, rate)
    assert trimmed.size < speech.size + rate * 0.05  # ~0.67 s of padding removed
    sentence = join_smoothly(padded, "That is done.", rate)
    clause = join_smoothly(padded, "First,", rate)
    assert sentence.size - clause.size == pytest.approx(rate * (PAUSE_AFTER_S["sentence"] - PAUSE_AFTER_S["clause"]),
                                                        abs=2)
    assert abs(float(sentence[0])) < 1e-3  # faded in: no click at the seam
