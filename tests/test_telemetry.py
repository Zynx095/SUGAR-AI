"""Persistent telemetry: measurements from real events, no conversation content, history, export, retention."""

from __future__ import annotations

import csv
import io
import json
import time

from sugar.core.events import Event
from sugar.core.telemetry import TelemetryStore


def feed(store: TelemetryStore, kind: str, data: dict, ts: float | None = None) -> None:
    store._on_event(Event(kind, data, ts or time.time()))


def test_turn_metrics_provider_failures_and_events_are_recorded_without_content(tmp_path):
    store = TelemetryStore(tmp_path / "t.db")
    feed(store, "metrics.turn", {"source": "voice", "metrics": {"llm_ttft_ms": 900, "tts_first_audio_ms": 300,
                                                                "voice_to_voice_ms": 1800},
                                 "info": {"provider": "freellm", "category": "general_knowledge", "route": "answer"}})
    feed(store, "metrics.turn", {"source": "text", "metrics": {"llm_ttft_ms": 1100},
                                 "info": {"provider": "freellm"}})
    feed(store, "llm.error", {"provider": "ollama", "error": "refused", "produced": False})
    feed(store, "voice.changed", {"id": "female", "reason": "voice.set", "palette": ["#e8114f"]})
    feed(store, "wake.detected", {"text": "Hey SID, read my diary", "kind": "exact", "verified": None})
    feed(store, "tool.complete", {"name": "terminal.run", "ok": False, "summary": "secret output", "ms": 40})
    feed(store, "assistant.message", {"text": "a private reply"})  # not telemetry: ignored
    history = store.history()
    assert history["series"]["llm_ttft_ms"][0]["p50"] == 1000
    assert {"provider": "freellm", "answers": 2, "failures": 0, "ttft_p50": 1000} in history["providers"]
    assert {"provider": "ollama", "answers": 0, "failures": 1, "ttft_p50": None} in history["providers"]
    assert history["counts"]["voice_change"] == 1 and history["counts"]["wake_activation"] == 1
    dump = store.export("json")
    assert "diary" not in dump and "secret output" not in dump and "private reply" not in dump


def test_export_formats_and_date_filter(tmp_path):
    store = TelemetryStore(tmp_path / "t.db")
    old, new = time.time() - 5 * 86400, time.time()
    feed(store, "metrics.turn", {"metrics": {"total_ms": 1}, "info": {}}, old)
    feed(store, "metrics.turn", {"metrics": {"total_ms": 2}, "info": {}}, new)
    recent = json.loads(store.export("json", since=new - 60))
    assert [m["value"] for m in recent["measurements"]] == [2.0]
    rows = list(csv.reader(io.StringIO(store.export("csv"))))
    assert rows[0][0] == "table" and len(rows) == 3


def test_retention_purges_old_rows(tmp_path):
    path = tmp_path / "t.db"
    store = TelemetryStore(path, retention_days=30)
    feed(store, "metrics.turn", {"metrics": {"total_ms": 1}, "info": {}}, time.time() - 40 * 86400)
    feed(store, "metrics.turn", {"metrics": {"total_ms": 2}, "info": {}})
    store.close()
    reopened = TelemetryStore(path, retention_days=30)
    assert [m["value"] for m in reopened.rows("measurements")] == [2.0]
