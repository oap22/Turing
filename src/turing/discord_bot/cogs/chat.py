"""Chat cog -- handles conversational commands for the Turing bot."""

from __future__ import annotations

import traceback
from typing import TYPE_CHECKING

import structlog
from discord.ext import commands

from ..formatters import chunk_message

if TYPE_CHECKING:
    from ..bot import TuringBot

logger = structlog.get_logger("turing.discord.chat")


class ChatCog(commands.Cog, name="Chat"):
    """Cog for conversational interactions with the AI agent."""

    def __init__(self, bot: TuringBot) -> None:
        self.bot = bot

    @commands.command(name="ask")
    async def ask_command(self, ctx: commands.Context, *, question: str) -> None:
        """Explicitly ask the AI agent a question.

        Usage: ``!turing ask <question>``
        """
        if not self.bot.agent:
            await ctx.reply("Agent not initialized yet. Please wait...")
            return

        try:
            async with ctx.typing():
                response = await self.bot.agent.handle_message(
                    message=question,
                    channel_id=str(ctx.channel.id),
                    user_id=str(ctx.author.id),
                    user_name=ctx.author.display_name,
                )

            chunks = chunk_message(response)
            for chunk in chunks:
                await ctx.reply(chunk)

        except Exception as exc:
            logger.error("ask.error", error=str(exc), traceback=traceback.format_exc())
            await ctx.reply(
                "Sorry, I ran into an error while processing your question. "
                "Please try again in a moment."
            )

    @commands.command(name="think")
    async def think_command(self, ctx: commands.Context, *, question: str) -> None:
        """Ask the AI agent to think step by step about a question.

        Prefixes the user's question with an instruction to reason
        through the problem methodically before answering.

        Usage: ``!turing think <question>``
        """
        if not self.bot.agent:
            await ctx.reply("Agent not initialized yet. Please wait...")
            return

        try:
            augmented = (
                "Think through this step by step, showing your reasoning "
                f"before giving a final answer:\n\n{question}"
            )

            async with ctx.typing():
                response = await self.bot.agent.handle_message(
                    message=augmented,
                    channel_id=str(ctx.channel.id),
                    user_id=str(ctx.author.id),
                    user_name=ctx.author.display_name,
                )

            chunks = chunk_message(response)
            for chunk in chunks:
                await ctx.reply(chunk)

        except Exception as exc:
            logger.error("think.error", error=str(exc), traceback=traceback.format_exc())
            await ctx.reply(
                "Sorry, I ran into an error while thinking through your question. "
                "Please try again in a moment."
            )
