"""TTS facade, playback and streaming speech output."""

from __future__ import annotations

import asyncio
import os

import numpy as np
import pytest
from conftest import run

from sugar.audio.playback import AudioPlayer
from sugar.audio.speech import SpeechOutput
from sugar.audio.tts import OUTPUT_RATE, Synthesizer
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
    assert audio.size == pytest.approx(OUTPUT_RATE * 0.1, rel=0.02)  # resampled to the output rate


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

    engine = MeloEngine(TTSSettings())
    engine.load()
    started = time.perf_counter()
    audio = engine.synthesize("I found the problem.")
    elapsed = time.perf_counter() - started
    assert audio.size > engine.sample_rate * 0.5
    assert elapsed < (0.5 if engine.device == "cuda" else 3.0)
