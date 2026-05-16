"""Tests for NatsBus.connect's LAN-only guard.

Live NATS connection round-trip lives in `tests/integration/` under the
`integration` marker.
"""

from __future__ import annotations

from unittest.mock import patch

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
        "nats://8.8.8.8:4222",
        "tls://172.40.0.5:4222",  # 172.40 is outside 172.16/12 private range
    ],
)
def test_public_urls_rejected_by_lan_check(url: str) -> None:
    assert _is_lan_url(url) is False


def test_public_hostname_rejected_when_resolves_publicly() -> None:
    with patch("turing.transport.nats_bus._resolve_host", return_value="93.184.216.34"):
        assert _is_lan_url("tls://nats.example.com:4222") is False


async def test_connect_refuses_public_url_when_lan_only_true() -> None:
    # Pin DNS to a public IP so the resolver-aware check rejects deterministically.
    with (
        patch("turing.transport.nats_bus._resolve_host", return_value="8.8.8.8"),
        pytest.raises(ValueError, match="lan"),
    ):
        await NatsBus.connect(
            url="tls://nats.example.com:4222",
            tls_enabled=True,
            nkey_seed=None,
            lan_only=True,
        )


def test_docker_compose_service_name_resolved_to_private_ip_is_accepted() -> None:
    """Docker-compose service names like `nats` resolve to RFC1918 bridge IPs.

    The LAN check must resolve unqualified hostnames rather than rejecting them
    on string-shape alone — otherwise the dev fleet can't talk to its broker.
    """
    with patch("turing.transport.nats_bus._resolve_host", return_value="172.18.0.2"):
        assert _is_lan_url("nats://nats:4222") is True


def test_unresolvable_hostname_is_rejected() -> None:
    with patch("turing.transport.nats_bus._resolve_host", return_value=None):
        assert _is_lan_url("nats://nonexistent-host:4222") is False


def test_public_hostname_resolved_to_public_ip_is_rejected() -> None:
    with patch("turing.transport.nats_bus._resolve_host", return_value="8.8.8.8"):
        assert _is_lan_url("tls://nats.example.com:4222") is False
