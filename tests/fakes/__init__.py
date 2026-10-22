"""Fakes of real servers, for the engines' tests (05-connectors.md, section 8).

They live here, not in `steve_sdk`, because they need what a module's
author does not: `trustme` for a certificate authority made for the test,
and `aiosmtpd`. Each runs on loopback, which the socket fence allows, and
each is a `steve_sdk.testing.World`, so one contract suite drives them all.
"""
