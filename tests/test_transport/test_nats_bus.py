"""Tests for NatsBus.connect's LAN-only guard.

Live NATS connection round-trip lives in `tests/integration/` under the
`integration` marker.
"""

from __future__ import annotations

import pytest

from turing.transport.nats_bus import NatsBus, _is_lan_url


@pytest.mark.parametrize(
    "url",
    [
        "nats://127.0.0.1:4222",
        "nats://localhost:4222",
        "tls://10.0.0.5:4222",
        "tls://192.168.1.10:4222",
        "tls://172.16.0.5:4222",
        "tls://pi-alpha.local:4222",
    ],
)
def test_lan_urls_accepted(url: str) -> None:
    assert _is_lan_url(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "tls://nats.example.com:4222",
        "nats://8.8.8.8:4222",
        "tls://172.40.0.5:4222",  # 172.40 is outside 172.16/12 private range
    ],
)
def test_public_urls_rejected_by_lan_check(url: str) -> None:
    assert _is_lan_url(url) is False


async def test_connect_refuses_public_url_when_lan_only_true() -> None:
    with pytest.raises(ValueError, match="lan"):
        await NatsBus.connect(
            url="tls://nats.example.com:4222",
            tls_enabled=True,
            nkey_seed=None,
            lan_only=True,
        )
