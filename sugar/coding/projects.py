"""Project discovery and spoken-name resolution.

Projects are discovered by scanning configured roots (``coding.project_roots``)
for directories with project markers (.git, package.json, pyproject.toml, …).
Nothing is hard-coded: any repo under those roots becomes addressable by
voice, and paths can also be spoken ("the project in D colon College
Projects JIVA"), resolved component by component against the real
directory tree with fuzzy matching.

The registry is persisted to ``data/projects.json`` with last-opened times so
"open my project" and "switch back" have something to work with.
"""

from __future__ import annotations

import difflib
import json
import logging
import os
import re
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from sugar.core.processes import CREATE_NO_WINDOW

log = logging.getLogger(__name__)

MARKERS = (
    ".git", "package.json", "pyproject.toml", "requirements.txt", "setup.py", "Cargo.toml", "go.mod", "pom.xml",
    "build.gradle", "build.gradle.kts", "composer.json", "Gemfile", "CMakeLists.txt", "platformio.ini",
    "pubspec.yaml", "deno.json", "manage.py", "index.html", "Dockerfile", "CLAUDE.md",
)
SKIP_DIRS = {
    "node_modules", "venv", ".venv", "env", ".git", "__pycache__", "dist", "build", ".next", "target", "out",
    ".idea", ".vscode", "site-packages", "bin", "obj", ".cache", "data", "assets", "public", "src", "lib",
}
STACK_HINTS = {
    "package.json": "node", "pyproject.toml": "python", "requirements.txt": "python", "setup.py": "python",
    "Cargo.toml": "rust", "go.mod": "go", "pom.xml": "java", "build.gradle": "java", "composer.json": "php",
    "Gemfile": "ruby", "platformio.ini": "embedded", "pubspec.yaml": "flutter", "manage.py": "django",
}


@dataclass
class Project:
    name: str
    path: str
    stack: list[str] = field(default_factory=list)
    has_git: bool = False
    last_opened: float = 0.0
    aliases: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def spoken_key(text: str) -> str:
    """Lower-case, alphanumerics only: 'Q-Shield' == 'q shield' == 'qshield'."""
    return re.sub(r"[^a-z0-9]", "", text.lower())


_SOUNDS = [("ph", "f"), ("ck", "k"), ("q", "k"), ("c", "k"), ("z", "s"), ("ee", "i"), ("ea", "i"), ("y", "i"),
           ("oo", "u"), ("ou", "u"), ("w", "v"), ("ue", "u")]


def phonetic_key(text: str) -> str:
    """Crude sound-alike key so 'jeeva' ≈ 'jiva' and 'cue shield' ≈ 'Q-Shield'."""
    key = spoken_key(text)
    for source, target in _SOUNDS:
        key = key.replace(source, target)
    return re.sub(r"(.)\1+", r"\1", key)


def similarity(a: str, b: str) -> float:
    plain = difflib.SequenceMatcher(None, spoken_key(a), spoken_key(b)).ratio()
    sound = difflib.SequenceMatcher(None, phonetic_key(a), phonetic_key(b)).ratio()
    return max(plain, sound)


def _spoken_variants(name: str) -> set[str]:
    variants = {spoken_key(name)}
    spaced = re.sub(r"[-_.]+", " ", re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name)).lower()
    variants.add(spoken_key(spaced))
    variants.add(spoken_key(spaced.replace(" ai", "")))
    return {v for v in variants if v}


class ProjectRegistry:
    def __init__(self, roots: list[Path], store_path: Path, depth: int = 2) -> None:
        self._roots = roots
        self._store_path = store_path
        self._depth = depth
        self._projects: dict[str, Project] = {}
        self._lock = threading.Lock()
        self._load()

    # ------------------------------------------------------------------ persistence

    def _load(self) -> None:
        try:
            data = json.loads(self._store_path.read_text(encoding="utf-8"))
            for item in data.get("projects", []):
                project = Project(**item)
                self._projects[project.path.lower()] = project
        except (OSError, json.JSONDecodeError, TypeError):
            pass

    def save(self) -> None:
        with self._lock:
            items = [p.to_dict() for p in self._projects.values()]
        try:
            self._store_path.parent.mkdir(parents=True, exist_ok=True)
            self._store_path.write_text(json.dumps({"projects": items}, indent=1), encoding="utf-8")
        except OSError:
            pass

    # ------------------------------------------------------------------ discovery

    def discover(self) -> int:
        found: dict[str, Project] = {}
        for root in self._roots:
            if root.is_dir():
                self._scan(root, 0, found)
        with self._lock:
            for key, project in found.items():
                existing = self._projects.get(key)
                if existing:
                    project.last_opened = existing.last_opened
                    project.aliases = existing.aliases
                self._projects[key] = project
            # Forget projects whose folder disappeared (e.g. an unplugged drive is kept: only drop deleted dirs
            # on drives that are present).
            for key in list(self._projects):
                path = Path(self._projects[key].path)
                if Path(path.anchor).exists() and not path.exists():
                    del self._projects[key]
        self.save()
        return len(self._projects)

    def _scan(self, directory: Path, depth: int, found: dict[str, Project]) -> None:
        try:
            entries = list(os.scandir(directory))
        except OSError:
            return
        names = {entry.name for entry in entries}
        markers = [m for m in MARKERS if m in names]
        if markers and depth > 0:
            stack = sorted({STACK_HINTS[m] for m in markers if m in STACK_HINTS})
            found[str(directory).lower()] = Project(directory.name, str(directory), stack, ".git" in names)
            if ".git" in names:
                return  # don't descend into a repository's subfolders
        if depth >= self._depth:
            return
        for entry in entries:
            if entry.is_dir(follow_symlinks=False) and entry.name not in SKIP_DIRS and not entry.name.startswith("."):
                self._scan(Path(entry.path), depth + 1, found)

    def add(self, path: Path) -> Project:
        project = Project(path.name, str(path), has_git=(path / ".git").exists())
        with self._lock:
            self._projects[str(path).lower()] = project
        self.save()
        return project

    # ------------------------------------------------------------------ queries

    def all(self) -> list[Project]:
        with self._lock:
            projects = list(self._projects.values())
        return sorted(projects, key=lambda p: (-p.last_opened, p.name.lower()))

    def names(self) -> list[str]:
        return [p.name for p in self.all()]

    def resolve(self, spoken: str) -> Project | None:
        """Fuzzy match a spoken project name ("jeeva", "q shield", "the sugar one")."""
        query = re.sub(r"\b(?:the|my|our|project|repo|repository|folder|codebase|one|app)\b", " ", spoken.lower())
        key = spoken_key(query)
        if not key:
            return None
        projects = self.all()
        exact = [p for p in projects if key in _spoken_variants(p.name) or key in map(spoken_key, p.aliases)]
        if exact:
            return exact[0]
        scored = []
        for project in projects:
            best = max(similarity(key, variant) for variant in _spoken_variants(project.name))
            if key in spoken_key(project.name) and len(key) >= 4:
                best = max(best, 0.86)
            scored.append((best, project))
        scored.sort(key=lambda item: (-item[0], -item[1].last_opened))
        if scored and scored[0][0] >= 0.78:
            return scored[0][1]
        return None

    def mark_opened(self, project: Project) -> None:
        project.last_opened = time.time()
        with self._lock:
            self._projects[project.path.lower()] = project
        self.save()

    def git_summary(self, project: Project) -> dict:
        if not project.has_git:
            return {}
        def git(*args: str) -> str:
            try:
                return subprocess.run(["git", "-C", project.path, *args], capture_output=True, text=True, timeout=10,
                                      creationflags=CREATE_NO_WINDOW).stdout.strip()
            except (subprocess.SubprocessError, OSError):
                return ""
        status = git("status", "--porcelain")
        return {"branch": git("rev-parse", "--abbrev-ref", "HEAD"),
                "changed_files": len([line for line in status.splitlines() if line.strip()])}


# --------------------------------------------------------------------------- spoken paths

_DRIVE_RE = re.compile(r"^\s*([a-z])\s*(?:colon|drive|:)\s*(.*)$")


def resolve_spoken_path(spoken: str) -> Path | None:
    """'D colon College Projects JIVA' → D:\\College\\...\\JIVA (matched against real folders)."""
    match = _DRIVE_RE.match(spoken.lower().strip())
    if not match:
        return None
    current = Path(f"{match.group(1).upper()}:\\")
    if not current.exists():
        return None
    words = [w for w in re.split(r"[\s\\/,]+", match.group(2)) if w and w not in {"slash", "backslash", "folder"}]
    index = 0
    last_matched_end = 0
    while index < len(words):
        children = _child_dirs(current)
        best: tuple[float, str, int] | None = None
        for span in range(min(4, len(words) - index), 0, -1):
            candidate = "".join(words[index:index + span])
            for child in children:
                score = similarity(candidate, child)
                if score >= 0.8 and (best is None or score > best[0] or (score == best[0] and span > best[2])):
                    best = (score, child, span)
        if best is None:
            index += 1  # filler or a folder that isn't where the user thinks ("Projects")
            continue
        current = current / best[1]
        index += best[2]
        last_matched_end = index
    tail = words[last_matched_end:]
    if tail:
        # The last spoken name wasn't found along the spoken route: search below what did match.
        found = _search_below(current, tail[-3:])
        if found is None:
            return None
        current = found
    return current if current.exists() and current != Path(current.anchor) else None


def _child_dirs(directory: Path) -> list[str]:
    try:
        return [entry.name for entry in os.scandir(directory) if entry.is_dir() and entry.name not in SKIP_DIRS]
    except OSError:
        return []


def _search_below(root: Path, tail_words: list[str], max_depth: int = 3) -> Path | None:
    candidates = ["".join(tail_words[i:]) for i in range(len(tail_words))]
    best: tuple[float, Path] | None = None
    frontier = [(root, 0)]
    while frontier:
        directory, depth = frontier.pop(0)
        for child in _child_dirs(directory):
            path = directory / child
            score = max(similarity(candidate, child) for candidate in candidates)
            if score >= 0.8 and (best is None or score > best[0]):
                best = (score, path)
            if depth + 1 < max_depth and not child.startswith("."):
                frontier.append((path, depth + 1))
    return best[1] if best else None
