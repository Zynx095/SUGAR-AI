"""Dependencies shared by tool implementations."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sugar.coding.projects import ProjectRegistry
    from sugar.coding.sessions import CodingSessionManager
    from sugar.computer.apps import AppCatalog
    from sugar.computer.spotify import SpotifyController
    from sugar.config.settings import Settings
    from sugar.core.events import EventBus
    from sugar.intelligence.working import WorkingMemory
    from sugar.memory.store import MemoryStore


@dataclass
class ToolServices:
    settings: Settings
    bus: EventBus
    apps: AppCatalog
    projects: ProjectRegistry
    sessions: CodingSessionManager
    memory: MemoryStore
    working: WorkingMemory
    spotify: SpotifyController
    data_dir: Path
    state: dict[str, Any] = field(default_factory=dict)  # e.g. last assistant reply for "copy that"
