"""Per-turn latency tracing.

A :class:`TurnTrace` collects monotonic timestamps for each pipeline
milestone of one user turn. When the turn completes, derived latencies are
computed, published as a ``metrics.turn`` event (for the developer panel),
and appended to ``data/metrics/turns.jsonl`` (for benchmark reports).
"""

from __future__ import annotations

import itertools
import json
import statistics
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sugar.core.events import EventBus

_turn_counter = itertools.count(1)

# derived metric -> (start mark, end mark)
DERIVED: dict[str, tuple[str, str]] = {
    "endpoint_ms": ("speech_end", "endpoint"),
    "stt_wait_ms": ("endpoint", "stt_final"),
    "route_ms": ("stt_final", "route"),
    "llm_ttft_ms": ("llm_start", "llm_first_token"),
    "llm_total_ms": ("llm_start", "llm_done"),
    "tool_ms": ("tool_start", "tool_done"),
    "tts_first_audio_ms": ("tts_first_chunk", "first_audio"),
    "voice_to_voice_ms": ("speech_end", "first_audio"),
    "text_to_voice_ms": ("received", "first_audio"),
    "interrupt_ms": ("interrupt_detect", "interrupt_silent"),
    "total_ms": ("received", "done"),
}


@dataclass
class TurnTrace:
    source: str = "voice"
    turn_id: int = field(default_factory=lambda: next(_turn_counter))
    marks: dict[str, float] = field(default_factory=dict)
    info: dict[str, Any] = field(default_factory=dict)

    def mark(self, name: str, at: float | None = None, *, overwrite: bool = False) -> None:
        if overwrite or name not in self.marks:
            self.marks[name] = time.perf_counter() if at is None else at

    def has(self, name: str) -> bool:
        return name in self.marks

    def derived(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for metric, (start, end) in DERIVED.items():
            if start in self.marks and end in self.marks:
                out[metric] = max(0, int((self.marks[end] - self.marks[start]) * 1000))
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "turn_id": self.turn_id,
            "source": self.source,
            "wall_time": time.time(),
            "metrics": self.derived(),
            "info": self.info,
        }


class MetricsRecorder:
    def __init__(self, bus: EventBus, out_dir: Path | None, keep: int = 300) -> None:
        self._bus = bus
        self._history: deque[dict[str, Any]] = deque(maxlen=keep)
        self._lock = threading.Lock()
        self._file = None
        if out_dir is not None:
            out_dir.mkdir(parents=True, exist_ok=True)
            self._file = out_dir / "turns.jsonl"

    def finish(self, trace: TurnTrace) -> dict[str, Any]:
        trace.mark("done")
        record = trace.to_dict()
        with self._lock:
            self._history.append(record)
        if self._file is not None:
            try:
                with self._file.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(record, default=str) + "\n")
            except OSError:
                pass
        self._bus.publish("metrics.turn", **record)
        return record

    def summary(self) -> dict[str, dict[str, float]]:
        """p50 / p90 / count for every derived metric seen so far."""
        with self._lock:
            history = list(self._history)
        buckets: dict[str, list[int]] = {}
        for record in history:
            for name, value in record["metrics"].items():
                buckets.setdefault(name, []).append(value)
        result: dict[str, dict[str, float]] = {}
        for name, values in buckets.items():
            values.sort()
            result[name] = {
                "count": len(values),
                "p50": float(statistics.median(values)),
                "p90": float(values[min(len(values) - 1, int(len(values) * 0.9))]),
            }
        return result
