"""Admin cog -- privileged commands restricted to configured admin users."""

from __future__ import annotations

import time
import traceback
from typing import TYPE_CHECKING

import psutil
import structlog
from discord.ext import commands

from ..formatters import format_code_block, truncate

if TYPE_CHECKING:
    from ..bot import TuringBot

logger = structlog.get_logger("turing.discord.admin")

# Bot start time -- set once on module load so uptime can be computed.
_BOT_START_TIME: float = time.time()


def _is_admin(ctx: commands.Context) -> bool:
    """Return True if the invoking user is in the admin list."""
    bot: TuringBot = ctx.bot  # type: ignore[assignment]
    return ctx.author.id in bot.config.discord_admin_ids


class AdminCog(commands.Cog, name="Admin"):
    """Administrative commands -- only available to configured admin users."""

    def __init__(self, bot: TuringBot) -> None:
        self.bot = bot

    # ------------------------------------------------------------------
    # Shared admin check
    # ------------------------------------------------------------------
    async def cog_check(self, ctx: commands.Context) -> bool:  # type: ignore[override]
        """Restrict every command in this cog to admin users."""
        if not _is_admin(ctx):
            await ctx.reply("You do not have permission to use this command.")
            return False
        return True

    # ------------------------------------------------------------------
    # !turing status
    # ------------------------------------------------------------------
    @commands.command(name="status")
    async def status_command(self, ctx: commands.Context) -> None:
        """Show node status: uptime, memory usage, current model, peer count."""
        try:
            uptime_seconds = int(time.time() - _BOT_START_TIME)
            hours, remainder = divmod(uptime_seconds, 3600)
            minutes, seconds = divmod(remainder, 60)
            uptime_str = f"{hours}h {minutes}m {seconds}s"

            mem = psutil.virtual_memory()
            mem_used = f"{mem.used / (1024 ** 3):.1f} GB"
            mem_total = f"{mem.total / (1024 ** 3):.1f} GB"
            mem_pct = f"{mem.percent}%"

            peer_count = len(self.bot.mesh_node.peers) if self.bot.mesh_node else 0

            model_info = getattr(self.bot.config, "ollama_model", "unknown")
            cloud_model = getattr(self.bot.config, "anthropic_model", "unknown")
            routing_mode = getattr(self.bot.config, "llm_routing_mode", "unknown")

            lines = [
                f"Node:           {self.bot.config.node_name}",
                f"Bot Uptime:     {uptime_str}",
                f"Memory:         {mem_used} / {mem_total} ({mem_pct})",
                f"Local Model:    {model_info}",
                f"Cloud Model:    {cloud_model}",
                f"Routing Mode:   {routing_mode}",
                f"Mesh Peers:     {peer_count}",
            ]
            await ctx.reply(format_code_block("\n".join(lines)))

        except Exception as exc:
            logger.error("status.error", error=str(exc), traceback=traceback.format_exc())
            await ctx.reply("Failed to retrieve status information.")

    # ------------------------------------------------------------------
    # !turing peers
    # ------------------------------------------------------------------
    @commands.command(name="peers")
    async def peers_command(self, ctx: commands.Context) -> None:
        """List connected mesh peers with their status."""
        if not self.bot.mesh_node:
            await ctx.reply("Mesh networking is not enabled.")
            return

        try:
            peers = self.bot.mesh_node.peers
            if not peers:
                await ctx.reply("No mesh peers connected.")
                return

            lines: list[str] = [f"{'Peer':<20} {'Status':<12}"]
            lines.append("-" * 32)
            for peer_name, peer_info in peers.items():
                status = peer_info.get("status", "unknown") if isinstance(peer_info, dict) else str(peer_info)
                lines.append(f"{peer_name:<20} {status:<12}")

            await ctx.reply(format_code_block("\n".join(lines)))

        except Exception as exc:
            logger.error("peers.error", error=str(exc), traceback=traceback.format_exc())
            await ctx.reply("Failed to retrieve peer information.")

    # ------------------------------------------------------------------
    # !turing memory search <query>
    # ------------------------------------------------------------------
    @commands.command(name="memory")
    async def memory_command(self, ctx: commands.Context, action: str, *, query: str) -> None:
        """Search stored facts and memories.

        Usage: ``!turing memory search <query>``
        """
        if action.lower() != "search":
            await ctx.reply("Usage: `!turing memory search <query>`")
            return

        if not self.bot.memory_store:
            await ctx.reply("Memory store is not available.")
            return

        try:
            async with ctx.typing():
                results = await self.bot.memory_store.search_facts(query)

            if not results:
                await ctx.reply(f"No memories found for: *{query}*")
                return

            lines: list[str] = []
            for i, fact in enumerate(results, 1):
                text = fact.get("text", str(fact)) if isinstance(fact, dict) else str(fact)
                lines.append(f"{i}. {text}")

            output = truncate("\n".join(lines), max_length=1800)
            await ctx.reply(format_code_block(output))

        except Exception as exc:
            logger.error("memory.error", error=str(exc), traceback=traceback.format_exc())
            await ctx.reply("Failed to search memories.")

    # ------------------------------------------------------------------
    # !turing forget <topic>
    # ------------------------------------------------------------------
    @commands.command(name="forget")
    async def forget_command(self, ctx: commands.Context, *, topic: str) -> None:
        """Remove memories about a topic.

        Usage: ``!turing forget <topic>``
        """
        if not self.bot.memory_store:
            await ctx.reply("Memory store is not available.")
            return

        try:
            # Search for matching facts first to show what will be removed.
            async with ctx.typing():
                results = await self.bot.memory_store.search_facts(topic)

            if not results:
                await ctx.reply(f"No memories found matching: *{topic}*")
                return

            # If the store has a delete/forget method, call it.
            if hasattr(self.bot.memory_store, "forget"):
                await self.bot.memory_store.forget(topic)
                await ctx.reply(
                    f"Removed {len(results)} memory/memories related to: *{topic}*"
                )
            elif hasattr(self.bot.memory_store, "delete_facts"):
                await self.bot.memory_store.delete_facts(topic)
                await ctx.reply(
                    f"Removed {len(results)} memory/memories related to: *{topic}*"
                )
            else:
                await ctx.reply(
                    "The memory store does not support deletion. "
                    "Please remove memories manually."
                )

        except Exception as exc:
            logger.error("forget.error", error=str(exc), traceback=traceback.format_exc())
            await ctx.reply("Failed to remove memories.")

    # ------------------------------------------------------------------
    # !turing config
    # ------------------------------------------------------------------
    @commands.command(name="config")
    async def config_command(self, ctx: commands.Context) -> None:
        """Show current non-secret configuration values."""
        # Fields that contain sensitive data and must never be displayed.
        secret_fields = {
            "discord_token",
            "anthropic_api_key",
        }

        try:
            config_dict = self.bot.config.model_dump()
            lines: list[str] = []
            for key, value in sorted(config_dict.items()):
                if key in secret_fields:
                    lines.append(f"{key}: ********")
                else:
                    lines.append(f"{key}: {value}")

            output = truncate("\n".join(lines), max_length=1800)
            await ctx.reply(format_code_block(output))

        except Exception as exc:
            logger.error("config.error", error=str(exc), traceback=traceback.format_exc())
            await ctx.reply("Failed to retrieve configuration.")
