"""Setting up the remote channel from the app: Signal installed, linked and
unlinked; Telegram given a bot, paired and unpaired.

Linking was a command line: `python -m channels.signal link` printed a QR
code into a terminal and saved it as a PNG in the data folder, and
signal-cli and its Java had to be put in place by hand first. A user of the
app has no terminal. So the app asks here, and `runtime.signal_state` says
how it is going (`/signal/status`):

- `POST /signal/install`: signal-cli and its Java, downloaded and checked
  against their pins in the background (core/addons.py). The state is
  "installing", with how far it has got, then "unlinked".
- `POST /signal/link`: a QR code to scan, as SVG, in the answer and nowhere
  else. The state is "linking" until the phone finishes, or the code
  expires; then Signal starts ("ready"), or is "unlinked" again, saying
  why.
- `POST /signal/unlink`, or "unlink" typed in Note to Self
  (channels/bridge.UNPAIR_WORDS): Signal stops, its keys are deleted, and
  messages still waiting for it stop waiting. The phone lists the device
  until the user removes it there, and the goodbye says so.

Telegram (channels/telegram.py), at `/telegram/status`:
- `POST /telegram/token` `{"token"}`: the user's bot, from BotFather,
  checked with Telegram before it is kept in the keychain. A new bot
  forgets the chat paired to the old one.
- `POST /telegram/pair`: six digits for the user to send the bot within
  ten minutes; the first private chat to send them is paired.
- `POST /telegram/unpair`, or "/unpair" sent in the chat: the chat is
  forgotten, with its files, and messages waiting for it stop waiting.

Unlinking, unpairing and a new bot also take back what the chat set going
(`approvals.forget_chat`): its waiting cards are withdrawn, the routines and
goals it approved are paused, and its yeses are undone, so one turned on
again in the app asks before each send.

Linking, giving a bot and pairing take the app's sign-in once a passphrase
is set (`signin.refused`): a linked phone approves cards for as long as it
stays, past the app's sign-out when Windows locks. They took nothing until
step 11's audit, and whoever could read the run file's token could pair a
chat of their own. Unlinking and unpairing take no sign-in: taking an
approver away is always open.
"""

from __future__ import annotations

import asyncio
import logging
import secrets as _random

from starlette.requests import Request
from starlette.responses import JSONResponse

from agent import approvals, outbox, runtime
from agent.worker import bodies, links, signin
from channels import signal, telegram
from core import addons, secrets

log = logging.getLogger("worker")

# One setup step at a time: two clicks on "Link" must not start two
# signal-cli processes on one account folder.
_lock = asyncio.Lock()
# The link attempt under way: its number, which a newer attempt or an
# unlink moves on so that the older one's ending is ignored, and its
# process.
_attempt: dict = {"n": 0, "proc": None}
# The install and the link waits, held: the loop keeps only a weak
# reference to a task, and one nothing holds can be collected half done.
_running: set[asyncio.Task] = set()

# The states in which Signal is set up and linked, or on its way to being:
# installing again, or linking a second device, would undo that.
_SET_UP = frozenset({"starting", "ready", "failed", "cold"})


async def signal_install(_: Request) -> JSONResponse:
    async with _lock:
        state = runtime.signal_state.get("state")
        if state == "installing":
            return JSONResponse({"bridge": runtime.signal_state}, 202)
        if state in _SET_UP or state == "linking":
            return JSONResponse({"error": "Signal is set up already."}, 409)
        links.signal_is("installing", done=0, of=addons.SIGNAL_CLI.size + addons.JAVA.size)
        _run(_install())
        return JSONResponse({"bridge": runtime.signal_state}, 202)


def _run(work) -> None:
    task = asyncio.create_task(work)
    _running.add(task)
    task.add_done_callback(_running.discard)


async def _install() -> None:
    loop = asyncio.get_running_loop()

    def progress(done: int, of: int) -> None:
        # From the download's thread: the state is the loop's to change.
        loop.call_soon_threadsafe(runtime.signal_state.update, {"done": done, "of": of})

    try:
        await asyncio.to_thread(signal.install, progress)
    except addons.AddonError as exc:
        log.warning("Signal was not installed: %s", exc)
        links.signal_is("unavailable", error=str(exc))
        return
    await links.start_signal()


async def signal_link(request: Request) -> JSONResponse:
    """A QR code to scan with the phone, as SVG. Only while Signal is set up
    and not linked; asked again, the earlier code is ended and a new one
    made."""
    if (refusal := await signin.refused(await bodies.body(request))) is not None:
        return refusal
    async with _lock:
        state = runtime.signal_state.get("state")
        if state not in ("unlinked", "linking"):
            why = ("Signal is linked already." if state in _SET_UP
                   else "Set Signal up first.")
            return JSONResponse({"error": why}, 409)
        await _end_attempt()
        try:
            link, proc = await asyncio.to_thread(signal.start_link)
        except signal.Unready as exc:
            links.signal_is("unavailable", hint=str(exc))
            return JSONResponse({"error": str(exc)}, 409)
        except (signal.LinkFailed, OSError) as exc:
            links.signal_is("unlinked", error=str(exc))
            return JSONResponse({"error": str(exc)}, 502)
        _attempt["n"] += 1
        _attempt["proc"] = proc
        links.signal_is("linking", wait_s=signal.LINK_WAIT_S)
        _run(_await_link(_attempt["n"], proc))
        return JSONResponse({"qr": signal.qr_svg(link), "wait_s": signal.LINK_WAIT_S})


async def _await_link(n: int, proc) -> None:
    result = await asyncio.to_thread(signal.finish_link, proc)
    if _attempt["n"] != n:
        return  # a newer code, or an unlink, took over
    _attempt["proc"] = None
    if result["linked"]:
        log.info("Signal linked")
        await links.start_signal()
    else:
        links.signal_is("unlinked", error=result["error"])


async def _end_attempt() -> None:
    proc, _attempt["proc"] = _attempt["proc"], None
    _attempt["n"] += 1
    if proc is not None:
        await asyncio.to_thread(signal.end_link, proc)


async def signal_unlink(_: Request) -> JSONResponse:
    taken = await unlink_signal()
    return JSONResponse({"bridge": runtime.signal_state,
                         "goodbye": _goodbye(signal.SignalChannel.goodbye, taken, "Signal")})


def _goodbye(goodbye: str, taken: approvals.TakenBack | None, name: str) -> str:
    """The channel's goodbye, and, in the app, what unlinking it took back."""
    return " ".join(part for part in (goodbye, taken and taken.says(name)) if part)


def _take_back(channel: str, why: str) -> approvals.TakenBack | None:
    if runtime.conn is None:
        return None
    taken = approvals.forget_chat(runtime.conn, channel, why)
    log.info("%s: withdrew cards %s, paused %s", why, taken.withdrawn, taken.paused)
    return taken


async def unlink_signal() -> approvals.TakenBack | None:
    """Forget the Signal account: the bridge stopped, a link under way
    ended, what the chat set going taken back, this device's keys deleted,
    and the messages waiting for Signal stopped, since nothing will send
    them. Taken back before the keys go, so a failure deleting them still
    leaves nothing the chat approved running."""
    async with _lock:
        await _end_attempt()
        bridge, runtime.signal = runtime.signal, None
        if bridge is not None:
            await bridge.stop()
        taken = _take_back("signal", "Signal was unlinked")
        try:
            await asyncio.to_thread(signal.forget)
        except OSError as exc:
            links.signal_is("failed", error=f"Signal's files could not all be deleted: {exc}")
            return taken
        if runtime.conn is not None:
            outbox.stop_channel(runtime.conn, "signal", "Signal was unlinked")
        links.signal_is("unlinked")
        log.info("Signal unlinked")
        return taken


# -- Telegram ----------------------------------------------------------------


async def telegram_token(request: Request) -> JSONResponse:
    """Use the user's bot: its token checked with Telegram, kept, and
    polled. Pairing is per bot, so a new one forgets the chat paired to the
    old."""
    body = await bodies.body(request)
    if (refusal := await signin.refused(body)) is not None:
        return refusal
    token = str(body.get("token") or "").strip()
    async with _lock:
        try:
            bot = await telegram.check_token(token)
        except telegram.TelegramError as exc:
            return JSONResponse({"error": str(exc)}, 400)
        bridge, runtime.telegram = runtime.telegram, None
        if bridge is not None:
            await bridge.stop()
        _take_back("telegram", "Telegram's bot was replaced")
        await asyncio.to_thread(telegram.forget)
        await asyncio.to_thread(secrets.set_secret, telegram.TOKEN_KEY, token)
        if runtime.conn is not None:
            outbox.stop_channel(runtime.conn, "telegram", "Telegram's bot was replaced")
        await links.start_telegram()
        return JSONResponse({"bridge": runtime.telegram_state, "bot": bot})


async def telegram_pair(request: Request) -> JSONResponse:
    """Six digits to send the bot within ten minutes. Only while no chat is
    paired; asked again, a new code replaces the old."""
    if (refusal := await signin.refused(await bodies.body(request))) is not None:
        return refusal
    async with _lock:
        bridge = runtime.telegram
        if bridge is None:
            return JSONResponse({"error": "Give Steve a bot first."}, 409)
        if bridge.channel.chat_id is not None:
            return JSONResponse({"error": "Telegram is paired already."}, 409)
        code = f"{_random.randbelow(10 ** 6):06d}"
        bridge.channel.offer(code, telegram.PAIR_WAIT_S)
        links.telegram_is("pairing", bot=bridge.channel.bot, wait_s=telegram.PAIR_WAIT_S)
        return JSONResponse({"code": code, "bot": bridge.channel.bot,
                             "wait_s": telegram.PAIR_WAIT_S})


async def telegram_unpair(_: Request) -> JSONResponse:
    taken = await unpair_telegram()
    return JSONResponse({"bridge": runtime.telegram_state,
                         "goodbye": _goodbye(telegram.TelegramChannel.goodbye, taken,
                                             "Telegram")})


async def unpair_telegram() -> approvals.TakenBack | None:
    """Forget the paired chat and its files, take back what it set going,
    and stop the messages waiting for it. The bot goes on polling, for a new
    code: a remote channel can unpair itself, and only the app pairs one
    again."""
    async with _lock:
        bridge = runtime.telegram
        # The chat stops answering first: taken back while it still could,
        # what it said in between made cards after the take-back.
        if bridge is not None:
            await bridge.channel.unpair()
        else:
            await asyncio.to_thread(telegram.forget)
        taken = _take_back("telegram", "Telegram was unpaired")
        if runtime.conn is not None:
            outbox.stop_channel(runtime.conn, "telegram", "Telegram was unpaired")
        if bridge is not None:
            links.telegram_is("unpaired", bot=bridge.channel.bot)
        log.info("Telegram unpaired")
        return taken
