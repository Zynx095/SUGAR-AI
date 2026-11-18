"""Choosing a model provider for a request: capability, availability, privacy and measured latency.

Each provider declares what it is good at (:data:`CAPABILITIES`). Each request category has a preferred
chain (``llm.routes``, configurable). The router keeps that order but

  * drops providers that lack a required capability (tools for an agent turn, ``local`` for private data);
  * drops providers whose circuit breaker is open (a failed health check or recent errors), so an offline
    provider is not waited on for its full timeout on every request;
  * moves a provider back when its recent time-to-first-token is far slower than an alternative's
    (latency-aware), using real measurements only.

Claude Code is not a chat provider here: it is the engineering execution engine and is reached only
through the PROJECT_ANALYSIS / BUILD_EXECUTION routes.
"""

from __future__ import annotations

from dataclasses import dataclass

CAPABILITIES: dict[str, frozenset[str]] = {
    "freellm": frozenset({"chat", "reasoning", "creative", "coding", "tools", "fast", "external"}),
    "claude": frozenset({"chat", "reasoning", "creative", "coding", "strong", "external"}),
    "ollama": frozenset({"chat", "coding", "tools", "local", "offline"}),
}

DEFAULT_ROUTES: dict[str, list[str]] = {
    "general_conversation": ["freellm", "ollama", "claude"],
    "general_knowledge": ["freellm", "claude", "ollama"],
    "coding_qa": ["freellm", "claude", "ollama"],
    "creative_writing": ["freellm", "claude", "ollama"],
    "roleplay": ["freellm", "ollama", "claude"],
    "education": ["freellm", "claude", "ollama"],
    "math_reasoning": ["claude", "freellm", "ollama"],
    "research": ["freellm", "ollama"],
    "health_information": ["freellm", "claude", "ollama"],
    "productivity": ["freellm", "ollama", "claude"],
    "document_creation": ["freellm", "ollama"],
    "image_generation": ["freellm", "ollama", "claude"],
    "desktop": ["freellm", "ollama"],
}
SLOW_FACTOR = 3.0  # a provider this many times slower (TTFT) than an alternative is moved back
MIN_SAMPLES = 3


@dataclass
class RouteChoice:
    chain: list[str]
    reason: str
    local_only: bool = False


class ProviderRouter:
    def __init__(self, pool, routes: dict[str, list[str]] | None = None,
                 capabilities: dict[str, frozenset[str]] | None = None) -> None:
        self._pool = pool
        self.routes = {**DEFAULT_ROUTES, **(routes or {})}
        self.capabilities = {**CAPABILITIES, **(capabilities or {})}

    def chain_for(self, category: str, *, need_tools: bool = False, local_only: bool = False) -> RouteChoice:
        preferred = list(self.routes.get(category, self.routes["general_conversation"]))
        required = set()
        if need_tools:
            required.add("tools")
        if local_only:
            required.add("local")
        capable = [name for name in preferred if required <= self.capabilities.get(name, frozenset())
                   and name in self._pool.providers]
        healthy = [p.name for p in self._pool.available(capable)]
        chain = healthy or capable  # every circuit open: still try rather than go mute
        chain = self._latency_order(chain)
        reason = f"{category}: {', '.join(chain) or 'no provider'}"
        if local_only:
            reason += " (local only: private data)"
        return RouteChoice(chain, reason, local_only)

    def _latency_order(self, chain: list[str]) -> list[str]:
        latency = getattr(self._pool, "latency", lambda name: None)
        if len(chain) < 2:
            return chain
        first, second = chain[0], chain[1]
        a, b = latency(first), latency(second)
        if a and b and a["samples"] >= MIN_SAMPLES and b["samples"] >= MIN_SAMPLES and a["ttft_ms"] > SLOW_FACTOR * b["ttft_ms"]:
            return [second, first, *chain[2:]]
        return chain
