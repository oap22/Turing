"""System cog -- hardware and connectivity monitoring commands."""

from __future__ import annotations

import platform
import time
import traceback
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import psutil
import structlog
from discord.ext import commands

from ..formatters import format_code_block, format_system_info

if TYPE_CHECKING:
    from ..bot import TuringBot

logger = structlog.get_logger("turing.discord.system")

# Captured at import time so bot uptime can be computed.
_BOT_START_TIME: float = time.time()


class SystemCog(commands.Cog, name="System"):
    """Commands for inspecting hardware, uptime, and connectivity."""

    def __init__(self, bot: TuringBot) -> None:
        self.bot = bot

    # ------------------------------------------------------------------
    # !turing sysinfo
    # ------------------------------------------------------------------
    @commands.command(name="sysinfo")
    async def sysinfo_command(self, ctx: commands.Context) -> None:
        """Show CPU, memory, disk, and temperature information."""
        try:
            cpu_percent = psutil.cpu_percent(interval=0.5)
            mem = psutil.virtual_memory()
            disk = psutil.disk_usage("/")

            info: dict[str, object] = {
                "hostname": platform.node(),
                "platform": f"{platform.system()} {platform.release()}",
                "cpu_percent": cpu_percent,
                "memory_total": mem.total,
                "memory_used": mem.used,
                "memory_percent": mem.percent,
                "disk_total": disk.total,
                "disk_used": disk.used,
                "disk_percent": disk.percent,
            }

            # Temperature is only available on some platforms (Linux).
            temps = _get_cpu_temperature()
            if temps is not None:
                info["temperature"] = temps

            # Load average (Unix only)
            try:
                load1, load5, load15 = psutil.getloadavg()
                info["load_avg"] = f"{load1:.2f} / {load5:.2f} / {load15:.2f}"
            except (AttributeError, OSError):
                pass

            await ctx.reply(format_system_info(info))

        except Exception as exc:
            logger.error("sysinfo.error", error=str(exc), traceback=traceback.format_exc())
            await ctx.reply("Failed to retrieve system information.")

    # ------------------------------------------------------------------
    # !turing uptime
    # ------------------------------------------------------------------
    @commands.command(name="uptime")
    async def uptime_command(self, ctx: commands.Context) -> None:
        """Show system and bot uptime."""
        try:
            # System uptime
            boot_time = datetime.fromtimestamp(psutil.boot_time(), tz=UTC)
            sys_uptime = datetime.now(tz=UTC) - boot_time
            sys_days = sys_uptime.days
            sys_hours, sys_rem = divmod(sys_uptime.seconds, 3600)
            sys_mins, sys_secs = divmod(sys_rem, 60)

            # Bot uptime
            bot_uptime_secs = int(time.time() - _BOT_START_TIME)
            bot_hours, bot_rem = divmod(bot_uptime_secs, 3600)
            bot_mins, bot_secs = divmod(bot_rem, 60)

            lines = [
                f"System Uptime:  {sys_days}d {sys_hours}h {sys_mins}m {sys_secs}s",
                f"Bot Uptime:     {bot_hours}h {bot_mins}m {bot_secs}s",
                f"Boot Time:      {boot_time.strftime('%Y-%m-%d %H:%M:%S UTC')}",
            ]
            await ctx.reply(format_code_block("\n".join(lines)))

        except Exception as exc:
            logger.error("uptime.error", error=str(exc), traceback=traceback.format_exc())
            await ctx.reply("Failed to retrieve uptime information.")

    # ------------------------------------------------------------------
    # !turing disk
    # ------------------------------------------------------------------
    @commands.command(name="disk")
    async def disk_command(self, ctx: commands.Context) -> None:
        """Show disk usage details for all mounted partitions."""
        try:
            partitions = psutil.disk_partitions(all=False)
            lines: list[str] = [
                f"{'Mount':<20} {'Total':>10} {'Used':>10} {'Free':>10} {'Use%':>6}",
                "-" * 58,
            ]

            for part in partitions:
                try:
                    usage = psutil.disk_usage(part.mountpoint)
                except PermissionError:
                    continue
                lines.append(
                    f"{part.mountpoint:<20} "
                    f"{_fmt_bytes(usage.total):>10} "
                    f"{_fmt_bytes(usage.used):>10} "
                    f"{_fmt_bytes(usage.free):>10} "
                    f"{usage.percent:>5.1f}%"
                )

            await ctx.reply(format_code_block("\n".join(lines)))

        except Exception as exc:
            logger.error("disk.error", error=str(exc), traceback=traceback.format_exc())
            await ctx.reply("Failed to retrieve disk information.")

    # ------------------------------------------------------------------
    # !turing health
    # ------------------------------------------------------------------
    @commands.command(name="health")
    async def health_command(self, ctx: commands.Context) -> None:
        """Check LLM providers, database, and mesh connectivity."""
        try:
            checks: list[str] = []

            # Database check
            db_ok = await _check_database(self.bot)
            checks.append(f"{'[OK]' if db_ok else '[FAIL]':>6}  Database")

            # Ollama (local LLM) check
            ollama_ok = await _check_ollama(self.bot.config.ollama_host)
            checks.append(
                f"{'[OK]' if ollama_ok else '[FAIL]':>6}  Ollama ({self.bot.config.ollama_host})"
            )

            # Anthropic check
            anthropic_ok = bool(self.bot.config.anthropic_api_key)
            checks.append(
                f"{'[OK]' if anthropic_ok else '[FAIL]':>6}  "
                f"Anthropic API Key {'configured' if anthropic_ok else 'missing'}"
            )

            # Mesh connectivity
            if self.bot.config.mesh_enabled and self.bot.mesh_node:
                peer_count = len(self.bot.mesh_node.peers)
                checks.append(f"  [OK]  Mesh ({peer_count} peer(s))")
            else:
                checks.append(" [OFF]  Mesh (disabled)")

            # Agent check
            agent_ok = self.bot.agent is not None
            checks.append(f"{'[OK]' if agent_ok else '[FAIL]':>6}  Agent")

            header = "Health Check"
            await ctx.reply(
                format_code_block(f"{header}\n{'=' * len(header)}\n" + "\n".join(checks))
            )

        except Exception as exc:
            logger.error("health.error", error=str(exc), traceback=traceback.format_exc())
            await ctx.reply("Failed to perform health check.")


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _fmt_bytes(num_bytes: int | float) -> str:
    """Format byte count in human-readable form."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num_bytes) < 1024.0:
            return f"{num_bytes:.1f}{unit}"
        num_bytes /= 1024.0
    return f"{num_bytes:.1f}PB"


def _get_cpu_temperature() -> float | None:
    """Read CPU temperature if available (Linux / Raspberry Pi)."""
    try:
        sensors_temperatures = getattr(psutil, "sensors_temperatures", None)
        if sensors_temperatures is None:
            return None
        temps = sensors_temperatures()
        if not temps:
            return None
        # Try common sensor names on Raspberry Pi and generic Linux
        for name in ("cpu_thermal", "coretemp", "cpu-thermal"):
            if temps.get(name):
                return float(temps[name][0].current)
        # Fallback: return the first available sensor
        first_key = next(iter(temps))
        if temps[first_key]:
            return float(temps[first_key][0].current)
    except (AttributeError, OSError, StopIteration):
        pass
    return None


async def _check_database(bot: TuringBot) -> bool:
    """Return True if the database file exists and is accessible."""
    try:
        db_path = bot.config.db_path
        return bool(db_path.exists())
    except Exception:
        return False


async def _check_ollama(host: str) -> bool:
    """Return True if the Ollama server is reachable."""
    try:
        import httpx

        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(host)
            return resp.status_code == 200
    except Exception:
        return False
