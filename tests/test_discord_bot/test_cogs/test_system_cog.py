"""Tests for ``turing.discord_bot.cogs.system.SystemCog``."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from turing.discord_bot.cogs import system as system_mod
from turing.discord_bot.cogs.system import SystemCog, _fmt_bytes, _get_cpu_temperature

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def test_fmt_bytes_scales_units() -> None:
    assert _fmt_bytes(512).endswith("B")
    assert "KB" in _fmt_bytes(2048)
    assert "MB" in _fmt_bytes(5 * 1024 * 1024)
    assert "GB" in _fmt_bytes(3 * 1024**3)


def test_get_cpu_temperature_returns_none_when_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(system_mod.psutil, "sensors_temperatures", lambda: {}, raising=False)
    assert _get_cpu_temperature() is None


def test_get_cpu_temperature_prefers_known_sensor(monkeypatch) -> None:
    sensor = SimpleNamespace(current=42.5)
    monkeypatch.setattr(
        system_mod.psutil,
        "sensors_temperatures",
        lambda: {"cpu_thermal": [sensor]},
        raising=False,
    )
    assert _get_cpu_temperature() == 42.5


def test_get_cpu_temperature_falls_back_to_first(monkeypatch) -> None:
    sensor = SimpleNamespace(current=37.0)
    monkeypatch.setattr(
        system_mod.psutil,
        "sensors_temperatures",
        lambda: {"other": [sensor]},
        raising=False,
    )
    assert _get_cpu_temperature() == 37.0


def test_get_cpu_temperature_swallows_oserror(monkeypatch) -> None:
    def _raise() -> dict:
        raise OSError("nope")

    monkeypatch.setattr(system_mod.psutil, "sensors_temperatures", _raise, raising=False)
    assert _get_cpu_temperature() is None


# ---------------------------------------------------------------------------
# sysinfo
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sysinfo_reports_metrics(monkeypatch, make_bot, make_context) -> None:
    bot = make_bot()
    cog = SystemCog(bot)
    ctx = make_context(bot=bot)

    monkeypatch.setattr(system_mod.psutil, "cpu_percent", lambda interval=0.5: 12.3)
    monkeypatch.setattr(
        system_mod.psutil,
        "virtual_memory",
        lambda: SimpleNamespace(total=8_000_000_000, used=2_000_000_000, percent=25.0),
    )
    monkeypatch.setattr(
        system_mod.psutil,
        "disk_usage",
        lambda path: SimpleNamespace(total=100_000_000_000, used=40_000_000_000, percent=40.0),
    )
    monkeypatch.setattr(system_mod, "_get_cpu_temperature", lambda: 55.5)
    monkeypatch.setattr(system_mod.psutil, "getloadavg", lambda: (0.1, 0.2, 0.3))

    await cog.sysinfo_command.callback(cog, ctx)

    ctx.reply.assert_awaited_once()


@pytest.mark.asyncio
async def test_sysinfo_handles_exception(monkeypatch, make_bot, make_context) -> None:
    bot = make_bot()
    cog = SystemCog(bot)
    ctx = make_context(bot=bot)

    def _raise(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(system_mod.psutil, "cpu_percent", _raise)

    await cog.sysinfo_command.callback(cog, ctx)
    assert "failed" in ctx.reply.await_args.args[0].lower()


# ---------------------------------------------------------------------------
# uptime
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_uptime_reports_times(monkeypatch, make_bot, make_context) -> None:
    bot = make_bot()
    cog = SystemCog(bot)
    ctx = make_context(bot=bot)

    monkeypatch.setattr(system_mod.psutil, "boot_time", lambda: 1_700_000_000.0)

    await cog.uptime_command.callback(cog, ctx)
    body = ctx.reply.await_args.args[0]
    assert "System Uptime" in body
    assert "Bot Uptime" in body


@pytest.mark.asyncio
async def test_uptime_handles_exception(monkeypatch, make_bot, make_context) -> None:
    bot = make_bot()
    cog = SystemCog(bot)
    ctx = make_context(bot=bot)

    def _raise() -> float:
        raise RuntimeError("nope")

    monkeypatch.setattr(system_mod.psutil, "boot_time", _raise)

    await cog.uptime_command.callback(cog, ctx)
    assert "failed" in ctx.reply.await_args.args[0].lower()


# ---------------------------------------------------------------------------
# disk
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disk_lists_partitions(monkeypatch, make_bot, make_context) -> None:
    bot = make_bot()
    cog = SystemCog(bot)
    ctx = make_context(bot=bot)

    monkeypatch.setattr(
        system_mod.psutil,
        "disk_partitions",
        lambda all=False: [SimpleNamespace(mountpoint="/", device="/dev/sda1")],
    )
    monkeypatch.setattr(
        system_mod.psutil,
        "disk_usage",
        lambda path: SimpleNamespace(
            total=100_000_000_000, used=40_000_000_000, free=60_000_000_000, percent=40.0
        ),
    )

    await cog.disk_command.callback(cog, ctx)
    body = ctx.reply.await_args.args[0]
    assert "/" in body
    assert "Mount" in body


@pytest.mark.asyncio
async def test_disk_skips_permission_error(monkeypatch, make_bot, make_context) -> None:
    bot = make_bot()
    cog = SystemCog(bot)
    ctx = make_context(bot=bot)

    monkeypatch.setattr(
        system_mod.psutil,
        "disk_partitions",
        lambda all=False: [
            SimpleNamespace(mountpoint="/root_restricted", device="x"),
            SimpleNamespace(mountpoint="/", device="y"),
        ],
    )

    def _usage(path: str):
        if "restricted" in path:
            raise PermissionError("denied")
        return SimpleNamespace(total=1, used=0, free=1, percent=0.0)

    monkeypatch.setattr(system_mod.psutil, "disk_usage", _usage)

    await cog.disk_command.callback(cog, ctx)
    body = ctx.reply.await_args.args[0]
    assert "restricted" not in body


@pytest.mark.asyncio
async def test_disk_handles_exception(monkeypatch, make_bot, make_context) -> None:
    bot = make_bot()
    cog = SystemCog(bot)
    ctx = make_context(bot=bot)

    def _raise(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(system_mod.psutil, "disk_partitions", _raise)

    await cog.disk_command.callback(cog, ctx)
    assert "failed" in ctx.reply.await_args.args[0].lower()


# ---------------------------------------------------------------------------
# health
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_all_components_ok(monkeypatch, make_bot, make_context) -> None:
    bot = make_bot(
        agent=MagicMock(),
        mesh_node=SimpleNamespace(peers={"n2": {}}),
    )
    # Enable mesh on the proxy config so the mesh branch is hit.
    bot.config._overrides["mesh_enabled"] = True
    cog = SystemCog(bot)
    ctx = make_context(bot=bot)

    monkeypatch.setattr(system_mod, "_check_database", AsyncMock(return_value=True))
    monkeypatch.setattr(system_mod, "_check_ollama", AsyncMock(return_value=True))

    await cog.health_command.callback(cog, ctx)

    body = ctx.reply.await_args.args[0]
    assert "Health Check" in body
    assert "Database" in body
    assert "Mesh" in body
    assert "Agent" in body


@pytest.mark.asyncio
async def test_health_reports_disabled_mesh_and_missing_pieces(
    monkeypatch, make_bot, make_context
) -> None:
    bot = make_bot(agent=None, mesh_node=None)
    bot.config._overrides["mesh_enabled"] = False
    bot.config._overrides["anthropic_api_key"] = ""
    cog = SystemCog(bot)
    ctx = make_context(bot=bot)

    monkeypatch.setattr(system_mod, "_check_database", AsyncMock(return_value=False))
    monkeypatch.setattr(system_mod, "_check_ollama", AsyncMock(return_value=False))

    await cog.health_command.callback(cog, ctx)

    body = ctx.reply.await_args.args[0]
    assert "[OFF]" in body
    assert "missing" in body
    assert "[FAIL]" in body


@pytest.mark.asyncio
async def test_health_handles_exception(monkeypatch, make_bot, make_context) -> None:
    bot = make_bot()
    cog = SystemCog(bot)
    ctx = make_context(bot=bot)

    monkeypatch.setattr(system_mod, "_check_database", AsyncMock(side_effect=RuntimeError("boom")))

    await cog.health_command.callback(cog, ctx)
    assert "failed" in ctx.reply.await_args.args[0].lower()


# ---------------------------------------------------------------------------
# _check_database / _check_ollama
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_database_returns_true_when_exists(tmp_path, make_bot) -> None:
    bot = make_bot()
    db = tmp_path / "exists.db"
    db.write_text("")
    bot.config._overrides["db_path"] = db
    assert await system_mod._check_database(bot) is True


@pytest.mark.asyncio
async def test_check_database_returns_false_on_error(make_bot) -> None:
    bot = make_bot()

    class _Boom:
        @property
        def db_path(self):  # type: ignore[no-untyped-def]
            raise RuntimeError("nope")

    bot.config = _Boom()
    assert await system_mod._check_database(bot) is False


@pytest.mark.asyncio
async def test_check_ollama_returns_false_on_error() -> None:
    # No server at this URL; httpx will raise -- function must catch.
    assert await system_mod._check_ollama("http://127.0.0.1:1") is False
