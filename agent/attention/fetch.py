"""The one door to the public web for the attention loop's feeds.

Weather and news come from free, keyless public endpoints -- Open-Meteo,
the National Weather Service, Google News RSS -- fetched from a worker
thread (sources are blocking by design). Every fetch goes through here so
there is one seam to shut in tests (`tests/unit/conftest.py` refuses it)
and one place that sets what a polite client must: a user agent the NWS
asks for, a timeout, redirects followed (Google's section feeds answer with
a 302), and an error that says which feed failed.

Nothing about the user leaves through here except their home coordinates, which
are a city's, and the topics the user follows. A calendar's private link is not
read here: it is an account's, read through its module's context
(`core/engines/ics.py`).
"""

from __future__ import annotations

import httpx2

from core import identity

# The NWS refuses requests without one and asks that it identify the
# application. No contact address: it would be the user's.
USER_AGENT = identity.user_agent("personal assistant; one user")

TIMEOUT_S = 20.0


class FetchError(RuntimeError):
    """A feed could not be read. Carries the feed's name and the reason."""


def _get(url: str, params: dict | None, accept: str,
         headers: dict | None = None) -> httpx2.Response:
    try:
        response = httpx2.get(
            url,
            params=params,
            headers={"User-Agent": USER_AGENT, "Accept": accept, **(headers or {})},
            timeout=TIMEOUT_S,
            follow_redirects=True,
        )
    except httpx2.HTTPError as exc:
        raise FetchError(f"{url}: {type(exc).__name__}: {exc}") from exc
    if response.status_code != 200:
        # The body usually says why (the NWS returns a problem document);
        # a bare status code is the silent failure protocols §11 warns of.
        raise FetchError(f"{url}: HTTP {response.status_code}: {response.text[:200]}")
    return response


def get_json(url: str, params: dict | None = None, *, headers: dict | None = None) -> dict:
    """`headers` for the one authenticated read, OpenRouter's balance
    (`sources/health.py`); the rest are keyless.

    A reply that is not a JSON object is a feed that could not be read,
    like a refused one: a captive portal's login page, or a proxy's error,
    answers 200 with HTML, and raised a decoding error no caller expected."""
    response = _get(url, params, "application/json, application/geo+json", headers)
    try:
        data = response.json()
    except ValueError:
        raise FetchError(f"{url}: not JSON: {response.text[:200]!r}") from None
    if not isinstance(data, dict):
        raise FetchError(f"{url}: not a JSON object: {type(data).__name__}")
    return data


def get_text(url: str, params: dict | None = None) -> str:
    return _get(url, params, "application/rss+xml, application/xml, text/xml").text
