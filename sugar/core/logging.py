"""Structured logging.

Two sinks:
  * console — short human-readable lines
  * ``data/logs/sugar.jsonl`` — one JSON object per record (rotated)

Pipeline milestones are logged with :func:`log_event` using UPPER_SNAKE names
(``VOICE_DETECTED``, ``STT_FINAL``, ``LLM_FIRST_TOKEN`` …) so they can be
grepped and parsed by the benchmark tooling.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import sys
import time
from pathlib import Path
from typing import Any

_EVENT_ATTR = "sugar_event"
_FIELDS_ATTR = "sugar_fields"
_REDACT_KEYS = ("key", "token", "secret", "password", "authorization")


def _redact(fields: dict[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for name, value in fields.items():
        if any(marker in name.lower() for marker in _REDACT_KEYS):
            clean[name] = "[redacted]"
        else:
            clean[name] = value
    return clean


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": round(record.created, 3),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        event = getattr(record, _EVENT_ATTR, None)
        if event:
            payload["event"] = event
            payload.update(getattr(record, _FIELDS_ATTR, {}))
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


class ConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        stamp = time.strftime("%H:%M:%S", time.localtime(record.created))
        millis = int(record.msecs)
        event = getattr(record, _EVENT_ATTR, None)
        if event:
            fields = getattr(record, _FIELDS_ATTR, {})
            detail = " ".join(f"{k}={_short(v)}" for k, v in fields.items())
            line = f"{stamp}.{millis:03d} {record.levelname[0]} {event} {detail}".rstrip()
        else:
            line = f"{stamp}.{millis:03d} {record.levelname[0]} {record.name}: {record.getMessage()}"
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


def _short(value: Any, limit: int = 90) -> str:
    text = str(value).replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def setup_logging(level: str = "INFO", log_dir: Path | None = None, console: bool = True) -> None:
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.DEBUG)

    if console:
        stream = logging.StreamHandler(sys.stdout)
        stream.setLevel(getattr(logging, level.upper(), logging.INFO))
        stream.setFormatter(ConsoleFormatter())
        root.addHandler(stream)

    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_dir / "sugar.jsonl", maxBytes=5_000_000, backupCount=5, encoding="utf-8"
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(JsonFormatter())
        root.addHandler(file_handler)

    # Third-party libraries are chatty at DEBUG/INFO.
    for noisy in ("httpx", "httpcore", "urllib3", "faster_whisper", "websockets", "spotipy",
                  "transformers", "huggingface_hub", "asyncio", "numba", "filelock"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def log_event(event: str, *, severity: int = logging.INFO, logger: str = "sugar", **fields: Any) -> None:
    """Log a named pipeline milestone with structured fields."""
    logging.getLogger(logger).log(
        severity,
        event,
        extra={_EVENT_ATTR: event, _FIELDS_ATTR: _redact(fields)},
    )
