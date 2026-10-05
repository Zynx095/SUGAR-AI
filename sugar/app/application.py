"""Application assembly and lifecycle.

Start-up is progressive: the UI and the text path are available within a
second, while Whisper and MeloTTS load in background threads (the UI shows
each component's status). If a component fails, Sugar keeps running without
it and says so — no silent failure, no crash.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any

from sugar.agent.executor import ToolExecutor
from sugar.agent.loop import AgentLoop
from sugar.agent.permissions import PermissionManager
from sugar.coding.projects import ProjectRegistry
from sugar.coding.sessions import CodingSessionManager
from sugar.computer import ComputerControl
from sugar.computer.apps import AppCatalog
from sugar.computer.context import BROWSER_PROCESSES
from sugar.computer.spotify import SpotifyController
from sugar.config.settings import Settings, save_override
from sugar.core.events import EventBus
from sugar.core.logging import log_event
from sugar.core.metrics import MetricsRecorder, TurnTrace
from sugar.core.state import StateMachine
from sugar.intelligence.context import ContextBuilder
from sugar.intelligence.conversation import ConversationManager
from sugar.intelligence.fastpath import DesktopView, FastPath
from sugar.intelligence.orchestrator import Orchestrator
from sugar.intelligence.router import Router
from sugar.intelligence.working import WorkingMemory
from sugar.memory.store import MemoryStore
from sugar.providers import build_provider_pool
from sugar.tools import build_registry
from sugar.tools.calculator import evaluate_spoken_math
from sugar.tools.services import ToolServices

log = logging.getLogger(__name__)

PREWARM_PHRASES = ["Yeah?", "Done.", "Stopped.", "Standing by.", "On it.", "Typing.", "Let me think about that.",
                   "Okay, I won't.", "Mic's off.", "Got it, I'll remember that."]
LIVE_SETTINGS = {
    "tts.speed", "tts.voice", "tts.volume", "vad.threshold", "vad.endpoint_default_ms", "vad.endpoint_complete_ms",
    "vad.endpoint_incomplete_ms", "conversation.engagement", "conversation.engagement_timeout_s",
    "permissions.auto_approve_level", "ui.developer_mode", "stt.partials",
}


class SugarApp:
    def __init__(self, settings: Settings, *, voice: bool = True) -> None:
        self.settings = settings
        self.voice_enabled = voice
        self.bus = EventBus()
        self.state = StateMachine(self.bus)
        data = settings.paths.data_dir
        self.metrics = MetricsRecorder(self.bus, data / "metrics")
        self.memory = MemoryStore(data / "sugar.db")
        self.working = WorkingMemory.load(data / "working.json")
        self.projects = ProjectRegistry(settings.coding.project_roots, data / "projects.json",
                                        settings.coding.discovery_depth)
        self.sessions = CodingSessionManager(settings.coding, self.bus, data / "coding_sessions.json")
        self.apps = AppCatalog(data / "apps.json")
        self.spotify = SpotifyController(settings)
        self.computer = ComputerControl(settings, self.apps, self.spotify)
        self.shared_state: dict[str, Any] = {}
        self.pool = build_provider_pool(settings, self.bus)

        self.services = ToolServices(settings, self.bus, self.apps, self.projects, self.sessions, self.memory,
                                     self.working, self.spotify, self.computer, data, self.shared_state)
        self.registry = build_registry(self.services)
        self.permissions = PermissionManager(settings.permissions, self.bus)
        self.executor = ToolExecutor(self.registry, self.permissions, self.bus)
        self.fastpath = FastPath(resolve_app=self.computer.apps.resolve_name, resolve_project=self.projects.resolve,
                                 evaluate_math=evaluate_spoken_math, desktop=self.desktop_view)
        self.router = Router(settings, self.fastpath, self.working, coding_active=lambda: bool(self.sessions.running()))
        self.context = ContextBuilder(settings, self.memory, self.working, lambda: self.conversation.conversation_id,
                                      extra_context=self._extra_context)
        self.agent = AgentLoop(self.pool, self.executor, self.registry, self.bus)
        self.orchestrator = Orchestrator(settings, self.bus, self.router, self.pool, self.agent, self.executor,
                                         self.context, self.working, self.sessions, self.shared_state)

        # Audio (constructed now, models loaded in start()).
        from sugar.audio.playback import AudioPlayer
        from sugar.audio.speech import SpeechOutput
        from sugar.audio.tts import build_synthesizer

        self.synth = build_synthesizer(settings.tts, data / "tts_tmp")
        self.player = AudioPlayer(device=settings.audio.output_device)
        self.player.volume = settings.tts.volume
        self.speech = SpeechOutput(self.synth, self.player, self.bus)
        self.stt = None
        self.pipeline = None
        if voice:
            from sugar.audio.capture import MicrophoneStream
            from sugar.audio.echo import EchoGuard
            from sugar.audio.pipeline import VoicePipeline
            from sugar.audio.stt import SpeechRecognizer
            from sugar.audio.vad import create_vad

            self.stt = SpeechRecognizer(settings.stt, settings.paths.whisper_dir)
            mic = MicrophoneStream(settings.audio.input_device, settings.audio.sample_rate, settings.audio.block_size)
            echo = EchoGuard(mode="off" if settings.audio.echo_mode == "off" else "auto")
            self.pipeline = VoicePipeline(settings, self.bus, mic, create_vad(), echo, self.player)

        self.conversation = ConversationManager(
            settings, self.bus, self.state, self.metrics, self.speech, self.orchestrator, self.fastpath,
            self.permissions, self.memory, self.working, self.sessions, self.shared_state,
            stt=self.stt, pipeline=self.pipeline,
        )
        self.conversation.on_idle_stop = self._stop_media
        self.components: dict[str, dict[str, Any]] = {}
        self.ui = None
        self.ui_ready = threading.Event()  # set once the UI server is listening (window can open)
        self.audio_output = True  # False for text-only runs
        self._stopping = asyncio.Event()
        self._background: set[asyncio.Task] = set()
        self.loop: asyncio.AbstractEventLoop | None = None

    def _spawn(self, coro) -> asyncio.Task:
        """Start a background task that shutdown() cancels and awaits (no orphaned subprocesses)."""
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return task

    # ------------------------------------------------------------------ desktop context

    def desktop_view(self) -> DesktopView:
        """Cached desktop facts for the fast-path grammar (no OS calls beyond IsWindow)."""
        context = self.computer.context
        active = context.active()
        dialog = context.pending_dialog
        search = context.last_search
        last = self.working.last_action
        return DesktopView(
            active_app=active.app if active else None,
            active_is_browser=bool(active and active.process in BROWSER_PROCESSES),
            has_dialog=dialog is not None,
            dialog_kind=dialog.kind if dialog else None,
            has_search=bool(search and time.time() - search.ts < 1800),
            last_domain=last.domain if last and time.time() - last.ts < 600 else None,
            titles=tuple(w.title.lower() for w in context.recent_windows()[:8]),
        )

    def _extra_context(self) -> str:
        parts = [self.sessions.describe_for_context(), self.computer.describe()]
        return "\n".join(p for p in parts if p)

    async def _stop_media(self) -> str | None:
        """"Stop" with nothing of Sugar's to stop: pause what's playing, if anything is."""
        if not self.computer.available:
            return None
        sessions = await self.computer.media.sessions()
        if not any(s.status == "playing" for s in sessions):
            return None
        result = await self.computer.media.control("pause")
        return result.details if result.success else None

    # ------------------------------------------------------------------ status

    def _component(self, name: str, status: str, detail: str = "") -> None:
        self.components[name] = {"status": status, "detail": detail, "ts": time.time()}
        log_event("COMPONENT", component=name, status=status, detail=detail)
        self.bus.publish("system.status", component=name, status=status, detail=detail)

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.bus.bind(self.loop)
        self.conversation.start(self.loop)
        self.speech.start(self.loop)
        if self.settings.ui.enabled:
            from sugar.ui.server import UIServer

            self.ui = UIServer(self.bus, self.settings.ui.host, self.settings.ui.port, self.handle_command,
                               self.snapshot)
            await self.ui.start()
            self._component("ui", "ready", self.ui.url.split("?")[0])
        self.ui_ready.set()
        self.vocabulary_refresh()
        if self.computer.available:
            self.computer.start(self.bus)
            status = self.computer.status()
            self._component("computer", "ready", f"default browser {status['default_browser'] or 'none'}; "
                            f"YouTube search via {status['youtube_search']}")
        else:
            self._component("computer", "disabled", "desktop control is off for this run")
        self._spawn(self._background_discovery())
        self._spawn(self._check_providers())
        await self._load_audio()

    async def _load_audio(self) -> None:
        loop = asyncio.get_running_loop()
        if not self.audio_output:
            for name in ("speaker", "tts", "stt", "microphone"):
                self._component(name, "disabled", "text-only run")
            self.speech.muted = True
            self.conversation._settle_state("startup complete")
            self.bus.publish("system.ready", components=self.components)
            return
        self.speech.muted = True  # until a TTS engine is loaded; typed turns still get text replies
        self._component("speaker", "loading")
        try:
            self.player.start()
            self._component("speaker", "ready", self.player.device_name or "")
        except Exception as exc:
            self._component("speaker", "error", f"no speaker: {exc}")

        async def load_tts() -> None:
            self._component("tts", "loading")
            try:
                await loop.run_in_executor(None, self.synth.load)
                await loop.run_in_executor(None, self.synth.prewarm, PREWARM_PHRASES)
                engine = self.synth._active
                detail = f"{self.synth.engine_name} on {getattr(engine, 'device', 'cpu')}"
                self.speech.muted = False
                self._component("tts", "ready", detail)
            except Exception as exc:
                log.exception("TTS failed to load")
                self._component("tts", "error", str(exc))

        async def load_stt() -> None:
            if self.stt is None:
                self._component("stt", "disabled", "voice input off")
                return
            self._component("stt", "loading")
            try:
                await loop.run_in_executor(None, self.stt.load)
                detail = f"{self.stt.final.name} + {self.stt.fast.name} on {self.stt.device}"
                self._component("stt", "ready", detail)
            except Exception as exc:
                log.exception("STT failed to load")
                self._component("stt", "error", str(exc))

        await asyncio.gather(load_tts(), load_stt())
        if self.pipeline is not None and self.components.get("stt", {}).get("status") == "ready":
            self.pipeline.attach(self.conversation, loop)
            try:
                self.pipeline.start()
                self._component("microphone", "ready", self.pipeline.mic.device_name or "")
            except Exception as exc:
                log.exception("microphone failed")
                self._component("microphone", "error", str(exc))
        elif self.pipeline is None:
            self._component("microphone", "disabled", "voice input off")
        self.conversation._settle_state("startup complete")
        self.bus.publish("system.ready", components=self.components)
        log_event("READY", components={k: v["status"] for k, v in self.components.items()})

    async def _background_discovery(self) -> None:
        loop = asyncio.get_running_loop()
        try:
            count = await loop.run_in_executor(None, self.projects.discover)
            self._component("projects", "ready", f"{count} projects")
            self.vocabulary_refresh()
        except Exception as exc:
            self._component("projects", "error", str(exc))
        if time.time() - self.apps.loaded_at > 6 * 3600:
            try:
                count = await loop.run_in_executor(None, self.apps.refresh)
                self._component("apps", "ready", f"{count} apps")
            except Exception as exc:
                self._component("apps", "error", str(exc))
        else:
            self._component("apps", "ready", f"{len(self.apps.names())} apps (cached)")
        cli = await self.sessions.cli()
        self._component("claude_code", "ready" if cli else "unavailable",
                        f"Claude Code {cli.version}" if cli else "claude CLI not found")

    async def _check_providers(self) -> None:
        report = await self.pool.check_health()
        for name, info in report.items():
            self._component(f"llm:{name}", "ready" if info["ok"] else "unavailable", info["detail"])

    def vocabulary_refresh(self) -> None:
        if self.stt is not None:
            self.stt.set_vocabulary(self.projects.names()[:20])

    async def run_forever(self) -> None:
        await self._stopping.wait()

    def request_stop(self) -> None:
        if self.loop is not None:
            self.loop.call_soon_threadsafe(self._stopping.set)

    async def shutdown(self) -> None:
        log.info("shutting down")
        for task in list(self._background):
            task.cancel()
        if self._background:
            await asyncio.gather(*self._background, return_exceptions=True)
        await self.sessions.shutdown()
        self.computer.stop()
        if self.pipeline is not None:
            self.pipeline.stop()
        self.speech.cancel()
        self.speech.shutdown()
        self.player.stop_stream()
        self.conversation.shutdown()
        if self.ui is not None:
            await self.ui.stop()
        await self.pool.aclose()
        self.memory.close()

    # ------------------------------------------------------------------ UI bridge

    def snapshot(self) -> dict[str, Any]:
        turns = self.memory.recent_turns(self.conversation.conversation_id, 40)
        return {
            "state": self.state.state.value,
            "engaged": self.conversation.is_engaged(),
            "components": self.components,
            "settings": self.settings.model_dump(mode="json", include={"tts", "vad", "conversation", "ui",
                                                                         "permissions", "stt"}),
            "history": turns,
            "project": self.working.active_project,
            "projects": [p.to_dict() for p in self.projects.all()[:30]],
            "sessions": [s.to_dict() for s in self.sessions.all()],
            "providers": self.pool.status(),
            "metrics": self.metrics.summary(),
            "memories": [m.to_dict() for m in self.memory.list(limit=100)],
            "voices": getattr(self.synth._active, "voices", []) if self.synth._active else [],
            "mic_paused": bool(self.pipeline and self.pipeline.paused),
            "pending_permission": self.permissions.pending.to_dict() if self.permissions.pending else None,
            "desktop": self.computer.context.snapshot() if self.computer.available else None,
        }

    async def handle_command(self, message: dict[str, Any]) -> dict[str, Any] | None:
        cmd = message.get("cmd")
        if cmd == "text":
            asyncio.create_task(self.conversation.handle_text(str(message.get("text", ""))))
            return {"ok": True}
        if cmd == "stop":
            await self.conversation.submit("stop", source="text", trace=TurnTrace("text"))
            return {"ok": True}
        if cmd == "mic":
            if self.pipeline is None:
                return {"ok": False, "error": "voice input is off"}
            if message.get("on"):
                self.pipeline.resume()
            else:
                self.pipeline.pause()
            self.conversation._settle_state("mic toggle")
            return {"ok": True, "paused": self.pipeline.paused}
        if cmd == "wake":
            self.conversation.engage()
            return {"ok": True}
        if cmd == "sleep":
            self.conversation.disengage()
            return {"ok": True}
        if cmd == "permission":
            resolved = self.permissions.resolve(bool(message.get("approved")), message.get("id"))
            return {"ok": resolved}
        if cmd == "setting":
            return self.apply_setting(str(message.get("key")), message.get("value"))
        if cmd == "memory.delete":
            deleted = self.memory.delete(int(message.get("id", -1)))
            self.bus.publish("memory.changed", action="delete", id=message.get("id"))
            return {"ok": deleted}
        if cmd == "new_conversation":
            self.conversation.new_conversation()
            return {"ok": True}
        if cmd == "coding":
            action = message.get("action")
            tool = {"pause": "claude.pause", "resume": "claude.resume", "stop": "claude.stop"}.get(action)
            if tool is None:
                return {"ok": False, "error": "unknown action"}
            result = await self.executor.execute(tool, {}, origin="user")
            return {"ok": result.ok, "summary": result.summary}
        if cmd == "snapshot":
            return self.snapshot()
        if cmd == "open_url":
            url = str(message.get("url", ""))
            if not url.startswith(("http://", "https://")):
                return {"ok": False, "error": "only web links can be opened"}
            import webbrowser

            await asyncio.to_thread(webbrowser.open, url)
            return {"ok": True}
        return {"ok": False, "error": f"unknown command {cmd}"}

    def apply_setting(self, key: str, value: Any) -> dict[str, Any]:
        if key not in LIVE_SETTINGS:
            return {"ok": False, "error": f"{key} can't be changed at runtime"}
        section, name = key.split(".", 1)
        model = getattr(self.settings, section)
        current = getattr(model, name)
        try:
            coerced = type(current)(value) if current is not None and not isinstance(current, str) else value
        except (TypeError, ValueError):
            return {"ok": False, "error": "invalid value"}
        setattr(model, name, coerced)
        save_override(self.settings, key, coerced)
        if section == "tts":
            self.synth._cache.clear()
            if name == "volume":
                self.player.volume = float(coerced)
        if section == "vad" and self.pipeline is not None:
            from sugar.audio.pipeline import endpoint_config_from

            self.pipeline.endpointer.config = endpoint_config_from(self.settings)
        self.bus.publish("settings.changed", key=key, value=coerced)
        return {"ok": True, "value": coerced}
