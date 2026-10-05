"""Persistent memory (SQLite + FTS5).

Tables:
  * ``turns``     — conversation history (what the user said, what Sugar said
                    and what the user actually heard), per conversation id;
  * ``memories``  — deliberate long-term facts with a *scope*
                    (``user``, ``project:<path>``, ``session:<id>``) and a *kind*
                    (preference, fact, note, task, summary);
  * ``memories_fts`` — full-text index used for relevance search.

Nothing is stored implicitly except history: memories are written when the
user asks Sugar to remember something or when a coding session finishes
(its summary becomes project memory). Everything is listable and deletable.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS turns (
    id INTEGER PRIMARY KEY,
    conversation TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    heard TEXT,
    ts REAL NOT NULL,
    meta TEXT
);
CREATE INDEX IF NOT EXISTS turns_conversation ON turns(conversation, id);
CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY,
    scope TEXT NOT NULL,
    kind TEXT NOT NULL,
    content TEXT NOT NULL,
    source TEXT,
    importance REAL NOT NULL DEFAULT 0.5,
    created REAL NOT NULL,
    updated REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS memories_scope ON memories(scope);
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    content, content='memories', content_rowid='id', tokenize='porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content) VALUES ('delete', old.id, old.content);
END;
CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content) VALUES ('delete', old.id, old.content);
    INSERT INTO memories_fts(rowid, content) VALUES (new.id, new.content);
END;
"""

_STOPWORDS = {
    "the", "a", "an", "and", "or", "to", "of", "in", "on", "for", "is", "are", "was", "it", "that", "this",
    "what", "do", "you", "me", "my", "i", "can", "please", "with", "about", "be", "at", "sugar", "how", "why",
}


@dataclass
class Memory:
    id: int
    scope: str
    kind: str
    content: str
    source: str | None
    importance: float
    created: float
    updated: float

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def project_scope(path: str | Path) -> str:
    return "project:" + str(Path(path)).lower().replace("\\", "/")


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", text.lower())).strip()


class MemoryStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ------------------------------------------------------------------ history

    def add_turn(self, conversation: str, role: str, content: str, *, heard: str | None = None,
                 meta: dict[str, Any] | None = None) -> int:
        with self._lock:
            cursor = self._db.execute(
                "INSERT INTO turns(conversation, role, content, heard, ts, meta) VALUES (?,?,?,?,?,?)",
                (conversation, role, content, heard, time.time(), json.dumps(meta) if meta else None),
            )
            self._db.commit()
            return int(cursor.lastrowid)

    def recent_turns(self, conversation: str, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT role, content, heard, ts FROM turns WHERE conversation=? ORDER BY id DESC LIMIT ?",
                (conversation, limit),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def last_conversation(self) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT conversation FROM turns ORDER BY id DESC LIMIT 1").fetchone()
        return row["conversation"] if row else None

    def clear_history(self, conversation: str | None = None) -> int:
        with self._lock:
            if conversation:
                cursor = self._db.execute("DELETE FROM turns WHERE conversation=?", (conversation,))
            else:
                cursor = self._db.execute("DELETE FROM turns")
            self._db.commit()
            return cursor.rowcount

    # ------------------------------------------------------------------ memories

    def remember(self, content: str, *, scope: str = "user", kind: str = "fact", source: str = "user",
                 importance: float = 0.6) -> Memory:
        content = content.strip()
        if not content:
            raise ValueError("empty memory")
        now = time.time()
        key = _normalise(content)
        with self._lock:
            for row in self._db.execute("SELECT * FROM memories WHERE scope=? AND kind=?", (scope, kind)):
                if _normalise(row["content"]) == key:
                    self._db.execute("UPDATE memories SET updated=?, importance=MAX(importance, ?) WHERE id=?",
                                     (now, importance, row["id"]))
                    self._db.commit()
                    return self._get(row["id"])
            cursor = self._db.execute(
                "INSERT INTO memories(scope, kind, content, source, importance, created, updated) "
                "VALUES (?,?,?,?,?,?,?)",
                (scope, kind, content, source, importance, now, now),
            )
            self._db.commit()
            return self._get(int(cursor.lastrowid))

    def _get(self, memory_id: int) -> Memory:
        row = self._db.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
        return Memory(**dict(row))

    def get(self, memory_id: int) -> Memory | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
        return Memory(**dict(row)) if row else None

    def list(self, scope: str | None = None, kind: str | None = None, limit: int = 200) -> list[Memory]:
        query, args = "SELECT * FROM memories WHERE 1=1", []
        if scope:
            query += " AND scope=?"
            args.append(scope)
        if kind:
            query += " AND kind=?"
            args.append(kind)
        query += " ORDER BY updated DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._db.execute(query, args).fetchall()
        return [Memory(**dict(row)) for row in rows]

    def delete(self, memory_id: int) -> bool:
        with self._lock:
            cursor = self._db.execute("DELETE FROM memories WHERE id=?", (memory_id,))
            self._db.commit()
            return cursor.rowcount > 0

    def search(self, query: str, scopes: list[str] | None = None, limit: int = 5) -> list[Memory]:
        terms = [t for t in re.findall(r"[a-zA-Z0-9]+", query.lower()) if t not in _STOPWORDS and len(t) > 1]
        if not terms:
            return []
        match = " OR ".join(f'"{t}"*' for t in terms[:12])
        sql = ("SELECT m.*, bm25(memories_fts) AS rank FROM memories_fts JOIN memories m ON m.id = memories_fts.rowid "
               "WHERE memories_fts MATCH ?")
        args: list[Any] = [match]
        if scopes:
            sql += f" AND m.scope IN ({','.join('?' * len(scopes))})"
            args += scopes
        sql += " ORDER BY rank LIMIT ?"
        args.append(limit * 3)
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
        now = time.time()
        scored = []
        for row in rows:
            data = dict(row)
            rank = data.pop("rank")
            recency = 1.0 / (1.0 + (now - data["updated"]) / 86400.0 / 30.0)  # decays over months
            scored.append((-rank * (0.5 + data["importance"]) * (0.7 + 0.3 * recency), Memory(**data)))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [memory for _, memory in scored[:limit]]

    def forget(self, query: str, scopes: list[str] | None = None) -> Memory | None:
        """Delete the single best match for a spoken description ("forget that I like jazz")."""
        matches = self.search(query, scopes, limit=1)
        if matches and self.delete(matches[0].id):
            return matches[0]
        return None
