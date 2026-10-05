"""Phase 1: configuration, event bus, state machine, metrics."""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path

from conftest import Recorder, run

from sugar.config.settings import _env_overrides, load_settings, save_override
from sugar.core.events import EventBus
from sugar.core.metrics import MetricsRecorder, TurnTrace
from sugar.core.state import AssistantState, StateMachine


def test_defaults_load_without_files(settings):
    assert settings.llm.chat_chain[0] == "freellm"
    assert settings.vad.endpoint_complete_ms < settings.vad.endpoint_default_ms < settings.vad.endpoint_incomplete_ms
    assert settings.paths.data_dir.exists()


def test_yaml_then_overrides_then_env_precedence(tmp_path: Path, monkeypatch):
    config = tmp_path / "sugar.yaml"
    config.write_text("tts:\n  speed: 1.3\n  voice: EN-US\nconversation:\n  user_name: Sam\n", encoding="utf-8")
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "overrides.json").write_text(json.dumps({"tts": {"speed": 0.9}}), encoding="utf-8")
    monkeypatch.setenv("SUGAR__CONVERSATION__USER_NAME", "Yuki")
    monkeypatch.setenv("SUGAR__VAD__THRESHOLD", "0.62")

    s = load_settings(config_file=config, data_dir=data_dir)

    assert s.tts.voice == "EN-US"  # yaml
    assert s.tts.speed == 0.9  # runtime override beats yaml
    assert s.conversation.user_name == "Yuki"  # env beats everything
    assert s.vad.threshold == 0.62  # env values are parsed, not strings


def test_corrupt_override_file_is_ignored(tmp_path: Path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "overrides.json").write_text("{not json", encoding="utf-8")
    s = load_settings(config_file=tmp_path / "none.yaml", use_env=False, data_dir=data_dir)
    assert s.tts.speed == 1.1


def test_save_override_round_trip(settings):
    save_override(settings, "tts.speed", 1.25)
    reloaded = load_settings(config_file=Path("missing.yaml"), use_env=False, data_dir=settings.paths.data_dir)
    assert reloaded.tts.speed == 1.25


def test_env_override_parser_ignores_other_variables():
    parsed = _env_overrides({"PATH": "x", "SUGAR__UI__PORT": "8765", "SUGAR__LLM__CHAT_CHAIN": "[ollama]"})
    assert parsed == {"ui": {"port": 8765}, "llm": {"chat_chain": ["ollama"]}}


def test_secrets_are_read_from_environment_only(settings, monkeypatch):
    monkeypatch.setenv("FREELLMAPI_API_KEY", "freellmapi-test")
    assert settings.secret("FREELLMAPI_API_KEY") == "freellmapi-test"
    assert settings.secret(None) is None
    assert "freellmapi-test" not in settings.model_dump_json()


def test_event_bus_patterns(bus: EventBus):
    seen: list[str] = []
    bus.subscribe("stt.*", lambda e: seen.append(e.type))
    bus.subscribe("state.changed", lambda e: seen.append("exact"))
    bus.publish("stt.partial", text="a")
    bus.publish("stt.final", text="b")
    bus.publish("tts.start")
    bus.publish("state.changed", state="idle")
    assert seen == ["stt.partial", "stt.final", "exact"]


def test_event_bus_survives_broken_subscriber(bus: EventBus):
    seen = []
    bus.subscribe("*", lambda e: 1 / 0)
    bus.subscribe("*", lambda e: seen.append(e.type))
    bus.publish("x")
    assert seen == ["x"]


def test_event_bus_marshals_cross_thread_publishes():
    async def scenario():
        bus = EventBus()
        bus.bind(asyncio.get_running_loop())
        threads_seen: list[int] = []
        bus.subscribe("ping", lambda e: threads_seen.append(threading.get_ident()))
        worker = threading.Thread(target=lambda: bus.publish("ping"))
        worker.start()
        worker.join()
        await asyncio.sleep(0.01)
        return threads_seen, threading.get_ident()

    seen, loop_thread = run(scenario())
    assert seen == [loop_thread]


def test_state_machine_follows_table(bus: EventBus):
    rec = Recorder(bus, "state.changed")
    sm = StateMachine(bus)
    assert sm.transition(AssistantState.LISTENING)
    assert sm.transition(AssistantState.TRANSCRIBING)
    assert sm.transition(AssistantState.THINKING)
    assert sm.transition(AssistantState.SPEAKING)
    assert sm.transition(AssistantState.INTERRUPTED, "barge-in")
    assert sm.transition(AssistantState.LISTENING)
    assert [e.data["state"] for e in rec.events] == [
        "listening", "transcribing", "thinking", "speaking", "interrupted", "listening"
    ]


def test_state_machine_rejects_illegal_transition(bus: EventBus):
    sm = StateMachine(bus)
    sm.transition(AssistantState.PAUSED)
    assert not sm.transition(AssistantState.SPEAKING)
    assert sm.state is AssistantState.PAUSED
    sm.force(AssistantState.ERROR, "test")
    assert sm.state is AssistantState.ERROR


def test_turn_trace_derives_latencies(bus: EventBus, tmp_path: Path):
    rec = Recorder(bus, "metrics.turn")
    trace = TurnTrace()
    trace.mark("speech_end", 10.0)
    trace.mark("endpoint", 10.4)
    trace.mark("stt_final", 10.5)
    trace.mark("llm_start", 10.6)
    trace.mark("llm_first_token", 11.6)
    trace.mark("first_audio", 11.9)
    metrics = trace.derived()
    assert metrics["endpoint_ms"] == 400
    assert metrics["llm_ttft_ms"] == 1000
    assert metrics["voice_to_voice_ms"] == 1900

    recorder = MetricsRecorder(bus, tmp_path / "metrics")
    recorder.finish(trace)
    assert rec.events and rec.events[0].data["metrics"]["voice_to_voice_ms"] == 1900
    assert (tmp_path / "metrics" / "turns.jsonl").exists()
    assert recorder.summary()["voice_to_voice_ms"]["p50"] == 1900
