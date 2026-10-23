"""A `.env` near Steve is offered to setup by its names, never its values.

The lookup's order, which names count, and what a file is read as
(agent/worker/envfile.py). The routes that use it are tested with setup's
others, in tests/unit/test_setup.py. Every folder here is the test's own:
a real `.env` on this computer is never read.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.worker import envfile
from core import paths


def _env(folder: Path, text: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    file = folder / ".env"
    file.write_text(text, encoding="utf-8", newline="\n")
    return file


@pytest.fixture
def near(tmp_path, monkeypatch):
    """Steve's own folder, `tmp_path/above/steve`, and the one above it."""
    own = tmp_path / "above" / "steve"
    own.mkdir(parents=True)
    monkeypatch.setattr(envfile, "own_folder", lambda: own)
    return own


def test_a_checkout_looks_in_its_own_root_and_an_install_in_its_install_folder(monkeypatch,
                                                                              tmp_path):
    """The folder a clone's README tells the user to put the file in is the
    one looked in; an install looks beside its own program, never in the
    data folder, which is Steve's to write."""
    monkeypatch.setattr(paths, "is_installed", lambda: False)
    assert envfile.folders() == [paths.SOURCE, paths.SOURCE.parent]
    monkeypatch.setattr(paths, "is_installed", lambda: True)
    monkeypatch.setattr(paths, "install_dir", lambda: tmp_path / "Programs" / "Steve")
    assert envfile.folders() == [tmp_path / "Programs" / "Steve", tmp_path / "Programs"]


def test_the_own_folder_is_looked_in_before_the_folder_above(near):
    """The file nearest Steve is the one meant for it; the one above is
    shared with other projects, so it comes second."""
    _env(near.parent, "OPENAI_API_KEY=above\n")
    assert [f["file"] for f in envfile.found()] == [str(near.parent / ".env")]
    _env(near, "OPENROUTER_API_KEY=own\n")
    found = envfile.found()
    assert [f["file"] for f in found] == [str(near / ".env"), str(near.parent / ".env")]
    assert found[0]["keys"] == ["OPENROUTER_API_KEY"] and found[1]["keys"] == ["OPENAI_API_KEY"]


def test_a_folder_named_env_or_a_file_too_big_is_not_read(near):
    """Only a plain file of a `.env`'s size is one; anything else is not
    read whole into memory."""
    (near / ".env").mkdir()
    _env(near.parent, "OPENAI_API_KEY=x\n" + "#" * envfile.MOST_BYTES)
    assert envfile.found() == []


def test_only_the_provider_keys_are_taken_and_any_other_name_is_not_shown(near):
    """Another program's key, or Steve's own prefixed name, is not Steve's
    to take; the authenticator's secret is made by setup, never handed in."""
    _env(near, "PORTKEY_API_KEY=pk\nSTEVE_OPENAI_API_KEY=s\nAPPROVAL_TOTP_SECRET=t\n"
               "TELEGRAM_BOT_TOKEN=b\nBRAVE_API_KEY=brave\n")
    assert envfile.found() == [{"file": str(near / ".env"), "keys": ["BRAVE_API_KEY"],
                                "empty": []}]
    held = envfile.values(str(near / ".env"))
    assert held == {"BRAVE_API_KEY": "brave"}  # pragma: allowlist secret


def test_a_file_naming_none_of_the_keys_is_not_offered(near):
    """A `.env` for some other project is not worth a question."""
    _env(near, "DATABASE_URL=postgres://x\n")
    assert envfile.found() == []


def test_empty_values_are_named_as_empty_and_have_no_value(near):
    """A copy of `.env.example` not yet filled in says so, and offers no
    empty key for checking."""
    _env(near, 'OPENAI_API_KEY=\nOPENROUTER_API_KEY=""\nBRAVE_API_KEY=b\n')
    assert envfile.found()[0]["empty"] == ["OPENAI_API_KEY", "OPENROUTER_API_KEY"]
    assert envfile.values(str(near / ".env")) == {"BRAVE_API_KEY": "b"}


def test_a_file_is_read_as_dotenv_files_are_written(near):
    """`export`, quotes, comments and a byte-order mark are the shapes
    such files take; a key read with its quotes would be refused."""
    file = near / ".env"
    lines = [chr(0xFEFF) + "# keys", 'export OPENAI_API_KEY="sk-a b"',  # pragma: allowlist secret
             "OPENROUTER_API_KEY='or' # quoted, then a comment",  # pragma: allowlist secret
             "BRAVE_API_KEY=bsa # mine", "BRAVE_API_KEY=bsa2"]  # pragma: allowlist secret
    file.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    assert envfile.values(str(file)) == {
        "OPENAI_API_KEY": "sk-a b",  # pragma: allowlist secret
        "OPENROUTER_API_KEY": "or",  # pragma: allowlist secret
        "BRAVE_API_KEY": "bsa2"}  # pragma: allowlist secret


def test_a_path_not_offered_is_never_read(near, tmp_path):
    """The app names the file to use; any other path it sends, a
    `.env` elsewhere or another file, is refused before it is opened."""
    elsewhere = _env(tmp_path / "elsewhere", "OPENAI_API_KEY=x\n")
    _env(near, "OPENAI_API_KEY=y\n")
    assert envfile.values(str(elsewhere)) is None
    assert envfile.values(str(near / "pyproject.toml")) is None
