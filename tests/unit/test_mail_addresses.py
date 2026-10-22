"""An address format is never written to as if it were an address.

On 8 October 2026 the user said "lastname.firstname@northwind.example is the
format" for a thank-you to Owen Tate. The task kept the format, and a
Contoso draft was saved addressed to "lastname.firstname@northwind.example",
exactly as written. A model can be told not to; this holds it in code, at
the tools and again at their executors.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent.toolset import mail


@pytest.mark.parametrize("to", [
    "lastname.firstname@northwind.example", "first.last@fabrikam.example",
    "firstname@tailspin.example", "f.last@contoso.example",
    "first_name.last_name@x.example.org", "FirstName.LastName@Northwind.example",
    "Owen Tate <lastname.firstname@northwind.example>", "a@b.example, first.last@fabrikam.example",
])
def test_a_format_in_place_of_an_address_is_caught(to):
    assert mail.placeholder_address(to) is not None


@pytest.mark.parametrize("to", [
    "tate.owen@northwind.example", "asha.shah@fabrikam.example", "j.smith@contoso.example",
        "f.l@x.example",
    "maya@contoso.example", "lastly@x.example", "names.office@x.example", "Owen Tate", "",
])
def test_a_real_address_is_left_alone(to):
    assert mail.placeholder_address(to) is None


def _tools() -> dict:
    registered: dict = {}
    mail.register(lambda fn: registered.setdefault(fn.__name__, fn))
    return registered


@pytest.mark.parametrize("name", ["draft_email", "send_email"])
async def test_the_drafting_and_sending_tools_refuse_it_before_anything_is_written(
        name, monkeypatch):
    async def must_not(*_a, **_k):
        raise AssertionError("it went on to write the email")

    monkeypatch.setattr(mail, "act", must_not)
    ctx = SimpleNamespace(deps=SimpleNamespace(tools_used=[]))

    result = await _tools()[name](ctx, to="lastname.firstname@northwind.example",
                                  subject="Thank you",
                                  body="Thanks for the chat.", account="outlook")

    assert result["ok"] is False and "the person's own address" in result["error"]
    # It says where a real address is, not how to build one from a format:
    # an address built from a pattern is mail to a stranger, or to the wrong
    # person of that name.
    assert "ortiz.sam" not in result["error"]


async def test_an_executor_refuses_it_too():
    """For a card made before the tools refused one."""
    result = await mail._do_draft_email(to="lastname.firstname@northwind.example",
                                        subject="Thank you",
                                        body="Thanks.", account="outlook")

    assert result["ok"] is False
