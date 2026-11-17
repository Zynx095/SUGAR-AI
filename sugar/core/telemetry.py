"""Persistent telemetry: measurements and events over time, with retention and export.

Only measured numbers and categorical facts are stored — never conversation text, tool arguments,
transcripts or file contents. Retention is ``telemetry.retention_days`` (old rows are purged on start).
Stored in ``data/telemetry.db`` (SQLite).
"""

from __future__ import annotations

import csv
import io
import json
import sqlite3
import statistics
import threading
import time
from pathlib import Path
from typing import Any

from sugar.core.events import Event, EventBus

_METRICS = ("endpoint_ms", "stt_wait_ms", "llm_ttft_ms", "llm_total_ms", "tts_first_audio_ms", "voice_to_voice_ms",
            "text_to_voice_ms", "total_ms", "interrupt_ms")


class TelemetryStore:
    def __init__(self, path: Path, retention_days: int = 30) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript("""
                CREATE TABLE IF NOT EXISTS measurements (ts REAL, metric TEXT, value REAL, provider TEXT,
                                                         category TEXT, route TEXT, source TEXT);
                CREATE INDEX IF NOT EXISTS measurements_ts ON measurements(ts);
                CREATE TABLE IF NOT EXISTS events (ts REAL, kind TEXT, name TEXT, status TEXT, value REAL,
                                                   detail TEXT);
                CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
            """)
            self._db.commit()
        self.retention_days = retention_days
        self.purge()

    # ------------------------------------------------------------------ recording

    def attach(self, bus: EventBus) -> None:
        bus.subscribe("*", self._on_event)

    def _on_event(self, event: Event) -> None:
        handler = _HANDLERS.get(event.type)
        if handler is not None:
            try:
                handler(self, event.data, event.ts)
            except Exception:  # telemetry must never break the app
                pass

    def measure(self, ts: float, metric: str, value: float, *, provider: str | None = None,
                category: str | None = None, route: str | None = None, source: str | None = None) -> None:
        with self._lock:
            self._db.execute("INSERT INTO measurements VALUES (?,?,?,?,?,?,?)",
                             (ts, metric, float(value), provider, category, route, source))
            self._db.commit()

    def event(self, ts: float, kind: str, name: str | None = None, status: str | None = None,
              value: float | None = None, detail: dict[str, Any] | None = None) -> None:
        with self._lock:
            self._db.execute("INSERT INTO events VALUES (?,?,?,?,?,?)",
                             (ts, kind, name, status, value, json.dumps(detail) if detail else None))
            self._db.commit()

    def purge(self) -> int:
        cutoff = time.time() - self.retention_days * 86400
        with self._lock:
            removed = self._db.execute("DELETE FROM measurements WHERE ts < ?", (cutoff,)).rowcount
            removed += self._db.execute("DELETE FROM events WHERE ts < ?", (cutoff,)).rowcount
            self._db.commit()
        return removed

    def clear(self) -> None:
        with self._lock:
            self._db.execute("DELETE FROM measurements")
            self._db.execute("DELETE FROM events")
            self._db.commit()

    # ------------------------------------------------------------------ queries

    def rows(self, table: str, since: float | None = None, until: float | None = None) -> list[dict[str, Any]]:
        assert table in ("measurements", "events")
        since = since or 0.0
        until = until or time.time() + 1
        with self._lock:
            rows = self._db.execute(f"SELECT * FROM {table} WHERE ts >= ? AND ts <= ? ORDER BY ts",  # noqa: S608
                                    (since, until)).fetchall()
        return [dict(row) for row in rows]

    def history(self, since: float | None = None, until: float | None = None) -> dict[str, Any]:
        measurements = self.rows("measurements", since, until)
        events = self.rows("events", since, until)
        daily: dict[str, dict[str, list[float]]] = {}
        for row in measurements:
            day = time.strftime("%Y-%m-%d", time.localtime(row["ts"]))
            daily.setdefault(row["metric"], {}).setdefault(day, []).append(row["value"])
        series = {metric: [{"day": day, "p50": round(statistics.median(values)), "n": len(values)}
                           for day, values in sorted(days.items())] for metric, days in daily.items()}
        providers: dict[str, dict[str, Any]] = {}
        for row in measurements:
            if row["metric"] == "llm_ttft_ms" and row["provider"]:
                providers.setdefault(row["provider"], {"ttft": [], "failures": 0})["ttft"].append(row["value"])
        for row in events:
            if row["kind"] == "provider_failure" and row["name"]:
                providers.setdefault(row["name"], {"ttft": [], "failures": 0})["failures"] += 1
        comparison = [{"provider": name, "answers": len(data["ttft"]), "failures": data["failures"],
                       "ttft_p50": round(statistics.median(data["ttft"])) if data["ttft"] else None}
                      for name, data in sorted(providers.items())]
        counts: dict[str, int] = {}
        for row in events:
            counts[row["kind"]] = counts.get(row["kind"], 0) + 1
        errors = [{"ts": row["ts"], "kind": row["kind"], "name": row["name"], "status": row["status"]}
                  for row in events if row["kind"] in ("error", "provider_failure", "tool_failure")][-50:]
        return {"series": series, "providers": comparison, "counts": counts, "errors": errors,
                "measurements": len(measurements), "events": len(events), "retention_days": self.retention_days}

    def export(self, fmt: str = "json", since: float | None = None, until: float | None = None) -> str:
        measurements = self.rows("measurements", since, until)
        events = self.rows("events", since, until)
        if fmt == "json":
            return json.dumps({"measurements": measurements, "events": events}, indent=1)
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["table", "ts", "iso_time", "metric_or_kind", "value", "provider_or_name", "category_or_status",
                         "route", "source_or_detail"])
        for row in measurements:
            writer.writerow(["measurement", row["ts"], _iso(row["ts"]), row["metric"], row["value"], row["provider"],
                             row["category"], row["route"], row["source"]])
        for row in events:
            writer.writerow(["event", row["ts"], _iso(row["ts"]), row["kind"], row["value"], row["name"], row["status"],
                             "", row["detail"] or ""])
        return buffer.getvalue()

    def close(self) -> None:
        with self._lock:
            self._db.close()


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts))


# ---------------------------------------------------------------------- event → rows (no content)

def _turn(store: TelemetryStore, data: dict, ts: float) -> None:
    info = data.get("info") or {}
    for metric in _METRICS:
        value = (data.get("metrics") or {}).get(metric)
        if value is not None:
            store.measure(ts, metric, value, provider=info.get("provider"), category=info.get("category"),
                          route=info.get("route"), source=data.get("source"))


def _provider_failure(store, data, ts):
    store.event(ts, "provider_failure", data.get("provider"), "produced" if data.get("produced") else "before_text")


def _coding(store, data, ts):
    status = data.get("status")
    if status in ("completed", "failed", "cancelled") and data.get("last_duration_ms") is not None:
        store.event(ts, "claude_task", data.get("mode"), status, data.get("last_duration_ms"),
                    {"cost_usd": data.get("last_cost_usd"), "usage": data.get("last_usage")})


def _check(store, data, ts):
    if data.get("status") in ("passed", "failed"):
        store.event(ts, f"{data.get('kind')}_result", data.get("kind"), data.get("status"))


def _voice(store, data, ts):
    store.event(ts, "voice_change", data.get("id"), data.get("reason"))


def _wake(store, data, ts):
    store.event(ts, "wake_activation", data.get("kind"), "verified" if data.get("verified") else "direct")


def _wake_rejected(store, data, ts):
    store.event(ts, "wake_rejected", None, "look-alike")


def _tool(store, data, ts):
    store.event(ts, "tool", data.get("name"), "ok" if data.get("ok") else "failed", data.get("ms"))
    if not data.get("ok"):
        store.event(ts, "tool_failure", data.get("name"), "failed")


def _error(store, data, ts):
    store.event(ts, "error", data.get("source"), None)


def _verdict(store, data, ts):
    if data.get("decision") != "allow":
        store.event(ts, "security_verdict", data.get("tool"), data.get("decision"), None, {"risk": data.get("risk")})


_HANDLERS = {
    "metrics.turn": _turn, "llm.error": _provider_failure, "coding.session": _coding, "coding.check": _check,
    "voice.changed": _voice, "wake.detected": _wake, "wake.rejected": _wake_rejected, "tool.complete": _tool,
    "error": _error, "security.verdict": _verdict,
}
