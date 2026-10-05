"""Central configuration.

Precedence (lowest → highest):
    1. defaults declared on the models below
    2. ``sugar.yaml`` in the project root (user-editable)
    3. ``data/overrides.json`` (changes made at runtime from the UI)
    4. environment variables ``SUGAR__SECTION__KEY=value`` (``.env`` is loaded first)

Secrets are never stored in config files: providers reference the *name* of
the environment variable that holds their key.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field

ROOT_DIR = Path(__file__).resolve().parents[2]


class PathsSettings(BaseModel):
    data_dir: Path = ROOT_DIR / "data"
    models_dir: Path = Path(r"D:\Sugar_Models")

    @property
    def whisper_dir(self) -> Path:
        return self.models_dir / "Whisper"


class AudioSettings(BaseModel):
    input_device: str | int | None = None  # name fragment or index; None = system default
    output_device: str | int | None = None
    sample_rate: int = 16000
    block_size: int = 512  # 32 ms — one Silero VAD window
    preroll_ms: int = 400  # audio kept from before speech onset (prevents clipped words)
    echo_mode: Literal["auto", "gate", "off"] = "auto"


class VADSettings(BaseModel):
    threshold: float = 0.5  # speech probability that counts as voice
    negative_threshold: float = 0.35  # below this counts as silence
    start_ms: int = 96  # sustained voice needed to open an utterance
    pause_ms: int = 128  # sustained silence that marks a pause
    min_utterance_ms: int = 250  # voiced audio below this is discarded
    endpoint_complete_ms: int = 380  # silence before ending a semantically complete turn
    endpoint_default_ms: int = 750
    endpoint_incomplete_ms: int = 1700  # "I want you to…", "actually wait…"
    max_utterance_s: float = 45.0
    barge_in_threshold: float = 0.75
    barge_in_ms: int = 192  # sustained voice needed to interrupt Sugar


class STTSettings(BaseModel):
    device: Literal["auto", "cuda", "cpu"] = "auto"
    final_model: str = "large-v3-turbo"
    fast_model: str = "small.en"
    cpu_final_model: str = "small.en"
    cpu_fast_model: str = "base.en"
    language: str = "en"
    partials: bool = True
    partial_interval_ms: int = 700
    hotwords: bool = False  # bias Whisper toward `vocabulary` (raises hallucinations on noise)
    vocabulary: list[str] = Field(
        default_factory=lambda: ["Sugar", "Claude", "Claude Code", "VS Code", "Spotify", "GitHub"]
    )


class TTSSettings(BaseModel):
    engine: Literal["melo", "sapi", "none"] = "melo"
    device: Literal["auto", "cuda", "cpu"] = "auto"
    voice: str = "EN-Newest"
    speed: float = 1.1
    sdp_ratio: float = 0.5
    noise_scale: float = 0.6
    noise_scale_w: float = 0.8
    volume: float = 1.0
    fallback: bool = True  # fall back to Windows SAPI if MeloTTS fails
    max_spoken_chars: int = 900  # beyond this Sugar says "the rest is on screen"


class FreeLLMSettings(BaseModel):
    enabled: bool = True
    base_url: str = "http://127.0.0.1:31415/v1"
    api_key_env: str = "FREELLMAPI_API_KEY"
    chat_model: str = "auto"
    smart_model: str = "auto:smart"
    # Forwarded as `reasoning_effort`; FreeLLMAPI also uses it to pick backends. "none" keeps chat on
    # fast non-thinking models (measured TTFT ~1.2 s vs ~2 s for "low").
    effort_by_purpose: dict[str, str] = Field(
        default_factory=lambda: {"chat": "none", "agent": "low", "reasoning": "medium"}
    )
    timeout_s: float = 45.0
    connect_timeout_s: float = 3.0


class OllamaSettings(BaseModel):
    enabled: bool = True
    base_url: str = "http://127.0.0.1:11434"
    chat_model: str = "gemma3:4b"
    smart_model: str = "deepseek-r1:8b"
    keep_alive: str = "10m"
    timeout_s: float = 60.0


class ClaudeSettings(BaseModel):
    enabled: bool = True
    api_key_env: str = "ANTHROPIC_API_KEY"
    api_model: str = "claude-opus-5-5"
    cli_model: str = "opus"  # used through the Claude CLI when no API key is configured
    max_tokens: int = 4096
    timeout_s: float = 180.0


class LLMSettings(BaseModel):
    freellm: FreeLLMSettings = FreeLLMSettings()
    ollama: OllamaSettings = OllamaSettings()
    claude: ClaudeSettings = ClaudeSettings()
    chat_chain: list[str] = Field(default_factory=lambda: ["freellm", "ollama", "claude"])
    reasoning_chain: list[str] = Field(default_factory=lambda: ["claude", "freellm", "ollama"])
    temperature: float = 0.6


class ConversationSettings(BaseModel):
    user_name: str = "Yuki"
    assistant_name: str = "Sugar"
    wake_words: list[str] = Field(
        default_factory=lambda: ["sugar", "shugar", "suga", "sugga", "sugah", "shuga"]
    )
    engagement: Literal["wake_word", "always"] = "wake_word"
    engagement_timeout_s: float = 45.0
    history_turns: int = 12
    context_chars: int = 12000
    proactive_updates: bool = True


class PermissionSettings(BaseModel):
    # 0 READ, 1 NON_DESTRUCTIVE, 2 SENSITIVE, 3 DESTRUCTIVE.
    auto_approve_level: int = 1
    trusted_tools: list[str] = Field(default_factory=list)
    allowed_roots: list[Path] = Field(default_factory=lambda: [Path("D:/College"), Path.home()])
    confirmation_timeout_s: float = 25.0
    typing_delay_s: float = 1.5


class CodingSettings(BaseModel):
    claude_executable: str | None = None  # auto-detected when empty
    model: str = "opus"
    permission_mode: str = "auto"  # falls back to acceptEdits if the CLI lacks it
    analysis_permission_mode: str = "plan"
    effort: str | None = None
    max_budget_usd: float | None = None
    disallowed_tools: list[str] = Field(
        default_factory=lambda: ["Bash(git push *)", "Bash(git reset --hard *)", "Bash(rm -rf *)"]
    )
    project_roots: list[Path] = Field(
        default_factory=lambda: [
            Path("D:/College"),
            Path("D:/College/SOMESHIT DOWNLOADS"),
            Path("D:/College/WEBSITES"),
            Path.home() / "portfolio",
        ]
    )
    discovery_depth: int = 2
    progress_interval_s: float = 30.0


class SpotifySettings(BaseModel):
    enabled: bool = True
    client_id_env: str = "SPOTIFY_CLIENT_ID"
    client_secret_env: str = "SPOTIFY_CLIENT_SECRET"
    redirect_uri: str = "https://www.google.com/"
    scope: str = "user-read-playback-state user-modify-playback-state"


class WeatherSettings(BaseModel):
    location: str = ""  # empty = locate by IP
    units: Literal["metric", "imperial"] = "metric"


class UISettings(BaseModel):
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 0  # 0 = pick a free port
    window: Literal["auto", "webview", "edge", "browser", "none"] = "auto"
    developer_mode: bool = False


class LoggingSettings(BaseModel):
    level: str = "INFO"
    console: bool = True
    file: bool = True


class Settings(BaseModel):
    paths: PathsSettings = PathsSettings()
    audio: AudioSettings = AudioSettings()
    vad: VADSettings = VADSettings()
    stt: STTSettings = STTSettings()
    tts: TTSSettings = TTSSettings()
    llm: LLMSettings = LLMSettings()
    conversation: ConversationSettings = ConversationSettings()
    permissions: PermissionSettings = PermissionSettings()
    coding: CodingSettings = CodingSettings()
    spotify: SpotifySettings = SpotifySettings()
    weather: WeatherSettings = WeatherSettings()
    ui: UISettings = UISettings()
    logging: LoggingSettings = LoggingSettings()

    def secret(self, env_name: str | None) -> str | None:
        """Read a secret from the environment by variable name."""
        if not env_name:
            return None
        value = os.environ.get(env_name, "").strip()
        return value or None

    @property
    def overrides_path(self) -> Path:
        return self.paths.data_dir / "overrides.json"


def _deep_merge(base: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _env_overrides(environ: dict[str, str]) -> dict[str, Any]:
    """Turn ``SUGAR__TTS__SPEED=1.2`` into ``{"tts": {"speed": "1.2"}}``.

    Values are parsed as YAML scalars so booleans, numbers and lists work.
    """
    result: dict[str, Any] = {}
    for name, raw in environ.items():
        if not name.upper().startswith("SUGAR__"):
            continue
        parts = [p.lower() for p in name.split("__")[1:] if p]
        if not parts:
            continue
        try:
            value = yaml.safe_load(raw)
        except yaml.YAMLError:
            value = raw
        node = result
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
    return result


def load_settings(
    config_file: Path | None = None,
    *,
    use_env: bool = True,
    data_dir: Path | None = None,
) -> Settings:
    """Build settings from defaults, ``sugar.yaml``, runtime overrides and env."""
    if use_env:
        load_dotenv(ROOT_DIR / ".env", override=False)

    layers: dict[str, Any] = {}
    config_file = config_file or ROOT_DIR / "sugar.yaml"
    if config_file.exists():
        loaded = yaml.safe_load(config_file.read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            raise ValueError(f"{config_file} must contain a mapping at the top level")
        layers = _deep_merge(layers, loaded)

    if data_dir is not None:
        layers = _deep_merge(layers, {"paths": {"data_dir": str(data_dir)}})

    provisional = Settings.model_validate(layers)
    overrides_path = provisional.overrides_path
    if overrides_path.exists():
        try:
            layers = _deep_merge(layers, json.loads(overrides_path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            pass  # a corrupt override file must never stop Sugar from starting

    if use_env:
        layers = _deep_merge(layers, _env_overrides(dict(os.environ)))

    settings = Settings.model_validate(layers)
    settings.paths.data_dir.mkdir(parents=True, exist_ok=True)
    return settings


def save_override(settings: Settings, dotted_key: str, value: Any) -> None:
    """Persist one runtime setting change (e.g. ``tts.speed``) to overrides.json."""
    path = settings.overrides_path
    try:
        current = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, json.JSONDecodeError):
        current = {}
    node = current
    parts = dotted_key.split(".")
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(current, indent=2), encoding="utf-8")
