"""The saved logins' list and their sessions are replaced whole, never
rewritten where they are (core/browser/vault.py).

The list (`sites.json`) was rewritten in place. A read that met it torn --
another thread saving, or a crash mid-write -- found no valid JSON and took
it for no logins at all, and the next login saved wrote that back: every
other one gone from the list, with its password left orphaned in the
Credential Manager.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from core.browser import vault as vaults


class _Keychain(dict):
    def set(self, key, value):
        self[key] = value

    def delete(self, key):
        self.pop(key, None)


def _vault(tmp_path: Path) -> vaults.Vault:
    return vaults.Vault(root=tmp_path / "browser", keyring=_Keychain(),
                        protect=lambda b: b, unprotect=lambda b: b)


def test_saving_a_login_or_its_session_replaces_the_file_whole(tmp_path, monkeypatch):
    """Written beside, then put in place: a reader sees the old file or the
    new one, never part of one."""
    store = _vault(tmp_path)
    store.save_site("contoso", ["contoso.example"])
    replaced: list[str] = []
    real = os.replace

    def spy(src, dst):
        replaced.append(Path(dst).name)
        return real(src, dst)

    monkeypatch.setattr(os, "replace", spy)
    store.save_site("library", ["library.example"])
    store.save_state("library", {"cookies": []})

    assert replaced == ["sites.json", "library.bin"]
    assert [site.name for site in store.sites()] == ["contoso", "library"]
    assert store.load_state("library") == {"cookies": []}


def test_a_save_cut_short_leaves_every_login_saved_before(tmp_path, monkeypatch):
    """The disk failing mid-save fails the save, and the list is as it was."""
    store = _vault(tmp_path)
    store.save_site("contoso", ["contoso.example"])
    real = os.fsync

    def cut(fd):
        raise OSError("the disk went away")

    monkeypatch.setattr(os, "fsync", cut)
    with pytest.raises(OSError):
        store.save_site("library", ["library.example"])
    monkeypatch.setattr(os, "fsync", real)

    assert [site.name for site in store.sites()] == ["contoso"]
