"""Tests for NatsBus.connect's LAN-only guard.

Live NATS connection round-trip lives in `tests/integration/` under the
`integration` marker.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from turing.transport.nats_bus import NatsBus, _is_lan_url, _resolve_host


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


class TestConnect:
    """connect() builds the nats kwargs and wraps the returned client.

    `nats` is imported lazily inside connect(); patching `nats.connect`
    intercepts the call without a live broker.
    """

    async def test_minimal_kwargs_when_no_tls_or_nkey(self) -> None:
        client = MagicMock()
        with patch("nats.connect", new=AsyncMock(return_value=client)) as connect:
            bus = await NatsBus.connect(
                url="nats://127.0.0.1:4222",
                tls_enabled=False,
                nkey_seed=None,
                lan_only=True,
            )
        assert isinstance(bus, NatsBus)
        kwargs = connect.await_args.kwargs
        assert kwargs == {"servers": ["nats://127.0.0.1:4222"]}

    async def test_tls_and_nkey_kwargs_are_forwarded(self) -> None:
        client = MagicMock()
        with patch("nats.connect", new=AsyncMock(return_value=client)) as connect:
            await NatsBus.connect(
                url="tls://10.0.0.5:4222",
                tls_enabled=True,
                nkey_seed="SUACSSEED",
                lan_only=True,
            )
        kwargs = connect.await_args.kwargs
        assert kwargs["servers"] == ["tls://10.0.0.5:4222"]
        assert kwargs["tls"] is True
        assert kwargs["nkeys_seed_str"] == "SUACSSEED"

    async def test_lan_only_guard_rejects_before_connecting(self) -> None:
        # The guard must fire before nats.connect is ever reached.
        with (
            patch("turing.transport.nats_bus._resolve_host", return_value="8.8.8.8"),
            patch("nats.connect", new=AsyncMock()) as connect,
            pytest.raises(ValueError, match="lan"),
        ):
            await NatsBus.connect(
                url="tls://nats.example.com:4222",
                tls_enabled=True,
                nkey_seed=None,
                lan_only=True,
            )
        connect.assert_not_awaited()

    async def test_public_url_allowed_when_lan_only_false(self) -> None:
        client = MagicMock()
        with patch("nats.connect", new=AsyncMock(return_value=client)) as connect:
            bus = await NatsBus.connect(
                url="tls://8.8.8.8:4222",
                tls_enabled=True,
                nkey_seed=None,
                lan_only=False,
            )
        assert isinstance(bus, NatsBus)
        connect.assert_awaited_once()


class TestPublishSubscribeClose:
    async def test_publish_delegates_to_client(self) -> None:
        client = MagicMock()
        client.publish = AsyncMock()
        await NatsBus(client).publish("subject.x", b"payload")
        client.publish.assert_awaited_once_with("subject.x", b"payload")

    async def test_subscribe_wraps_handler_with_msg_data(self) -> None:
        client = MagicMock()
        client.subscribe = AsyncMock()
        received: list[bytes] = []

        async def handler(data: bytes) -> None:
            received.append(data)

        await NatsBus(client).subscribe("subject.x", handler)

        client.subscribe.assert_awaited_once()
        assert client.subscribe.await_args.args[0] == "subject.x"
        cb = client.subscribe.await_args.kwargs["cb"]

        # Simulate a delivered NATS message; the wrapper unwraps `.data`.
        await cb(SimpleNamespace(data=b"hello"))
        assert received == [b"hello"]

    async def test_close_drains_the_client(self) -> None:
        client = MagicMock()
        client.drain = AsyncMock()
        await NatsBus(client).close()
        client.drain.assert_awaited_once()


class TestLanUrlEdges:
    def test_empty_host_is_rejected(self) -> None:
        assert _is_lan_url("nats://") is False

    def test_resolved_to_non_ip_string_is_rejected(self) -> None:
        with patch("turing.transport.nats_bus._resolve_host", return_value="not-an-ip"):
            assert _is_lan_url("nats://weird-host:4222") is False

    def test_resolve_host_returns_ip_on_success(self) -> None:
        with patch("turing.transport.nats_bus.socket.gethostbyname", return_value="10.0.0.9"):
            assert _resolve_host("some-host") == "10.0.0.9"

    def test_resolve_host_returns_none_on_oserror(self) -> None:
        with patch("turing.transport.nats_bus.socket.gethostbyname", side_effect=OSError):
            assert _resolve_host("some-host") is None
