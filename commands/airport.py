"""Delta International Airport — flight-style join/leave announcements.

Posts a FLIGHT ARRIVAL notice when a member lands in Delta City and a
FLIGHT DEPARTURE notice when one boards an outbound flight, both in
#delta-international-airport.

The message text is built by the pure functions ``build_arrival_message``
and ``build_departure_message`` so it can be unit-tested without a Discord
connection; the cog only wires them to on_member_join / on_member_remove.
"""

from __future__ import annotations

import datetime
import logging

import discord
from discord.ext import commands

from config import settings
from delta_city import permissions

log = logging.getLogger(__name__)


def build_arrival_message(member, year: int | None = None) -> str:
    """Render the FLIGHT ARRIVAL announcement for *member*.

    ``member.mention`` is used for {user} and the channel/role mentions
    come from config, so the rendered string is stable in tests.
    """
    if year is None:
        year = datetime.date.today().year
    return (
        "✈️ **FLIGHT ARRIVAL: NOW LANDING IN DELTA CITY**\n"
        "\n"
        f"Please welcome {member.mention}! 🎉\n"
        f"Flight **DC-{year}** has touched down at Delta International Airport.\n"
        "\n"
        "🏙️ **WELCOME TO THE CITY OF LIFE!**\n"
        "\n"
        "You're not through the gates just yet. Before you step out into the "
        "Metropolis, complete your arrival checklist:\n"
        "\n"
        f"**1.** Read the city regulations in "
        f"<#{settings.RULES_CHANNEL_ID}>\n"
        f"**2.** Apply for citizenship in "
        f"<#{settings.CITIZENSHIP_CHANNEL_ID}> with `!check-in`\n"
        "**3.** Wait at arrivals for an immigration officer to clear you\n"
        "\n"
        "🛂 **Status:** Asylum, awaiting clearance\n"
        "\n"
        "Mind your luggage, watch your step, and enjoy your stay in DC.\n"
        "\n"
        f"<@&{settings.IMMIGRATION_OFFICER_ROLE_ID}> a new arrival is waiting. "
        "Please direct them through immigration when you get a moment."
    )


def build_departure_message(display_name: str) -> str:
    """Render the FLIGHT DEPARTURE announcement for a member by name.

    The name is sent as plain text (a mention of a departed member would
    show as "Unknown User"), with markdown and mentions escaped.
    """
    name = discord.utils.escape_markdown(display_name)
    name = discord.utils.escape_mentions(name)
    return (
        "🛫 **FLIGHT DEPARTURE: NOW BOARDING OUT OF DELTA CITY**\n"
        "\n"
        f"{name} has boarded an outbound flight and travelled out of Delta City.\n"
        "\n"
        "Thank you for spending time in the City of Life. The runway is always "
        "open if you decide to come back. 🏙️\n"
        "\n"
        "Safe travels, you'll be missed 🥺"
    )


class Airport(commands.Cog):
    """Flight-style announcements for #delta-international-airport."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        bot.add_listener(self._on_member_join, "on_member_join")
        bot.add_listener(self._on_member_remove, "on_member_remove")

    # -- helpers --------------------------------------------------------------

    async def _get_airport_channel(self, member) -> discord.TextChannel | None:
        """Resolve the airport channel for *member*'s guild (or None)."""
        guild = getattr(member, "guild", None)
        if guild is None:
            log.warning(
                "Airport: no guild for %s (%s); skipping announcement",
                member, getattr(member, "id", "?"),
            )
            return None
        if not settings.AIRPORT_CHANNEL_ID:
            log.warning(
                "Airport: AIRPORT_CHANNEL_ID is not set; skipping announcement"
            )
            return None
        try:
            channel_id = int(settings.AIRPORT_CHANNEL_ID)
        except ValueError:
            log.warning(
                "Airport: AIRPORT_CHANNEL_ID %r is not a valid channel ID; "
                "skipping announcement",
                settings.AIRPORT_CHANNEL_ID,
            )
            return None
        channel = guild.get_channel(channel_id)
        if channel is None:
            log.warning(
                "Airport: channel %s not found in guild %s; "
                "skipping announcement",
                settings.AIRPORT_CHANNEL_ID, guild.name,
            )
        return channel

    # -- events ---------------------------------------------------------------

    async def _on_member_join(self, member: discord.Member) -> None:
        """Stamp the Asylum role, then announce the arrival."""
        log.info("Airport: member joined: %s (%s)", member, member.id)
        if member.bot:
            return

        # The Asylum role is stamped first and independently: a failure to
        # post in the airport channel must never block the role grant.
        try:
            await permissions.add_asylum_role(member)
        except discord.Forbidden:
            log.warning(
                "Airport: missing Manage Roles permission — "
                "could not give %s the %s role",
                member, settings.ASYLUM_ROLE,
            )
        except discord.HTTPException as exc:
            log.warning(
                "Airport: could not give %s the %s role: %s",
                member, settings.ASYLUM_ROLE, exc,
            )

        channel = await self._get_airport_channel(member)
        if channel is None:
            return
        try:
            await channel.send(
                build_arrival_message(member),
                allowed_mentions=discord.AllowedMentions(
                    users=[member.id],
                    roles=[int(settings.IMMIGRATION_OFFICER_ROLE_ID)],
                    everyone=False,
                    replied_user=False,
                ),
            )
        except discord.Forbidden:
            log.warning(
                "Airport: missing Send Messages permission in channel %s; "
                "skipping arrival announcement for %s",
                channel.id, member,
            )
        except discord.HTTPException as exc:
            log.warning(
                "Airport: failed to post arrival for %s: %s", member, exc,
            )

    async def _on_member_remove(self, member: discord.Member) -> None:
        """Announce the departure. Citizen records/roles are untouched."""
        log.info("Airport: member left: %s (%s)", member, member.id)
        if member.bot:
            return

        channel = await self._get_airport_channel(member)
        if channel is None:
            return
        try:
            await channel.send(
                build_departure_message(member.display_name),
                allowed_mentions=discord.AllowedMentions(
                    users=[],
                    roles=[],
                    everyone=False,
                    replied_user=False,
                ),
            )
        except discord.Forbidden:
            log.warning(
                "Airport: missing Send Messages permission in channel %s; "
                "skipping departure announcement for %s",
                channel.id, member,
            )
        except discord.HTTPException as exc:
            log.warning(
                "Airport: failed to post departure for %s: %s", member, exc,
            )


async def setup(bot: commands.Bot) -> None:
    """Called by discord.py when the cog is loaded from bot.py."""
    await bot.add_cog(Airport(bot))