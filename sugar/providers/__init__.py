"""Provider construction from settings."""

from __future__ import annotations

import logging

from sugar.coding.claude_code import find_claude_executable
from sugar.config.settings import Settings
from sugar.core.events import EventBus
from sugar.providers.base import LLMProvider
from sugar.providers.claude import ClaudeAPIProvider, ClaudeCLIProvider
from sugar.providers.ollama import OllamaProvider
from sugar.providers.openai_compat import OpenAICompatibleProvider
from sugar.providers.pool import ProviderPool

log = logging.getLogger(__name__)


def build_provider_pool(settings: Settings, bus: EventBus) -> ProviderPool:
    llm = settings.llm
    providers: dict[str, LLMProvider] = {}

    if llm.freellm.enabled:
        key = settings.secret(llm.freellm.api_key_env)
        if not key:
            log.warning("FreeLLMAPI enabled but %s is not set", llm.freellm.api_key_env)
        providers["freellm"] = OpenAICompatibleProvider(
            "freellm",
            llm.freellm.base_url,
            key,
            llm.freellm.chat_model,
            timeout_s=llm.freellm.timeout_s,
            connect_timeout_s=llm.freellm.connect_timeout_s,
            extra_by_purpose={purpose: {"reasoning_effort": effort}
                              for purpose, effort in llm.freellm.effort_by_purpose.items() if effort},
        )

    if llm.ollama.enabled:
        providers["ollama"] = OllamaProvider(
            llm.ollama.base_url,
            llm.ollama.chat_model,
            keep_alive=llm.ollama.keep_alive,
            timeout_s=llm.ollama.timeout_s,
        )

    if llm.claude.enabled:
        api_key = settings.secret(llm.claude.api_key_env)
        if api_key:
            providers["claude"] = ClaudeAPIProvider(
                api_key, llm.claude.api_model, max_tokens=llm.claude.max_tokens, timeout_s=llm.claude.timeout_s
            )
        else:
            executable = find_claude_executable(settings.coding.claude_executable)
            if executable:
                providers["claude"] = ClaudeCLIProvider(
                    executable,
                    llm.claude.cli_model,
                    settings.paths.data_dir / "claude_workdir",
                    timeout_s=llm.claude.timeout_s,
                )
            else:
                log.info("Claude unavailable: no %s and no Claude CLI found", llm.claude.api_key_env)

    return ProviderPool(providers, bus)


__all__ = ["build_provider_pool", "ProviderPool"]
