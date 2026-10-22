"""Gmail thread reads reject stale or non-Gmail identifiers before they derail a turn."""

from __future__ import annotations

import json

from googleapiclient.errors import HttpError

from modules.google_cloud import gmail


class _Request:
    def __init__(self, response=None, error=None):
        self.response = response or {"messages": []}
        self.error = error

    def execute(self):
        if self.error:
            raise self.error
        return self.response


class _Threads:
    def __init__(self, request, captured):
        self.request = request
        self.captured = captured

    def get(self, **kwargs):
        self.captured.update(kwargs)
        return self.request


class _Users:
    def __init__(self, threads):
        self._threads = threads

    def threads(self):
        return self._threads


class _Service:
    def __init__(self, request, captured):
        self._users = _Users(_Threads(request, captured))

    def users(self):
        return self._users


class _Response:
    status = 400
    reason = "Bad Request"


def _invalid_id_error():
    return HttpError(
        _Response(),
        json.dumps({"error": {"code": 400, "message": "Invalid id value"}}).encode(),
    )


def test_invalid_id_response_directs_the_caller_to_search_again():
    """Google's opaque 400 should lead to a fresh search instead of repeating the bad read."""
    captured = {}
    service = _Service(_Request(error=_invalid_id_error()), captured)
    original = gmail._gmail
    gmail._gmail = lambda: service
    try:
        result = gmail.thread("deadbeef")
    finally:
        gmail._gmail = original

    assert "could not be found" in result["error"].lower()
    assert "search" in result["error"].lower()
    assert result["kind"] == "not_found"
    assert captured["id"] == "deadbeef"


def test_valid_thread_ids_are_trimmed_before_the_provider_request():
    """Whitespace around a copied id is harmless, but passing it through is not."""
    captured = {}
    service = _Service(_Request(), captured)
    original = gmail._gmail
    gmail._gmail = lambda: service
    try:
        result = gmail.thread("  18c9a0b7d3e4f5a6  ")
    finally:
        gmail._gmail = original

    assert captured["id"] == "18c9a0b7d3e4f5a6"
    assert result["thread_id"] == "18c9a0b7d3e4f5a6"


def test_malformed_thread_ids_are_rejected_without_a_provider_request():
    """A contact name was once passed as an id, so validate locally before Gmail sees it."""
    captured = {}
    service = _Service(_Request(), captured)
    original = gmail._gmail
    gmail._gmail = lambda: service
    try:
        result = gmail.thread("Hugo Lind <hugo@x.example>")
    finally:
        gmail._gmail = original

    assert not captured
    assert result["kind"] == "invalid_input"
    assert "search" in result["error"].lower()


def test_other_provider_errors_keep_the_existing_google_error_result():
    """Only Google's invalid-id response is translated; unrelated failures keep their detail."""
    class _ServerResponse:
        status = 500
        reason = "backend unavailable"

    captured = {}
    error = HttpError(_ServerResponse(), b"backend unavailable")
    service = _Service(_Request(error=error), captured)
    original = gmail._gmail
    gmail._gmail = lambda: service
    try:
        result = gmail.thread("deadbeef")
    finally:
        gmail._gmail = original

    assert result["status"] == 500
    assert result["kind"] == "http"
