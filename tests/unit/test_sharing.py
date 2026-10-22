"""Sharing a module: what an export holds, and what an import refuses (06-modules.md, section 6).

An export is the one way a module's code leaves the computer, and an import
the one way a stranger's zip is unpacked on it. The export must carry the
code and nothing the module keeps for its user, and none of the user's own
names or addresses left in the code. The import must write nothing outside
its folder, to no device, through no link, and no more than the module
caps allow, whatever the zip claims; and what it unpacks must hash as the
loader hashes it. The two-folder test, run in a real container, is in
tests/gate/test_shared_modules.py.
"""

from __future__ import annotations

import asyncio
import base64
import io
import random
import stat
import zipfile
from functools import partial

import pytest

from agent import facts, modadmit, modshare, runtime
from core import addons, db, lifecycle, modules, paths, sharing
from core.lifecycle import MAX_FILE, MAX_FILES, Refused
from tests import persona
from tests.unit.test_facts import Words
from tests.unit.test_lifecycle import MANIFEST, TEST, TOOL, App, Reviewer, Screen, Trial, _write
from tools import memory

ID = "webby"
PARTNER_EMAIL = "theo@marsh.example"


@pytest.fixture
def conn():
    conn = db.open_store(paths.db_path())
    yield conn
    conn.close()


def _install(conn, source) -> None:
    admission = asyncio.run(modadmit.admit(conn, source, origin="imported", screener=Screen(),
                                           reviewer=Reviewer(), tester=Trial()))
    lifecycle.approve(conn, admission)


def _zip(*entries: tuple[str | zipfile.ZipInfo, bytes],
         compression: int = zipfile.ZIP_DEFLATED) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression) as archive:
        for name, body in entries:
            flags = name.flag_bits if isinstance(name, zipfile.ZipInfo) else 0
            archive.writestr(name, body)
            archive.filelist[-1].flag_bits |= flags  # set after: writing clears them
    return out.getvalue()


def _module(*extra: tuple[str | zipfile.ZipInfo, bytes]) -> bytes:
    manifest = MANIFEST.format(id=ID, version="1", hosts='"api.webby.example"', reads="")
    return _zip(("manifest.toml", manifest.encode()), ("tool.py", TOOL.encode()),
                ("tests/test_tool.py", TEST.encode()), *extra)


def _info(name: str, *, mode: int = 0, flags: int = 0,
          compress: int = zipfile.ZIP_DEFLATED) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name)
    info.external_attr = mode << 16
    info.flag_bits |= flags
    info.compress_type = compress
    return info


def _noise(size: int) -> bytes:
    # Bytes that do not compress, so a cap is met before the ratio is.
    return random.Random(size).randbytes(size)


def test_an_exported_module_unpacks_to_the_files_approved_by_the_loaders_own_hash(
        conn, tmp_path):
    """What was approved is what loads: the zip holds the code folder
    alone, unpacked it hashes as the kept version does, and the same
    version exports to the same bytes."""
    _install(conn, _write(tmp_path / ID, reads=()))
    (paths.module_state_dir(ID) / "cursor.json").write_text('{"STATE-MARK": 1}',
                                                            encoding="utf-8")
    paths.module_runs_file(ID).write_text('{"RUNS-MARK": 1}', encoding="utf-8")
    copies = paths.module_copy_dir(ID, "0")
    copies.mkdir(parents=True)
    (copies / "cursor.json").write_text('{"COPY-MARK": 1}', encoding="utf-8")
    code = paths.module_code_dir(ID, "1")
    (code / "__pycache__").mkdir()
    (code / "__pycache__" / "tool.cpython-313.pyc").write_bytes(b"PYC-MARK")

    name, data = modshare.export_zip(conn, ID)

    assert name == "webby-1.zip"
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert sorted(archive.namelist()) == ["manifest.toml", "tests/test_tool.py", "tool.py"]
        unpacked = b"".join(archive.read(n) for n in archive.namelist())
    for mark in (b"STATE-MARK", b"RUNS-MARK", b"COPY-MARK", b"PYC-MARK"):
        assert mark not in data and mark not in unpacked
    sharing.extract(data, tmp_path / "out")
    assert addons.tree_hash(tmp_path / "out") == addons.tree_hash(code, skip=frozenset(
        {"__pycache__"}))
    assert modshare.export_zip(conn, ID)[1] == data


def test_an_export_that_names_the_user_or_someone_they_write_to_is_refused_saying_where(
        conn, tmp_path, monkeypatch):
    """A module built for one person can hold their name as a default or a
    friend's address in a test; once the zip has left, nothing calls it
    back. A name of one word is not a term: "may" is in every docstring."""
    monkeypatch.setattr(memory, "embed", Words())
    facts.add(conn, "user", "name", persona.NAME, source="confirmed")
    facts.add(conn, "user", "preferred_name", persona.FIRST, source="confirmed")
    conn.execute("INSERT INTO people (email, name) VALUES (?, ?)",
                 (PARTNER_EMAIL, persona.PARTNER))
    conn.commit()
    source = _write(tmp_path / ID, reads=())
    (source / "tool.py").write_text(TOOL + '\n\nOWNER = "MAYA_LINDQVIST"\n', encoding="utf-8",
                                    newline="\n")
    (source / "tests" / "test_tool.py").write_text(
        TEST + f'\n\nTO = "{PARTNER_EMAIL}"\n', encoding="utf-8", newline="\n")
    (source / "notes.md").write_text("Maya may like this; it may rain.\n", encoding="utf-8",
                                     newline="\n")
    _install(conn, source)

    with pytest.raises(Refused, match="names you or someone you know") as refused:
        modshare.export_zip(conn, ID)
    said = str(refused.value)
    assert "tool.py, line 5: maya lindqvist" in said
    assert "tests/test_tool.py, line 8: theo marsh example;" in said
    assert "line 8: theo marsh;" not in said
    assert "notes.md" not in said


def test_a_built_in_module_is_not_exported(conn):
    modules.register(conn)
    with pytest.raises(Refused, match="comes with Steve"):
        modshare.export_zip(conn, "countdown")


@pytest.mark.parametrize("entry, says", [
    (("../evil.py", b"x"), "climbs out"),
    (("tests/../../evil.py", b"x"), "climbs out"),
    (("/evil.py", b"x"), "absolute path"),
    (("C:/Users/evil.py", b"x"), "absolute path"),
    (("c:evil.py", b"x"), "absolute path"),
    (("tests\\..\\..\\evil.py", b"x"), "climbs out"),
    (("tool.py:hidden", b"x"), "Windows reads as something else"),
    (("tests/evil\x01.py", b"x"), "Windows reads as something else"),
    (("con.py", b"x"), "gives a device"),
    (("NUL", b"x"), "gives a device"),
    (("tests/aux.tar.gz", b"x"), "gives a device"),
    (("com1.txt", b"x"), "gives a device"),
    (("lpt\u00b9.py", b"x"), "gives a device"),
    (("con .py", b"x"), "gives a device"),
    (("evil.py.", b"x"), "a dot or a space"),
    (("evil.py ", b"x"), "a dot or a space"),
    (("tests//evil.py", b"x"), "empty part"),
    (("./evil.py", b"x"), "empty part"),
    (("x" * 201, b"x"), "over 200 characters"),
    (("TOOL.py", b"x"), "in it twice"),
    ((_info("link.py", mode=stat.S_IFLNK | 0o777), b"../../secrets"), "is a link"),
    ((_info("locked.py", flags=0x1), b"x"), "encrypted"),
    ((_info("odd.py", compress=zipfile.ZIP_BZIP2), b"x"), "packed in a way"),
], ids=lambda value: value if isinstance(value, str) else None)
def test_an_import_refuses_an_entry_that_would_land_anywhere_but_its_own_folder(
        tmp_path, entry, says):
    """Each way a zip's names reach past the folder or onto a device on
    Windows, a link, and a size the caps refuse: refused before anything
    is written, and the folder never made."""
    into = tmp_path / "quarantine"
    with pytest.raises(Refused, match=says):
        sharing.extract(_module(entry), into)
    assert not into.exists()


# Made in the test, not at collection: pytest would print each in full.
TOO_MUCH = {
    "not a zip": lambda: b"PK but not a zip",
    "the zip is over 5 MB": lambda: bytes(sharing.MAX_PACKED + 1),
    "over 512 KB": lambda: _module(("big.txt", _noise(MAX_FILE + 1))),
    "over 100 times its size": lambda: _module(("zeros.txt", bytes(400_000))),
    "more than 200 files": lambda: _zip(*((f"f{i}.txt", b"x") for i in range(MAX_FILES + 1))),
    "more than 200 files or 4 MB": lambda: _zip(
        *((f"f{i}.txt", _noise(500_000)) for i in range(9)), compression=zipfile.ZIP_STORED),
    "more than 400 entries": lambda: _zip(
        *((f"d{i}/", b"") for i in range(2 * MAX_FILES + 1))),
}


@pytest.mark.parametrize("says", TOO_MUCH)
def test_an_import_refuses_a_zip_too_large_or_too_many_before_unpacking_it(tmp_path, says):
    """A few kilobytes can unpack to gigabytes: the caps a module's folder
    is held to are checked on the zip before a byte is written."""
    into = tmp_path / "quarantine"
    with pytest.raises(Refused, match=says):
        sharing.extract(TOO_MUCH[says](), into)
    assert not into.exists()


def test_a_zip_that_understates_a_files_size_is_refused_as_its_bytes_are_read(tmp_path):
    """The sizes a zip states are a claim, and the caps and the ratio are
    checked on them: a file whose bytes run on past its stated size is
    stopped there and fails its checksum, so it is refused, never written
    in part."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.toml", b"manifest_version = 1\n")
        archive.writestr("tool.py", b"x = 1\n" * 1000)
        archive.filelist[-1].file_size = 6
    into = tmp_path / "quarantine"

    with pytest.raises(Refused, match="the zip is damaged"):
        sharing.extract(out.getvalue(), into)
    assert not into.exists()


def test_the_app_exports_a_module_and_imports_the_zip_up_to_a_fresh_card(
        conn, tmp_path, monkeypatch):
    """The routes carry the zip as base64, held to the extractor's caps;
    an import of one already installed is refused unless replacing."""
    monkeypatch.setattr(runtime, "conn", conn)
    monkeypatch.setattr(modadmit, "admit", partial(modadmit.admit, screener=Screen(),
                                                   reviewer=Reviewer(), tester=Trial()))
    app = App()
    _install(conn, _write(tmp_path / ID, reads=()))

    exported = app.call("POST", "/control/modules", json={"op": "export", "id": ID}).json()
    assert exported["ok"] and exported["name"] == "webby-1.zip"
    again = app.call("POST", "/control/modules", json={"op": "import", "zip": exported["zip"]})
    assert again.status_code == 400 and "installed already" in again.json()["error"]
    replaced = app.call("POST", "/control/modules", json={
        "op": "import", "zip": exported["zip"], "replace": True}).json()
    assert replaced["ok"] and isinstance(replaced["admission"], int)
    [card] = lifecycle.waiting(conn)
    assert (card["origin"], card["kind"]) == ("imported", "replace")
    assert card["details"]["hash"] == addons.tree_hash(paths.module_code_dir(ID, "1"))

    for body, says in (({"zip": "not base64!"}, "did not arrive whole"),
                       ({"zip": "AAAA" * (sharing.MAX_PACKED // 3 + 1)}, "over 5 MB"),
                       ({}, "choose a zip")):
        refused = app.call("POST", "/control/modules", json={"op": "import", **body})
        assert refused.status_code == 400 and says in refused.json()["error"], body
    assert base64.b64decode(exported["zip"])[:2] == b"PK"
    assert list(paths.scratch_dir().glob("import-*")) == []
