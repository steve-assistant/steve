"""The fake Bot API, served over HTTP, carries the channel as Telegram does.

A worker the end-to-end tests start reaches Telegram at the address
`STEVE_TELEGRAM_URL` names (core/endpoints.py), over a socket; in a test's
own process the fences let the channel's client through to such a fake on
this computer and refuse the real service (tests/gate/fences.py). So the
channel is built here as the worker builds it, with no transport of the
test's, and driven through pairing, a photo and a reply: a fake whose paths
drifted from the client's would fail every Telegram scenario far from the
cause.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from channels import telegram
from channels.base import Outgoing
from core import endpoints
from tests.fakes.telegram import TOKEN, USER, FakeBotApi

JPEG = b"\xff\xd8\xff\xe0" + b"\0" * 64


async def _listen(channel: telegram.TelegramChannel, fake: FakeBotApi) -> list:
    got = []

    async def take():
        async for message in channel.receive():
            got.append(message)

    task = asyncio.create_task(take())
    try:
        await asyncio.wait_for(fake.caught_up(), 15)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    return got


async def test_served_the_fake_pairs_a_chat_hears_a_photo_and_takes_the_reply(monkeypatch):
    fake = FakeBotApi()
    with fake.running() as url:
        monkeypatch.setenv("STEVE_TELEGRAM_URL", url)
        assert endpoints.telegram() == url
        channel = telegram.TelegramChannel(TOKEN, retry_s=0.01)
        await channel.start()
        try:
            channel.offer("123456")
            fake.says("123456")
            await _listen(channel, fake)
            fake.files["files/big"] = JPEG
            fake.says(caption="what is this?", photo=[{"file_id": "big", "file_size": 60_000}])
            [photo] = await _listen(channel, fake)
            assert await channel.send(Outgoing(text="A receipt."))
        finally:
            await channel.stop()

    assert channel.chat_id == USER
    assert photo.text == "what is this?" and [p.read_bytes() for p in photo.images] == [JPEG]
    assert fake.sent()[-1]["text"] == "A receipt."


async def test_a_poll_with_nothing_to_give_waits_rather_than_answering_at_once():
    """Over HTTP a worker polls in a loop: a fake that answered an empty
    poll at once would have it spin, a core's worth of CPU for the length
    of every test."""
    fake = FakeBotApi()
    with fake.running():
        loop = asyncio.get_running_loop()
        started = loop.time()
        status, body = await fake.answer(f"/bot{TOKEN}/getUpdates", b'{"offset": 0}')
        waited = loop.time() - started

    assert (status, body["result"]) == (200, [])
    assert waited >= 0.9


def test_without_a_fake_named_the_bot_api_is_still_refused():
    """The fence's door opens only to an address on this computer."""
    assert endpoints.telegram() == endpoints.TELEGRAM
    with pytest.raises(RuntimeError, match="tried to reach live Telegram's Bot API"):
        telegram.BotApi(TOKEN)
