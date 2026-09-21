"""Central configuration for the DELTA CITY bot.

Everything the rest of the project reads from one place:
- environment variables (loaded from .env)
- the list of states and their 3-letter codes
- the role lists (national / state offices)
- the allowed gender and status values

Change values here and the whole bot follows. No other file hard-codes
these lists.
"""

import os

from dotenv import load_dotenv

load_dotenv()

# ---- environment variables -------------------------------------------------
# The bot token. Keep it ONLY in .env or your host's env settings — never in code.
TOKEN = os.getenv("DISCORD_TOKEN")

# Optional: your server (guild) ID, handy for debugging.
GUILD_ID = os.getenv("GUILD_ID")

# Channel where new arrivals are directed to complete registration.
# Leave empty to have the bot welcome new members via DM instead.
REGISTRATION_CHANNEL_ID = os.getenv("REGISTRATION_CHANNEL_ID", "")

# Channel where the public welcome message is posted.
# Leave empty to only send the welcome via DM.
WELCOME_CHANNEL_ID = os.getenv("WELCOME_CHANNEL_ID", "")

# Flight-style join/leave announcements (Delta International Airport).
# Channel that receives arrival and departure announcements.
AIRPORT_CHANNEL_ID = "1550786898627395624"

# Role stamped on every new member by on_member_join (entry status).
ASYLUM_ROLE_ID = "1550851264391286864"

# Channel holding the city regulations, linked from the arrival message.
RULES_CHANNEL_ID = "1550810323181633576"

# Channel where citizenship applications open, linked from the arrival message.
CITIZENSHIP_CHANNEL_ID = "1550783870025338921"

# Role pinged in the arrival message so an officer can process the newcomer.
IMMIGRATION_OFFICER_ROLE_ID = "1550831141555273809"

# Where the SQLite database file lives.
DATABASE_PATH = os.getenv("DATABASE_PATH", "data/delta_city.db")

# ---- the fictional nation --------------------------------------------------
NATION_NAME = "DELTA CITY"
NATION_CODE = "DC"  # prefix of every citizen ID: DC-ASB-0001

# ---- the six states of Delta City ------------------------------------------
STATES = ["Asaba", "Warri", "Ughelli", "Ozoro", "Kwale", "Agbor"]

# 3-letter code for each state, used inside citizen IDs.
# First citizen of Asaba -> DC-ASB-0001
STATE_CODES = {
    "Asaba": "ASB",
    "Warri": "WAR",
    "Ughelli": "UGH",
    "Ozoro": "OZO",
    "Kwale": "KWA",
    "Agbor": "AGB",
}

# "Overseas" is reserved for the future immigration system (visitors abroad,
# diplomats, etc.). It is NOT one of the six states and cannot be picked in
# registration. Keep it here so future systems can reference it.
OVERSEAS = "Overseas"

# ---- national offices: exactly ONE holder each -----------------------------
NATIONAL_ROLES = [
    "President",
    "Vice President",
    "Senate President",
    "Deputy Senate President",
    "Speaker",
    "Deputy Speaker",
]

# National offices that can have MANY holders (the cabinet / parliament).
# Discord role names: "Minister", "Senator", "House Member".
NATIONAL_MULTI_ROLES = ["Minister", "Senator", "House Member"]

# ---- state offices: exactly ONE holder per state ---------------------------
STATE_ROLES = [
    "Governor",
    "Deputy Governor",
    "State Speaker",
    "State Deputy Speaker",
]

# State offices that can have MANY holders per state.
# Discord role names: "Commissioner of <State>", "State Assembly Member of <State>".
STATE_MULTI_ROLES = ["Commissioner", "State Assembly Member"]

# Every role that counts as holding a government office (used for
# eligibility checks and display logic).
GOVERNMENT_ROLES = (
    NATIONAL_ROLES + NATIONAL_MULTI_ROLES + STATE_ROLES + STATE_MULTI_ROLES
)

# ---- citizen attributes ------------------------------------------------------
# Exactly the options offered in the registration modal. Stored verbatim.
GENDER_OPTIONS = ["Male", "Female", "Other", "Prefer not to say"]

# Public registration statuses. More can be added later for the immigration
# system (e.g. "Citizen Abroad", "Diplomat") — the DB column is free text.
CITIZEN_STATUSES = ["Citizen", "Resident", "Visitor"]

# Internal status applied when a registered member leaves the server.
INACTIVE_STATUS = "Inactive"

# ---- government appointment eligibility --------------------------------------
# Whether a member must have a registered citizen profile before an
# administrator can appoint them to a government office. Turn off to restore
# the old behaviour of appointing anyone.
REQUIRE_CITIZEN_FOR_APPOINTMENT = True

# How many audit entries the !dchelp / debug commands may show at once.
AUDIT_RECENT_LIMIT = 10

# ---- permission tiers -------------------------------------------------------
# The one true authority. Full access to everything, server and bot, and the
# ONLY role that may register or modify a Discord Admin / Chief Admin account.
CHIEF_ADMIN_ROLE = "Chief Administrator"

# May register citizens alongside admins — but never Admin or Chief Admin
# accounts (protected targets; see delta_city.permissions.can_register).
IMMIGRATION_OFFICER_ROLE = "Immigration Officer"

# Stamped on every new member by on_member_join. While held, the member can
# see only #airport and #citizens. Removed when registration completes.
UNVERIFIED_ROLE = "Unverified"

# Entry status role: stamped on every new member by on_member_join and held
# until the member's check-in application is approved. The ONLY role that
# may run !check-in (the self-service entry command).
ASYLUM_ROLE = "Asylum"

# National citizen role swapped in for Asylum when an application is
# approved. Every registered citizen holds it.
CITIZEN_ROLE_NAME = "Citizen"

# Home-state role handed out on approval, e.g. "Citizen of Asaba".
# See delta_city.permissions.state_citizen_role_name.

# Roles (beyond Discord Administrators and the Chief Administrator) that may
# run !appoint / !dismiss.
APPOINT_ALLOWED_ROLES = ["President", "Vice President", "Chief of Staff"]

# Channels visible to Unverified members. Everything else is locked to
# @everyone until registration removes the Unverified role.
AIRPORT_CHANNEL_NAME = "airport"
CITIZENS_CHANNEL_NAME = "citizens"

# Official government channels (created/found by channel name).
BIRTH_CERTIFICATE_CHANNEL_NAME = "birth-certificate"
ENTRY_AND_EXIT_CHANNEL_NAME = "entry-and-exit"
IMMIGRATION_OFFICE_CHANNEL_NAME = "immigration-office"

# Tribal name pools live in delta_city/identity.py (NAME_POOLS, per community);
# they are the source of truth for auto-generated citizen names.
