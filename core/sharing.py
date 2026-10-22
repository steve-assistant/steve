"""A module shared as a zip: exported alone, imported through a strict extractor.

Export. A module's code sits beside what it keeps for its user: its
`state/`, the copies a rollback restores, and its accounts' secrets in the
Credential Manager. A zip made from the module's whole folder would hand
the recipient the sender's sync cursors, cached mail, or worse. So export
reads one version's code folder alone (`paths.module_code_dir`), refuses a
link in it, and refuses a file that names the user or someone they know,
saying where: a module Steve built for one person can hold that person's
address as a default, and once the zip has left nothing calls it back.

Import. A zip is outside input. An entry named `../../x`, `C:/x` or `nul.py`
writes outside the folder it is unpacked into, or to a device; a link
points the loader anywhere; a few kilobytes can unpack to gigabytes. So
every entry is checked before the first is written, the bytes read are held
to the sizes the zip states, and each file is created afresh, never
written over. What comes out is a folder in quarantine,
which goes through admission as any module from outside does
(`agent/modadmit.admit`): staged, hashed with the loader's own function,
reviewed, tested, and approved afresh by its new user, with their own
secrets. No signatures in v0.1: an imported module is always untrusted,
and its card shows its hash.
"""

from __future__ import annotations

import io
import re
import shutil
import stat
import zipfile
from collections.abc import Callable, Iterable
from pathlib import Path, PurePosixPath

from core import lifecycle, paths
from core.lifecycle import MAX_FILE, MAX_FILES, MAX_TOTAL, Refused

# A zip is held to the caps admission holds a module's folder to, so one it
# would refuse is refused before it is unpacked; the zip itself may be a
# little larger than its files, stored.
MAX_PACKED = MAX_TOTAL + 1024 * 1024
MAX_ENTRIES = 2 * MAX_FILES  # its folders too
MAX_NAME = 200
# Code compresses three to ten times; a hundred is a bomb's.
MAX_RATIO = 100
# Fixed, so a version exported twice is the same zip.
WHEN = (1980, 1, 1, 0, 0, 0)

Finder = Callable[[str], list[tuple[int, str]]]

_WORD = re.compile(r"[a-z0-9]+")
# A name Windows gives a device, whatever follows its first dot: `nul.py`
# opens the null device, not a file. The superscript digits count too.
_DEVICES = frozenset({"con", "prn", "aux", "nul", "conin$", "conout$",
                      *(f"{kind}{n}" for kind in ("com", "lpt")
                        for n in "123456789\u00b9\u00b2\u00b3")})
# What Windows refuses in a name, or reads as something else: a colon names
# a drive or a file's hidden stream. A backslash never arrives: zipfile
# reads one as a slash on Windows, so `..\x` is refused as `../x`.
_UNSAFE = re.compile(r'[\x00-\x1f<>:"|?*]')
_DRIVE = re.compile(r"[A-Za-z]:")


def finder(terms: Iterable[str]) -> Finder:
    """A scan for `terms`: each matched by its words, without case,
    wherever they stand together, so "Maya Lindqvist", "maya.lindqvist" and
    "MAYA_LINDQVIST" are one, and an address is found in a link as in a
    string. A term of one word is left out: a name like "May" or "Will"
    would stop every module whose comments say may or will. What `find`
    returns is each hit's line and its words."""
    wanted = {words for term in terms if len(words := tuple(_WORD.findall(term.lower()))) > 1}
    longest = max(map(len, wanted), default=0)

    def find(text: str) -> list[tuple[int, str]]:
        spots = list(_WORD.finditer(text.lower()))
        said = [spot.group(0) for spot in spots]
        found = []
        for i in range(len(said)):
            # The longest term starting here: an address, not the name in it.
            for n in range(min(longest, len(said) - i), 0, -1):
                if tuple(said[i:i + n]) in wanted:
                    found.append((text.count("\n", 0, spots[i].start()) + 1,
                                  " ".join(said[i:i + n])))
                    break
        return found

    return find


def export(module_id: str, version: str, find: Finder) -> bytes:
    """One version of a module as a zip: its manifest, code and tests, and
    nothing beside them. Refused, saying where, at a file whose name or
    text `find` finds anything in."""
    folder = paths.module_code_dir(module_id, version)
    if not (folder / "manifest.toml").is_file():
        raise Refused(f"version {version} of {module_id} is not kept here")
    files = [name for name, info in lifecycle.walk(folder, PurePosixPath())
             if info is not None and "__pycache__" not in name.parts]
    found = []
    for name in files:
        text = (folder / name).read_text(encoding="utf-8", errors="replace")
        found += [f"{name}: {term}" for _, term in find(str(name))]
        found += [f"{name}, line {line}: {term}" for line, term in find(text)]
    if found:
        raise Refused("it names you or someone you know, so it stays here: "
                      + "; ".join(found[:10]))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        for name in files:
            info = zipfile.ZipInfo(str(name), WHEN)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            archive.writestr(info, (folder / name).read_bytes())
    return out.getvalue()


def extract(data: bytes, into: Path) -> None:
    """Unpack the zip in `data` into `into`, a folder not yet made; or
    Refused, saying what was wrong. Every entry is checked before the
    first is written."""
    if len(data) > MAX_PACKED:
        raise Refused(f"the zip is over {MAX_PACKED >> 20} MB")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, ValueError, EOFError):
        raise Refused("it is not a zip") from None
    with archive:
        entries = archive.infolist()
        _check(entries, len(data))
        into.mkdir(parents=True)
        try:
            _write(archive, entries, into)
        except (zipfile.BadZipFile, EOFError, OSError) as exc:
            shutil.rmtree(into, ignore_errors=True)
            raise Refused(f"the zip is damaged: {type(exc).__name__}") from None
        except BaseException:
            shutil.rmtree(into, ignore_errors=True)
            raise


def _write(archive: zipfile.ZipFile, entries: list[zipfile.ZipInfo], into: Path) -> None:
    root = into.resolve()
    for info in entries:
        path = into.joinpath(*PurePosixPath(info.filename).parts)
        # Each name was checked already; this is the folder's own word.
        if not path.resolve().is_relative_to(root):
            raise Refused(f"{info.filename!r} lands outside the folder")
        if info.is_dir():
            path.mkdir(parents=True, exist_ok=True)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        # zipfile stops a file at the size the zip states, which `_check`
        # held to the caps, and fails on its checksum there if the bytes
        # ran on: the sizes checked are the sizes written.
        with archive.open(info) as source:
            body = source.read()
        # Created, never opened over: a second entry by one name fails here.
        with path.open("xb") as out:
            out.write(body)


def _check(entries: list[zipfile.ZipInfo], packed: int) -> None:
    if len(entries) > MAX_ENTRIES:
        raise Refused(f"it has more than {MAX_ENTRIES} entries")
    seen: set[str] = set()
    files = total = 0
    for info in entries:
        name = _name(info.filename)
        # Windows does not tell `a.py` from `A.py`: the second would land on the first.
        if name.lower() in seen:
            raise Refused(f"{name!r} is in it twice")
        seen.add(name.lower())
        if stat.S_ISLNK(info.external_attr >> 16):
            raise Refused(f"{name!r} is a link, which could point anywhere")
        if info.flag_bits & 0x1:
            raise Refused(f"{name!r} is encrypted")
        if info.is_dir():
            continue
        if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            raise Refused(f"{name!r} is packed in a way Steve does not unpack")
        files += 1
        total += info.file_size
        if info.file_size > MAX_FILE:
            raise Refused(f"{name!r} is over {MAX_FILE // 1024} KB")
        if files > MAX_FILES or total > MAX_TOTAL:
            raise Refused(f"it unpacks to more than {MAX_FILES} files or {MAX_TOTAL >> 20} MB")
    if total > MAX_RATIO * packed:
        raise Refused(f"it unpacks to over {MAX_RATIO} times its size")


def _name(name: str) -> str:
    """An entry's name, if it is a plain relative path Windows writes as
    it reads; Refused otherwise."""
    bare = name[:-1] if name.endswith("/") else name
    shown = repr(bare[:MAX_NAME])
    if not bare or len(bare) > MAX_NAME:
        raise Refused(f"an entry's name is empty or over {MAX_NAME} characters")
    if bare.startswith("/") or _DRIVE.match(bare):
        raise Refused(f"{shown} is an absolute path")
    for part in bare.split("/"):
        if part == "..":
            raise Refused(f"{shown} climbs out of its folder with ..")
        if part in ("", "."):
            raise Refused(f"{shown} has an empty part")
        if _UNSAFE.search(part):
            raise Refused(f"{shown} has a character Windows reads as something else")
        if part.endswith((".", " ")):
            raise Refused(f"{shown} ends a part with a dot or a space, which Windows drops")
        if part.split(".", 1)[0].rstrip(" ").lower() in _DEVICES:
            raise Refused(f"{shown} is a name Windows gives a device")
    return bare
