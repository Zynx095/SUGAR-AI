"""Shared helpers for the benchmark scripts: timing, statistics, result files and tables."""

from __future__ import annotations

import json
import platform
import statistics
import sys
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT, ROOT / "tests"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

RESULTS = ROOT / "data" / "benchmarks"


def ms_since(start: float) -> float:
    return (time.perf_counter() - start) * 1000


def stats(samples: list[float]) -> dict[str, float]:
    if not samples:
        return {"n": 0}
    ordered = sorted(samples)
    p90 = ordered[min(len(ordered) - 1, int(round(0.9 * (len(ordered) - 1))))]
    return {"n": len(samples), "median": round(statistics.median(samples), 1), "p90": round(p90, 1),
            "min": round(ordered[0], 1), "max": round(ordered[-1], 1)}


def measure(fn: Callable[[], Any], repeat: int, warmup: int = 0) -> tuple[list[float], Any]:
    result = None
    for _ in range(warmup):
        result = fn()
    samples = []
    for _ in range(repeat):
        start = time.perf_counter()
        result = fn()
        samples.append(ms_since(start))
    return samples, result


def environment() -> dict[str, Any]:
    info: dict[str, Any] = {"python": platform.python_version(), "os": platform.platform(),
                            "machine": platform.machine(), "date": datetime.now().isoformat(timespec="seconds")}
    try:
        import torch

        if torch.cuda.is_available():
            info["gpu"] = torch.cuda.get_device_name(0)
    except Exception:
        pass
    return info


def save(name: str, rows: list[dict[str, Any]], extra: dict[str, Any] | None = None) -> Path:
    RESULTS.mkdir(parents=True, exist_ok=True)
    path = RESULTS / f"{name}-{datetime.now():%Y%m%d-%H%M%S}.json"
    path.write_text(json.dumps({"benchmark": name, "environment": environment(), "rows": rows, **(extra or {})},
                               indent=2), encoding="utf-8")
    return path


def table(rows: list[dict[str, Any]]) -> str:
    lines = ["| Measurement | n | median ms | p90 ms | min | max | notes |", "|---|---|---|---|---|---|---|"]
    for row in rows:
        s = row.get("stats", {})
        lines.append(f"| {row['name']} | {s.get('n', '')} | {s.get('median', '')} | {s.get('p90', '')} | "
                     f"{s.get('min', '')} | {s.get('max', '')} | {row.get('notes', '')} |")
    return "\n".join(lines)


def row(name: str, samples: list[float], notes: str = "") -> dict[str, Any]:
    return {"name": name, "stats": stats(samples), "samples": [round(s, 2) for s in samples], "notes": notes}
