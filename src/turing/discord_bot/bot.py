"""Core Discord bot for the Turing assistant."""

from __future__ import annotations

import re

import discord
import structlog
from discord.ext import commands


def _strip_bot_mentions(content: str, user_id: int | None) -> str:
    """Strip every shape of the bot's user mention from `content`.

    Handles `<@id>` and `<@!id>`. Leaves role mentions (`<@&id>`) and
    other users' mentions intact. Returns the input unchanged when
    `user_id` is None (e.g. before the bot has logged in).
    """
    if user_id is None:
        return content
    pattern = re.compile(rf"<@!?{user_id}>")
    return pattern.sub("", content).strip()


class TuringBot(commands.Bot):
    """Discord bot that bridges conversations to the Turing AI agent.

    Responds to direct mentions and DMs, routing messages through the
    configured agent for processing and replying with formatted responses.
    """

    def __init__(
        self,
        config,
        agent=None,
        mesh_node=None,
        memory_store=None,
    ):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        super().__init__(
            command_prefix=config.discord_command_prefix + " ",
            intents=intents,
            help_command=None,
        )
        self.config = config
        self.agent = agent
        self.mesh_node = mesh_node
        self.memory_store = memory_store
        self.logger = structlog.get_logger("turing.discord")

    async def setup_hook(self) -> None:
        """Load cogs on startup."""
        from .cogs.admin import AdminCog
        from .cogs.chat import ChatCog
        from .cogs.system import SystemCog

        await self.add_cog(ChatCog(self))
        await self.add_cog(AdminCog(self))
        await self.add_cog(SystemCog(self))
        self.logger.info("bot.ready", node=self.config.node_name)

    async def on_message(self, message: discord.Message) -> None:
        """Respond to mentions and DMs."""
        if message.author.bot:
            return

        # Log every human message that reaches us — keeps drop paths observable (bug #141).
        self.logger.debug(
            "bot.message_received",
            channel_id=getattr(message.channel, "id", None),
            author_id=getattr(message.author, "id", None),
            content_len=len(message.content or ""),
            mention_count=len(message.mentions or []),
        )

        is_dm = isinstance(message.channel, discord.DMChannel)
        # `mentioned_in` covers user mentions, role mentions of the bot,
        # and reply auto-pings — and uses ID-based equality so it survives
        # post-reconnect ClientUser drift.
        is_mentioned = self.user is not None and self.user.mentioned_in(message)

        if not (is_dm or is_mentioned):
            await self.process_commands(message)
            return

        bot_user_id = self.user.id if self.user is not None else None
        content = _strip_bot_mentions(message.content, bot_user_id)

        if not content:
            self.logger.warning(
                "bot.message_dropped",
                reason="empty_after_strip",
                channel_id=getattr(message.channel, "id", None),
                author_id=getattr(message.author, "id", None),
            )
            return

        if self.agent:
            try:
                async with message.channel.typing():
                    response = await self.agent.handle_message(
                        message=content,
                        channel_id=str(message.channel.id),
                        user_id=str(message.author.id),
                        user_name=message.author.display_name,
                    )
            except Exception as exc:
                # Bug #159: any unhandled provider error (credit exhaustion,
                # rate limit, transient 5xx, etc.) used to propagate to
                # discord.py's on_message handler — user got silence, logs
                # got a multi-thousand-line traceback. Reply gracefully and
                # log a single structured event instead.
                self.logger.error(
                    "bot.agent_error",
                    error_type=type(exc).__name__,
                    error=str(exc),
                    channel_id=getattr(message.channel, "id", None),
                    author_id=getattr(message.author, "id", None),
                )
                await message.reply(
                    "I'm having trouble reaching the LLM right now — "
                    "please try again in a minute."
                )
                await self.process_commands(message)
                return

            from .formatters import chunk_message

            chunks = chunk_message(response)
            for chunk in chunks:
                await message.reply(chunk)
        else:
            await message.reply("Agent not initialized yet. Please wait...")

        await self.process_commands(message)

    async def start_bot(self) -> None:
        """Start the bot with the configured token."""
        await self.start(self.config.discord_token)
