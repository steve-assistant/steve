"""The backend, as shipped, answers in the fake world, and keeps its secrets.

The first of the end-to-end files, and the floor the others stand on: a
supervisor started as the app starts it, on a fresh data folder, reaches
the fakes the world names and none of the real services, answers a
message over the socket the app's chat uses, and reads its keys from the
world's keychain (the run's canaries) rather than from any fixed value a
test could share.
"""

from __future__ import annotations

import pytest

from tests.e2e.backend import running
from tests.fakes.openai import FakeOpenAI
from tests.fakes.world import world

ANSWER = "Oslo, on the Oslofjord."


@pytest.fixture(scope="module")
def steve(fenced):
    with (world(fenced / "world", openai=FakeOpenAI(otherwise=ANSWER)) as made,
          running(made, fenced / "data") as backend):
        yield backend


async def test_a_message_on_the_apps_socket_is_answered_by_the_model_the_world_serves(steve):
    said = await steve.chat("What is the capital of Norway?")

    assert said["type"] == "reply", steve.logs()
    assert said["text"] == ANSWER
    # With the key the keychain held for this run, never a fixed one.
    asked = steve.world.openai.asked
    assert asked and {key for _, key in asked} == {steve.world.secrets["OPENAI_API_KEY"]}


def test_the_backend_runs_on_the_files_data_folder_and_names_its_port_there(steve):
    assert (steve.data / "run" / "backend.json").is_file()
    assert steve.get("/_sup/health").json()["ok"]
