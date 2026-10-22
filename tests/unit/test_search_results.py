"""A mail search hands the model what it can use, not what it must read past.

On 6 October 2026 a ten-result search carried each message's time three
ways, Gmail's ids for their labels, and newsletter snippets padded with
hundreds of invisible characters and undecoded entities.
"""

from __future__ import annotations

from agent.toolset.mail import for_the_model, localise

NEWSLETTER = {
    "id": "1a1119d2543bb1a3", "thread_id": "1a1119d2543bb1a3",
    "from": "Ben at Fieldnotes <fieldnotes@substack.com>",
    "subject": "We&#39;re hiring &amp; more",
    "snippet": "roles at breakout startups " + "͏ " * 120 + "and &quot;more&quot;",
    "labels": ["IMPORTANT", "CATEGORY_UPDATES", "INBOX", "Label_1", "Label_2", "UNREAD"],
    "unread": True, "internal_date": 1791296759000,
    "date": "Tue, 6 Oct 2026 14:25:59 +0000", "mailbox": "gmail",
}


def test_a_newsletter_snippet_loses_its_padding_and_its_entities():
    [message] = for_the_model(localise([dict(NEWSLETTER)]))

    assert message["snippet"] == 'roles at breakout startups and "more"'
    assert message["subject"] == "We're hiring & more"


def test_the_time_is_given_once_and_the_labels_that_mean_something_are_kept():
    [message] = for_the_model(localise([dict(NEWSLETTER)]))

    assert "when" in message and "internal_date" not in message and "date" not in message
    assert message["labels"] == ["IMPORTANT", "CATEGORY_UPDATES", "INBOX"]
    assert "thread_id" not in message, "the same id twice"
    assert message["unread"] is True and message["id"] == "1a1119d2543bb1a3"
    assert len(str(message)) < len(str(NEWSLETTER)) / 2


def test_a_message_without_a_readable_time_keeps_the_one_it_has():
    [message] = for_the_model([{"id": "outlook#1", "thread_id": "T1", "date": "yesterday",
                                "labels": ["Label_3"]}])

    assert message == {"id": "outlook#1", "thread_id": "T1", "date": "yesterday"}


def test_only_the_model_s_copy_is_trimmed():
    """The attention loop reads `internal_date` from the same messages."""
    original = localise([dict(NEWSLETTER)])

    for_the_model(original)

    assert original[0]["internal_date"] == 1791296759000
