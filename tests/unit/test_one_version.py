"""Steve has one version (step 8, item 2).

It was written in five places, and they had drifted: pyproject.toml said
0.1.0.dev0 where the rest said 0.1.0. The updater refuses an older version,
and the installer a downgrade, so each must read the same number as the
worker reports (core/identity.VERSION, which agent/version.py passes on).
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

from agent.version import STEVE_VERSION

REPO = Path(__file__).resolve().parents[2]


def test_pyproject_says_the_workers_version():
    project = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["version"] == STEVE_VERSION


def test_the_apps_three_files_say_the_workers_version():
    """tauri.conf.json is what the installer and the updater compare."""
    ui = REPO / "ui"
    tauri = json.loads((ui / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8"))
    cargo = tomllib.loads((ui / "src-tauri" / "Cargo.toml").read_text(encoding="utf-8"))
    package = json.loads((ui / "package.json").read_text(encoding="utf-8"))

    assert tauri["version"] == STEVE_VERSION
    assert cargo["package"]["version"] == STEVE_VERSION
    assert package["version"] == STEVE_VERSION


def test_the_lock_records_the_same_version():
    """uv.lock is what the build installs from; a stale one means a relock was missed."""
    lock = tomllib.loads((REPO / "uv.lock").read_text(encoding="utf-8"))
    steve = next(p for p in lock["package"] if p["name"] == "steve")
    assert steve["version"] == STEVE_VERSION
