"""Tests for NtfyAlertClient — the best-effort ntfy push fallback (ADR-0010 §2).

The contract under test mirrors the retired Discord client: ``ntfy_push``
never raises. The happy path POSTs the alert text to ``{base_url}/{topic}``;
every failure mode (no topic, no base URL, transport error, non-2xx status)
is caught and dropped so the alert engine's state is never perturbed.

httpx is mocked with an injected ``MockTransport`` so no network is touched.
"""

from __future__ import annotations

import httpx
import pytest

import turing.coordinator.alerts.ntfy_client as ntfy_mod
from turing.coordinator.alerts.ntfy_client import NtfyAlertClient


def _patch_transport(monkeypatch: pytest.MonkeyPatch, handler) -> list[httpx.Request]:  # type: ignore[no-untyped-def]
    """Swap ``httpx.AsyncClient`` for one wired to a ``MockTransport``.

    Returns a list that the handler appends each captured request to.
    """
    seen: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    real_client = httpx.AsyncClient

    def _factory(*args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs["transport"] = httpx.MockTransport(_handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(ntfy_mod.httpx, "AsyncClient", _factory)
    return seen


@pytest.mark.asyncio
async def test_push_posts_text_to_base_url_slash_topic(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    seen = _patch_transport(monkeypatch, lambda req: httpx.Response(200))
    client = NtfyAlertClient("http://surface.tailnet.ts.net:8090", "turing-alerts")
    await client.ntfy_push("⚠ pi-beta TEMP danger: 87.4°C (>82.0°C)")
    assert len(seen) == 1
    req = seen[0]
    assert req.method == "POST"
    assert str(req.url) == "http://surface.tailnet.ts.net:8090/turing-alerts"
    assert req.content.decode("utf-8") == "⚠ pi-beta TEMP danger: 87.4°C (>82.0°C)"


@pytest.mark.asyncio
async def test_trailing_slash_on_base_url_is_normalised(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    seen = _patch_transport(monkeypatch, lambda req: httpx.Response(200))
    client = NtfyAlertClient("http://surface.tailnet.ts.net:8090/", "topic")
    await client.ntfy_push("hi")
    assert str(seen[0].url) == "http://surface.tailnet.ts.net:8090/topic"


@pytest.mark.asyncio
async def test_no_topic_is_a_silent_noop(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    seen = _patch_transport(monkeypatch, lambda req: httpx.Response(200))
    client = NtfyAlertClient("http://surface.tailnet.ts.net:8090", None)
    await client.ntfy_push("⚠ anything")
    assert seen == []  # fallback disabled by config — no HTTP attempted


@pytest.mark.asyncio
async def test_no_base_url_is_a_silent_noop(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    seen = _patch_transport(monkeypatch, lambda req: httpx.Response(200))
    client = NtfyAlertClient(None, "topic")
    await client.ntfy_push("⚠ anything")
    assert seen == []


@pytest.mark.asyncio
async def test_non_2xx_status_is_swallowed(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _patch_transport(monkeypatch, lambda req: httpx.Response(503))
    client = NtfyAlertClient("http://surface.tailnet.ts.net:8090", "topic")
    await client.ntfy_push("⚠ anything")  # must not raise


@pytest.mark.asyncio
async def test_transport_error_is_swallowed(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    def _boom(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host", request=req)

    _patch_transport(monkeypatch, _boom)
    client = NtfyAlertClient("http://surface.tailnet.ts.net:8090", "topic")
    await client.ntfy_push("⚠ anything")  # must not raise
