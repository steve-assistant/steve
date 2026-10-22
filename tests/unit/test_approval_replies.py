"""Reading "yes" from a phone.

The bug this file exists to prevent: a bare "yes" arriving while two cards are
waiting, and the system picking one. That failure is completely silent — the
right-looking thing happens, to the wrong recipient, and nothing anywhere
reports an error. So the rule is that ambiguity is answered with a question,
never with a guess.

The other half is knowing when *not* to intercept. This runs before the model
sees the message, which is what makes an approval impossible to express as
model output — and also means anything it swallows by mistake is a turn the user
never gets an answer to.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from agent import cardwords
from agent.approvals import Action, parse_reply
from core import monitor
from core.consent import TOKEN_WORDS


def _waiting(*tokens: str) -> list[Action]:
    return [
        Action(
            id=i + 1, tool="send_email", args={}, preview=f"Email about thing {i + 1}",
            status="pending", token=token, reach="outward", reversal="irreversible",
            reasons=[], spend_usd=0.0, expires_at=None,
        )
        for i, token in enumerate(tokens)
    ]


@pytest.mark.parametrize("said", [
    "add pepper to the list", "buy almond milk", "the harbor tour at 5",
    "meet at the anchor bar", "is the copper pot clean", "I can't fathom why",
    "walnut or almond cake?", "drift wood for the table", "a hollow excuse",
    "Juniper Cafe at noon", "Harbor Hotel, room 12", "the willow by the lake",
])
def test_an_ordinary_short_message_is_not_taken_for_an_answer_whatever_waits(said):
    """A short message naming a waiting card's word is asked about, and
    said at more length it reaches the model with the word taken out: with
    PEPPER, ALMOND or HARBOR waiting, "add pepper to the list" was never
    heard (step 11's fourth review). So no card's word is one an ordinary
    message says by chance (core/consent.TOKEN_WORDS)."""
    every = _waiting(*(word.upper() for word in TOKEN_WORDS))

    assert parse_reply(said, every) is None


# --------------------------------------------------------------------------
# The ambiguity
# --------------------------------------------------------------------------


def test_a_bare_yes_with_two_waiting_asks_rather_than_guessing():
    """The bug this file is named for. Approving the wrong card is silent
    until the moment somebody receives something they should not have."""
    decision = parse_reply("yes", _waiting("MAPLE", "COBALT"))

    assert decision is not None
    assert decision.action_id is None, "it picked one"
    assert "MAPLE" in decision.reply and "COBALT" in decision.reply


def test_a_bare_yes_with_one_waiting_approves_it():
    """This has to work, or the whole thing feels bureaucratic. One card and
    "yes" means yes."""
    decision = parse_reply("yes", _waiting("MAPLE"))

    assert decision.action_id == 1
    assert decision.approved is True


def test_a_token_wins_even_with_several_waiting():
    decision = parse_reply("yes cobalt", _waiting("MAPLE", "COBALT"))

    assert decision.action_id == 2
    assert decision.approved is True


def test_a_token_alone_is_not_enough():
    """Naming a card is not the same as answering about it. It is asked
    about, saying what the card is: it went to the model, and an answer the
    verbs did not know went with it (step 11's third review)."""
    decision = parse_reply("cobalt", _waiting("MAPLE", "COBALT"))

    assert decision is not None and decision.action_id is None
    assert "YES COBALT" in decision.reply and "Email about thing 2" in decision.reply


def test_an_unknown_token_does_not_fall_back_to_the_bare_yes_path():
    """"YES COBALT" when COBALT already expired must not approve MAPLE
    instead. The user named something; answer about that, not about whatever
    happens to be left."""
    decision = parse_reply("yes cobalt", _waiting("MAPLE"))
    assert decision is None or decision.action_id != 1


def test_a_press_or_a_reaction_decides_the_card_it_is_on_or_none():
    """A thumbs-up on a card answered an hour ago reads "YES MAPLE", and
    MAPLE may since have gone to a newer card: it decided that card, which
    the user had not been shown (step 11's audit). Bound to its card, it
    is told MAPLE is not waiting, even when nothing else is."""
    newer = _waiting("COBALT", "MAPLE")[1:]  # card 2 drew MAPLE after card 1 was answered

    late = parse_reply("YES MAPLE", newer, card=1)
    alone = parse_reply("YES MAPLE", [], card=1)
    own = parse_reply("YES MAPLE", newer, card=2)

    assert late is not None and late.action_id is None and "MAPLE waiting" in late.reply
    assert alone is not None and alone.action_id is None
    assert own is not None and (own.action_id, own.approved) == (2, True)


# --------------------------------------------------------------------------
# Yes and no
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "word", ["yes", "y", "yep", "yes please", "Yes, please", "send it", "go ahead", "approve", "👍"]
)
def test_the_ways_the_user_might_say_yes(word):
    assert parse_reply(word, _waiting("MAPLE")).approved is True


@pytest.mark.parametrize("word", ["ok", "okay", "sure", "go"])
def test_an_acknowledgement_is_not_an_approval(word):
    """How the user says "understood", not how the user says "send it". With the card's
    word they still count -- "ok maple" can only mean one thing."""
    assert parse_reply(word, _waiting("MAPLE")) is None
    assert parse_reply(f"{word} maple", _waiting("MAPLE")).approved is True


@pytest.mark.parametrize(
    "text",
    [
        "send me the notes",
        "what time do we go",
        "ok thanks",
        "sure, what time?",
        "yes I saw that",
        "no worries",
        "stop by later",
        "don't forget the milk",
    ],
)
def test_a_short_message_that_merely_contains_a_yes_is_a_message(text):
    """The first version took any of these as an answer: a yes-word anywhere
    in six words or fewer, with one card waiting, sent it. "send me the
    notes" would have sent the user's pending email."""
    assert parse_reply(text, _waiting("MAPLE")) is None


def test_a_bare_yes_after_the_conversation_moved_on_is_asked_about():
    """The user wrote something since the card, so the latest question may be the
    model's. Guessing would send the email; handing it to the model would
    let the model claim it had. It asks, and names the card."""
    decision = parse_reply("yes", _waiting("MAPLE"), bare_ok=False)

    assert decision.action_id is None
    assert "YES MAPLE" in decision.reply


def test_naming_the_card_works_however_long_ago_it_was_asked():
    decision = parse_reply("yes maple", _waiting("MAPLE"), bare_ok=False)

    assert decision.action_id == 1
    assert decision.approved is True


def test_a_card_word_in_passing_is_not_swallowed():
    """"the maple one" names no waiting card and answers nothing. Replying
    "I don't have a MAPLE waiting" would eat the message."""
    assert parse_reply("the maple one", _waiting("COBALT")) is None
    assert "MAPLE" in parse_reply("yes maple", _waiting("COBALT")).reply


@pytest.mark.parametrize("word", ["no", "n", "nope", "stop", "cancel", "reject", "👎"])
def test_the_ways_the_user_might_say_no(word):
    assert parse_reply(word, _waiting("MAPLE")).approved is False


@pytest.mark.parametrize("text", ["OK, no MAPLE", "ok no to maple", "sure, no maple",
                                  "no, go maple", "maple yes no"],
                         ids=["ok-no", "ok-no-to", "sure-no", "no-go", "yes-no"])
def test_a_reply_with_both_a_yes_and_a_no_asks_which_was_meant(text):
    """"OK, no MAPLE" approved the card: the first verb was taken, and "ok"
    came first (step 11's audit). Either reading of such a reply could be
    the wrong one, and one of them acts."""
    decision = parse_reply(text, _waiting("MAPLE"))

    assert decision is not None and decision.action_id is None
    assert "YES MAPLE" in decision.reply and "NO MAPLE" in decision.reply


@pytest.mark.parametrize("text", ["Don’t send MAPLE", "Don‘t send maple",
                                  "Do not send MAPLE", "never send maple", "no! go maple",
                                  "no. send maple", "No!Go maple"],
                         ids=["curly-dont", "left-curly-dont", "do-not", "never", "no-bang-go",
                              "no-stop-send", "no-bang-go-tight"])
def test_a_refusal_however_it_is_typed_never_approves(text):
    """"Don’t send MAPLE", typed on a phone whose keyboard curls the
    apostrophe, approved the card: the curly word was no word of refusal,
    and "send" was the only verb read. So did "Do not send MAPLE", and "no!
    go maple", whose "no!" was not the word "no" (step 11's review)."""
    decision = parse_reply(text, _waiting("MAPLE"))

    assert decision is not None and decision.approved is not True


# The second review's probe (step 11): each of these approved the card.
_NOT_A_PLAIN_YES = [
    ("Shouldn\u2019t send MAPLE", "shouldnt"), ("Can't send maple", "cant"),
    ("Don`t send MAPLE", "backtick"), ("Don\u00b4t send MAPLE", "acute"),
    ("Don\u02bct send MAPLE", "modifier-apostrophe"), ("Don\uff07t send MAPLE", "fullwidth"),
    ("\u2018no\u2019 send maple", "quoted-no"), ("\uff2e\uff2f send maple", "fullwidth-no"),
    ("\U0001F6AB send maple", "no-entry"), ("did you send maple?", "did-you"),
    ("Send MAPLE?", "question"), ("Don\u2019t send MAPLE", "curly-dont"),
    ("yes?", "bare-question"), ("send\u200b maple, not", "hidden-space-not"),
]


@pytest.mark.parametrize("text", [t for t, _ in _NOT_A_PLAIN_YES],
                         ids=[i for _, i in _NOT_A_PLAIN_YES])
def test_a_reply_that_says_more_than_yes_never_approves(text):
    """A yes approved unless one of a list of refusals was found in it, and
    each review found another way to refuse that the list did not hold: a
    contraction, an apostrophe from another keyboard, full-width letters, an
    emoji, a question (step 11's second review). Now a reply naming the
    card approves only when every word in it is a yes, the card's word, a
    filler or the code."""
    decision = parse_reply(text, _waiting("MAPLE"))

    assert decision is None or decision.approved is not True


@pytest.mark.parametrize("text", ["ＮＯ ＭＡＰＬＥ", "Don´t MAPLE",
                                  "‘no’ maple", "Don`t, maple"],
                         ids=["fullwidth", "acute", "quoted", "backtick"])
def test_a_refusal_however_it_is_dressed_refuses(text):
    """Asked about, these would be safe; but they say no, and the card
    should be dropped, whatever keyboard or font wrote them."""
    decision = parse_reply(text, _waiting("MAPLE"))

    assert decision is not None and decision.approved is False, decision


@pytest.mark.parametrize("text, bare_ok", [
    ("yes", True), ("YES MAPLE", True), ("yes maple", True), ("send it", True),
    ("go ahead please", True), ("go ahead please maple", True), ("Yes, MAPLE, thanks!", True),
    ("\U0001F44D\U0001F3FD maple", True), ("\u2705\ufe0f", True), ("YES MAPLE 123 456", True),
    ("ok maple", True), ("send maple now", True),
], ids=["yes", "yes-word", "yes-word-lower", "send-it", "go-ahead-please",
        "go-ahead-please-word", "yes-word-thanks", "thumbs-skin-tone", "check-selector",
        "with-code", "ok-word", "send-now"])
def test_a_plain_yes_still_approves(text, bare_ok):
    """The allowlist must not make the yes bureaucratic: what users type to
    approve, with an emoji's skin tone or the authenticator code, goes."""
    decision = parse_reply(text, _waiting("MAPLE"), bare_ok=bare_ok)

    assert decision is not None and decision.approved is True, decision


@pytest.mark.parametrize("text, approved", [("ok maple", True), ("no to maple", False),
                                            ("maple, go", True), ("nope, maple", False)],
                         ids=["ok", "no-to", "go", "nope"])
def test_a_reply_with_one_kind_of_verb_is_still_decided(text, approved):
    assert parse_reply(text, _waiting("MAPLE")).approved is approved


def test_punctuation_and_case_do_not_matter():
    assert parse_reply("Yes.", _waiting("MAPLE")).approved is True
    assert parse_reply("YES MAPLE", _waiting("MAPLE")).approved is True


def test_undo_names_a_card():
    decision = parse_reply("undo maple", _waiting("MAPLE"))
    assert decision.action_id == 1
    assert decision.approved is False


# --------------------------------------------------------------------------
# When not to intercept
# --------------------------------------------------------------------------


def test_nothing_waiting_means_nothing_to_intercept():
    """Otherwise "yes" in ordinary conversation vanishes into the approval
    path and the user never gets a reply."""
    assert parse_reply("yes", []) is None


def test_a_sentence_that_merely_starts_with_yes_goes_to_the_model():
    """"yes, and also what's the weather" is a turn, not an approval. The
    intercept swallowing it would lose the question entirely."""
    assert parse_reply(
        "yes and also what is the weather like tomorrow", _waiting("MAPLE")
    ) is None


def test_an_ordinary_message_is_not_an_answer():
    assert parse_reply("what's on my calendar?", _waiting("MAPLE")) is None


def test_an_empty_message_is_not_an_answer():
    assert parse_reply("   ", _waiting("MAPLE")) is None


def test_a_message_about_a_card_is_not_an_answer_about_it():
    """"what is maple" names the token but answers nothing: it is told what
    MAPLE is, and how to answer it. A longer message reaches the model."""
    decision = parse_reply("what is maple", _waiting("MAPLE"))

    assert decision is not None and decision.action_id is None
    assert "Email about thing 1" in decision.reply and "say a little more" in decision.reply
    assert parse_reply("what is maple about and who asked for it", _waiting("MAPLE")) is None


# The third review's probe (step 11): each of these approved the card.
_STILL_NOT_A_PLAIN_YES = [
    ("Do you send maple", "do-you"), ("do send maple", "do-send"),
    ("Send MAPLE\u037e", "greek-question-mark"), ("send maple\uff1f", "fullwidth-question-mark"),
    ("yes maple \u2753", "question-ornament"), ("yes maple \u061f", "arabic-question-mark"),
    ("yes maple \u0664\u0668\u0662\u0669\u0661\u0663", "arabic-indic-digits"),
]


@pytest.mark.parametrize("text", [t for t, _ in _STILL_NOT_A_PLAIN_YES],
                         ids=[i for _, i in _STILL_NOT_A_PLAIN_YES])
def test_a_question_or_a_word_more_is_asked_about(text):
    """"do" and "you" were fillers each alone, so "Do you send maple" was a
    yes; a question mark NFKC turns into something else -- Greek's, into a
    semicolon -- was no question; and any script's digits were a code (step
    11's third review). Asked, never decided."""
    decision = parse_reply(text, _waiting("MAPLE"))

    assert decision is not None and decision.action_id is None, decision
    assert "YES MAPLE" in decision.reply


@pytest.mark.parametrize("text", [
    "yes mapl" + chr(0x0435), "y" + chr(0x0435) + "s m" + chr(0x0430) + "ple",
    "yes m" + chr(0x00E4) + "ple", "no " + chr(0x0441) + "obalt", chr(0x0441) + "obalt",
], ids=["cyrillic-e", "cyrillic-in-both", "accented", "a-no", "the-word-alone"])
def test_a_waiting_word_in_look_alike_letters_is_asked_about_and_decides_nothing(text):
    """"yes maple" with a Cyrillic e was not the word, so it went to the
    model, and the word with it (step 11's fourth review). It is about the
    card, so it is asked about, as the word with no plain answer is."""
    decision = parse_reply(text, _waiting("MAPLE", "COBALT"))

    assert decision is not None and decision.action_id is None and decision.approved is None
    named = "COBALT" if "obalt" in text else "MAPLE"
    assert f"YES {named}" in decision.reply and "say a little more" in decision.reply


def test_a_word_in_look_alike_letters_for_a_card_already_going_stops_nothing():
    """An UNDO that is not the word asks, and says how to stop it."""
    going = _waiting("MAPLE")

    decision = parse_reply("undo mapl" + chr(0x0435), [], holding=going)

    assert decision.action_id is None
    assert decision.reply == "MAPLE is already going -- say UNDO MAPLE to stop it."


@pytest.mark.parametrize("text", ["yep maple", "yeah maple", "approved maple",
                                  "send maple now thank you", "do it maple",
                                  "YES MAPLE \uff14\uff18\uff12\uff19\uff11\uff13"],
                         ids=["yep", "yeah", "approved", "thank-you", "do-it", "fullwidth-code"])
def test_the_yeses_the_bare_answer_knows_name_a_card_too(text):
    """"yep maple" went to the model: "yep" was a bare yes, not a verb with
    the card's word (step 11's third review)."""
    decision = parse_reply(text, _waiting("MAPLE"))

    assert decision is not None and decision.approved is True, decision


@pytest.mark.parametrize("text", ["send thanks", "thanks send", "approve thanks",
                                  "Send, thanks.", "please send", "go please ahead"],
                         ids=["send-thanks", "thanks-send", "approve-thanks", "punctuated",
                              "please-send", "polite-inside"])
def test_a_polite_word_beside_a_lone_verb_is_asked_about(text):
    """A polite word dropped anywhere left a bare answer: "send thanks",
    which may ask Steve to send someone thanks, approved the only card
    waiting (step 11's third review)."""
    decision = parse_reply(text, _waiting("MAPLE"))

    assert decision is None or decision.action_id is None, decision


@pytest.mark.parametrize("text", ["yes thanks", "please yes please", "send it thanks",
                                  "thanks, send it", "do it pls", "\u2705 thanks",
                                  "go ahead please", "yes please thanks"])
def test_a_polite_word_at_the_ends_of_a_bare_yes_leaves_it_a_yes(text):
    assert parse_reply(text, _waiting("MAPLE")).approved is True


# --------------------------------------------------------------------------
# The listing it sends back
# --------------------------------------------------------------------------


def test_the_relisting_shows_enough_to_choose_between_them():
    decision = parse_reply("yes", _waiting("MAPLE", "COBALT"))

    assert "thing 1" in decision.reply
    assert "thing 2" in decision.reply
    assert "YES" in decision.reply


def test_the_relisting_costs_no_model_call():
    """Deterministic on purpose: it works when the model is down, and cannot
    be talked out of asking."""
    decision = parse_reply("yes", _waiting("MAPLE", "COBALT"))
    assert decision.approved is None


# --------------------------------------------------------------------------
# The half-minute after a yes
# --------------------------------------------------------------------------


def _going(*tokens: str) -> list[Action]:
    actions = _waiting(*tokens)
    for a in actions:
        a.status = "executing"
        a.id += 100
    return actions


def test_undo_finds_a_card_that_is_already_going():
    """The bug this guards: an action stops being *pending* the instant it is
    approved, so an intercept that only looks at pending stops recognising it
    exactly during the thirty seconds UNDO is for. "UNDO MAPLE" would reach
    the model and the mail would go anyway."""
    decision = parse_reply("undo maple", [], _going("MAPLE"))

    assert decision.action_id == 101
    assert decision.approved is False


def test_a_second_yes_to_something_already_going_says_so():
    assert "UNDO" in parse_reply("yes maple", [], _going("MAPLE")).reply


def test_an_in_flight_card_does_not_make_a_bare_yes_ambiguous():
    """The user is not being asked about it, so it must not appear in the count --
    otherwise every approval turns the next one into a re-listing."""
    decision = parse_reply("yes", _waiting("COBALT"), _going("MAPLE"))

    assert decision.action_id == 1
    assert decision.approved is True


def test_a_bare_stop_with_only_in_flight_actions_goes_to_the_model():
    """Too blunt to spend an irreversible action on. The model can ask."""
    assert parse_reply("stop", [], _going("MAPLE")) is None


# --------------------------------------------------------------------------
# What a yes approves, whole
# --------------------------------------------------------------------------

# A routine's card: its first line spends fifty characters before whom it
# may reach.
ROUTINE_SHOWS = ("Each run of this routine may, without asking: " + monitor.describe_envelope(
    {"send_email": {"targets": ["mom@example.com", "zed@mail.example"]}}) + ".")


def _routine_and(*tokens: str) -> list[Action]:
    routine, *others = _waiting("MAPLE", *tokens)
    return [replace(routine, tool="create_routine",
                    preview=f"{ROUTINE_SHOWS}\nRoutine: Sunday note"), *others]


@pytest.mark.parametrize("reply, others", [
    ("what is maple", ()),        # asked which card it is about
    ("yes no maple", ()),         # asked yes or no
    ("yes", ("COBALT",)),         # asked which of several
    ("yes quartz", ()),           # told the one still open
    ("pending", ("COBALT",)),     # told what waits
])
def test_every_line_that_says_a_waiting_word_says_all_its_yes_approves(reply, others):
    """Cut at sixty, seventy or eighty characters, a yes to "email:
    mom@example.com, " approved zed@mail.example too (step 11's fourth
    review). Each such line is one the code writes for the card, which
    keeps its word on the way to the phone (agent/cardwords.py)."""
    waiting = _routine_and(*others)

    said = parse_reply(reply, waiting).reply

    [line] = [line for line in said.splitlines() if "MAPLE" in line]
    assert ROUTINE_SHOWS in line
    cards = [cardwords.Waiting(a.token, cardwords.first_line(a.preview)) for a in waiting]
    assert cardwords.redact(said, cards) == said


def test_pending_is_answered_by_the_code_whatever_waits():
    """The model reads no waiting card's word, so it cannot say what waits."""
    assert parse_reply("pending", []).reply == "Nothing is waiting for your yes."
    said = parse_reply("Pending?", _waiting("MAPLE", "COBALT")).reply
    assert said.splitlines() == ["Waiting for your yes:", "  MAPLE  Email about thing 1",
                                 "  COBALT  Email about thing 2"]
