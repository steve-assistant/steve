"""Telegram: a bot the user makes, paired to one chat by a code.

For a user who does not use Signal. The bot is theirs (BotFather makes it,
and its token is kept in the keychain, `TOKEN_KEY`); Steve reads it by
long-polling `getUpdates`, so nothing listens on a port.

What holds:
- **One chat.** Until it is paired, Steve reads nothing but the code the
  app shows, sent within ten minutes in a private chat with the bot
  (`TelegramChannel.offer`). Anyone can find a bot and write to it, so
  every other chat is ignored -- logged once each, never answered -- and
  five wrong codes withdraw the code, so ten minutes are not a million
  guesses.
- **Nothing read twice.** The update offset is kept in the data folder, and
  moved past a batch before any of it is handled: a message lost to a crash
  mid-turn is better than one acted on twice ("buy the tickets").
- **A card gets Yes and No buttons.** A press is the words "YES MAPLE"
  arriving from the paired chat, so the approval intercept stays the only
  thing that decides. A high-risk card gets none: a button cannot carry
  the authenticator's code.
- **Voice notes and photos** are fetched into the data folder and deleted
  a day later (`sweep_attachments`), as Signal's are. Files Steve cannot
  open are named, not fetched.
- **The token is in every URL the Bot API takes,** so no error that could
  carry one is passed on as it came (`BotApi`), and the worker's HTTP
  loggers are quiet (agent/server.QUIET_LOGGERS).

What Telegram is not: end-to-end encrypted. Its servers read every message
and every card (docs/security-model.md, 4.3), and it keeps a message for a
computer asleep for only a day. Setup says both.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import re
import shutil
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx2

from channels import photos
from channels.base import KEEP_ATTACHMENTS_S, Capability, Channel, Incoming, Outgoing, delete_older
from channels.bridge import CARD_ANSWER
from channels.plaintext import shown
from core import endpoints, paths, secrets

log = logging.getLogger("telegram")

TOKEN_KEY = "TELEGRAM_BOT_TOKEN"
# What BotFather gives: the bot's number, a colon, and 35 characters.
TOKEN_SHAPE = re.compile(r"\d{5,}:[A-Za-z0-9_-]{30,}")
# How long one `getUpdates` waits for a message before asking again.
LONG_POLL_S = 50
# The pause after a failed poll: Telegram down, the network gone, a token
# revoked. Each is said in the status meanwhile.
RETRY_S = 5.0
# A pairing code: six digits, ten minutes, five wrong tries.
PAIR_WAIT_S = 600
PAIR_TRIES = 5
# The longest wait a "Too Many Requests" may name that a call sits out
# before trying once more; a longer one fails the call, and the bridge or
# the outbox tries it later.
RATE_WAIT_MOST_S = 30.0
# What the app says when another program polls the same bot: Telegram gives
# each update to one poller, so Steve would get nothing, while showing ready.
TAKEN = ("Another program is reading this bot's messages, so Steve gets none. "
         "Stop it, or give Steve a bot of its own.")
# The most one Telegram message holds.
MOST_CHARS = 4096

# A button's data: the answer, and the card it is on, by its id. Buttons
# sent before the id was added carry the answer alone.
_PRESSED = re.compile(r"((?:YES|NO) [A-Za-z]+)(?: (\d{1,12}))?")
_CODE_SHAPE = re.compile(r"\d{6}")

PAIRED = ("Paired: this chat is Steve's now. Cards come here with Yes and No buttons;"
          " send /unpair to stop.")


def folder() -> Path:
    return paths.data_dir() / "telegram"


def attachment_dir() -> Path:
    return folder() / "attachments"


def _state_path() -> Path:
    return folder() / "state.json"


def _load() -> dict:
    """The paired chat and the update offset; nothing paired when the file
    is missing or not one this wrote."""
    try:
        held = json.loads(_state_path().read_text(encoding="utf-8"))
        chat = held.get("chat_id")
        return {"chat_id": chat if isinstance(chat, int) else None,
                "offset": int(held.get("offset") or 0)}
    except (OSError, ValueError, TypeError, AttributeError):
        return {"chat_id": None, "offset": 0}


def _save(state: dict) -> None:
    target = _state_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, sort_keys=True), encoding="utf-8", newline="\n")
    tmp.replace(target)


def token() -> str | None:
    """The bot's token, if the user gave Steve one. BLOCKING: the keychain."""
    return secrets.get(TOKEN_KEY)


def paired() -> bool:
    """Whether a chat is paired. BLOCKING: it reads a small file."""
    return _load()["chat_id"] is not None


def forget() -> None:
    """Forget the paired chat, the offset and every file fetched. The bot
    and its token stay, to pair again. BLOCKING."""
    shutil.rmtree(folder(), ignore_errors=True)


def sweep_attachments(*, now: float | None = None) -> int:
    """Delete the fetched files older than `KEEP_ATTACHMENTS_S`; how many
    went. BLOCKING."""
    cutoff = (time.time() if now is None else now) - KEEP_ATTACHMENTS_S
    return delete_older([attachment_dir()], cutoff)


class TelegramError(RuntimeError):
    """The Bot API said no, or could not be reached; never with the token.
    `code` is the HTTP status when Telegram answered, and `retry_after` the
    wait it named for a "Too Many Requests"."""

    def __init__(self, text: str, *, code: int | None = None,
                 retry_after: float | None = None) -> None:
        super().__init__(text)
        self.code = code
        self.retry_after = retry_after


def _plainly(exc: TelegramError) -> str:
    """A failed poll as the app says it: what is wrong for the user, rather
    than the method and Telegram's own words, which the log keeps."""
    if exc.code == 409:
        return TAKEN
    if exc.code is None:
        return "Steve can't reach Telegram just now, and keeps trying."
    return f"Telegram refused Steve ({exc}). Steve keeps trying."


def _transport() -> httpx2.AsyncBaseTransport | None:
    """The real network. The test fences refuse it (tests/gate/fences.py);
    a test passes a fake Bot API instead."""
    return None


class BotApi:
    """The few Bot API methods Steve calls, as JSON over HTTPS."""

    def __init__(self, token: str, *, base: str | None = None,
                 transport: httpx2.AsyncBaseTransport | None = None) -> None:
        self._token = token
        base = base or endpoints.telegram()
        self._files = f"{base}/file/bot{token}/"
        self._client = httpx2.AsyncClient(
            base_url=f"{base}/bot{token}/",
            transport=transport if transport is not None else _transport(),
            timeout=httpx2.Timeout(LONG_POLL_S + 15, connect=10.0))

    def _hidden(self, text: str) -> str:
        return text.replace(self._token, "<token>")

    async def call(self, method: str, **params: Any) -> Any:
        """One method, tried once more after the wait Telegram names when it
        says "Too Many Requests". Without the wait, the bridge's next tries
        come seconds apart, inside Telegram's window, and are refused too."""
        try:
            return await self._call(method, params)
        except TelegramError as exc:
            if exc.retry_after is None or exc.retry_after > RATE_WAIT_MOST_S:
                raise
            await asyncio.sleep(exc.retry_after)
        return await self._call(method, params)

    async def _call(self, method: str, params: dict[str, Any]) -> Any:
        try:
            response = await self._client.post(
                method, json={key: value for key, value in params.items() if value is not None})
        except httpx2.HTTPError as exc:
            raise TelegramError(self._hidden(f"{method}: {type(exc).__name__}: {exc}")) from None
        try:
            body = response.json()
        except ValueError:
            raise TelegramError(f"{method}: HTTP {response.status_code}",
                                code=response.status_code) from None
        if not isinstance(body, dict) or not body.get("ok"):
            said = body.get("description") if isinstance(body, dict) else None
            told = body.get("parameters") if isinstance(body, dict) else None
            wait = told.get("retry_after") if isinstance(told, dict) else None
            raise TelegramError(
                self._hidden(f"{method}: {said or response.status_code}"),
                code=response.status_code,
                retry_after=float(wait) if isinstance(wait, int | float) else None)
        return body.get("result")

    async def download(self, file_path: str) -> bytes:
        try:
            response = await self._client.get(self._files + file_path.lstrip("/"))
        except httpx2.HTTPError as exc:
            raise TelegramError(self._hidden(f"download: {type(exc).__name__}: {exc}")) from None
        if response.status_code != 200:
            raise TelegramError(f"download: HTTP {response.status_code}")
        return response.content

    async def aclose(self) -> None:
        await self._client.aclose()


async def check_token(token: str, *, transport: httpx2.AsyncBaseTransport | None = None) -> str:
    """The bot's username, if `token` is a working bot's; else
    `TelegramError` saying why."""
    if not TOKEN_SHAPE.fullmatch(token or ""):
        raise TelegramError("that is not a bot token: BotFather's look like 123456:ABC-...")
    api = BotApi(token, transport=transport)
    try:
        me = await api.call("getMe")
    finally:
        await api.aclose()
    if not isinstance(me, dict) or not me.get("is_bot") or not me.get("username"):
        raise TelegramError("that token is not a bot's")
    return str(me["username"])


@dataclass
class _Offer:
    code: str
    until: float  # monotonic
    wrong: int = 0


@dataclass
class _Media:
    """One file a message carries, before it is fetched."""

    file_id: str
    kind: str  # the type Telegram reports, "" when it reports none
    name: str | None
    suffix: str
    is_audio: bool = False


class TelegramChannel(Channel):
    name = "telegram"
    goodbye = ("Unpaired: Steve won't read this chat again. To pair it again, use the app."
               " The bot stays yours: BotFather deletes it.")

    def __init__(self, token: str, *, transport: httpx2.AsyncBaseTransport | None = None,
                 on_paired: Callable[[], None] | None = None, poll_s: int = LONG_POLL_S,
                 retry_s: float = RETRY_S) -> None:
        self._token = token
        self._transport = transport
        self._api: BotApi | None = None
        self.on_paired = on_paired
        self.poll_s = poll_s
        self.retry_s = retry_s
        self.pairing = 0
        self._chat_id: int | None = None
        self.bot: str | None = None
        self.error: str | None = None
        # The last reply that failed partway, and how many chunks of it went.
        self._partly: tuple[str, int] | None = None
        self._offset = 0
        self._offer: _Offer | None = None
        self._ignored: set = set()
        self._stop = asyncio.Event()

    @property
    def capabilities(self) -> set[Capability]:
        return {Capability.TEXT, Capability.VOICE_IN, Capability.ATTACHMENTS, Capability.TYPING}

    @property
    def chat_id(self) -> int | None:
        """The paired chat, or None."""
        return self._chat_id

    @chat_id.setter
    def chat_id(self, chat: int | None) -> None:
        # Each change is a new pairing, counted where nothing can skip it. An
        # answer is checked against the pairing it came over, as it is
        # recorded (agent/worker/links.answers_from). Checking that some chat
        # was paired let one read before an unpair count once another chat,
        # or the same one again, was paired (step 11's third review).
        self._chat_id = chat
        self.pairing += 1

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        if self._api is not None:
            return
        held = await asyncio.to_thread(_load)
        self.chat_id, self._offset = held["chat_id"], held["offset"]
        self._api = BotApi(self._token, transport=self._transport)
        self._stop.clear()
        try:
            me = await self._api.call("getMe")
            self.bot = str(me.get("username") or "") or None
        except TelegramError as exc:
            # Not fatal: the network may be down, and polling says so.
            self.error = _plainly(exc)

    async def stop(self) -> None:
        self._stop.set()
        api, self._api = self._api, None
        if api is not None:
            await api.aclose()

    # -- pairing -------------------------------------------------------------

    def offer(self, code: str, wait_s: float = PAIR_WAIT_S) -> None:
        """Pair the first private chat to send `code` within `wait_s`."""
        self._offer = _Offer(code=code, until=time.monotonic() + wait_s)

    @property
    def offering(self) -> bool:
        return self._offer is not None and time.monotonic() <= self._offer.until

    async def unpair(self) -> None:
        """Forget the paired chat and every file fetched from it; the bot
        keeps polling, for a new code."""
        self.chat_id = None
        self._offer = None
        await asyncio.to_thread(forget)
        await asyncio.to_thread(_save, {"chat_id": None, "offset": self._offset})

    async def _try_pairing(self, chat_id: int, text: str) -> None:
        offer = self._offer
        if offer is None or time.monotonic() > offer.until:
            self._ignore(chat_id)
            return
        # "/start", or anything that is not six digits, is not a guess: the
        # first thing a new chat sends is "/start".
        if not _CODE_SHAPE.fullmatch(text):
            return
        if hmac.compare_digest(text.encode(), offer.code.encode()):
            self.chat_id, self._offer = chat_id, None
            await asyncio.to_thread(_save, {"chat_id": chat_id, "offset": self._offset})
            log.info("Telegram paired")
            await self._say(chat_id, PAIRED)
            if self.on_paired is not None:
                self.on_paired()
            return
        offer.wrong += 1
        if offer.wrong >= PAIR_TRIES:
            self._offer = None
            log.warning("a Telegram pairing code was withdrawn after %d wrong tries", PAIR_TRIES)

    def _ignore(self, chat_id: object) -> None:
        if chat_id not in self._ignored:
            self._ignored.add(chat_id)
            log.info("ignoring a Telegram chat that is not the paired one")

    # -- receiving -----------------------------------------------------------

    async def receive(self) -> AsyncIterator[Incoming]:
        """Each message from the paired chat, until stopped. A failed poll is
        said in the status and tried again; a channel that stops listening
        after one hiccup has silently stopped working."""
        while not self._stop.is_set():
            if self._api is None:
                return
            try:
                updates = await self._api.call(
                    "getUpdates", offset=self._offset or None, timeout=self.poll_s,
                    allowed_updates=["message", "callback_query"])
                self.error = None
            except TelegramError as exc:
                if self.error != _plainly(exc):
                    log.warning("Telegram: %s", exc)
                self.error = _plainly(exc)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), self.retry_s)
                continue
            updates = [u for u in updates or [] if isinstance(u, dict)]
            if not updates:
                continue
            # Past the whole batch first: at most once, never twice.
            self._offset = max(int(u.get("update_id") or 0) for u in updates) + 1
            await asyncio.to_thread(_save, {"chat_id": self.chat_id, "offset": self._offset})
            read = [m for u in updates if (m := await self._read(u)) is not None]
            for message in _albums(read):
                yield message

    async def _read(self, update: dict) -> Incoming | None:
        if isinstance(query := update.get("callback_query"), dict):
            return await self._pressed(query)
        message = update.get("message")
        if not isinstance(message, dict):
            return None
        chat = message.get("chat") or {}
        chat_id, sender = chat.get("id"), message.get("from") or {}
        if chat.get("type") != "private" or sender.get("is_bot"):
            self._ignore(chat_id)
            return None
        text = str(message.get("text") or message.get("caption") or "").strip()
        if self.chat_id is None:
            await self._try_pairing(chat_id, text)
            return None
        if chat_id != self.chat_id:
            self._ignore(chat_id)
            return None
        if text == "/start":
            return None
        # As the chat is checked: fetching what came with it waits.
        return await self._incoming(update, message, text, self.pairing)

    async def _incoming(self, update: dict, message: dict, text: str,
                        link: int) -> Incoming | None:
        stem = f"{int(update.get('update_id') or 0)}"
        audio: Path | None = None
        attachments: list[Path] = []
        unopened: list[str] = []
        others: list[tuple[Path, str, str | None]] = []
        waiting: dict[Path, _Media] = {}
        for n, media in enumerate(_media_of(message)):
            dest = attachment_dir() / f"{stem}-{n}{media.suffix}"
            if media.is_audio and audio is None:
                if await self._fetch(media.file_id, dest):
                    audio = dest
                    attachments.append(dest)
                else:
                    unopened.append("a voice note Steve could not fetch")
                continue
            others.append((dest, media.kind, media.name))
            waiting[dest] = media
        # Sorted before anything is fetched: a file Steve cannot open is
        # named, not downloaded.
        sorted_ = photos.sort(others)
        images = []
        for dest in sorted_.images:
            if await self._fetch(waiting[dest].file_id, dest):
                images.append(dest)
                attachments.append(dest)
            else:
                name = waiting[dest].name
                unopened.append(f"{name}, a photo Steve could not fetch" if name
                                else "a photo Steve could not fetch")
        unopened += sorted_.unopened
        if not text and audio is None and not images and not unopened:
            return None
        return Incoming(
            text=text,
            channel=self.name,
            sender=str((message.get("from") or {}).get("id") or ""),
            message_id=str(message.get("message_id") or ""),
            timestamp=int(message.get("date") or 0) * 1000,
            audio_path=audio,
            attachments=attachments,
            images=images,
            unopened=unopened,
            link=link,
            raw=update,
        )

    async def _pressed(self, query: dict) -> Incoming | None:
        """A button on a card: the words it stands for, from the paired chat
        alone."""
        card = query.get("message") or {}
        chat_id = (card.get("chat") or {}).get("id")
        presser = (query.get("from") or {}).get("id")
        with contextlib.suppress(TelegramError):
            await self._api.call("answerCallbackQuery", callback_query_id=query.get("id"))
        data = str(query.get("data") or "")
        if self.chat_id is None or chat_id != self.chat_id or presser != self.chat_id:
            self._ignore(chat_id)
            return None
        link = self.pairing
        if not (pressed := _PRESSED.fullmatch(data)):
            return None
        # The buttons come off: a card is answered once.
        with contextlib.suppress(TelegramError):
            await self._api.call("editMessageReplyMarkup", chat_id=chat_id,
                                 message_id=card.get("message_id"),
                                 reply_markup={"inline_keyboard": []})
        # No timestamp: a press is now, and the card's own date would read
        # as a message that waited.
        answer, card = pressed.groups()
        return Incoming(text=answer, channel=self.name, sender=str(presser),
                        message_id=str(query.get("id") or ""), timestamp=0, raw=query,
                        card=int(card) if card else None, link=link)

    async def _fetch(self, file_id: str, dest: Path) -> bool:
        try:
            found = await self._api.call("getFile", file_id=file_id)
            data = await self._api.download(str(found["file_path"]))
        except (TelegramError, KeyError, TypeError) as exc:
            log.warning("a Telegram file was not fetched: %s", exc)
            return False
        await asyncio.to_thread(_write, dest, data)
        return True

    # -- sending -------------------------------------------------------------

    async def send(self, reply: Outgoing, *, to: str | None = None) -> bool:
        if self.chat_id is None or self._api is None:
            return False
        # As Signal's: Telegram was sent the reply's Markdown as written,
        # so "**3pm**" and "## Today" showed as their characters. Plain, and
        # with no waiting card's word but a card's (channels/plaintext.shown).
        chunks = _chunks(shown(reply)) or [""]
        if chunks == [""]:
            return True
        # A long reply that failed partway is sent again whole, by the bridge
        # or the outbox; the chunks that already went are not sent twice.
        went = self._partly[1] if self._partly and self._partly[0] == reply.text else 0
        self._partly = None
        try:
            for n in range(went, len(chunks)):
                last = n == len(chunks) - 1
                await self._api.call("sendMessage", chat_id=self.chat_id, text=chunks[n],
                                     reply_markup=_buttons(reply) if last else None)
                went = n + 1
        except TelegramError as exc:
            log.warning("Telegram did not take a message: %s", exc)
            if went:
                self._partly = (reply.text, went)
            return False
        return True

    async def _say(self, chat_id: int, text: str) -> None:
        with contextlib.suppress(TelegramError):
            await self._api.call("sendMessage", chat_id=chat_id, text=text)

    async def typing(self, on: bool, *, to: str | None = None) -> None:
        if on and self.chat_id is not None and self._api is not None:
            with contextlib.suppress(TelegramError):
                await self._api.call("sendChatAction", chat_id=self.chat_id, action="typing")

    def status(self) -> dict:
        return {**super().status(), "paired": self.chat_id is not None, "bot": self.bot,
                "error": self.error}


def _write(dest: Path, data: bytes) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)


def _media_of(message: dict) -> list[_Media]:
    """The files a message carries, as Telegram describes them. Never a
    name of Telegram's for a path: each is saved under one of Steve's."""
    found: list[_Media] = []
    for key in ("voice", "audio"):
        if isinstance(item := message.get(key), dict) and item.get("file_id"):
            found.append(_Media(str(item["file_id"]), str(item.get("mime_type") or ""),
                                item.get("file_name"), ".ogg" if key == "voice" else ".audio",
                                is_audio=True))
    sizes = [s for s in message.get("photo") or [] if isinstance(s, dict) and s.get("file_id")]
    if sizes:
        # The largest Telegram made that a model takes.
        fits = [s for s in sizes if int(s.get("file_size") or 0) <= photos.MOST_BYTES] or sizes
        found.append(_Media(str(fits[-1]["file_id"]), "image/jpeg", None, ".jpg"))
    if isinstance(item := message.get("document"), dict) and item.get("file_id"):
        kind = str(item.get("mime_type") or "")
        found.append(_Media(str(item["file_id"]), kind, item.get("file_name"),
                            {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
                             "image/gif": ".gif"}.get(kind, ".file")))
    for key, what in (("video", "a video"), ("video_note", "a video message"),
                      ("sticker", "a sticker"), ("animation", "an animation")):
        if isinstance(item := message.get(key), dict) and item.get("file_id"):
            found.append(_Media(str(item["file_id"]), "", what, ".file"))
    return found


def _albums(messages: list[Incoming]) -> list[Incoming]:
    """An album comes as one message a photo, sharing a group id: merged
    into one, so three photos sent together are one turn, not three."""
    out: list[Incoming] = []
    by_group: dict[str, Incoming] = {}
    for message in messages:
        group = ((message.raw.get("message") or {}).get("media_group_id")
                 if isinstance(message.raw, dict) else None)
        if group and (first := by_group.get(str(group))) is not None:
            first.text = "\n".join(t for t in (first.text, message.text) if t)
            first.attachments += message.attachments
            together = first.images + message.images
            first.images = together[:photos.MOST_IMAGES]
            first.unopened += message.unopened + [
                f"a photo past the {photos.MOST_IMAGES} Steve looks at in a message"
            ] * len(together[photos.MOST_IMAGES:])
            continue
        if group:
            by_group[str(group)] = message
        out.append(message)
    return out


def _chunks(text: str) -> list[str]:
    """`text` in pieces Telegram takes, each line whole in one piece where
    it fits in one.

    A piece was cut at a line only in the second half of Telegram's limit,
    and otherwise wherever the limit fell. A briefing's line for a card to
    a class list -- its word, then some three thousand characters of
    addresses -- started in the first half, so the word went in one
    message with only the addresses before the cut (step 11's fifth
    review). Now a piece ends at the last line that fits in it whole. A
    line longer than any message is cut where it must be, and has every
    waiting card's word taken out first: no part of it says all a yes to
    that card approves (agent/cardwords.mask)."""
    if len(text) <= MOST_CHARS:
        return [text] if text else []
    pieces: list[str] = []
    piece = ""
    for line in text.split("\n"):
        if len(line) > MOST_CHARS:
            from agent import cardwords

            line = cardwords.mask(line, cardwords.words_now())
        if piece and len(piece) + 1 + len(line) <= MOST_CHARS:
            piece += "\n" + line
            continue
        if piece.strip():
            pieces.append(piece.rstrip())
        while len(line) > MOST_CHARS:
            pieces.append(line[:MOST_CHARS])
            line = line[MOST_CHARS:]
        piece = line
    if piece.strip():
        pieces.append(piece.rstrip())
    return pieces


def _buttons(reply: Outgoing) -> dict | None:
    """Yes and No for an ordinary card; None for anything else.

    Only a message sent as a card (`Outgoing.card`) has them, naming the
    word on its own last line, the one every card ends with
    (agent/approvals.card). They went on any message that held a card's
    line, so a reply the model wrote, or another card's description, could
    carry Yes and No for a card the user had never been shown (step 11's
    audit)."""
    words = CARD_ANSWER.findall(reply.text or "") if reply.card is not None else []
    if not words:
        return None
    # And the card's id: a press decides that card or none. Its word may
    # since have gone to a newer card, which a late press decided unseen.
    word = words[-1]
    return {"inline_keyboard": [[{"text": "Yes", "callback_data": f"YES {word} {reply.card}"},
                                 {"text": "No", "callback_data": f"NO {word} {reply.card}"}]]}
