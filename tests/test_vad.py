"""VAD, endpointing, echo guard and turn-completion heuristics."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from sugar.audio.echo import EchoGuard
from sugar.audio.endpointing import COMPLETE, INCOMPLETE, EndpointConfig, Endpointer
from sugar.audio.vad import EnergyVAD, SileroVAD
from sugar.intelligence.turns import assess_completeness

FRAME = 512
FRAME_S = 0.032
SAMPLE = Path(__file__).resolve().parents[1] / "tests" / "data" / "speech_sample.flac"


def frames_for(probs, start_t=0.0):
    """Feed a probability sequence; frame audio encodes its index for checks."""
    for i, p in enumerate(probs):
        yield np.full(FRAME, i, dtype=np.float32), p, start_t + i * FRAME_S


def run_endpointer(probs, config=None, hints=None):
    ep = Endpointer(config or EndpointConfig())
    events = []
    for i, (frame, p, t) in enumerate(frames_for(probs)):
        if hints and i in hints and ep.active_utterance:
            ep.set_hint(ep.active_utterance, hints[i])
        events += ep.process(frame, p, t)
    return ep, events


def test_utterance_includes_preroll_and_trims_trailing_silence():
    probs = [0.0] * 20 + [0.9] * 20 + [0.0] * 40
    _, events = run_endpointer(probs)
    kinds = [e.kind for e in events]
    assert kinds == ["start", "pause", "end"]
    end = events[-1]
    first_frame_index = int(end.audio[0])
    assert first_frame_index <= 20 - 3, "pre-roll must reach back before the onset"
    last_frame_index = int(end.audio[-1])
    assert 39 <= last_frame_index <= 39 + 8, "only a short tail after the last voiced frame"
    assert end.voiced_ms == 20 * 32


def test_short_blip_is_discarded():
    _, events = run_endpointer([0.0] * 5 + [0.9] * 4 + [0.0] * 40)
    assert [e.kind for e in events] == ["start", "pause", "discard"]


def test_pause_then_resume_stays_one_utterance():
    probs = [0.9] * 15 + [0.0] * 10 + [0.9] * 15 + [0.0] * 40
    _, events = run_endpointer(probs)
    assert [e.kind for e in events] == ["start", "pause", "resume", "pause", "end"]
    assert len({e.utterance_id for e in events}) == 1


def test_hint_controls_end_of_turn_delay():
    cfg = EndpointConfig()
    probs = [0.9] * 20 + [0.0] * 80
    _, complete = run_endpointer(probs, cfg, hints={25: COMPLETE})
    _, incomplete = run_endpointer(probs, cfg, hints={25: INCOMPLETE})
    complete_end = complete[-1].t - complete[-1].speech_end_t
    incomplete_end = incomplete[-1].t - incomplete[-1].speech_end_t
    assert complete_end == pytest.approx(cfg.endpoint_complete_ms / 1000, abs=0.04)
    assert incomplete_end == pytest.approx(cfg.endpoint_incomplete_ms / 1000, abs=0.04)


def test_hint_is_reset_when_the_user_resumes():
    probs = [0.9] * 15 + [0.0] * 8 + [0.9] * 10 + [0.0] * 60
    ep, events = run_endpointer(probs, hints={18: COMPLETE})
    end = events[-1]
    assert end.reason == "silence:unknown"


def test_max_length_forces_end():
    cfg = EndpointConfig(max_utterance_s=1.0)
    _, events = run_endpointer([0.9] * 60, cfg)
    ends = [e for e in events if e.kind == "end"]
    assert ends and ends[0].reason == "max_length"
    assert events[-1].kind == "start"  # continued speech opens the next utterance


def test_snapshot_and_cancel():
    ep = Endpointer()
    for frame, p, t in frames_for([0.9] * 10):
        ep.process(frame, p, t)
    assert ep.snapshot().size > 0
    ep.cancel()
    assert ep.active_utterance is None and ep.snapshot() is None


@pytest.mark.skipif(not SAMPLE.exists(), reason="speech sample missing")
def test_silero_detects_real_speech_and_ignores_silence():
    import soundfile

    audio, rate = soundfile.read(SAMPLE, dtype="float32")
    assert rate == 16000
    vad = SileroVAD()
    silence = [vad(np.zeros(FRAME, dtype=np.float32)) for _ in range(30)]
    speech = [vad(audio[i:i + FRAME]) for i in range(0, 16000 * 3, FRAME) if i + FRAME <= len(audio)]
    assert max(silence) < 0.2
    assert np.mean(speech) > 0.6


def test_energy_vad_fallback_reacts_to_loud_frames():
    vad = EnergyVAD()
    quiet = np.random.default_rng(0).normal(0, 0.001, FRAME).astype(np.float32)
    for _ in range(50):
        vad(quiet)
    assert vad(quiet) < 0.3
    assert vad(np.sin(np.linspace(0, 200, FRAME)).astype(np.float32) * 0.3) > 0.9


def test_echo_guard_blocks_own_voice_but_passes_louder_user():
    guard = EchoGuard(margin_db=9.0)
    t = 0.0
    for _ in range(40):  # Sugar talking; mic hears it at -20 dB
        guard.note_playback(0.2, t)
        assert guard.gate(0.02, 0.95, t) == 0.0
        t += 0.032
    assert guard.coupling == pytest.approx(0.1, rel=0.05)
    guard.note_playback(0.2, t)
    assert guard.gate(0.2, 0.95, t) == 0.95  # user much louder than the echo


def test_echo_guard_keeps_gating_during_the_tail_then_releases():
    guard = EchoGuard(tail_s=0.35)
    for i in range(20):
        guard.note_playback(0.2, i * 0.032)
        guard.gate(0.02, 0.9, i * 0.032)
    last = 19 * 0.032
    assert guard.gate(0.02, 0.9, last + 0.2) == 0.0  # reverb tail
    assert guard.gate(0.02, 0.9, last + 0.5) == 0.9  # playback over


def test_echo_guard_off_mode_is_transparent():
    guard = EchoGuard(mode="off")
    guard.note_playback(0.5, 0.0)
    assert guard.gate(0.0001, 0.8, 0.0) == 0.8


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("I want you to", "incomplete"),
        ("Open VS Code, actually wait", "incomplete"),
        ("Open VS Code and", "incomplete"),
        ("Can you", "incomplete"),
        ("What", "incomplete"),
        ("Stop.", "complete"),
        ("Wait.", "complete"),
        ("Yes", "complete"),
        ("What's the weather like in Chennai today?", "complete"),
        ("Tell me about the architecture of this project", "unknown"),
        ("", "unknown"),
    ],
)
def test_turn_completion(text, expected):
    assert assess_completeness(text) == expected


def test_turn_completion_uses_command_grammar():
    assert assess_completeness("open chrome", is_command=lambda t: t == "open chrome") == "complete"
