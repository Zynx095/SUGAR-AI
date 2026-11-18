"""A local Ollama server that SID starts and stops itself.

The portable Ollama build lives on D: (``llm.ollama.executable``) with its models on D:
(``llm.ollama.models_dir``), so nothing is installed system-wide and nothing large lands on C:.
If an Ollama server is already listening (for example the user's own install), SID uses it and
leaves it alone; it only stops a server it started.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

from sugar.core.processes import CREATE_NO_WINDOW

log = logging.getLogger(__name__)


class OllamaServer:
    def __init__(self, base_url: str, executable: str | None, models_dir: str | None) -> None:
        self.base_url = base_url.rstrip("/")
        self.executable = Path(executable) if executable else None
        self.models_dir = Path(models_dir) if models_dir else None
        self._process: subprocess.Popen | None = None
        self.status = "unknown"  # external | started | unavailable | missing

    async def reachable(self, timeout_s: float = 1.5) -> bool:
        try:
            async with httpx.AsyncClient(timeout=timeout_s) as client:
                response = await client.get(f"{self.base_url}/api/version")
            return response.status_code == 200
        except httpx.HTTPError:
            return False

    async def ensure_running(self, wait_s: float = 25.0) -> str:
        if await self.reachable():
            self.status = "external" if self._process is None else "started"
            return self.status
        if self.executable is None or not self.executable.exists():
            self.status = "missing"
            return self.status
        env = dict(os.environ)
        host = urlparse(self.base_url)
        env["OLLAMA_HOST"] = f"{host.hostname or '127.0.0.1'}:{host.port or 11434}"
        if self.models_dir is not None:
            self.models_dir.mkdir(parents=True, exist_ok=True)
            env["OLLAMA_MODELS"] = str(self.models_dir)
        log.info("starting Ollama from %s (models in %s)", self.executable, self.models_dir)
        self._process = await asyncio.to_thread(
            subprocess.Popen, [str(self.executable), "serve"], env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW)
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                self.status = "unavailable"
                log.warning("Ollama exited with code %s", self._process.returncode)
                self._process = None
                return self.status
            if await self.reachable():
                self.status = "started"
                return self.status
            await asyncio.sleep(0.5)
        self.status = "unavailable"
        return self.status

    def stop(self) -> None:
        process, self._process = self._process, None
        if process is None or process.poll() is not None:
            return
        self.status = "stopped"
        # `ollama serve` starts model runner children: end the whole tree.
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True,
                       creationflags=CREATE_NO_WINDOW, check=False)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
