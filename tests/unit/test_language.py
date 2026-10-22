"""The language Steve writes in and the region it reads the world for
(core/language.py): Windows' own until the user chooses, and asked by each
place that was fixed to US English -- the news edition, the web's pages,
the sandboxed browser, web search, and what the models write.

The fences pin Windows' answer to a US-English one for every test; these
tests give their own.
"""

from __future__ import annotations

import pytest

from agent.attention.sources import news
from core import knobs, language, websearch
from core.language import _ask_windows as ASK_WINDOWS  # before any fence replaces it
from tools import web


@pytest.fixture
def windows(monkeypatch):
    """Windows answering as told."""

    def says(asked_language, asked_region):
        monkeypatch.setattr(language, "_ask_windows", lambda: (asked_language, asked_region))
        language.windows.cache_clear()

    yield says
    language.windows.cache_clear()


def test_windows_own_language_and_region_stand_until_the_user_chooses(windows):
    """Setup doesn't ask: a French Windows is answered in French from the
    first message, and a choice in Settings counts at the next request,
    with no restart."""
    windows("fr-FR", "FR")
    assert language.chosen() == ("fr-FR", "FR")

    knobs.write({"LANGUAGE": "de-DE"})
    assert language.chosen() == ("de-DE", "FR")
    knobs.write({"REGION": "AT"})
    assert language.chosen() == ("de-DE", "AT")
    knobs.write({"LANGUAGE": None, "REGION": None})
    assert language.chosen() == ("fr-FR", "FR")


@pytest.mark.parametrize(("asked", "taken"), [
    (("fr-FR", "FR"), ("fr-FR", "FR")),
    (("ca-ES-valencia", "ES"), ("ca-ES", "ES")),
    (("zh-Hans-CN", "CN"), ("zh-Hans-CN", "CN")),
    (("en-GB", "001"), ("en-GB", "GB")),
    (("es-419", "419"), ("es-419", "US")),
    ((None, None), ("en-US", "US")),
    (("not a language", "france"), ("en-US", "US")),
])
def test_what_windows_says_is_taken_only_in_a_shape_steve_can_send(windows, asked, taken):
    """Windows' answer goes into a header, a feed's address and a prompt.
    A variant it adds is dropped rather than the language; a region it
    names by number ("001", the world) becomes the language's own; and
    anything else becomes US English, as everything was before."""
    windows(*asked)
    assert language.windows() == taken


def test_a_windows_that_cannot_say_is_taken_as_us_english(monkeypatch):
    def fails():
        raise OSError("no such function in this kernel32")

    monkeypatch.setattr(language, "_ask_windows", fails)
    language.windows.cache_clear()
    try:
        assert language.windows() == language.FALLBACK
    finally:
        language.windows.cache_clear()


def test_this_windows_names_a_language_and_region_steve_can_send(monkeypatch):
    """The real read, without administrator rights: whatever this laptop's
    Windows is in, what it says fits the patterns the knobs hold."""
    asked_language, asked_region = ASK_WINDOWS()
    assert asked_language and language.LANGUAGE.fullmatch(language._fit(asked_language) or "")
    assert asked_region
    assert language.name("fr-FR") == "French (France)"
    assert language.name("zz-ZZ") == "zz-ZZ"


@pytest.mark.parametrize(("name", "given"), [
    ("LANGUAGE", "french"), ("LANGUAGE", "fr_FR"), ("LANGUAGE", "FR-fr"), ("LANGUAGE", 7),
    ("LANGUAGE", "fr-FR; rm -rf"), ("REGION", "usa"), ("REGION", "us"), ("REGION", "001"),
    ("REGION", ""),
])
def test_a_language_or_region_out_of_shape_is_refused(name, given):
    """Each is sent as is, in a header and an address: a value with a
    space, a semicolon or a newline in it would carry whatever it holds."""
    with pytest.raises(knobs.BadKnob):
        knobs.value(name, given)


def test_the_news_is_the_users_edition(windows):
    """It was Google News' American edition for everyone."""
    windows("en-US", "US")
    assert news.feed("section:WORLD")[1] == {"hl": "en-US", "gl": "US", "ceid": "US:en"}

    knobs.write({"LANGUAGE": "fr-FR", "REGION": "FR"})
    url, params = news.feed("climate")
    assert url == news.SEARCH
    assert params == {"hl": "fr-FR", "gl": "FR", "ceid": "FR:fr", "q": "climate when:1d"}


def test_pages_are_asked_for_in_the_users_language(windows):
    """A site with a French page gave a French user its English one."""
    windows("fr-FR", "FR")
    asked = web._headers_for("https://www.example.fr/", "browser")
    assert asked["Accept-Language"] == "fr-FR,fr;q=0.9,en;q=0.8"
    assert web.BROWSER_HEADERS["Accept-Language"] == "en-US,en;q=0.9"
    # Wikimedia is sent only the user agent its policy asks for.
    assert "Accept-Language" not in web._headers_for("https://fr.wikipedia.org/", "browser")


def test_searches_are_for_the_users_country(windows):
    """With no country, "bank holidays" was answered for the US wherever
    the user was. Brave is sent a country only where it searches by one:
    any other is refused, and the search would go to OpenAI, slower."""
    windows("fr-FR", "FR")
    tool = websearch.request_for("bank holidays", 5)["tools"][0]
    assert tool["user_location"] == {"type": "approximate", "country": "FR"}
    assert websearch.brave_params("bank holidays", 5) == {
        "q": "bank holidays", "count": 5, "country": "FR"}

    knobs.write({"REGION": "IS"})
    assert websearch.request_for("bank holidays", 5)["tools"][0]["user_location"]["country"] == "IS"
    assert websearch.brave_params("bank holidays", 5) == {"q": "bank holidays", "count": 5}


@pytest.mark.parametrize(("tag", "header"), [
    ("en-US", "en-US,en;q=0.9"),
    ("fr-FR", "fr-FR,fr;q=0.9,en;q=0.8"),
    ("fr", "fr,en;q=0.9"),
    ("en", "en"),
])
def test_a_header_asks_for_the_language_then_english(tag, header):
    assert language.accept_language(tag) == header


def test_the_models_are_told_the_users_language_by_name(windows):
    windows("fr-FR", "FR")
    said = language.instruction()
    assert "French (France) (fr-FR)" in said
    assert "unless they write to you in another language" in said
