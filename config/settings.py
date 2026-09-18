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
    "Ozoro": "OZR",
    "Kwale": "KWL",
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
