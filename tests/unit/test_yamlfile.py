"""The frozen YAML files are parsed once per version on disk, and read as
they always were.

`load_policy` ran on every model request and the consent rules four to six
times per action, each a 10-19 ms pure-Python parse on the event loop
(5 October 2026). Caching what decides what may happen is only safe if an
edit is seen at once, a caller cannot change what the next one reads, and a
broken file still fails every time.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from core import yamlfile
from core.consent import load_rules
from core.policy import load_policy

CORE = Path(__file__).resolve().parents[2] / "core"


@pytest.mark.parametrize("name", ["policy.yaml", "consent.yaml"])
def test_the_fast_loader_reads_each_frozen_file_exactly_as_the_safe_one_did(name):
    text = (CORE / name).read_text(encoding="utf-8")

    assert yaml.load(text, Loader=yamlfile.LOADER) == yaml.safe_load(text)  # noqa: S506


def test_an_edit_is_read_at_once(tmp_path):
    rules = tmp_path / "rules.yaml"
    rules.write_text("send_email: ask\n", encoding="utf-8")
    assert yamlfile.load(rules) == {"send_email": "ask"}

    rules.write_text("send_email: free\n", encoding="utf-8")  # same size
    stamp = rules.stat().st_mtime_ns + 1_000_000
    os.utime(rules, ns=(stamp, stamp))

    assert yamlfile.load(rules) == {"send_email": "free"}


def test_what_one_caller_does_to_the_rules_does_not_change_them_for_the_next(tmp_path):
    rules = tmp_path / "rules.yaml"
    rules.write_text("gated:\n  send_email: ask\n", encoding="utf-8")

    yamlfile.load(rules)["gated"]["send_email"] = "free"

    assert yamlfile.load(rules) == {"gated": {"send_email": "ask"}}


def test_a_broken_file_fails_every_time_rather_than_once(tmp_path):
    rules = tmp_path / "rules.yaml"
    rules.write_text("gated: [unclosed\n", encoding="utf-8")

    for _ in range(2):
        with pytest.raises(yaml.YAMLError):
            yamlfile.load(rules)


def test_the_policy_and_the_rules_are_parsed_once_while_unchanged(monkeypatch):
    parsed: list[str] = []
    real = yaml.load

    def counting(stream, Loader):  # noqa: N803 - yaml's own name
        parsed.append(stream[:20])
        return real(stream, Loader=Loader)  # noqa: S506

    monkeypatch.setattr(yamlfile, "_parsed", {})
    monkeypatch.setattr(yamlfile.yaml, "load", counting)

    for _ in range(5):
        load_policy()
        load_rules()

    assert len(parsed) == 2
