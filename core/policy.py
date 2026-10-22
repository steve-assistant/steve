"""Frozen-core policy: what the agent may change, and proof that it didn't.

This module is itself frozen. It is imported by the supervisor, never by the
worker, so a compromised worker cannot monkey-patch the rules it is judged by.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from core import yamlfile

REPO_ROOT = Path(__file__).resolve().parent.parent
POLICY_PATH = REPO_ROOT / "core" / "policy.yaml"
LOCK_PATH = REPO_ROOT / "core" / "policy.lock"


@dataclass(frozen=True)
class BrowserConfig:
    """Whether a user may turn the sandboxed browser on: the ceiling over
    their switches in core/knobs.py. See the `browser` block in policy.yaml;
    off unless it says otherwise."""

    enabled: bool = False
    why: str = ""
    # Clean-room browsing workers for the user's background tasks
    # (agent/workers.py `Crew.start_browsing`): a worker with a browser kept
    # to the sites it was given, and nothing of the user's to read. Off
    # unless it says otherwise.
    background: bool = False


@dataclass(frozen=True)
class CriticConfig:
    """The action critic (core/critic.py): `shadow` reviews every unattended
    action and changes nothing; `enforce` makes anything it does not judge
    plainly part of their instruction a card. There is no `off`: the night
    cannot stop its own reviewer, only the user can, here."""

    mode: str = "shadow"
    # How sure it must be that an action serves their instruction to let it
    # go ahead without them, once enforcing.
    allow_at: float = 0.6


@dataclass(frozen=True)
class ModelsConfig:
    """The model scout's bounds: the `models` block in policy.yaml, checked
    by core/models_guard.py. With the block missing no role is switched
    automatically, so nothing is ever a candidate."""

    auto_switch: bool = False
    why: str = ""
    auto_roles: tuple[str, ...] = ()
    min_context: int = 200_000
    ceiling_usd_per_mtok: dict[str, float] = field(
        default_factory=lambda: {"input": 0.0, "output": 0.0})
    cheaper_by: float = 0.10
    noninferiority: float = 0.05
    better_by: float = 0.10
    significance: float = 0.05
    better_max_cost: float = 1.5
    min_turns: int = 30
    triage_agreement: float = 0.90
    latency: dict[str, dict] = field(default_factory=dict)
    candidate_budget_usd: float = 1.00
    month_budget_usd: float = 5.00
    # The day's watch after a switch. Missing, it is the strictest sensible
    # one rather than none.
    watch: dict[str, float] = field(default_factory=lambda: {
        "hours": 24, "baseline_hours": 48, "latency_rise": 1.5, "cost_rise": 2.0,
        "min_calls": 5})

    @classmethod
    def from_raw(cls, raw: dict | None) -> ModelsConfig:
        raw = dict(raw or {})
        raw["auto_roles"] = tuple(raw.get("auto_roles") or ())
        return cls(**raw)


@dataclass(frozen=True)
class SelfUpgradeConfig:
    """Whether the agent may begin changes to itself unprompted. See the
    `self_upgrade` block in policy.yaml."""

    agent_initiated: bool = True
    why: str = ""

    def refusal(self, origin: str) -> str | None:
        """Why an attempt from `origin` may not start, or None if it may.

        Only "user" -- the user asking in a conversation -- is exempt. Anything
        else, including an origin nobody anticipated, is the agent's own
        initiative, refused while it is off.
        """
        if origin == "user" or self.agent_initiated:
            return None
        return f"self-upgrade is paused: {self.why or 'see core/policy.yaml'}"


@dataclass(frozen=True)
class RoleConfig:
    """One role of the coding pipeline: which model, and how far it may go."""

    model: str
    budget_usd: float
    max_turns: int
    max_seconds: int


@dataclass(frozen=True)
class CoderConfig:
    """The `coder:` block in policy.yaml. Frozen, so the agent cannot pick
    its own judge or raise its own budget."""

    night_budget_usd: float = 2.00
    attempt_budget_usd: float = 0.50
    coder: RoleConfig = RoleConfig("gpt-6-luna", 0.25, 60, 1200)
    tester: RoleConfig = RoleConfig("gpt-6-luna", 0.10, 30, 1200)
    judge: RoleConfig = RoleConfig("gpt-6-luna-high", 0.15, 3, 300)

    @classmethod
    def from_raw(cls, raw: dict | None) -> CoderConfig:
        raw = dict(raw or {})
        roles = {name: RoleConfig(**raw.pop(name)) for name in ("coder", "tester", "judge")
                 if name in raw}
        return cls(**raw, **roles)


@dataclass(frozen=True)
class Policy:
    version: int
    frozen: list[str]
    self_upgrade: SelfUpgradeConfig = SelfUpgradeConfig()
    coder: CoderConfig = CoderConfig()
    browser: BrowserConfig = BrowserConfig()
    models: ModelsConfig = ModelsConfig()
    critic: CriticConfig = CriticConfig()

    def is_frozen(self, rel_path: str) -> bool:
        return _matches_any(rel_path, self.frozen)


def _normalise(rel_path: str) -> PurePosixPath:
    # NB: str.lstrip("./") strips *characters*, not a prefix, so it would eat
    # the leading dot of ".gitignore" and quietly drop dotfiles from the
    # frozen set. Remove the "./" prefix explicitly instead.
    s = str(rel_path).replace("\\", "/")
    while s.startswith("./"):
        s = s[2:]
    return PurePosixPath(s)


def _matches_any(rel_path: str, patterns: list[str]) -> bool:
    p = _normalise(rel_path)
    # PurePath.full_match (3.13+) implements '**' across path segments.
    return any(p.full_match(pat) for pat in patterns)


def load_policy(path: Path = POLICY_PATH) -> Policy:
    raw = yamlfile.load(path)  # parsed once per version on disk
    return Policy(
        version=raw["version"],
        frozen=list(raw.get("frozen", [])),
        self_upgrade=SelfUpgradeConfig(**(raw.get("self_upgrade") or {})),
        coder=CoderConfig.from_raw(raw.get("coder")),
        browser=BrowserConfig(**(raw.get("browser") or {})),
        models=ModelsConfig.from_raw(raw.get("models")),
        critic=CriticConfig(**(raw.get("critic") or {})),
    )


# --------------------------------------------------------------------------
# Manifest: SHA-256 of every frozen file, checked before any merge.
# --------------------------------------------------------------------------


PRUNE_DIRS = {".git", ".venv", "__pycache__", "node_modules", "data", "target", "dist"}


def _starts(patterns: list[str]) -> list[str] | None:
    """Where each pattern's files can be: its path up to the first wildcard.
    None if a pattern starts with one, so only the whole tree holds its files."""
    starts = []
    for pattern in patterns:
        literal = []
        for part in pattern.split("/"):
            if any(c in part for c in "*?["):
                break
            literal.append(part)
        if not literal:
            return None
        starts.append("/".join(literal))
    return starts


def _files_under(top: Path):
    if top.is_file():
        yield top
        return
    for dirpath, dirnames, filenames in os.walk(top):
        dirnames[:] = sorted(d for d in dirnames if d not in PRUNE_DIRS)
        for name in sorted(filenames):
            yield Path(dirpath) / name


def _iter_frozen_files(policy: Policy, root: Path) -> list[Path]:
    """The frozen files, found by walking only where the patterns point.

    verify_lock() runs on every supervisor status call. A walk of the whole
    tree, even pruning .venv and node_modules, also read a spike's 1.1 GB of
    bundled runtimes and any worktree a tool had left under .claude/: 24
    seconds a walk, for files no pattern could match.
    """
    # Which path to skip, because the lock cannot contain its own hash.
    # Computed defensively: the lock normally sits under REPO_ROOT, but the
    # tree being walked is usually a *worktree* somewhere else, and a caller
    # may point at a lock outside either -- which used to raise ValueError
    # from relative_to() rather than simply skipping the right file.
    try:
        lock_rel = LOCK_PATH.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        lock_rel = LOCK_PATH.name
    starts = _starts(policy.frozen)
    tops = [root] if starts is None else [root / s for s in sorted(set(starts))]
    out: set[Path] = set()
    for top in tops:
        for path in _files_under(top):
            rel = path.relative_to(root).as_posix()
            # The lock cannot contain its own hash: writing it would invalidate it.
            if rel == lock_rel:
                continue
            if policy.is_frozen(rel):
                out.add(path)
    return sorted(out)


def _sha256(path: Path) -> str:
    """Hash a file, normalising line endings for text.

    The manifest has to mean the same thing on every machine and checkout.
    Hashing raw bytes would make every CRLF/LF conversion look like
    tampering -- which trains you to regenerate the lock reflexively, exactly
    the habit this check exists to prevent. Binary files are hashed as-is.
    """
    data = path.read_bytes()
    if b"\x00" not in data:  # no NUL byte: treat it as text
        data = data.replace(b"\r\n", b"\n")
    h = hashlib.sha256()
    h.update(data)
    return h.hexdigest()


def build_manifest(policy: Policy, root: Path = REPO_ROOT) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): _sha256(p) for p in _iter_frozen_files(policy, root)}


def write_lock(
    policy: Policy, root: Path = REPO_ROOT, lock_path: Path = LOCK_PATH
) -> dict[str, str]:
    manifest = build_manifest(policy, root)
    body = "".join(f"{digest}  {rel}\n" for rel, digest in sorted(manifest.items()))
    header = (
        "# SHA-256 manifest of every frozen file.\n"
        "# Regenerate deliberately with `python -m core.policy --write-lock`.\n"
        "# The supervisor refuses to merge when this does not match reality.\n"
    )
    # LF, whatever the platform: checkouts are LF (.gitattributes), and a lock
    # written with CRLF on Windows differed from every other checkout.
    lock_path.write_text(header + body, encoding="utf-8", newline="\n")
    return manifest


def read_lock(lock_path: Path = LOCK_PATH) -> dict[str, str]:
    if not lock_path.exists():
        return {}
    out: dict[str, str] = {}
    for line in lock_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        digest, _, rel = line.partition("  ")
        out[rel.strip()] = digest.strip()
    return out


def verify_lock(policy: Policy, root: Path = REPO_ROOT, lock_path: Path = LOCK_PATH) -> list[str]:
    """Return a list of human-readable violations. Empty means clean."""
    expected = read_lock(lock_path)
    actual = build_manifest(policy, root)
    problems: list[str] = []
    for rel, digest in sorted(expected.items()):
        if rel not in actual:
            problems.append(f"frozen file deleted: {rel}")
        elif actual[rel] != digest:
            problems.append(f"frozen file modified: {rel}")
    for rel in sorted(set(actual) - set(expected)):
        problems.append(f"untracked frozen file added: {rel}")
    return problems


if __name__ == "__main__":
    import sys

    pol = load_policy()
    if "--write-lock" in sys.argv:
        m = write_lock(pol)
        print(f"wrote {LOCK_PATH} with {len(m)} entries")
    else:
        issues = verify_lock(pol)
        if issues:
            print("FROZEN CORE VIOLATIONS:")
            for i in issues:
                print(f"  - {i}")
            sys.exit(1)
        print("frozen core intact")
