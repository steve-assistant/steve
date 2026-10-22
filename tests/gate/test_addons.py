"""The programs Steve downloads run only as they were pinned.

`core/addons.py` installs signal-cli and its Java when the user chooses
Signal. signal-cli runs holding the user's Signal keys, so what it runs is
pinned by hash, frozen, and checked twice: the archive before anything is
unpacked, and the unpacked files before every start. What must hold:
- a download that is not the pinned archive is deleted, and nothing of it
  is installed;
- an archive that unpacks to other files, or outside its folder, installs
  nothing;
- a file changed, added, removed or renamed after the install is noticed,
  and an install over it puts back the pinned files;
- what is in place already is not downloaded again;
- the download stops at the pinned size;
- and no test downloads for real (tests/gate/fences.py).

Each test pins a small archive of its own, made here, in place of the
180 MB ones.
"""

from __future__ import annotations

import hashlib
import io
import tarfile
import zipfile
from dataclasses import replace
from pathlib import Path

import httpx2
import pytest

from core import addons, paths

REAL_DOWNLOAD = addons._download
FILES = {"lib/signal-cli.jar": b"a jar", "lib/libsignal.jar": b"another jar",
         "bin/run": b"a launcher"}
TOP = "tool-1.0"


def _zip(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zipped:
        for name, data in files.items():
            zipped.writestr(name, data)
    return buffer.getvalue()


def _tar(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tarred:
        for name, data in files.items():
            member = tarfile.TarInfo(name)
            member.size = len(data)
            tarred.addfile(member, io.BytesIO(data))
    return buffer.getvalue()


def _pinned(tmp_path: Path, archive: bytes, files: dict[str, bytes] = FILES) -> addons.Addon:
    """An add-on pinned to `archive`, whose files are `files` under TOP."""
    source = tmp_path / "pinned" / TOP
    for name, data in files.items():
        (source / name).parent.mkdir(parents=True, exist_ok=True)
        (source / name).write_bytes(data)
    return addons.Addon(name="tool", version="1.0", url="https://github.com/a/tool.zip",
                        size=len(archive), sha256=hashlib.sha256(archive).hexdigest(),
                        top=TOP, tree=addons.tree_hash(source), what="The tool")


def _serving(monkeypatch, archive: bytes) -> list[str]:
    """`_download` answering with `archive`; the URLs it was asked for."""
    asked: list[str] = []

    def download(url, to, *, most, progress=None):
        asked.append(url)
        to.write_bytes(archive)
        if progress is not None:
            progress(len(archive), most)
        return hashlib.sha256(archive).hexdigest()

    monkeypatch.setattr(addons, "_download", download)
    return asked


def _nothing_left_behind(addon: addons.Addon) -> None:
    assert not (paths.scratch_dir() / f"{addon.name}-{addon.version}.download").exists()
    assert not (paths.addons_dir() / f".{addon.name}-{addon.version}.partial").exists()


def _in_top(files: dict[str, bytes]) -> dict[str, bytes]:
    return {f"{TOP}/{name}": data for name, data in files.items()}


# ---------------------------------------------------------------------------
# Installed as pinned
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("pack", [_zip, _tar], ids=["zip", "tar.gz"])
def test_an_add_on_is_downloaded_checked_and_unpacked_into_its_home(tmp_path, monkeypatch,
                                                                    pack):
    archive = pack(_in_top(FILES))
    addon = _pinned(tmp_path, archive)
    _serving(monkeypatch, archive)
    told: list[tuple[int, int]] = []

    home = addons.install(addon, progress=lambda done, of: told.append((done, of)))

    assert home == paths.addons_dir() / "tool-1.0"
    assert {p.relative_to(home).as_posix(): p.read_bytes()
            for p in home.rglob("*") if p.is_file()} == FILES
    assert addons.verify(addon) is None
    assert told == [(len(archive), len(archive))]
    _nothing_left_behind(addon)


def test_what_is_in_place_already_is_not_downloaded_again(tmp_path, monkeypatch):
    """Asked at every "set up Signal": a second 180 MB download for files
    already checked would be minutes wasted."""
    archive = _zip(_in_top(FILES))
    addon = _pinned(tmp_path, archive)
    asked = _serving(monkeypatch, archive)

    addons.install(addon)
    addons.install(addon)

    assert asked == [addon.url]


def test_an_add_on_changed_since_it_was_installed_is_replaced_by_the_pinned_files(
        tmp_path, monkeypatch):
    archive = _zip(_in_top(FILES))
    addon = _pinned(tmp_path, archive)
    asked = _serving(monkeypatch, archive)
    home = addons.install(addon)
    (home / "lib" / "planted.jar").write_bytes(b"not ours")
    (home / "lib" / "signal-cli.jar").write_bytes(b"changed")

    addons.install(addon)

    assert asked == [addon.url, addon.url]
    assert not (home / "lib" / "planted.jar").exists()
    assert (home / "lib" / "signal-cli.jar").read_bytes() == b"a jar"
    assert addons.verify(addon) is None


# ---------------------------------------------------------------------------
# Refused
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("pin", [
    {"sha256": "0" * 64},
    {"size": 1},
], ids=["another-archive", "another-size"])
def test_a_download_that_is_not_the_pinned_archive_is_deleted_and_nothing_installed(
        tmp_path, monkeypatch, pin):
    """A mirror, a proxy or a replaced release could hand over anything.
    Nothing of it is unpacked, let alone run."""
    archive = _zip(_in_top(FILES))
    addon = replace(_pinned(tmp_path, archive), **pin)
    _serving(monkeypatch, archive)

    with pytest.raises(addons.AddonError, match="^The tool did not download as published"):
        addons.install(addon)

    assert not addons.home(addon).exists()
    _nothing_left_behind(addon)


def test_an_archive_that_unpacks_to_other_files_installs_nothing(tmp_path, monkeypatch):
    """The archive's hash is the publisher's; the files' hash is what was
    run on 13 October. Both are checked, so a pin edited in one place and
    not the other cannot install files no one ran."""
    archive = _zip(_in_top(FILES))
    addon = replace(_pinned(tmp_path, archive), tree="0" * 64)
    _serving(monkeypatch, archive)

    with pytest.raises(addons.AddonError, match="unpacked to files other than the pinned ones"):
        addons.install(addon)

    assert not addons.home(addon).exists()
    _nothing_left_behind(addon)


def test_an_archive_without_its_folder_installs_nothing(tmp_path, monkeypatch):
    archive = _zip(FILES)
    addon = _pinned(tmp_path, archive)
    _serving(monkeypatch, archive)

    with pytest.raises(addons.AddonError, match="archive did not hold tool-1.0$"):
        addons.install(addon)

    assert not addons.home(addon).exists()
    _nothing_left_behind(addon)


@pytest.mark.parametrize("pack", [_zip, _tar], ids=["zip", "tar.gz"])
def test_an_archive_naming_a_path_outside_its_folder_installs_nothing(tmp_path, monkeypatch,
                                                                      pack):
    """A pinned archive is known, so this is the second lock on the door:
    a path that climbs out of the folder would write beside the install."""
    archive = pack({**_in_top(FILES), "../escaped.txt": b"outside"})
    addon = _pinned(tmp_path, archive)
    _serving(monkeypatch, archive)

    with pytest.raises(addons.AddonError):
        addons.install(addon)

    assert not addons.home(addon).exists()
    assert not (paths.addons_dir() / "escaped.txt").exists()
    assert not (paths.addons_dir().parent / "escaped.txt").exists()
    _nothing_left_behind(addon)


def test_a_download_that_fails_says_why_and_leaves_nothing(tmp_path, monkeypatch):
    addon = _pinned(tmp_path, _zip(_in_top(FILES)))

    def download(url, to, *, most, progress=None):
        to.write_bytes(b"half an archive")
        raise httpx2.ConnectError("no route")

    monkeypatch.setattr(addons, "_download", download)

    with pytest.raises(addons.AddonError,
                       match="^The tool could not be installed: the server could not be reached$"):
        addons.install(addon)

    _nothing_left_behind(addon)


# ---------------------------------------------------------------------------
# Checked before every start
# ---------------------------------------------------------------------------

def _changed(home: Path, how: str) -> None:
    jar = home / "lib" / "signal-cli.jar"
    if how == "changed":
        jar.write_bytes(b"a jar, changed")
    elif how == "added":
        (home / "lib" / "planted.jar").write_bytes(b"not ours")
    elif how == "removed":
        jar.unlink()
    else:
        jar.rename(home / "lib" / "renamed.jar")


@pytest.mark.parametrize("how", ["changed", "added", "removed", "renamed"])
def test_a_file_changed_after_the_install_is_noticed_before_it_is_run(tmp_path, monkeypatch,
                                                                      how):
    """Any process of the user's can write to the add-ons' folder, and
    signal-cli runs holding the user's Signal keys."""
    archive = _zip(_in_top(FILES))
    addon = _pinned(tmp_path, archive)
    _serving(monkeypatch, archive)
    home = addons.install(addon)

    _changed(home, how)

    assert addons.verify(addon) == ("The tool has changed since it was installed, so it is"
                                    " not run; set it up again to replace it")


def test_an_add_on_not_installed_says_so(tmp_path):
    addon = _pinned(tmp_path, b"never downloaded")
    assert addons.verify(addon) == "The tool is not installed"


# ---------------------------------------------------------------------------
# The download itself
# ---------------------------------------------------------------------------

class _Response:
    def __init__(self, blocks: list[bytes]):
        self.blocks = blocks

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        return None

    def iter_bytes(self, size):
        yield from self.blocks


def _streaming(monkeypatch, blocks: list[bytes]) -> list[dict]:
    asked: list[dict] = []

    def stream(method, url, **kwargs):
        asked.append({"method": method, "url": url, **kwargs})
        return _Response(blocks)

    monkeypatch.setattr(httpx2, "stream", stream)
    return asked


def test_the_download_is_written_hashed_and_told_as_it_goes(tmp_path, monkeypatch):
    asked = _streaming(monkeypatch, [b"abc", b"def"])
    told: list[tuple[int, int]] = []
    to = tmp_path / "archive"

    got = REAL_DOWNLOAD("https://github.com/a/tool.zip", to, most=6,
                        progress=lambda done, of: told.append((done, of)))

    assert got == hashlib.sha256(b"abcdef").hexdigest() and to.read_bytes() == b"abcdef"
    assert told == [(3, 6), (6, 6)]
    # GitHub's release links redirect to its storage.
    assert asked[0]["follow_redirects"] is True and asked[0]["timeout"] == addons.TIMEOUT_S


def test_the_download_stops_past_the_pinned_size(tmp_path, monkeypatch):
    """A server that never stops sending would fill the disk before the
    hash could be checked."""
    _streaming(monkeypatch, [b"abc", b"def", b"ghi"])

    with pytest.raises(addons.AddonError, match="ran past its 5 bytes"):
        REAL_DOWNLOAD("https://github.com/a/tool.zip", tmp_path / "archive", most=5)


def test_no_test_downloads_for_real(tmp_path):
    with pytest.raises(RuntimeError, match="add-on downloads"):
        addons._download(addons.SIGNAL_CLI.url, tmp_path / "archive",
                         most=addons.SIGNAL_CLI.size)


# ---------------------------------------------------------------------------
# The pins
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("addon", addons.ALL, ids=lambda a: a.name)
def test_each_pin_is_a_whole_hash_of_an_https_release_on_github(addon):
    """A truncated hash, or a plain-http URL, would leave the check above
    weaker than it reads."""
    assert addon.url.startswith("https://github.com/") and "/releases/download/" in addon.url
    for digest in (addon.sha256, addon.tree):
        assert len(digest) == 64 and set(digest) <= set("0123456789abcdef")
    assert addon.size > 1_000_000 and addon.top
    assert addons.home(addon).name == f"{addon.name}-{addon.version}"
