"""Jev, faked: answers by question name, in the shape the real service replies
in (tests/gate/fixtures/decisions/captured.json).

One fake for every site that asks a decision. Each site had its own fake of
the old call, built to that site's payload, so a change of service meant a
change to each. This one reads a request as the service does: a predicate
(`noul`) is answered with a probability, a choice with a value and a
probability for each.
"""

from __future__ import annotations

from core.decide import PRICE_PER_M_INPUT, Reply


class Decisions:
    """Stands in for `core.decide._post`.

    `answers` by question name: a number is a predicate's probability; a
    value is a choice made surely; a dict of value to probability is a
    choice of the likeliest; a function is given the input and answers as
    any of these. A question with no entry, or a function's None, is left
    unanswered. `failures` come first, one a call, and `down`, while set, is
    every call's: a `Reply` is returned and an exception raised, as the
    transport's would be. `asked` keeps every request, and each reply
    reports `tokens` of input, at Jev's price."""

    def __init__(self, *, failures: list | None = None, down=None, tokens: int = 200,
                 **answers) -> None:
        self.answers = answers
        self.failures = list(failures or [])
        self.down = down
        self.tokens = tokens
        self.asked: list[dict] = []

    @property
    def inputs(self) -> list[str]:
        """What each request asked about."""
        return [payload["state"] for payload in self.asked]

    def __call__(self, payload: dict, key: str) -> Reply:
        self.asked.append(payload)
        failure = self.down if self.down is not None else (
            self.failures.pop(0) if self.failures else None)
        if isinstance(failure, BaseException):
            raise failure
        if failure is not None:
            return failure
        answers = {}
        for name, question in payload["questions"].items():
            given = self._given(name, payload["state"])
            if given is not None:
                answers[name] = _answer(question, given)
        return Reply(200, {
            "model": f"{payload['model']}-20260917",
            "answers": answers,
            "usage": {"input_tokens": self.tokens, "output_tokens": 20,
                      "cost": self.tokens * PRICE_PER_M_INPUT / 1_000_000},
            "provider": "TypeSafe",
        })

    def _given(self, name: str, input: str):
        given = self.answers.get(name)
        return given(input) if callable(given) else given


def _answer(question: dict, given) -> dict:
    if question["type"] == "noul":
        return {"type": "noul", "noul": float(given)}
    values = list(question["criteria"])
    chances = given if isinstance(given, dict) else {v: float(v == given) for v in values}
    choice = max(chances, key=chances.get)
    return {"type": "choice", "choice": choice,
            "probabilities": {v: chances.get(v, 0.0) for v in values},
            "confidence": chances[choice]}


def install(monkeypatch, **kwargs) -> Decisions:
    """The fake, in place of the service, for one test."""
    fake = Decisions(**kwargs)
    monkeypatch.setattr("core.decide._post", fake)
    return fake
