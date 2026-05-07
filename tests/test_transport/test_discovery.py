"""Tests for transport.discovery — Pyre/Zyre announces only the NATS URL.

Per ADR 0001, discovery is the *only* role left for Pyre/Zyre. The announce
payload must contain the coordinator's NATS URL and nothing else — task and
result traffic rides NATS.
"""

from __future__ import annotations

import pytest

from turing.transport.discovery import NatsUrlAnnouncement


def test_announcement_headers_contain_only_nats_url() -> None:
    ann = NatsUrlAnnouncement(nats_url="nats://10.0.0.5:4222")

    headers = ann.to_headers()

    assert headers == {"nats_url": "nats://10.0.0.5:4222"}


def test_announcement_round_trips_through_headers() -> None:
    ann = NatsUrlAnnouncement(nats_url="tls://10.0.0.5:4222")
    restored = NatsUrlAnnouncement.from_headers(ann.to_headers())

    assert restored == ann


def test_announcement_ignores_unrelated_headers_when_parsing() -> None:
    headers = {"nats_url": "nats://10.0.0.5:4222", "legacy_capabilities": "ignored"}
    restored = NatsUrlAnnouncement.from_headers(headers)

    assert restored.nats_url == "nats://10.0.0.5:4222"


def test_announcement_rejects_missing_url() -> None:
    with pytest.raises(ValueError):
        NatsUrlAnnouncement.from_headers({"legacy_capabilities": "x"})
