"""Runs the UI's JavaScript unit tests (tests/ui/*.test.mjs) under Node's built-in test runner."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="Node.js is not installed")
def test_ui_javascript_units():
    files = sorted(str(p) for p in (ROOT / "tests" / "ui").glob("*.test.mjs"))
    assert files, "no UI tests found"
    completed = subprocess.run([NODE, "--test", *files], cwd=ROOT, capture_output=True, text=True, timeout=120,
                               encoding="utf-8", errors="replace")
    assert completed.returncode == 0, completed.stdout[-4000:] + completed.stderr[-2000:]
    assert "fail 0" in completed.stdout
