"""Speech recognition: hallucination filtering (always) and real models (opt-in)."""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest

from sugar.audio.stt import Evidence, SpeechRecognizer, filter_transcript
from sugar.config.settings import STTSettings

SAMPLE = Path(__file__).resolve().parent / "data" / "speech_sample.flac"
MODEL_TESTS = os.environ.get("SUGAR_MODEL_TESTS") == "1"


def check(text, *, logprob=-0.3, no_speech=0.05, compression=1.2, voiced=1200, prob=0.92):
    return filter_transcript(text, avg_logprob=logprob, no_speech_prob=no_speech, compression_ratio=compression,
                             evidence=Evidence(voiced, prob))


def test_classic_hallucinations_on_weak_audio_are_rejected():
    assert check("Thank you.", voiced=400, prob=0.6)[1] == "hallucination"
    assert check(" Thanks for watching!", no_speech=0.6)[1] == "hallucination"


def test_real_thanks_with_strong_evidence_is_kept():
    text, reason = check("Thank you.", voiced=900, prob=0.95)
    assert reason is None and text == "Thank you."


def test_short_confirmations_survive():
    assert check("Okay.", voiced=350, prob=0.8)[1] is None
    assert check("Yes.", voiced=300, prob=0.85)[1] is None


def test_bracketed_noise_tags_and_empty_output():
    assert check("[BLANK_AUDIO]") == ("", "empty")
    text, reason = check("(upbeat music) Open Chrome")
    assert text == "Open Chrome" and reason is None


def test_non_english_and_repetition_loops_are_rejected():
    assert check("Продолжение следует...")[1] == "non_english"
    assert check("the the the the the the", compression=3.1)[1] == "repetition"


def test_vocabulary_merges_and_dedupes():
    recognizer = SpeechRecognizer(STTSettings(vocabulary=["Sugar", "Claude"]), Path("."))
    recognizer.set_vocabulary(["JIVA", "claude", "Q-Shield"])
    assert recognizer.vocabulary == ["Sugar", "Claude", "JIVA", "Q-Shield"]


@pytest.mark.skipif(not MODEL_TESTS or not SAMPLE.exists(), reason="set SUGAR_MODEL_TESTS=1 to run model tests")
def test_real_models_transcribe_the_sample():
    import soundfile

    from sugar.config.settings import PathsSettings

    audio, _ = soundfile.read(SAMPLE, dtype="float32")
    recognizer = SpeechRecognizer(STTSettings(), PathsSettings().whisper_dir)
    recognizer.load()
    fast = recognizer.transcribe(audio, final=False)
    final = recognizer.transcribe(audio, final=True)
    expected = "then the good soul openly shouldered the burden"
    assert expected in fast.text.lower()
    assert expected in final.text.lower()
    assert final.ok and fast.ok
    silence = recognizer.transcribe(np.zeros(16000, dtype=np.float32), final=True, evidence=Evidence(300, 0.3))
    assert not silence.ok
