"""Provider pool: fallback chains with a small circuit breaker.

A chain such as ``["freellm", "ollama", "claude"]`` is tried in order. A
provider that fails before producing any text is skipped and the next one is
tried; one that fails *after* text was already streamed cannot be silently
replaced (the user may have heard part of the answer), so the error is raised.
Failed providers are benched with exponential back-off.
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from sugar.core.events import EventBus
from sugar.core.logging import log_event
from sugar.providers.base import LLMProvider, Message, ProviderError, ProviderHealth, StreamChunk

log = logging.getLogger(__name__)


class AllProvidersFailed(Exception):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors) or "no providers available")
        self.errors = errors


@dataclass
class _Circuit:
    failures: int = 0
    open_until: float = 0.0
    last_error: str = ""
    health: ProviderHealth | None = None
    history: list[float] = field(default_factory=list)

    def available(self, now: float) -> bool:
        return now >= self.open_until

    def record_failure(self, error: str, now: float) -> None:
        self.failures += 1
        self.last_error = error
        self.open_until = now + min(300.0, 10.0 * (2 ** (self.failures - 1)))

    def record_success(self) -> None:
        self.failures = 0
        self.open_until = 0.0
        self.last_error = ""


class ProviderPool:
    def __init__(self, providers: dict[str, LLMProvider], bus: EventBus) -> None:
        self.providers = providers
        self._bus = bus
        self._circuits = {name: _Circuit() for name in providers}

    def get(self, name: str) -> LLMProvider | None:
        return self.providers.get(name)

    def available(self, chain: list[str], *, need_tools: bool = False) -> list[LLMProvider]:
        now = time.monotonic()
        result = []
        for name in chain:
            provider = self.providers.get(name)
            if provider is None or not self._circuits[name].available(now):
                continue
            if need_tools and not provider.supports_tools:
                continue
            result.append(provider)
        return result

    async def stream(
        self,
        chain: list[str],
        messages: list[Message],
        *,
        tools: list[dict[str, Any]] | None = None,
        purpose: str = "chat",
        model_for: dict[str, str] | None = None,
        **options: Any,
    ) -> AsyncIterator[StreamChunk]:
        """Stream from the first provider in ``chain`` that works.

        Providers without tool support are still used (without tools) when no
        tool-capable provider is available, so Sugar can always answer.
        """
        candidates = self.available(chain, need_tools=bool(tools))
        if tools:
            fallbacks = [p for p in self.available(chain) if p not in candidates]
            candidates += fallbacks
        if not candidates:
            # Every circuit is open: try the chain anyway rather than going mute.
            candidates = [self.providers[n] for n in chain if n in self.providers]
        errors: list[str] = []
        for provider in candidates:
            circuit = self._circuits[provider.name]
            model = (model_for or {}).get(provider.name)
            produced = False
            started = time.perf_counter()
            log_event("LLM_START", provider=provider.name, model=model or "default", purpose=purpose)
            self._bus.publish("llm.start", provider=provider.name, model=provider.describe(model), purpose=purpose)
            try:
                async for chunk in provider.stream(
                    messages,
                    tools=tools if provider.supports_tools else None,
                    model=model,
                    purpose=purpose,
                    **options,
                ):
                    if chunk.kind in ("text", "tool_calls"):
                        produced = True
                    if chunk.kind == "meta":
                        chunk.data.setdefault("provider", provider.name)
                    yield chunk
            except ProviderError as exc:
                circuit.record_failure(str(exc), time.monotonic())
                log_event("LLM_ERROR", severity=logging.WARNING, provider=provider.name, error=str(exc),
                          produced=produced)
                self._bus.publish("llm.error", provider=provider.name, error=str(exc), produced=produced)
                if produced:
                    raise
                errors.append(str(exc))
                continue
            circuit.record_success()
            log_event("LLM_COMPLETE", provider=provider.name,
                      ms=int((time.perf_counter() - started) * 1000))
            return
        raise AllProvidersFailed(errors)

    async def check_health(self) -> dict[str, dict[str, Any]]:
        report: dict[str, dict[str, Any]] = {}
        for name, provider in self.providers.items():
            try:
                health = await provider.health_check()
            except Exception as exc:  # health checks must never crash start-up
                health = ProviderHealth(False, f"health check failed: {exc}")
            circuit = self._circuits[name]
            circuit.health = health
            if health.ok:
                circuit.record_success()
            else:
                circuit.record_failure(health.detail, time.monotonic())
            report[name] = {"ok": health.ok, "detail": health.detail, "latency_ms": health.latency_ms,
                            "local": provider.is_local, "tools": provider.supports_tools}
        self._bus.publish("providers.health", providers=report)
        return report

    def status(self) -> dict[str, dict[str, Any]]:
        now = time.monotonic()
        return {
            name: {
                "available": circuit.available(now),
                "failures": circuit.failures,
                "last_error": circuit.last_error,
                "health": circuit.health.detail if circuit.health else None,
            }
            for name, circuit in self._circuits.items()
        }

    async def aclose(self) -> None:
        for provider in self.providers.values():
            try:
                await provider.aclose()
            except Exception:
                log.debug("error closing provider %s", provider.name, exc_info=True)
