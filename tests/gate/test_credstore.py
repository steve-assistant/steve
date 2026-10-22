"""Module secrets keep whole, keep apart, and go when their account goes.

The keyring library could not store a token over 2,560 bytes (Microsoft's
refresh tokens pass it), wrote UTF-16 that other tools misread, and named
entries so that one module's could be listed with another's
(05-connectors.md, section 1). `core/credstore.py` is the one place module
secrets touch the Credential Manager; these run it on `MemoryStore`, which
holds the Credential Manager's own size limit, so no test reaches the real one.
"""

from __future__ import annotations

import pytest

from core import credstore, identity


@pytest.fixture
def mem():
    store = credstore.MemoryStore()
    credstore.use(store)
    yield store
    credstore.use(None)


def test_a_secret_over_the_credential_managers_limit_is_split_and_read_back_whole(mem):
    name = credstore.target("mod1", "acct1", "refresh_token")
    token = "é" + "x" * (credstore.MAX_BLOB * 2 + 7)
    credstore.write(name, token)
    assert credstore.read(name) == token
    assert {t for t in mem.entries if t.startswith(name)} == {
        name, f"{name}#1", f"{name}#2", f"{name}#3"}
    assert all(len(b) <= credstore.MAX_BLOB for b in mem.entries.values())


def test_a_secret_is_stored_as_utf8_bytes(mem):
    """UTF-16 blobs, as keyring wrote them, read back as text with a NUL
    between letters in other tools."""
    name = credstore.target("mod1", "acct1", "password")
    credstore.write(name, "pässwörd")
    assert mem.entries[name] == "pässwörd".encode()


def test_a_shorter_secret_written_over_a_split_one_leaves_no_parts_behind(mem):
    name = credstore.target("mod1", "acct1", "token")
    credstore.write(name, "y" * (credstore.MAX_BLOB + 1))
    credstore.write(name, "short")
    assert credstore.read(name) == "short"
    assert [t for t in mem.entries if t.startswith(name)] == [name]


def test_a_split_secret_missing_a_part_reads_as_none_not_as_half(mem):
    name = credstore.target("mod1", "acct1", "token")
    credstore.write(name, "z" * (credstore.MAX_BLOB + 1))
    del mem.entries[f"{name}#2"]
    assert credstore.read(name) is None


def test_forgetting_an_account_deletes_its_secrets_and_no_one_elses(mem):
    """Account keys are hex; forgetting `ab12` must not take `ab123`'s."""
    for account, value in (("ab12", "mine"), ("ab123", "theirs")):
        credstore.write(credstore.target("mod1", account, "password"), value)
        credstore.write(credstore.target("mod1", account, "token"), "t" * 3000)
    assert credstore.forget(f"{credstore.PREFIX}/mod1/ab12") == 2
    assert credstore.read(credstore.target("mod1", "ab123", "password")) == "theirs"
    assert not [t for t in mem.entries if "/ab12/" in t]


def test_only_names_under_steves_module_prefix_can_be_written_or_forgotten(mem):
    """Core's own keys and the other install's must be out of a module
    secret's reach whatever name reaches here."""
    for bad in (identity.KEYCHAIN, "other-app/x", "steve-modulesX/a/b/c"):
        with pytest.raises(ValueError):
            credstore.write(bad, "v")
        with pytest.raises(ValueError):
            credstore.forget(bad)
    for part in ("", "a/b", "a#1", "*"):
        with pytest.raises(ValueError):
            credstore.target("mod1", "acct1", part)
    assert mem.entries == {}


class _Backend:
    """keyring's Windows backend, as far as where it keeps a write goes."""

    persist = "enterprise"


def test_the_keys_and_the_browsers_logins_are_written_to_stay_on_this_computer(monkeypatch):
    """keyring's Windows backend writes CRED_PERSIST_ENTERPRISE, which a
    domain profile roams to every computer the user signs in to; a module's
    secrets were already kept to this one."""
    from keyring.backends.Windows import WinVaultKeyring

    from core import secrets
    from core.browser import vault

    backend = _Backend()
    written: list[tuple[str, str, str]] = []
    monkeypatch.setattr(secrets.keyring, "get_keyring", lambda: backend)
    monkeypatch.setattr(secrets.keyring, "set_password",
                        lambda service, key, value: written.append((service, key, backend.persist)))

    secrets.set_secret("OPENAI_API_KEY", "sk-test")
    backend.persist = "enterprise"  # each write sets it for itself
    vault._Keyring().set("example.com", "a login")

    assert written == [(secrets.SERVICE, "OPENAI_API_KEY", "local machine"),
                       (vault.SERVICE, "example.com", "local machine")]
    assert hasattr(WinVaultKeyring, "persist"), "keyring no longer lets a write stay here"
