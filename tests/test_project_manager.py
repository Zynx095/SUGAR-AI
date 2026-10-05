"""Project discovery, fuzzy/phonetic name matching and spoken paths."""

from __future__ import annotations

from pathlib import Path

import pytest

from sugar.coding.projects import ProjectRegistry, phonetic_key, resolve_spoken_path


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "College"
    for name, marker in [("SOMESHIT DOWNLOADS/Jiva", "package.json"), ("SOMESHIT DOWNLOADS/Q-Shield", ".git"),
                         ("SOMESHIT DOWNLOADS/SUGAR-AI", "pyproject.toml"), ("WEBSITES/RAM BB", "index.html"),
                         ("SOMESHIT DOWNLOADS/Jiva/node_modules/inner", "package.json")]:
        path = root / name
        path.mkdir(parents=True, exist_ok=True)
        if marker == ".git":
            (path / marker).mkdir()
        else:
            (path / marker).write_text("{}")
    return root


def test_discovery_finds_projects_and_skips_dependencies(tree, tmp_path):
    registry = ProjectRegistry([tree], tmp_path / "projects.json", depth=3)
    assert registry.discover() == 4
    names = sorted(p.name for p in registry.all())
    assert names == ["Jiva", "Q-Shield", "RAM BB", "SUGAR-AI"]
    assert ProjectRegistry([tree], tmp_path / "projects.json").all(), "persisted"


@pytest.mark.parametrize(("spoken", "expected"), [
    ("jiva", "Jiva"), ("jeeva", "Jiva"), ("q shield", "Q-Shield"), ("cue shield", "Q-Shield"),
    ("sugar ai", "SUGAR-AI"), ("ram bb", "RAM BB"), ("the sugar one", "SUGAR-AI"), ("photoshop", None),
])
def test_resolution(tree, tmp_path, spoken, expected):
    registry = ProjectRegistry([tree], tmp_path / "projects.json", depth=3)
    registry.discover()
    project = registry.resolve(spoken)
    assert (project.name if project else None) == expected


def test_phonetic_key():
    assert phonetic_key("Jeeva") == phonetic_key("jiva")


def test_spoken_path_walks_real_folders(tree, monkeypatch):
    import sugar.coding.projects as projects

    drive = tree.anchor  # e.g. "C:\\"
    letter = drive[0].lower()
    relative = tree.relative_to(drive)
    spoken_prefix = " ".join(relative.parts)
    result = resolve_spoken_path(f"{letter} colon {spoken_prefix} some shit downloads jiva")
    assert result == tree / "SOMESHIT DOWNLOADS" / "Jiva"
    # A folder that isn't on the spoken route is found by searching below the last match.
    assert resolve_spoken_path(f"{letter} colon {spoken_prefix} projects jiva") == tree / "SOMESHIT DOWNLOADS" / "Jiva"
    assert resolve_spoken_path(f"{letter} colon {spoken_prefix} nothing like this") is None
    assert projects.resolve_spoken_path("not a path at all") is None
