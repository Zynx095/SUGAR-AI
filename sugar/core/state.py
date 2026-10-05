"""Explicit assistant state machine.

The foreground interaction is always in exactly one state. Transitions are
validated against a fixed table; an illegal transition is rejected and
logged instead of silently corrupting the UI or the turn logic. Background
work (a Claude Code session, a timer) does not change this state — it is
reported separately so the user can keep talking while it runs.
"""

from __future__ import annotations

import logging
import threading
import time
from enum import StrEnum

from sugar.core.events import EventBus

log = logging.getLogger(__name__)


class AssistantState(StrEnum):
    IDLE = "idle"
    LISTENING = "listening"
    TRANSCRIBING = "transcribing"
    THINKING = "thinking"
    TOOL_EXECUTION = "tool_execution"
    SPEAKING = "speaking"
    INTERRUPTED = "interrupted"
    ERROR = "error"
    PAUSED = "paused"


S = AssistantState

TRANSITIONS: dict[AssistantState, frozenset[AssistantState]] = {
    S.IDLE: frozenset({S.LISTENING, S.THINKING, S.TOOL_EXECUTION, S.SPEAKING, S.PAUSED, S.ERROR}),
    S.LISTENING: frozenset({S.TRANSCRIBING, S.IDLE, S.INTERRUPTED, S.PAUSED, S.ERROR}),
    S.TRANSCRIBING: frozenset({S.THINKING, S.TOOL_EXECUTION, S.SPEAKING, S.LISTENING, S.IDLE, S.PAUSED, S.ERROR}),
    S.THINKING: frozenset({S.SPEAKING, S.TOOL_EXECUTION, S.IDLE, S.INTERRUPTED, S.PAUSED, S.ERROR}),
    S.TOOL_EXECUTION: frozenset({S.THINKING, S.SPEAKING, S.IDLE, S.INTERRUPTED, S.PAUSED, S.ERROR}),
    S.SPEAKING: frozenset({S.IDLE, S.INTERRUPTED, S.THINKING, S.TOOL_EXECUTION, S.PAUSED, S.ERROR}),
    S.INTERRUPTED: frozenset({S.LISTENING, S.TRANSCRIBING, S.THINKING, S.SPEAKING, S.IDLE, S.PAUSED, S.ERROR}),
    S.ERROR: frozenset({S.IDLE, S.LISTENING, S.SPEAKING, S.PAUSED}),
    S.PAUSED: frozenset({S.IDLE}),
}


class StateMachine:
    def __init__(self, bus: EventBus) -> None:
        self._bus = bus
        self._state = AssistantState.IDLE
        self._since = time.monotonic()
        self._lock = threading.Lock()

    @property
    def state(self) -> AssistantState:
        return self._state

    def is_(self, *states: AssistantState) -> bool:
        return self._state in states

    def can(self, target: AssistantState) -> bool:
        return target == self._state or target in TRANSITIONS[self._state]

    def transition(self, target: AssistantState, reason: str = "") -> bool:
        """Move to ``target`` if the table allows it. Returns whether it moved."""
        with self._lock:
            current = self._state
            if target == current:
                return True
            if target not in TRANSITIONS[current]:
                log.debug("rejected state transition %s -> %s (%s)", current.value, target.value, reason)
                return False
            self._apply(current, target, reason)
            return True

    def force(self, target: AssistantState, reason: str) -> None:
        """Unconditional move, reserved for error recovery and shutdown."""
        with self._lock:
            current = self._state
            if current != target:
                log.warning("forced state %s -> %s (%s)", current.value, target.value, reason)
                self._apply(current, target, reason)

    def _apply(self, current: AssistantState, target: AssistantState, reason: str) -> None:
        now = time.monotonic()
        held_ms = int((now - self._since) * 1000)
        self._state = target
        self._since = now
        self._bus.publish(
            "state.changed", state=target.value, previous=current.value, reason=reason, held_ms=held_ms
        )
