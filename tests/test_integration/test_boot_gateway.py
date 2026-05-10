"""Boot-path integration test for the gateway (issue #71).

Two missed wires from issue #70 (``GatewayService`` not instantiated in
``__main__.py`` and ``spa_assets_dir`` never threaded into ``create_app``)
shipped because every component had unit tests but the booted-process path
had none. This test exercises the actual ``_run`` entry point with the
Discord boundary stubbed so a regression on either wire fails loudly.
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
import socket
from typing import TYPE_CHECKING
from unittest.mock import patch

import httpx
import pytest

if TYPE_CHECKING:
    from pathlib import Path

from turing.config import TuringConfig
from turing.gateway.spa import spa_assets_path


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _wait_for_health(url: str, timeout: float = 5.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    last_err: Exception | None = None
    async with httpx.AsyncClient() as client:
        while asyncio.get_event_loop().time() < deadline:
            try:
                r = await client.get(url, timeout=1.0)
                if r.status_code == 200:
                    return
            except Exception as e:  # connection refused, etc.
                last_err = e
            await asyncio.sleep(0.1)
    raise AssertionError(f"gateway never came up at {url}: {last_err!r}")


@pytest.mark.asyncio
async def test_boot_path_serves_spa_and_authenticates(tmp_path: Path) -> None:
    """End-to-end: boot _run() with gateway enabled, hit /, assert SPA + auth.

    Catches both #70 regressions:
    - GatewayService not instantiated → /healthz never responds.
    - spa_assets_dir not threaded → / returns 404 even with a built bundle.
    """
    if spa_assets_path() is None:
        pytest.skip("SPA bundle not built (run `npm --prefix webui run build`)")

    port = _free_port()
    token = secrets.token_urlsafe(16)

    config = TuringConfig(
        _env_file=None,  # type: ignore[call-arg]
        node_name="test-pi",
        env="development",
        discord_token="stub-token",
        anthropic_api_key="stub-key",
        db_path=tmp_path / "turing.db",
        embedding_model_path=tmp_path / "no-embeddings",
        mesh_enabled=False,
        gateway_enabled=True,
        gateway_token=token,
        gateway_bind="127.0.0.1",
        gateway_port=port,
    )

    # Stub the Discord boundary so _run() doesn't dial Discord. The bot is
    # constructed normally; only its run loop and shutdown are no-ops.
    async def _no_op_start(self):  # type: ignore[no-untyped-def]
        await asyncio.Event().wait()  # park forever; cancelled at teardown

    async def _no_op_close(self):  # type: ignore[no-untyped-def]
        return None

    base = f"http://127.0.0.1:{port}"

    # add_signal_handler doesn't work on every event loop policy; bypass it.
    def _no_signal(*a, **kw):  # type: ignore[no-untyped-def]
        return None

    with (
        patch(
            "turing.discord_bot.bot.TuringBot.start_bot",
            new=_no_op_start,
        ),
        patch(
            "turing.discord_bot.bot.TuringBot.close",
            new=_no_op_close,
        ),
        patch(
            "turing.discord_bot.bot.TuringBot.is_closed",
            new=lambda self: True,
        ),
        patch.object(asyncio.get_event_loop(), "add_signal_handler", _no_signal),
    ):
        from turing.__main__ import _run

        run_task = asyncio.create_task(_run(config))
        try:
            await _wait_for_health(f"{base}/healthz")

            async with httpx.AsyncClient(follow_redirects=False) as client:
                # /healthz public
                r = await client.get(f"{base}/healthz")
                assert r.status_code == 200

                # / without cookie → 401
                r = await client.get(f"{base}/")
                assert r.status_code == 401

                # /token-handoff → 303 + Set-Cookie
                r = await client.get(f"{base}/token-handoff?token={token}")
                assert r.status_code == 303
                set_cookie = r.headers.get("set-cookie", "")
                assert "turing_gateway_token=" in set_cookie

                # / with cookie → 200, HTML, SPA marker present
                client.cookies.set("turing_gateway_token", token)
                r = await client.get(f"{base}/")
                assert r.status_code == 200, r.text
                assert r.headers["content-type"].startswith("text/html")
                body = r.text
                assert '<script type="module"' in body or "<title>" in body, (
                    "SPA index.html missing expected markers"
                )

                # /assets/<known> with cookie → 200
                assets_dir = spa_assets_path()
                assert assets_dir is not None
                asset_files = list((assets_dir / "assets").iterdir())
                assert asset_files, "no built assets present"
                first_asset = asset_files[0].name
                r = await client.get(f"{base}/assets/{first_asset}")
                assert r.status_code == 200
        finally:
            run_task.cancel()
            with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(run_task, timeout=5.0)
