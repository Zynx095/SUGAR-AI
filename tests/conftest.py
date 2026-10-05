from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sugar.config.settings import Settings, load_settings  # noqa: E402
from sugar.core.events import EventBus  # noqa: E402


def run(coro):
    """Run a coroutine to completion (the suite has no async plugin dependency)."""
    return asyncio.run(coro)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    loaded = load_settings(config_file=tmp_path / "missing.yaml", use_env=False, data_dir=tmp_path / "data")
    loaded.computer.backend = "none"  # unit tests never touch the real desktop
    return loaded


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


class Recorder:
    """Collects published events for assertions."""

    def __init__(self, bus: EventBus, pattern: str = "*") -> None:
        self.events = []
        bus.subscribe(pattern, self.events.append)

    def types(self) -> list[str]:
        return [e.type for e in self.events]

    def of(self, event_type: str):
        return [e for e in self.events if e.type == event_type]


@pytest.fixture
def recorder(bus: EventBus) -> Recorder:
    return Recorder(bus)
