"""DELTA CITY bot entry point.

What this file does
-------------------
This is the ONLY file that talks to Discord's API at startup. Everything
else lives in its own module:

    config/settings.py       -> names, states, roles, env vars
    database/database.py     -> SQLite citizen registry + audit log
    commands/government.py   -> !appoint, !dismiss, !government
    commands/registration.py -> !register (modal flow)
    commands/citizens.py     -> !citizen, !citizens, !move, !stateinfo, !dchelp
    utils/helpers.py         -> shared formatters and role helpers

Run the bot with:
    python bot.py
"""

import logging
import os

import discord
from discord.ext import commands

from config import settings
from database.database import DeltaCityDB

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("delta-city")

# discord.py needs the message_content intent for prefix commands and the
# members intent for on_member_join / on_member_remove.
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

# Cogs are plain extension paths; add new systems here, nothing else.
EXTENSIONS = (
    "commands.government",
    "commands.registration",
    "commands.citizens",
)


class DeltaCityBot(commands.Bot):
    """The bot itself. Subclasses commands.Bot so cogs can reach the shared
    database via `self.bot.db` (one connection, one lock, no races)."""

    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)
        self.db = DeltaCityDB(settings.DATABASE_PATH)

    async def setup_hook(self):
        """Load every cog once, before the bot connects."""
        for extension in EXTENSIONS:
            await self.load_extension(extension)
            log.info("Loaded cog: %s", extension)

    async def on_ready(self):
        log.info("Logged in as %s (ID %s)", self.user, self.user.id)

    async def on_member_join(self, member: discord.Member):
        """Welcome new members and point them at !register."""
        log.info("Member joined: %s (%s)", member, member.id)
        message = (
            f"👋 Welcome to **{settings.NATION_NAME}**, {member.mention}!\n\n"
            "Every resident here holds a citizen profile. Get yours by "
            "typing `!register` in chat — it takes under a minute."
        )
        channel_id = settings.REGISTRATION_CHANNEL_ID
        if channel_id:
            channel = self.get_channel(int(channel_id))
            if channel is not None:
                await channel.send(
                    f"🎉 **New arrival:** {member.mention} has joined "
                    f"**{settings.NATION_NAME}**! They need to complete "
                    "registration — see below.\n\n" + message
                )
                return
        # Fall back to DM if no channel is configured (or it vanished).
        try:
            await member.send(message)
        except discord.Forbidden:
            log.warning(
                "Could not DM %s (%s) — DMs closed; no public channel set.",
                member,
                member.id,
            )

    async def on_member_remove(self, member: discord.Member):
        """Mark a registered citizen Inactive when they leave the server."""
        row = self.db.get_citizen(member.id)
        if row is None:
            return
        log.info("Member left: %s (%s), citizen %s", member, member.id, row["citizen_id"])
        self.db.set_status(
            member.id,
            settings.INACTIVE_STATUS,
            actor_id=None,
        )
        self.db.log_audit(
            actor_id=None,
            action="member_left",
            subject_id=member.id,
            details=f"Citizen {row['citizen_id']} set to {settings.INACTIVE_STATUS} on leave",
        )

    async def close(self):
        self.db.close()
        await super().close()


def main():
    token = settings.TOKEN
    if not token:
        raise SystemExit(
            "DISCORD_TOKEN is not set. Copy .env.example to .env and fill it in."
        )
    bot = DeltaCityBot()
    bot.run(token)


if __name__ == "__main__":
    main()