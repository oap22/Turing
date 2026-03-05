"""Core Discord bot for the Turing assistant."""

from __future__ import annotations

import discord
import structlog
from discord.ext import commands


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

        # Check if mentioned or DM
        is_dm = isinstance(message.channel, discord.DMChannel)
        is_mentioned = self.user in message.mentions if message.mentions else False

        if is_dm or is_mentioned:
            # Strip the mention from message content
            content = message.content
            if is_mentioned and self.user is not None:
                content = (
                    content.replace(f"<@{self.user.id}>", "")
                    .replace(f"<@!{self.user.id}>", "")
                    .strip()
                )

            if not content:
                return

            # Process through agent
            if self.agent:
                async with message.channel.typing():
                    response = await self.agent.handle_message(
                        message=content,
                        channel_id=str(message.channel.id),
                        user_id=str(message.author.id),
                        user_name=message.author.display_name,
                    )

                # Send response, chunking if needed
                from .formatters import chunk_message

                chunks = chunk_message(response)
                for chunk in chunks:
                    await message.reply(chunk)
            else:
                await message.reply("Agent not initialized yet. Please wait...")

        # Process commands too
        await self.process_commands(message)

    async def start_bot(self) -> None:
        """Start the bot with the configured token."""
        await self.start(self.config.discord_token)
