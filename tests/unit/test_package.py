"""The installed tree's build (devtools/package.py) ships all of Steve and
nothing of this computer, and refuses a build that would not be the release.

The build itself downloads Python and takes minutes, so it is run by hand
(`python -m devtools.package build`, then `smoke` and `compare`), as step 8's
item 4 did. These pin the rules it runs on: what is copied, what is trimmed,
what a stranger's environment holds, and when it refuses.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from core import identity, paths
from devtools import package


class _Git:
    """git as the build asks it: a commit, and what `status` says."""

    def __init__(self, status: str = "") -> None:
        self.status = status
        self.asked: list[list[str]] = []

    def __call__(self, args, **kwargs):
        args = [str(a) for a in args]
        self.asked.append(args)
        if "rev-parse" in args:
            return subprocess.CompletedProcess(args, 0, "0" * 40 + "\n", "")
        if "status" in args:
            return subprocess.CompletedProcess(args, 0, self.status, "")
        raise AssertionError(f"the build went on to run {args}")


def test_a_checkout_with_changes_is_refused_unless_the_build_says_dirty(tmp_path):
    """A release must be a commit: edits nobody committed would ship
    unreviewed, under a version that does not name them."""
    with pytest.raises(package.BuildError, match="nobody committed"):
        package.build(tmp_path / "out", run=_Git(" M core/paths.py\n"), say=lambda _: None)


def test_a_version_that_drifted_refuses_the_build(tmp_path):
    """Five files carry the version; the installer, the updater and the
    worker would each report a different one."""
    checkout = tmp_path / "checkout"
    for rel in package.VERSIONED:
        (checkout / rel).parent.mkdir(parents=True, exist_ok=True)
        (checkout / rel).write_bytes((paths.SOURCE / rel).read_bytes())
    tauri = checkout / "ui" / "src-tauri" / "tauri.conf.json"
    conf = json.loads(tauri.read_text(encoding="utf-8"))
    conf["version"] = "9.9.9"
    tauri.write_text(json.dumps(conf), encoding="utf-8")

    with pytest.raises(package.BuildError, match=r"9\.9\.9 in ui/src-tauri/tauri\.conf\.json"):
        package.build(tmp_path / "out", checkout=checkout, run=_Git(), say=lambda _: None)


def test_this_checkouts_versions_agree_so_it_can_be_built():
    assert set(package.versions(paths.SOURCE).values()) == {identity.VERSION}


def test_every_package_setuptools_would_ship_is_copied():
    """A new top-level package left off the list would be missing from every
    install, and found only by a stranger's first import of it."""
    found = tomllib.loads((paths.SOURCE / "pyproject.toml").read_text(encoding="utf-8"))
    tops = {name.rstrip("*") for name in found["tool"]["setuptools"]["packages"]["find"]["include"]}
    assert tops | {"db"} == set(package.PACKAGES)


def _checkout_paths(source: Path) -> list[str]:
    """Each path `source` builds from the checkout's root by `/`, as text:
    `ROOT / "tests" / "x.json"` is "tests/x.json"."""
    def parts(node) -> list[str | None]:
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            return parts(node.left) + parts(node.right)
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return [node.value]
        return [ast.unparse(node)]

    found, inner = [], set()
    for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
        if not (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)):
            continue
        inner.update(id(c) for c in (node.left, node.right) if isinstance(c, ast.BinOp))
        if id(node) in inner:
            continue
        base, *rest = parts(node)
        if re.search(r"(ROOT|SOURCE|\.parents\[1\])$", base or "") and rest and all(rest):
            found.append("/".join(rest))
    return found


# Read by a developer's command alone, never by an install.
CHECKOUT_ONLY = {("voice/wake_train.py", "docs")}


def test_every_checkout_file_the_code_reads_by_path_ships_with_it():
    """The night's model trials and weekly yardstick read the gate's fixtures
    by path from the checkout's root, which an install does not have: they
    failed there on a missing file until 21 October 2026."""
    outside = {"tests", "docs", "devtools", "ui", "scripts", "private"}
    missing = set()
    for top in package.PACKAGES:
        for source in sorted((paths.SOURCE / top).rglob("*.py")):
            rel = source.relative_to(paths.SOURCE).as_posix()
            for path in _checkout_paths(source):
                if path.split("/")[0] in outside and path not in package.FILES:
                    missing.add((rel, path))
    assert missing == CHECKOUT_ONLY


def test_each_file_of_the_code_is_copied_and_nothing_git_ignores(tmp_path, monkeypatch):
    """A test's leftovers or a `__pycache__` in the checkout must not ship; a
    new file a developer's build includes must."""
    checkout = tmp_path / "checkout"
    (checkout / "core" / "__pycache__").mkdir(parents=True)
    (checkout / "core" / "kept.py").write_text("x = 1\n", encoding="utf-8")
    (checkout / "core" / "prompt.md").write_text("words\n", encoding="utf-8")
    (checkout / "core" / "__pycache__" / "kept.cpython-313.pyc").write_bytes(b"\0")
    (checkout / "core" / "new.py").write_text("y = 2\n", encoding="utf-8")
    (checkout / "tests").mkdir()
    (checkout / "tests" / "test_x.py").write_text("", encoding="utf-8")
    (checkout / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
    git = ["git", "-C", str(checkout)]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", ".gitignore", "core/kept.py", "core/prompt.md", "tests"],
                   check=True)
    monkeypatch.setattr(package, "PACKAGES", ("core",))
    monkeypatch.setattr(package, "FILES", ())

    assert package.copy_code(checkout, tmp_path / "app") == 3
    assert sorted(p.relative_to(tmp_path / "app").as_posix()
                  for p in (tmp_path / "app").rglob("*") if p.is_file()) == [
        "core/kept.py", "core/new.py", "core/prompt.md"]


def test_the_code_is_found_from_wherever_the_folder_is_moved(tmp_path):
    """The `.pth` is relative: an absolute one would name the build's folder,
    which is not where a stranger's install is."""
    built = tmp_path / "build" / "backend"
    (built / "Lib" / "site-packages").mkdir(parents=True)
    (built / "app" / "steve_probe_pth").mkdir(parents=True)
    package.write_pth(built)
    backend = tmp_path / "Stévé moved" / "backend"
    backend.parent.mkdir()
    built.rename(backend)

    shown = subprocess.run([sys.executable, "-I", "-c",
                            "import site, sys; site.addsitedir(sys.argv[1]);"
                            " import steve_probe_pth; print(steve_probe_pth.__path__[0])",
                            str(backend / "Lib" / "site-packages")],
                           capture_output=True, text=True, check=True)
    assert Path(shown.stdout.strip()) == backend / "app" / "steve_probe_pth"


def test_trimming_takes_what_a_backend_never_runs_and_the_record_lines_naming_it(tmp_path):
    """Launchers and the RECORD lines for them and for uv's own files name
    the build's folder and time: two builds of one commit differed by 27
    files until they went (spike S2)."""
    backend = tmp_path / "backend"
    site = backend / "Lib" / "site-packages"
    dist = site / "thing-1.0.dist-info"
    documents = backend / package.DISCOVERY
    for folder in (backend / "Scripts", backend / "tcl", backend / "Lib" / "idlelib", dist,
                   documents, backend / "DLLs"):
        folder.mkdir(parents=True, exist_ok=True)
    (backend / "Scripts" / "thing.exe").write_bytes(b"MZ")
    (backend / "python313.pdb").write_bytes(b"\0")
    (backend / "DLLs" / "_testcapi.pyd").write_bytes(b"\0")
    (backend / "DLLs" / "_ssl.pyd").write_bytes(b"\0")
    (dist / "direct_url.json").write_text("{}", encoding="utf-8")
    (dist / "uv_cache.json").write_text("{}", encoding="utf-8")
    (dist / "RECORD").write_text(
        "thing/__init__.py,sha256=x,1\n../../Scripts/thing.exe,sha256=y,2\n"
        "thing-1.0.dist-info/direct_url.json,sha256=z,3\n"
        "thing-1.0.dist-info/uv_cache.json,sha256=w,4\n", encoding="utf-8", newline="\n")
    for api in (*package.GOOGLE_APIS, "youtube.v3", "admin.directory_v1"):
        (documents / f"{api}.json").write_text("{}", encoding="utf-8")

    package.trim(backend)

    left = sorted(p.relative_to(backend).as_posix() for p in backend.rglob("*") if p.is_file())
    assert left == sorted([
        "DLLs/_ssl.pyd", "Lib/site-packages/thing-1.0.dist-info/RECORD",
        *(f"{package.DISCOVERY.as_posix()}/{api}.json" for api in package.GOOGLE_APIS)])
    assert (dist / "RECORD").read_text(encoding="utf-8") == "thing/__init__.py,sha256=x,1\n"


def test_pytests_folder_keeps_no_launcher_and_no_record_line_naming_one(tmp_path):
    """The launchers uv writes beside a `--target` install name the folder
    it was built in: two builds of one commit differed by three of them and
    the two RECORD files listing them."""
    dist = tmp_path / "pytest-9.0.dist-info"
    for folder in (tmp_path / "bin", tmp_path / "pytest", dist):
        folder.mkdir()
    (tmp_path / "bin" / "pytest.exe").write_bytes(b"MZ")
    (tmp_path / "pytest" / "__init__.py").write_text("", encoding="utf-8")
    (dist / "RECORD").write_text("pytest/__init__.py,sha256=x,0\nbin/pytest.exe,sha256=y,2\n",
                                 encoding="utf-8", newline="\n")

    package.trim_testing(tmp_path)

    assert not (tmp_path / "bin").exists()
    assert (tmp_path / "pytest" / "__init__.py").is_file()
    assert (dist / "RECORD").read_text(encoding="utf-8") == "pytest/__init__.py,sha256=x,0\n"


def test_the_google_apis_kept_are_the_ones_the_module_builds():
    """A discovery document trimmed away is a Google module that fails on a
    stranger's machine, and only there: a checkout has all six hundred."""
    code = "".join(p.read_text(encoding="utf-8")
                   for p in (paths.SOURCE / "modules" / "google_cloud").glob("*.py"))
    built = {f"{api}.{version}" for api, version
             in re.findall(r'service\("(\w+)", "(v\w+)"\)', code)}
    assert built == set(package.GOOGLE_APIS)


def test_bytecode_is_written_beside_each_source_and_a_file_that_does_not_compile_fails(
        tmp_path):
    """A source that does not compile would be a module that fails only on
    a stranger's first import of it. The first real build found the runner
    refusing compileall's own failure as a `TypeError` instead."""
    (tmp_path / "fine.py").write_text("x = 1\n", encoding="utf-8")
    package.compile_tree(Path(sys.executable), tmp_path)
    assert list((tmp_path / "__pycache__").glob("fine.*.pyc"))

    (tmp_path / "broken.py").write_text("def (:\n", encoding="utf-8")
    with pytest.raises(package.BuildError, match="broken.py"):
        package.compile_tree(Path(sys.executable), tmp_path)


def test_bytecode_is_compiled_in_one_process_with_a_fixed_seed_and_no_developer_python(
        tmp_path, monkeypatch):
    """Spread over three processes, the same source compiled to three
    different files, so two builds of one commit did not match."""
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    asked = []

    def compiles(args, **kwargs):
        asked.append(([str(a) for a in args], kwargs["env"]))
        return subprocess.CompletedProcess(args, 0, "", "")

    package.compile_tree(Path(sys.executable), tmp_path, compiles)

    [(args, env)] = asked
    assert not {"-j", "-I", "-E"} & set(args)
    assert env["PYTHONHASHSEED"] == "0"
    assert "PYTHONPATH" not in env


def test_the_export_is_read_package_by_package_with_its_hashes():
    exported = (
        "# This file was autogenerated by uv\n"
        "colorama==0.4.6 ; sys_platform == 'win32' \\\n"
        "    --hash=sha256:aa \\\n"
        "    --hash=sha256:bb\n"
        "    # via pytest\n"
        "Pygments==2.19.2 \\\n"
        "    --hash=sha256:cc\n")

    blocks = package._requirement_blocks(exported)

    assert blocks == {
        "colorama": "colorama==0.4.6 ; sys_platform == 'win32' \\\n    --hash=sha256:aa \\\n"
                    "    --hash=sha256:bb",
        "pygments": "Pygments==2.19.2 \\\n    --hash=sha256:cc"}


def test_the_smoke_runs_with_nothing_a_user_or_a_developer_set(tmp_path, monkeypatch):
    """A developer's PYTHONPATH, keys or STEVE_* would let a build that
    cannot start on a stranger's machine pass here."""
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    monkeypatch.setenv("STEVE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("PATH", str(tmp_path) + ";" + str(tmp_path / "more"))

    env = package.bare_env(tmp_path / "Local", tmp_path / "temp")

    assert not {"PYTHONPATH", "STEVE_DATA_DIR", "OPENAI_API_KEY"} & set(env)
    assert env["PATH"].lower().endswith("system32")
    assert env["LOCALAPPDATA"] == str(tmp_path / "Local")


def test_two_builds_are_compared_file_by_file(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    for root in (a, b):
        (root / "sub").mkdir(parents=True)
        (root / "same.txt").write_text("1", encoding="utf-8")
    (a / "sub" / "differs.txt").write_text("a", encoding="utf-8")
    (b / "sub" / "differs.txt").write_text("b", encoding="utf-8")
    (a / "only-a.txt").write_text("a", encoding="utf-8")

    assert package.compare(a, b) == ["only-a.txt", "sub/differs.txt"]


FIXTURES = paths.SOURCE / "ui" / "src-tauri" / "fixtures" / "integrity"


def test_the_list_names_every_file_of_the_tree_with_its_hash_and_the_version(tmp_path):
    """The app hashes what the list names and refuses the backend on any
    difference: a file left out is never checked, and one named by another
    path is reported missing on every start."""
    staging = tmp_path / "staging"
    shipped = {"backend/python.exe": b"MZ", "backend/app/core/x.py": b"x = 1\n",
               "runtime/node/node.exe": b"MZ2"}
    for rel, data in shipped.items():
        (staging / rel).parent.mkdir(parents=True, exist_ok=True)
        (staging / rel).write_bytes(data)
    (staging / "notes.txt").write_text("not shipped", encoding="utf-8")

    path = package.write_manifest(staging)

    listed = json.loads(path.read_text(encoding="utf-8"))
    assert listed == {"version": identity.VERSION,
                      "files": {rel: hashlib.sha256(data).hexdigest()
                                for rel, data in shipped.items()}}
    assert b"\r" not in path.read_bytes()


def test_a_tree_not_yet_built_is_refused_rather_than_listed(tmp_path):
    (tmp_path / "staging").mkdir()

    with pytest.raises(package.BuildError, match="build it first"):
        package.write_manifest(tmp_path / "staging")


class _Signer:
    """Tauri's signer: writes the signature it was given beside the file."""

    def __init__(self, signature: str, returncode: int = 0, stderr: str = ""):
        self.signature, self.returncode, self.stderr = signature, returncode, stderr
        self.asked: list[tuple[list[str], dict]] = []

    def __call__(self, args, **kwargs):
        self.asked.append(([str(a) for a in args], kwargs))
        if self.returncode == 0:
            target = Path(args[-1])
            target.with_name(target.name + ".sig").write_text(self.signature, encoding="ascii")
        return subprocess.CompletedProcess(args, self.returncode, "", self.stderr)


def _checkout(tmp_path: Path, pubkey: str) -> Path:
    checkout = tmp_path / "checkout"
    (checkout / "ui" / "node_modules" / ".bin").mkdir(parents=True)
    (checkout / "ui" / "node_modules" / ".bin" / "tauri.cmd").write_text("", encoding="utf-8")
    (checkout / "ui" / "src-tauri").mkdir()
    (checkout / "ui" / "src-tauri" / "tauri.conf.json").write_text(
        json.dumps({"plugins": {"updater": {"pubkey": pubkey}}}), encoding="utf-8")
    return checkout


def _signed(tmp_path: Path, signer: _Signer, trusted: str) -> Path:
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}\n", encoding="utf-8")
    return package.sign(manifest, tmp_path / "update.key", checkout=_checkout(tmp_path, trusted),
                        run=signer)


def test_the_list_is_signed_with_the_key_named_and_the_signer_can_never_prompt(
        tmp_path, monkeypatch):
    """A signer waiting for a password nobody can type would hang the build."""
    monkeypatch.delenv("TAURI_SIGNING_PRIVATE_KEY_PASSWORD", raising=False)
    signer = _Signer((FIXTURES / "manifest.json.sig").read_text(encoding="ascii"))

    signature = _signed(tmp_path, signer, (FIXTURES / "fixture.pub").read_text(encoding="ascii"))

    [(args, kwargs)] = signer.asked
    assert args[1:] == ["signer", "sign", "--private-key-path", str(tmp_path / "update.key"),
                        "--password", "", str(tmp_path / "manifest.json")]
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert signature == tmp_path / "manifest.json.sig"


def test_a_keys_password_comes_from_the_environment_and_never_the_command_line(
        tmp_path, monkeypatch):
    """Any process of the user can read another's command line."""
    monkeypatch.setenv("TAURI_SIGNING_PRIVATE_KEY_PASSWORD", "correct horse")
    signer = _Signer((FIXTURES / "manifest.json.sig").read_text(encoding="ascii"))

    _signed(tmp_path, signer, (FIXTURES / "fixture.pub").read_text(encoding="ascii"))

    [(args, _)] = signer.asked
    assert "--password" not in args
    assert "correct horse" not in args


def test_a_signature_by_a_key_the_app_does_not_trust_is_refused_and_removed(tmp_path):
    """Shipped, it would stop the backend on every user's computer, for files
    nobody changed."""
    signer = _Signer((FIXTURES / "manifest.json.sig").read_text(encoding="ascii"))

    with pytest.raises(package.BuildError, match="not the update key the app trusts"):
        _signed(tmp_path, signer, package.trusted_key(paths.SOURCE))

    assert not (tmp_path / "manifest.json.sig").exists()


def test_a_build_given_a_key_of_its_own_is_checked_against_that_key_not_the_checkouts(tmp_path):
    """CI's installer job signs with a key made for its run and builds the
    app to trust it; checked against the release key, every such build would
    be refused, and checked against neither, a wrong key would ship."""
    signer = _Signer((FIXTURES / "manifest.json.sig").read_text(encoding="ascii"))
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}\n", encoding="utf-8")
    checkout = _checkout(tmp_path, package.trusted_key(paths.SOURCE))

    signature = package.sign(manifest, tmp_path / "update.key", checkout=checkout, run=signer,
                             trusted=(FIXTURES / "fixture.pub").read_text(encoding="ascii"))
    assert signature.is_file()

    with pytest.raises(package.BuildError, match="the --pubkey given"):
        package.sign(manifest, tmp_path / "update.key", checkout=checkout, run=signer,
                     trusted=package.trusted_key(paths.SOURCE))


def test_the_manifest_command_trusts_the_public_key_file_it_is_given(tmp_path, monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(package, "write_manifest", lambda staging: tmp_path / "manifest.json")
    monkeypatch.setattr(package, "sign",
                        lambda listed, key, **kwargs: seen.update(kwargs) or listed)
    pub = tmp_path / "update.key.pub"
    pub.write_text("cHVibGlj\n", encoding="ascii")

    assert package.main(["manifest", "--key", str(tmp_path / "update.key"),
                         "--pubkey", str(pub), "--out", str(tmp_path)]) == 0
    assert seen == {"trusted": "cHVibGlj"}


def test_an_app_that_names_the_builders_folder_in_any_form_is_caught(tmp_path):
    """A release build named the folder it was built in, and with it the
    builder's Windows account (the step 11 audit, F7). The compiler writes a
    path with either slash, doubled in a debug string, or as UTF-16, so the
    scan looks for each."""
    profile = r"C:\Users\Builder"
    forms = [
        rb"C:\Users\Builder\.cargo\registry\src\tauri\src\app.rs",
        b"/users/builder/.cargo/registry",
        rb"C:\\Users\\Builder\\Documents\\steve",
        "C:\\Users\\Builder\\Documents\\steve".encode("utf-16-le"),
    ]
    for i, form in enumerate(forms):
        app = tmp_path / f"named-{i}.exe"
        app.write_bytes(b"MZ\0\0" + form + b"\0")
        assert package.scan(app, [profile]) == [profile], form

    clean = tmp_path / "clean.exe"
    clean.write_bytes(b"MZ\0\0~\\.cargo\\registry\0" + r"C:\Users\Other".encode("utf-16-le"))
    assert package.scan(clean, [profile]) == []


def test_the_scan_names_each_folder_it_finds_and_passes_a_clean_app(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("USERPROFILE", r"C:\Users\Builder")
    named, clean = tmp_path / "named.exe", tmp_path / "clean.exe"
    named.write_bytes(rb"MZ C:\Users\Builder\.cargo " + str(paths.SOURCE).encode("utf-16-le"))
    clean.write_bytes(rb"MZ ~\.cargo\registry")

    assert package.main(["scan", str(named)]) == 1
    said = capsys.readouterr().out
    assert r"C:\Users\Builder" in said and str(paths.SOURCE) in said
    assert package.main(["scan", str(clean)]) == 0


def test_a_signer_that_fails_says_why(tmp_path):
    signer = _Signer("", returncode=1, stderr="failed to decode secret key: incorrect password")

    with pytest.raises(package.BuildError, match="incorrect password"):
        _signed(tmp_path, signer, (FIXTURES / "fixture.pub").read_text(encoding="ascii"))
