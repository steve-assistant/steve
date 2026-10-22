"""The worker's HTTP and websocket handlers. The route table is in
`agent/server.py`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import time

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.websockets import WebSocket, WebSocketDisconnect

from agent import (
    approvals,
    attention,
    jobs,
    offloop,
    reminders,
    runtime,
)
from agent.loop import Conversation
from agent.models import Role
from agent.provider import describe
from agent.version import STEVE_VERSION
from agent.worker import answering, app_photos, bodies, duties, journal, links, signin
from channels import photos
from core import desk as rules
from core import localapi, spend
from tools import code_exec

log = logging.getLogger("worker")


STARTED_AT = time.monotonic()








async def reminder_status(_: Request) -> JSONResponse:
    active = [r.as_dict() for r in reminders.active(runtime.conn)]
    return JSONResponse({"active": active, "tick_s": reminders.TICK_S})


async def attention_status(_: Request) -> JSONResponse:
    """What the attention loop has noticed and done today -- counts, never
    content -- and its last tick."""
    return JSONResponse({**attention.status(runtime.conn), "last": runtime.attention_state})


async def action_decide(request: Request) -> JSONResponse:
    """A yes or a no from the app, which needs the app's live session once
    there is one to sign in with (agent/worker/signin.py). A high-risk yes
    carries the passphrase or Windows Hello's signature, checked and bound
    to the card by the frozen core; refused, the answer says what the app
    should ask for (`needs`): a sign-in, the passphrase, or one to choose
    first."""
    from agent.worker import approvers, signin
    from core import passphrase

    body = await bodies.body(request)
    if (refusal := await signin.refused(body)) is not None:
        return refusal
    action_id, approve = bodies.whole(body.get("action_id")), bool(body.get("approve"))
    if action_id is None:
        return bodies.bad("which action? its id is a number")
    # A yes to part of a grouped card: its items by their numbers, from 1
    # (`approvals.decide`). Taken as given, never coerced: "2" or True
    # would have chosen an item nobody ticked.
    only = body.get("only")
    if only is not None and not (isinstance(only, list)
                                 and all(type(n) is int for n in only)):
        return JSONResponse({"ok": False, "error": "only names a card's items by number"},
                            status_code=400)
    # The card as the app read it (`approvals.decide`): checked before a
    # passphrase or a glance is spent on a card that has changed since.
    # Anything but the card's hash, of any type, is a card read otherwise.
    seen = body.get("seen")
    action = approvals.get(runtime.conn, action_id)
    if approve and action is not None and (
            action.id in approvals.held_now()
            or (seen is not None and seen != action.args_hash)):
        return JSONResponse({"ok": False, "error": "the card changed since you read it: "
                             "read it again"}, status_code=400)
    if approve and action is not None and action.status == "pending" and action.high_risk:
        signed = body.get("hello")
        if isinstance(signed, dict):
            why = await approvers.accept_hello(action.id, action.tool,
                                               str(signed.get("nonce") or ""),
                                               str(signed.get("signature") or ""))
        else:
            why = await approvers.accept_passphrase(action.id, action.tool,
                                                    str(body.get("passphrase") or ""))
        if why:
            is_set = await asyncio.to_thread(passphrase.is_set)
            needs = "passphrase" if is_set else "new_passphrase"
            return JSONResponse({"ok": False, "error": why, "needs": needs}, status_code=400)
    outcome = await answering.decide_action(action_id, approve=approve, by="tray", only=only,
                                            seen=seen)
    return JSONResponse(outcome, status_code=200 if outcome.get("ok") else 400)


async def signal_status(_: Request) -> JSONResponse:
    if runtime.signal is None:
        # Deliberately not constructing a channel to ask: resolving the
        # account shells out to signal-cli, which blocks on the account lock.
        # A status endpoint must never be able to hang the worker.
        return JSONResponse({"bridge": runtime.signal_state, "channel": "signal"})
    # The app reads the bridge's state alone, which showed Signal ready
    # while its daemon was down; the channel's trouble is said there too.
    shown = dict(runtime.signal_state)
    trouble = getattr(runtime.signal.channel, "error", None)
    if trouble and not shown.get("error"):
        shown["error"] = trouble
    return JSONResponse({"bridge": shown, **runtime.signal.status()})


async def telegram_status(_: Request) -> JSONResponse:
    bridge = runtime.telegram
    if bridge is None:
        return JSONResponse({"bridge": runtime.telegram_state, "channel": "telegram"})
    # A code that ran out unused leaves the chat unpaired, said here rather
    # than by a timer.
    if (runtime.telegram_state.get("state") == "pairing" and not bridge.channel.offering
            and bridge.channel.chat_id is None):
        from agent.worker import links

        links.telegram_is("unpaired", bot=bridge.channel.bot)
    # As Signal's above: a poll Telegram refuses (a second program on the
    # same bot) is said on the page, which showed Telegram ready while
    # nothing came in.
    shown = dict(runtime.telegram_state)
    if bridge.channel.error and not shown.get("error"):
        shown["error"] = bridge.channel.error
    return JSONResponse({"bridge": shown, **bridge.status()})


async def voice_status(_: Request) -> JSONResponse:
    from agent.worker import desk

    return JSONResponse(
        {
            "warm": runtime.voice_warm,
            **(
                runtime.voice.status() if runtime.voice
                else {"listening": False, "speaking": False}
            ),
            # Hands-free ("hey Steve"): its switch, state and counts -- never words.
            "hands_free": desk.status(),
        }
    )


async def voice_listen(request: Request) -> JSONResponse:
    """The desktop app's switch for hands-free listening: {"on": true|false},
    or {"toggle": true} -- the other way from how the worker has it, decided
    here and not from the app's copy, which can be stale. `by` says which
    control asked (the hotkey, the tray, Settings), for the log."""
    from agent.worker import desk

    body = await bodies.body(request)
    on = body.get("on")
    if not isinstance(on, bool) and body.get("toggle") is not True:
        return JSONResponse({"ok": False, "error": "say {\"on\": true} or false, or "
                                                   "{\"toggle\": true}"}, status_code=400)
    by = re.sub(r"[^a-z-]", "", str(body.get("by") or "app").lower())[:20] or "app"
    if not isinstance(on, bool):
        on = not desk.switched_on()
    if on and not desk.offered():  # before the models load for it (item 15)
        return JSONResponse({"ok": False, "error": desk.NOT_OFFERED}, status_code=403)
    if on:
        await links.ensure_voice()  # loaded on first use (links.warm_voice)
    return JSONResponse({"ok": True, **(await desk.switch(on, by=by))})


async def voice_tap(request: Request) -> JSONResponse:
    """Notes of what the laptop plays -- a call, a video: {"on": true|false}.
    Works with hands-free off: it hears the laptop's output, not a microphone."""
    from agent.worker import desk

    body = await bodies.body(request)
    if not isinstance(body.get("on"), bool):
        return JSONResponse({"ok": False, "error": "say {\"on\": true} or false"},
                            status_code=400)
    if body["on"]:
        await links.ensure_voice()  # loaded on first use (links.warm_voice)
    said = desk.tap(body["on"], title=body.get("title") or None)
    return JSONResponse(said, status_code=200 if said.get("ok") else 409)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


async def health(_: Request) -> JSONResponse:
    return JSONResponse(
        {
            "ok": True,
            "version": STEVE_VERSION,
            "pid": os.getpid(),
            "duties": duties.duty_state(),
            "uptime_s": round(time.monotonic() - STARTED_AT, 3),
        }
    )


async def shutdown(_: Request) -> JSONResponse:
    """Ask uvicorn to stop accepting work and drain what it has."""
    if runtime.http is not None:
        runtime.http.should_exit = True
    return JSONResponse({"ok": True, "shutting_down": True, "pid": os.getpid()})


async def chat(request: Request) -> JSONResponse:
    body = await bodies.body(request)
    message = body.get("message")
    text = message.strip() if isinstance(message, str) else ""
    if not text:
        return JSONResponse({"error": "message is required"}, status_code=400)

    convo = answering.conversation(body.get("session_id"))
    try:
        reply = await convo.send(text)
    except Exception as exc:  # noqa: BLE001 - surface it, never 500 silently
        return JSONResponse(
            {"error": f"{type(exc).__name__}: {exc}", "session_id": convo.session_id},
            status_code=502,
        )
    return JSONResponse({"session_id": convo.session_id, **reply.as_dict()})


def _sandbox_status() -> dict:
    try:
        return code_exec.status()
    except Exception as exc:  # noqa: BLE001 - a status page must not 500
        return {"available": False, "reason": f"{type(exc).__name__}: {exc}"[:200]}


# The slow part of `/status`, off the loop and remembered. The tray polls
# every five seconds, and this ran on the loop each time: `docker info` and
# `docker images` with 15 s and 30 s timeouts. With Docker Desktop stopped or
# starting, that froze the worker for most of every poll, `/health` stopped
# answering, and the watchdog killed a worker with nothing wrong with it.
# It decides nothing; five minutes old is fine for a status light, and each
# check wakes Docker Desktop, whose Resource Saver pauses its VM only after
# five minutes with nothing asked of it. Google's sign-in had a light of its
# own here until item 9 of step 5: it is an account now, with its health.
_SANDBOX_PROBE = offloop.Probe(_sandbox_status, ttl_s=300)


async def status(_: Request) -> JSONResponse:
    totals = runtime.conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) c, COUNT(*) n FROM usage_events"
    ).fetchone()
    today = spend.spent(runtime.conn, spend.windows().day.since)  # since the user's midnight
    memories = runtime.conn.execute("SELECT COUNT(*) n FROM memories").fetchone()["n"]

    return JSONResponse(
        {
            "version": STEVE_VERSION,
            "pid": os.getpid(),
            "models": {r.value: describe(r) for r in Role},
            "sandbox": await _SANDBOX_PROBE.get(),
            "queue": jobs.summarise_queue(runtime.conn),
            "memories": memories,
            "cost": {
                "total_usd": round(totals["c"], 6),
                "today_usd": round(today, 6),
                "calls": totals["n"],
            },
            "active_sessions": len(runtime.conversations),
        }
    )


async def list_jobs(_: Request) -> JSONResponse:
    return JSONResponse({"jobs": [j.as_dict() for j in jobs.recent(runtime.conn, limit=50)]})


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------


# Messages that start a turn: the socket is busy until it is answered (a
# push-to-talk press, until it is let go and answered).
TURN_STARTS = frozenset({"chat", "voice_start", "voice_stop"})
# The close code that tells a client to come back: the app reconnects within
# a quarter of a second (ui/src/api.ts AgentSocket).
SERVICE_RESTART = 1012


def _why_not_listen(session: object) -> str | None:
    """Why a press may not open the microphone, or None when it may: once a
    passphrase is set, the press carries the app's live session, and the
    screen is not locked. The app hides the button signed out, but anything
    that held the token could press on the socket, and each press held the
    microphone up to two minutes (step 11's audit, C3). Blocking."""
    from voice import listen  # numpy: only once someone presses

    locked = listen.screen_locked()
    if rules.may_listen(switched_on=True, locked=locked, signed_in=signin.signed_in(session)):
        return None
    return "the screen is locked" if locked else "sign in to Steve first"


async def ws_session(ws: WebSocket) -> None:
    """The app's socket: chat and push-to-talk turns, and what is pushed to it.

    Pinned by the supervisor to the worker it opened on, so a turn under way
    is never cut off by a swap. But the supervisor cannot tell a socket
    mid-turn from one sitting idle, and the app holds two open all day: every
    swap waited out its whole 30-second drain, and then killed whatever was
    still going -- a research turn takes minutes (6 October 2026). The worker
    can tell: handing over its duties, it closes the idle ones at once
    (`duties.give_up_duties`), and a busy one once its turn is answered.
    """
    # `steve`, if the client offered it: the app's token rides in the other.
    await ws.accept(subprotocol=localapi.subprotocol(ws.scope))
    runtime.listeners.add(ws)
    convo: Conversation | None = None
    # A press while the speech models are not loaded (links.warm_voice):
    # recorded at once, and transcribed when they are.
    early: tuple | None = None
    # A press on a warm session, until its release: the microphone is this
    # socket's, and is let go if the socket goes first.
    pressed = False
    # The release's turn, run beside this loop rather than in it. Awaited
    # here, it held the loop until the reply had played, so a press during
    # the reply -- the barge-in -- was read only once it was over, and then
    # ended at once as a turn too short to hear (audit of 16 October 2026).
    turn_task: asyncio.Task | None = None

    async def answer(previous, voice, recording) -> None:
        from agent.worker import desk

        if previous is not None:
            # One turn at a time: the press that started this one has cut the
            # last one short, and it ends in a moment.
            with contextlib.suppress(Exception):
                await previous

        async def heard(text: str) -> None:
            # The words on release, before the reply is written and said:
            # shown only with the reply, a slow answer left the user unsure
            # they had been heard at all.
            await ws.send_json({"type": "voice_heard", "heard": text})

        try:
            # Through the desk's engine, which owns the speakers: what the
            # turn asks (notes of the room) lives on its own turn and starts
            # when it is over; a live conversation is not on offer to it
            # (agent/toolset/desk.talk_it_through).
            turn = await desk.hotkey_turn(
                lambda out: voice.finish_turn(recording, out=out, on_heard=heard))
            if not turn.transcript.ok and not turn.error:
                # Nothing was said, or it was silence. Tell the UI so it can
                # drop the listening indicator without inventing a turn.
                await ws.send_json({
                    "type": "voice_empty",
                    "reason": turn.transcript.rejected or "nothing heard",
                    **turn.as_dict(),
                })
            else:
                await ws.send_json({"type": "voice_turn", **turn.as_dict()})
        except Exception as exc:  # noqa: BLE001 - said to the app, and logged
            log.exception("push-to-talk turn failed")
            with contextlib.suppress(Exception):
                await ws.send_json({"type": "error", "error": f"{type(exc).__name__}: {exc}"})
        finally:
            if early is None and not pressed:  # not pressed again meanwhile
                runtime.turning.discard(ws)
            if duties.duty_state() == "retired" and ws not in runtime.turning:
                with contextlib.suppress(Exception):
                    await ws.close(code=SERVICE_RESTART)

    await ws.send_json(
        {"type": "hello", "version": STEVE_VERSION, "pid": os.getpid()}
    )
    try:
        while True:
            raw = await ws.receive_text()
            try:
                message = json.loads(raw)
            except ValueError:
                await ws.send_json({"type": "error", "error": "expected JSON"})
                continue

            kind = message.get("type")

            # A turn under way keeps its socket through a swap until it is
            # answered (`duties.give_up_duties` closes the idle ones).
            if kind in TURN_STARTS:
                runtime.turning.add(ws)
            try:
                if kind == "ping":
                    # Cheap liveness probe: proves the proxy path works without
                    # spending a model call.
                    await ws.send_json({"type": "pong", "pid": os.getpid()})

                elif kind == "chat":
                    text = (message.get("text") or "").strip()
                    # Photos by the names their upload was given
                    # (agent/worker/app_photos.py); a message may be only them.
                    named = message.get("photos") or []
                    if not isinstance(named, list):
                        await ws.send_json({"type": "error", "error": "photos is a list"})
                        continue
                    if not text and not named:
                        await ws.send_json({"type": "error", "error": "empty message"})
                        continue
                    came = await asyncio.to_thread(app_photos.attached, named)
                    # Recorded with the line for what came, as the phones'
                    # are, so later turns know a photo was sent.
                    asked = "\n".join(part for part in (photos.note(came.images, came.unopened),
                                                        text) if part)
                    if convo is None:
                        convo = answering.conversation(message.get("session_id"))
                    await ws.send_json({"type": "thinking", "session_id": convo.session_id})
                    line = journal.TurnLine(ws)
                    try:
                        reply = await convo.send(asked, on_tool=line, images=came.images)
                    except Exception as exc:  # noqa: BLE001
                        await ws.send_json(
                            {"type": "error", "error": f"{type(exc).__name__}: {exc}"}
                        )
                        continue
                    finally:
                        line.done()
                    await ws.send_json(
                        {"type": "reply", "session_id": convo.session_id, **reply.as_dict()}
                    )

                elif kind == "voice_start":
                    # The live time's clock starts here, at the press: started
                    # in the recorder, what ran before the open went unseen.
                    pressed_at = time.perf_counter()
                    if early is not None:
                        # Pressed again before the release (the button and
                        # the key): the first recording goes on. Replaced,
                        # it was never stopped, and its microphone stayed open.
                        # Even with the models up by now: the warm path opened
                        # the session's microphone too, which the release,
                        # handing on the first recording, never closed
                        # (review of steps 7 to 9, finding 2).
                        await ws.send_json({"type": "listening"})
                        continue
                    if why := await asyncio.to_thread(_why_not_listen, message.get("session")):
                        await ws.send_json({"type": "voice_unavailable",
                                            "warm": {"state": "refused", "error": why}})
                        continue
                    if runtime.voice is None:
                        # Load while the user talks: most of the load is over by the
                        # time the user lets go.
                        from voice import audio

                        loading = asyncio.get_running_loop().create_task(links.ensure_voice())
                        recorder = audio.Recorder()
                        try:
                            recorder.start(pressed_at)
                        except Exception as exc:  # noqa: BLE001 - no microphone is an answer
                            await ws.send_json({"type": "voice_unavailable", "warm": {
                                "state": "failed", "error": f"microphone: {exc}"[:200]}})
                            continue
                        early = (recorder, loading)
                        audio.tick_alone()
                        await ws.send_json({"type": "listening"})
                        continue
                    from agent.worker import desk

                    # Whatever the desk is saying stops, and a live
                    # conversation ends with their words in it kept.
                    desk.hotkey_down()
                    try:
                        runtime.voice.start_listening(pressed_at)
                    except Exception as exc:  # noqa: BLE001 - no microphone is an answer
                        # As on the cold path: uncaught, it dropped the socket.
                        await ws.send_json({"type": "voice_unavailable", "warm": {
                            "state": "failed", "error": f"microphone: {exc}"[:200]}})
                        continue
                    pressed = True
                    await ws.send_json({"type": "listening"})

                elif kind == "voice_stop":
                    pressed = False
                    recording = None
                    if early is not None:
                        (recorder, _loading), early = early, None
                        recording = recorder.stop()
                        await links.ensure_voice()
                    elif runtime.voice is not None:
                        # The microphone closes on the release. Left to the
                        # turn, it stayed open while the turn before was still
                        # being written, and what it heard went with this one.
                        recording = runtime.voice.stop_listening()
                    if runtime.voice is None:
                        runtime.turning.discard(ws)  # no turn to hold it
                        await ws.send_json({"type": "voice_unavailable",
                                            "warm": runtime.voice_warm})
                        continue
                    turn_task = asyncio.get_running_loop().create_task(
                        answer(turn_task, runtime.voice, recording))

                else:
                    await ws.send_json({"type": "error", "error": f"unknown type {kind!r}"})
            finally:
                # Held while recording, and while the release's turn runs
                # (`answer` lets it go).
                if kind not in ("voice_start", "voice_stop"):
                    runtime.turning.discard(ws)
            if duties.duty_state() == "retired" and ws not in runtime.turning:
                # Answered, on a worker that has handed over: the app
                # reconnects, to the one serving now.
                await ws.close(code=SERVICE_RESTART)
                return

    except WebSocketDisconnect:
        pass
    finally:
        runtime.listeners.discard(ws)
        runtime.turning.discard(ws)
        if early is not None:  # the app went away mid-press: let the mic go
            with contextlib.suppress(Exception):
                early[0].stop()
        if pressed and runtime.voice is not None:
            runtime.voice.cancel_listening()
        with contextlib.suppress(RuntimeError):
            await ws.close()
