"""Memory store, working memory and context assembly."""

from __future__ import annotations

from pathlib import Path

from sugar.intelligence.context import ContextBuilder
from sugar.intelligence.working import WorkingMemory
from sugar.memory.store import MemoryStore, project_scope


def store(tmp_path: Path) -> MemoryStore:
    return MemoryStore(tmp_path / "sugar.db")


def test_remember_dedupes_and_search_ranks(tmp_path):
    memory = store(tmp_path)
    first = memory.remember("User prefers Python for scripting", kind="preference")
    again = memory.remember("user prefers python for scripting.", kind="preference")
    assert first.id == again.id
    memory.remember("User's dog is called Biscuit")
    memory.remember("Jiva uses WebSockets for hospital updates", scope=project_scope("D:/Jiva"))
    hits = memory.search("which language do I prefer for scripting", ["user"])
    assert hits and "Python" in hits[0].content
    assert not memory.search("websockets", ["user"])  # scoped away
    assert memory.search("websockets", ["user", project_scope("D:/Jiva")])


def test_forget_and_delete(tmp_path):
    memory = store(tmp_path)
    memory.remember("User likes jazz")
    memory.remember("User likes rock climbing")
    removed = memory.forget("that I like jazz", ["user"])
    assert removed and "jazz" in removed.content
    assert [m.content for m in memory.list("user")] == ["User likes rock climbing"]
    assert memory.delete(memory.list("user")[0].id)
    assert memory.list("user") == []


def test_history_round_trip(tmp_path):
    memory = store(tmp_path)
    memory.add_turn("c1", "user", "hi")
    memory.add_turn("c1", "assistant", "Hello! Long answer here.", heard="Hello!")
    memory.add_turn("c2", "user", "other")
    turns = memory.recent_turns("c1")
    assert [t["role"] for t in turns] == ["user", "assistant"]
    assert turns[1]["heard"] == "Hello!"
    assert memory.last_conversation() == "c2"


def test_working_memory_options_and_persistence(tmp_path):
    path = tmp_path / "working.json"
    working = WorkingMemory.load(path)
    working.set_project("Jiva", "D:/Jiva")
    working.offer_options(["Jiva", "Q-Shield", "Portfolio"])
    assert working.resolve_option("the second one") == "Q-Shield"
    assert working.resolve_option("number 3") == "Portfolio"
    assert working.resolve_option("the last one") == "Portfolio"
    working.record_action("coding", "ran tests", ok=False, error="2 tests failed")
    reloaded = WorkingMemory.load(path)
    assert reloaded.active_project == {"name": "Jiva", "path": "D:/Jiva"}
    assert reloaded.last_error == "2 tests failed"
    assert "Active project: Jiva" in working.describe()


def test_context_includes_state_memories_and_bounded_history(tmp_path, settings):
    memory = store(tmp_path)
    working = WorkingMemory()
    working.set_project("Jiva", "D:/Jiva")
    memory.remember("User prefers concise answers", kind="preference")
    for i in range(40):
        memory.add_turn("conv", "user", f"question {i} " + "x" * 400)
        memory.add_turn("conv", "assistant", f"answer {i} " + "y" * 400)
    memory.add_turn("conv", "assistant", "A long reply", heard="A long")
    settings.conversation.context_chars = 4000
    builder = ContextBuilder(settings, memory, working, lambda: "conv", extra_context=lambda: "Claude Code: idle")
    messages = builder.build("what is the status?")
    system = messages[0]["content"]
    assert "Active project: Jiva" in system and "concise answers" in system and "Claude Code: idle" in system
    assert sum(len(m["content"]) for m in messages[1:]) < 6000
    assert messages[1]["role"] == "user" and messages[-1] == {"role": "user", "content": "what is the status?"}
    assert any("interrupted" in m["content"] for m in messages if m["role"] == "assistant")
