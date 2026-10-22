"""A fresh install is set up through the API the app's setup screens use.

The setup screens are tested against a fake backend (tests/unit/ui/) and
the routes in the worker's own process (tests/unit/test_setup.py). Here
the backend is the shipped one, with an empty keychain: each page answered
in order, the keys checked against the world's services and kept in its
keychain, the cap and the preset written where the next start reads them,
and the passphrase kept nowhere as typed. Then the backend is stopped and
started again on the same folder, as a restart of the computer does, and
setup is done: the app opens on the day, not on setup's first page.
"""

from __future__ import annotations

import secrets as random

from core import paths
from tests.e2e.backend import Backend, running
from tests.fakes.world import world


def answered(steve: Backend, page: str, answer: str = "") -> dict:
    said = steve.post("/setup/answer", {"page": page, "answer": answer})
    assert said.status_code == 200, said.text
    return said.json()


def test_a_fresh_install_is_set_up_page_by_page_and_opens_on_the_day_after_a_restart(tmp_path):
    data = paths.data_dir()
    with world(tmp_path / "world", kept=False) as made:
        typed = f"lantern harbour {random.token_hex(4)}"
        made.keep("PASSPHRASE", typed)
        with running(made, data) as steve:
            state = steve.get("/setup/state").json()
            assert state["next"] == "hello" and not state["done"] and not state["key_kept"]
            answered(steve, "hello", "seen")

            # A key OpenAI refuses is refused with its reason, and not kept.
            wrong = steve.post("/setup/key", {"service": "openai",
                                              "key": "sk-proj-copied-with-a-letter-missing"})
            assert wrong.status_code == 400 and wrong.json()["why"] == "wrong-key"
            assert "OPENAI_API_KEY" not in made.kept()
            kept = steve.post("/setup/key", {"service": "openai",
                                             "key": made.secrets["OPENAI_API_KEY"]})
            assert kept.json()["says"] == "OpenAI accepted the key."
            assert made.kept()["OPENAI_API_KEY"] == made.secrets["OPENAI_API_KEY"]

            assert steve.post("/control/knobs", {"changes": {"MONTH_USD": 20}}).json()["ok"]
            jev = steve.post("/setup/key", {"service": "openrouter",
                                            "key": made.secrets["OPENROUTER_API_KEY"]})
            assert jev.json()["credit"] == 7.5
            assert steve.put("/control/profile",
                             {"profile": {"name": "Maya Lindqvist"}}).status_code == 200
            assert steve.post("/approvals/passphrase", {"new": typed}).json()["ok"]
            answered(steve, "channel", "skipped")
            answered(steve, "accounts", "skipped")
            answered(steve, "docker", "skipped")
            assert steve.post("/setup/preset", {"preset": "quiet"}).json()["preset"] == "quiet"
            # "off": the app writes the Run key, and a test never does.
            before = answered(steve, "login", "off")
            assert before["next"] == "privacy" and not before["done"]
            done = answered(steve, "privacy")
            assert done["done"] and done["next"] is None
            assert not steve.leaks()

        with running(made, data) as again:
            state = again.get("/setup/state").json()
            assert state["done"] and state["next"] is None and state["key_kept"]
            assert state["cap"] == 20 and state["preset"] == "quiet"
            assert again.get("/approvals/setup").json()["passphrase"]
            assert not again.leaks()
