"""A blank left for someone to fill never goes out in an email.

On 13 October 2026, in the persona eval, a user with no name saved yet
asked for a reply to Kim, and the draft was signed "[Your name]". Saved as
a draft and sent as written, that is what Kim reads. Told in a prompt, a
model still slips now and then; this holds it in code, at the drafting and
sending tools and again at their executors, as an address format is held
(tests/unit/test_mail_addresses.py).

A sign-off made only of blanks is left out, and the mail goes unsigned: the
same day, in the yardstick, a routine's note to Mom signed "[Your name]" was
refused, and the model asked the user what to sign with instead of sending
it again, so the note never went. A blank anywhere else is refused.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent.toolset import mail
from tests import persona
from tests.gate.mailfake import Mailbox, connect


@pytest.mark.parametrize("body", [
    "Hi Kim,\n\nI'll send the photos tonight.\n\nThanks,\n[Your name]",
    "Best,\n<your name>", "Dear {name},", "Hello [Name],", "See you on [Insert date].",
    "Hi [Recipient's name],", "[First name] [Last name]", "Call me at [your phone number].",
    "[YOUR NAME]",
])
def test_a_blank_left_to_fill_is_found(body):
    assert mail.blank_in(body) is not None


@pytest.mark.parametrize("body", [
    "As the report says [1], it rained.", "[External] Re: invoice",
    "The [photos](https://photos.example/kim) are up.", "Maya <maya@contoso.example>",
    "Fixed in [JIRA-123].", "[attachment: photos.zip]", "Best,\nMaya", "[sic]",
    "[Yours truly]", "Your name is on the list.", "",
])
def test_brackets_that_are_no_blank_are_left_alone(body):
    """A footnote, a tag, a link's text or an address in angle brackets is
    ordinary mail; refusing it would stop a good draft."""
    assert mail.blank_in(body) is None


@pytest.mark.parametrize(("body", "sent"), [
    ("Hi Kim,\n\nTonight.\n\nThanks,\n[Your name]", "Hi Kim,\n\nTonight.\n\nThanks,"),
    ("Love,\n[Your name]\n", "Love,"),
    ("Best,\n[Your Name]\n[Your title]\n\n", "Best,"),
    ("Best,\n<your name>", "Best,"),
])
def test_a_sign_off_made_only_of_blanks_is_left_out(body, sent):
    assert mail.unsigned(body) == sent


@pytest.mark.parametrize("body", [
    "Best,\nMaya\n", "Thanks,\n[Your name], Contoso", "See you on [Insert date].\n\nBest,",
    "Dear {name},\n\nThanks.", "",
])
def test_a_body_with_no_sign_off_of_blanks_is_left_as_written(body):
    """A blank beside other words, or above the sign-off, stands for
    something the model must fill; that one is refused, not cut."""
    assert mail.unsigned(body) == body


def _tools() -> dict:
    registered: dict = {}
    mail.register(lambda fn: registered.setdefault(fn.__name__, fn))
    return registered


@pytest.mark.parametrize("name", ["draft_email", "send_email"])
async def test_the_drafting_and_sending_tools_refuse_a_blank_before_anything_is_written(
        name, monkeypatch):
    """The refusal says nothing was queued, and what to do with the blank:
    what it stands for, if the model knows, or nothing."""
    async def must_not(*_a, **_k):
        raise AssertionError("it went on to write the email")

    monkeypatch.setattr(mail, "act", must_not)
    ctx = SimpleNamespace(deps=SimpleNamespace(tools_used=[]))

    result = await _tools()[name](ctx, to="kim@contoso.example", subject="Re: Saturday photos",
                                  body="Hi Kim,\n\nSee you on [Insert date].\n\nThanks,")

    assert result == {"ok": False, "error": (
        "Nothing was written or queued: the body has a blank, [Insert date], that would go"
        " out as written. Call this again with it filled in from what you know, or with it"
        " left out.")}


async def test_a_draft_signed_with_a_blank_is_written_unsigned(monkeypatch):
    written: list[dict] = []

    async def act(ctx, name, args, **_):
        written.append(args)
        return {"ok": True}

    monkeypatch.setattr(mail, "act", act)
    connect(monkeypatch, gmail=Mailbox(persona.EMAIL))
    ctx = SimpleNamespace(deps=SimpleNamespace(tools_used=[], conn=None))

    result = await _tools()["draft_email"](ctx, to="kim@contoso.example",
                                           subject="Re: Saturday photos",
                                           body="Hi Kim,\n\nTonight.\n\nThanks,\n[Your name]")

    assert result == {"ok": True}
    assert [a["body"] for a in written] == ["Hi Kim,\n\nTonight.\n\nThanks,"]


@pytest.mark.parametrize("executor", [mail._do_draft_email, mail._do_send_email])
async def test_an_executor_refuses_a_blank_and_sends_a_blank_sign_off_unsigned(
        executor, monkeypatch):
    """For a card made before the tools held them: approved, it would still
    send the blank."""
    from agent import approvals

    box = Mailbox(persona.EMAIL)
    connect(monkeypatch, gmail=box)

    with approvals.approved():
        refused = await executor(to="kim@contoso.example", subject="Re: Saturday photos",
                                 body="See you on [Insert date].\n\nThanks,")
        went = await executor(to="kim@contoso.example", subject="Re: Saturday photos",
                              body="Thanks,\n[Your name]")

    assert refused["ok"] is False and "[Insert date]" in refused["error"]
    assert went.get("ok") is not False, went
    assert [m.body for m in box.items.values()] == ["Thanks,"]
