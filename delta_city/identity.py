"""Delta City identity system: city prefixes, communities, fictional names, and citizen IDs.

This module is the single source of truth for:
- City-to-prefix mapping (DC-ASB, DC-WAR, etc.)
- City-to-community/heritage mapping
- Fictional Deltaian name generation per community
- Transactional citizen ID sequencing (no random numbers, no duplicates)
"""

from __future__ import annotations

import random
import sqlite3
from typing import Any

from config.settings import STATE_CODES

# ---------------------------------------------------------------------------
# City prefixes
# ---------------------------------------------------------------------------

CITY_PREFIXES: dict[str, str] = dict(STATE_CODES)
# e.g. {"Asaba": "ASB", "Warri": "WAR", "Ughelli": "UGH",
#        "Ozoro": "OZO", "Kwale": "KWA", "Agbor": "AGB"}

# ---------------------------------------------------------------------------
# City → community/heritage mapping
# ---------------------------------------------------------------------------

CITY_COMMUNITIES: dict[str, list[str]] = {
    "Asaba": ["Anioma"],
    "Warri": ["Pidgin", "Itsekiri", "Urhobo"],
    "Ughelli": ["Urhobo"],
    "Ozoro": ["Isoko"],
    "Kwale": ["Ukwuani", "Ndokwa"],
    "Agbor": ["Ika"],
}

# Flat lookup: community → city (for validation / reverse mapping)
COMMUNITY_CITY: dict[str, str] = {
    comm: city for city, comms in CITY_COMMUNITIES.items() for comm in comms
}

# ---------------------------------------------------------------------------
# Gender options (per spec: Male, Female, Other — no inference)
# ---------------------------------------------------------------------------

GENDER_OPTIONS: list[str] = ["Male", "Female", "Other"]

# ---------------------------------------------------------------------------
# Fictional name pools — culturally appropriate per community
# ---------------------------------------------------------------------------

# Each community gets male_first, female_first, and surnames lists.
# "Pidgin" is a linguistic community; we use broader Warri-area Delta names.
_NAME_POOLS: dict[str, dict[str, list[str]]] = {
    "Anioma": {
        "male_first": [
            "Chukwudi", "Obinna", "Nnamdi", "Ifeanyichukwu", "Emeka",
            "Chinedu", "Uche", "Kenechukwu", "Arinze", "Madueke",
            "Obiora", "Chibueze", "Chisom", "Ikem", "Nkemdilim",
        ],
        "female_first": [
            "Adaeze", "Chinwe", "Obiageli", "Nneka", "Uchechi",
            "Chidinma", "Kamsiyochukwu", "Ifeoma", "Ngozi", "Ebele",
            "Chiamaka", "Ogechi", "Azubuike", "Ijemma", "Chisimdi",
        ],
        "surnames": [
            "Okafor", "Nwosu", "Igwe", "Obi", "Ekwueme",
            "Nwankwo", "Chukwuemeka", "Eze", "Onyeka", "Amadi",
            "Uchechukwu", "Odira", "Ndigwe", "Obegwe", "Okoye",
        ],
    },
    "Pidgin": {
        "male_first": [
            "Tega", "Ovie", "Oba", "Sotonye", "Boma",
            "Dumebi", "Ibiere", "Miebaka", "Tonye", "Oronto",
            "Ayo", "Semion", "Ogaga", "Ebie", "Onome",
        ],
        "female_first": [
            "Ebiere", "Ibiba", "Tonye", "Oreva", "Anabelle",
            "Braide", "Diepreye", "Miebaka", "Ina", "Abi",
            "Eunice", "Osauyi", "Peremobowei", "Sarah", "Miebaka",
        ],
        "surnames": [
            "Briggs", "Fubara", "Dappa", "Siri", "Jacks",
            "Miebaka", "Opuene", "Boma", "Ogeh", "Piriye",
            "Anga", "Oronto", "Douglas", "Tottenham", "Willcox",
        ],
    },
    "Itsekiri": {
        "male_first": [
            "Olu", "Emiko", "Atake", "Ilu", "Omatseye",
            "Ogheneruona", "Reuben", "Oritsegbemi", "Uwa", "Idehen",
            "Oyiboma", "Edun", "Efemini", "Omatsuli", "Agbajor",
        ],
        "female_first": [
            "Arhinah", "Atarhe", "Igbini", "Omolewa", "Omosede",
            "Oyiboma", "Edith", "Yeri", "Oritse", "Emitset",
            "Natasha", "Oreva", "Rapu", "Barivule", "Abigail",
        ],
        "surnames": [
            "Wariboko", "Ejinye", "Omolulu", "Deren", "Faithful",
            "Emiko", "Atake", "Ometan", "Okorodudu", "Agbajor",
            "Ijasini", "Opuene", "Gbubemi", "Oritsebemigho", "Ajasin",
        ],
    },
    "Urhobo": {
        "male_first": [
            "Ovwighose", "Akpoveta", "Oghene", "Onome", "Ubie",
            "Oreva", "Igbi", "Ekpoko", "Orhewere", "Oyovwikorita",
            "Uruemuesiri", "Oghenetejiri", "Izefunam", "Obarisi",
            "Udoka",
        ],
        "female_first": [
            "Ejunakavwe", "Ogheneruona", "Arhiomah", "Ighogbadu",
            "Efe", "Efemini", "Ore", "Ivie", "Evwerero", "Ufuoma",
            "Oyiboma", "Oghenemega", "Rereloluwa", "Osasu", "Kemeli",
        ],
        "surnames": [
            "Ovwighose", "Oyiboh", "Ometan", "Ugbogbo", "Okorodudu",
            "Akpitaye", "Dukuye", "Oladiri", "Oreva", "Gbide",
            "Okorotie", "Igbini", "Umuakpo", "Owhorkevbe", "Omavwa",
        ],
    },
    "Isoko": {
        "male_first": [
            "Oteri", "Emamoke", "Izu", "Obro", "Oviomughe",
            "Tagbarha", "Othuke", "Eruotor", "Ogbodu", "Irhue",
            "Udumeze", "Ivbagbe", "Akpo", "Oghenejivwe", "Onothuomah",
        ],
        "female_first": [
            "Oweni", "Ejogharogho", "Emamoke", "Igho", "Omoyibo",
            "Oghenemine", "Irewunmi", "Oreva", "Ivbiomah", "Enajero",
            "Edueche", "Evotobo", "Otesiri", "Rereloluwa", "Amatome",
        ],
        "surnames": [
            "Oteri", "Ogbodu", "Tagbarha", "Ivuomughe", "Irhue",
            "Emamoke", "Obrovhweyovwe", "Olipriye", "Umukoro", "Afiomah",
            "Egbo", "Udumeze", "Akpotu", "Omuvwie", "Eferakeya",
        ],
    },
    "Ukwuani": {
        "male_first": [
            "Chukwuemeka", "Obinna", "Ikem", "Uche", "Munachi",
            "Chinedu", "Nnamdi", "Kenechukwu", "Arinze", "Ejima",
            "Chibuzor", "Onyeka", "Udochukwu", "Nwachukwu", "Ifedinma",
        ],
        "female_first": [
            "Adaeze", "Nneka", "Chinwe", "Uchechi", "Ifeoma",
            "Chidinma", "Kamsiyochukwu", "Obiageli", "Ngozi", "Azubuike",
            "Chiamaka", "Ogechi", "Ijemba", "Chisimdi", "Obioma",
        ],
        "surnames": [
            "Aniukwu", "Nwabuwa", "Uzoh", "Obiorah", "Okonkwo",
            "Ekwueme", "Udeze", "Chukwu", "Eze", "Igwe",
            "Nwankwo", "Obi", "Uchechukwu", "Amuche", "Azikiwe",
        ],
    },
    "Ndokwa": {
        "male_first": [
            "Nnamdi", "Chukwuemeka", "Obiora", "Ikemba", "Uchechukwu",
            "Emeka", "Chinedu", "Kenechukwu", "Ifesinachi", "Arinze",
            "Nwachukwu", "Chibuike", "Onochie", "Ejimofor", "Udo",
        ],
        "female_first": [
            "Adaeze", "Chinwe", "Obiageli", "Nneka", "Ifeanyichukwu",
            "Chidinma", "Uchechi", "Ngozi", "Azubuike", "Kamsiyochukwu",
            "Chiamaka", "Obioma", "Ogechi", "Nwakaego", "Ijemba",
        ],
        "surnames": [
            "Nwachukwu", "Obiora", "Ekwueme", "Okafor", "Nwabuwa",
            "Udeze", "Igwe", "Chukwu", "Eze", "Obi",
            "Okonkwo", "Amuche", "Onyeka", "Nkemdilim", "Azikiwe",
        ],
    },
    "Ika": {
        "male_first": [
            "Chukwudi", "Obiora", "Nnamdi", "Ifeanyichukwu", "Emeka",
            "Chinedu", "Uche", "Kenechukwu", "Arinze", "Ikem",
            "Chibuzor", "Onyeka", "Nwachukwu", "Ogechi", "Udochukwu",
        ],
        "female_first": [
            "Adaeze", "Chinwe", "Obiageli", "Nneka", "Uchechi",
            "Chidinma", "Ifeoma", "Ngozi", "Kamsiyochukwu", "Azubuike",
            "Chiamaka", "Ogechi", "Obioma", "Ijemba", "Chisimdi",
        ],
        "surnames": [
            "Okafor", "Nwosu", "Igwe", "Obi", "Ekwueme",
            "Nwankwo", "Chukwuemeka", "Eze", "Onyeka", "Amadi",
            "Uchechukwu", "Okoye", "Azikiwe", "Ikenga", "Obiora",
        ],
    },
}

# Allow generating a name even if an exact community is not in the pool
# (future-proofing for new communities).
_DEFAULT_POOL = {
    "male_first": ["Chukwudi", "Emeka", "Obiora", "Ikem", "Uche"],
    "female_first": ["Adaeze", "Chinwe", "Nneka", "Ifeoma", "Chidinma"],
    "surnames": ["Okafor", "Nwosu", "Obi", "Eze", "Nwankwo"],
}

# ---------------------------------------------------------------------------
# Name generation
# ---------------------------------------------------------------------------

def generate_name(community: str, gender: str, *, seed: int | None = None) -> str:
    """Return a fictional Deltaian full name appropriate to *community*.

    The *gender* selects the first-name pool (male / female / other draws
    uniformly from both pools).  An optional *seed* is accepted for
    deterministic testing but is otherwise unused.
    """
    rng = random.Random(seed) if seed is not None else random
    pool = _NAME_POOLS.get(community, _DEFAULT_POOL)

    if gender == "Male":
        first = rng.choice(pool["male_first"])
    elif gender == "Female":
        first = rng.choice(pool["female_first"])
    else:
        first = rng.choice(pool["male_first"] + pool["female_first"])

    surname = rng.choice(pool["surnames"])
    return f"{first} {surname}"


# ---------------------------------------------------------------------------
# Citizen ID sequencing
# ---------------------------------------------------------------------------

# Schema for the per-city counter table.  The migration script
# (scripts/delta_city_migrate.py) creates this table as part of the full
# schema, but next_citizen_id() is defensive: it (re)creates the table on
# demand so the module works even against a database that predates the
# migration.
_SEQUENCES_DDL = """
CREATE TABLE IF NOT EXISTS citizen_id_sequences (
    city        TEXT PRIMARY KEY,
    next_number INTEGER NOT NULL CHECK (next_number >= 1)
)
"""


def _ensure_sequences_table(conn: sqlite3.Connection) -> None:
    """Create citizen_id_sequences if missing and seed one row per city.

    Idempotent: safe to call on every connection.  Seeding every city up
    front keeps next_citizen_id() a plain UPDATE inside the transaction,
    which avoids a read-then-write race entirely.  Never commits on its
    own so it can safely run inside the caller's transaction.
    """
    conn.execute(_SEQUENCES_DDL)
    for city in CITY_PREFIXES:
        conn.execute(
            "INSERT OR IGNORE INTO citizen_id_sequences (city, next_number) "
            "VALUES (?, 1)",
            (city,),
        )


def next_citizen_id(conn: sqlite3.Connection, city: str) -> str:
    """Atomically allocate and return the next Citizen ID for *city*.

    Uses a single SQLite transaction (BEGIN IMMEDIATE grabs the write
    lock) so two concurrent callers can never receive the same number.
    The ``citizen_id_sequences`` counter table is created/seeded on
    demand, so this works even before the migration has run.  If the
    caller's connection is already inside a transaction, the allocation
    joins that transaction instead of committing it out from under the
    caller.
    """
    if city not in CITY_PREFIXES:
        raise ValueError(f"Unknown city: {city!r}")

    # Record transaction state BEFORE touching the schema: _ensure_sequences_table
    # can open an implicit transaction of its own (its INSERT OR IGNORE under
    # default isolation level), and that must not be mistaken for a caller's
    # outer transaction — otherwise the allocation would never be committed.
    in_outer_txn = conn.in_transaction
    _ensure_sequences_table(conn)
    prefix = CITY_PREFIXES[city]

    if not in_outer_txn and conn.in_transaction:
        # Our own seeding opened an implicit transaction; commit it so the
        # schema and seed rows are visible to other connections, then start
        # a fresh immediate transaction for the allocation itself.
        conn.commit()
    if not in_outer_txn:
        # BEGIN IMMEDIATE grabs a reserved write lock so two threads
        # cannot both read the same counter at the same time.
        conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.execute(
            "SELECT next_number FROM citizen_id_sequences WHERE city = ?",
            (city,),
        )
        row = cur.fetchone()
        if row is None:
            # Seed the counter for this city on first use.
            conn.execute(
                "INSERT INTO citizen_id_sequences (city, next_number) VALUES (?, 2)",
                (city,),
            )
            number = 1
        else:
            number = int(row[0])
            conn.execute(
                "UPDATE citizen_id_sequences SET next_number = next_number + 1 "
                "WHERE city = ?",
                (city,),
            )
        if not in_outer_txn:
            conn.execute("COMMIT")
    except BaseException:
        if not in_outer_txn:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
        raise

    return f"DC-{prefix}-{number:04d}"


def make_citizen_id(city: str, number: int) -> str:
    """Format a known city + sequence number into a Citizen ID string."""
    prefix = CITY_PREFIXES[city]
    return f"DC-{prefix}-{number:04d}"


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def validate_city(city: str) -> str | None:
    """Return the canonical city name or *None* if invalid."""
    for name in CITY_PREFIXES:
        if name.lower() == city.strip().lower():
            return name
    return None


def validate_community(city: str, community: str) -> str | None:
    """Return the canonical community name for *city*, or *None*."""
    comms = CITY_COMMUNITIES.get(city, [])
    for c in comms:
        if c.lower() == community.strip().lower():
            return c
    return None


def validate_gender(gender: str) -> str | None:
    """Return the canonical gender string or *None* if invalid."""
    for g in GENDER_OPTIONS:
        if g.lower() == gender.strip().lower():
            return g
    return None


def is_community(city: str, community: str) -> bool:
    """Return True if *community* is a valid option for *city*."""
    return validate_community(city, community) is not None
