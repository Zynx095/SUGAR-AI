"""In-process event bus.

Every subsystem reports what it is doing by publishing small events. The UI,
the metrics recorder and the structured log all subscribe to the same
stream, so there is exactly one source of truth about what Sugar is doing.

``publish`` is safe to call from any thread: off-loop callers are marshalled
onto the asyncio loop with ``call_soon_threadsafe``.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

Handler = Callable[["Event"], Any]


@dataclass(slots=True)
class Event:
    type: str
    data: dict[str, Any]
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "data": self.data, "ts": self.ts}


def _matches(pattern: str, event_type: str) -> bool:
    if pattern == "*" or pattern == event_type:
        return True
    if pattern.endswith(".*"):
        return event_type.startswith(pattern[:-1])
    return False


class EventBus:
    def __init__(self) -> None:
        self._subscribers: list[tuple[str, Handler]] = []
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: int | None = None

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        """Attach the bus to the asyncio loop that owns all subscribers."""
        self._loop = loop
        self._loop_thread = threading.get_ident()

    def subscribe(self, pattern: str, handler: Handler) -> Callable[[], None]:
        with self._lock:
            self._subscribers.append((pattern, handler))

        def unsubscribe() -> None:
            with self._lock:
                try:
                    self._subscribers.remove((pattern, handler))
                except ValueError:
                    pass

        return unsubscribe

    def publish(self, event_type: str, **data: Any) -> None:
        event = Event(event_type, data)
        loop = self._loop
        if loop is None or threading.get_ident() == self._loop_thread:
            self._dispatch(event)
        elif not loop.is_closed():
            try:
                loop.call_soon_threadsafe(self._dispatch, event)
            except RuntimeError:
                pass  # loop shutting down

    def _dispatch(self, event: Event) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
        for pattern, handler in subscribers:
            if not _matches(pattern, event.type):
                continue
            try:
                result = handler(event)
                if inspect.isawaitable(result):
                    asyncio.ensure_future(result)
            except Exception:  # a broken subscriber must not break publishers
                log.exception("event handler failed for %s", event.type)
