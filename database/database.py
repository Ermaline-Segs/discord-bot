"""SQLite persistence layer for the DELTA CITY bot.

Tables
------
citizens        one row per registered Discord member
state_counters  per-state sequence used to build citizen IDs
appointments    active and historical government appointments
audit_log       append-only history of notable actions

The class is intentionally small and synchronous: discord.py already runs
on a single thread, and every method takes the lock so the same rules hold
inside tests.
"""

import os
import sqlite3
import threading
from datetime import datetime, timezone

from config.settings import NATION_CODE, STATE_CODES, STATES


def _now():
    """Current UTC time as an ISO-8601 string (millisecond precision)."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


# Every citizen ID is built from this template: DC-ASB-0001
_CITIZEN_ID_FORMAT = "{nation}-{code}-{number:04d}"


class DeltaCityDB:
    def __init__(self, db_path):
        self.db_path = str(db_path)
        self._lock = threading.RLock()
        # SQLite refuses to connect when the parent folder of the db file
        # does not exist yet (fresh clones have no data/ directory).
        parent = os.path.dirname(os.path.abspath(self.db_path))
        os.makedirs(parent, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._init_schema()

    # ---- schema ----

    def _init_schema(self):
        with self._lock, self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS citizens (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    discord_id TEXT UNIQUE NOT NULL,
                    citizen_id TEXT UNIQUE NOT NULL,
                    name TEXT NOT NULL,
                    gender TEXT NOT NULL,
                    registered_state TEXT NOT NULL,
                    state TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'Citizen',
                    joined_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS state_counters (
                    state TEXT PRIMARY KEY,
                    next_number INTEGER NOT NULL DEFAULT 1
                );

                CREATE TABLE IF NOT EXISTS appointments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    discord_id TEXT NOT NULL,
                    role_name TEXT NOT NULL,
                    state TEXT,
                    appointed_at TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1
                );

                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    actor_id TEXT,
                    action TEXT NOT NULL,
                    subject_id TEXT,
                    details TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_appointments_role
                    ON appointments(role_name, active);
                CREATE INDEX IF NOT EXISTS idx_citizens_state
                    ON citizens(state);
                """
            )
            self._ensure_registered_state()
            for state in STATES:
                self._conn.execute(
                    "INSERT OR IGNORE INTO state_counters (state, next_number) "
                    "VALUES (?, 1)",
                    (state,),
                )

    def _ensure_registered_state(self):
        """Add the registered_state column to older databases, if missing."""
        with self._lock:
            cols = [
                row[1]
                for row in self._conn.execute("PRAGMA table_info(citizens)")
            ]
            if "registered_state" not in cols:
                self._conn.execute(
                    "ALTER TABLE citizens ADD COLUMN registered_state TEXT"
                )
                self._conn.execute(
                    "UPDATE citizens SET registered_state = state "
                    "WHERE registered_state IS NULL"
                )
                self._conn.commit()

    def _get_row(self, discord_id):
        """Return the citizen row for a Discord user ID, or None.

        Callers must already hold self._lock.
        """
        cur = self._conn.execute(
            "SELECT * FROM citizens WHERE discord_id = ?",
            (str(int(discord_id)),),
        )
        row = cur.fetchone()
        return dict(row) if row else None

    # ---- citizen IDs ----

    def make_citizen_id(self, state: str, number: int) -> str:
        """Build a permanent citizen ID, e.g. DC-ASB-0001."""
        code = STATE_CODES.get(state)
        if code is None:
            raise ValueError(f"Unknown state: {state}")
        return _CITIZEN_ID_FORMAT.format(
            nation=NATION_CODE, code=code, number=number
        )

    def next_citizen_number(self, state: str) -> int:
        """Safely take the next registration number for one state.

        The counter row is bumped in a single atomic UPDATE, so two
        registrations happening at the same time can never receive the
        same number. Each state keeps its own counter, so the first
        Asaba citizen is DC-ASB-0001 and the first Warri citizen is
        DC-WAR-0001 independently.
        """
        if state not in STATE_CODES:
            raise ValueError(f"Unknown state: {state}")
        with self._lock:
            with self._conn:
                self._conn.execute(
                    "INSERT OR IGNORE INTO state_counters "
                    "(state, next_number) VALUES (?, 1)",
                    (state,),
                )
                self._conn.execute(
                    "UPDATE state_counters SET next_number = next_number + 1 "
                    "WHERE state = ?",
                    (state,),
                )
                row = self._conn.execute(
                    "SELECT next_number FROM state_counters WHERE state = ?",
                    (state,),
                ).fetchone()
                return int(row["next_number"]) - 1

    # ---- registration ----

    def register_citizen(
        self,
        discord_id,
        name: str,
        gender: str,
        state: str,
        status: str = "Citizen",
    ) -> tuple:
        """Create a citizen profile and return (row_dict, created: bool).

        If the Discord account already has a profile, the existing record
        is returned and created is False — Citizen IDs are permanent and
        are never re-issued, even after the member leaves and rejoins.
        """
        if state not in STATE_CODES:
            raise ValueError(f"Unknown state: {state}")
        with self._lock:
            existing = self._get_row(int(discord_id))
            if existing is not None:
                return existing, False
            number = self.next_citizen_number(state)
            now = _now()
            citizen_id = self.make_citizen_id(state, number)
            with self._conn:
                self._conn.execute(
                    "INSERT INTO citizens (discord_id, citizen_id, name, gender, "
                    "registered_state, state, status, joined_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(int(discord_id)),
                        citizen_id,
                        name,
                        gender,
                        state,
                        state,
                        status,
                        now,
                        now,
                    ),
                )
            self.log_audit(
                actor_id=None,
                action="register",
                subject_id=discord_id,
                details=f"Registered as {status} in {state} ({citizen_id})",
            )
            return (
                self._get_row(int(discord_id)),
                True,
            )

    # ---- lookups ----

    def get_citizens_in_state(self, state):
        """All citizens currently residing in the given state."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM citizens WHERE state = ? ORDER BY citizen_id",
                (state,),
            ).fetchall()
        return [dict(r) for r in rows]

    def citizen_count(self, state=None):
        """Total citizens, or citizens in one state when state is given."""
        with self._lock:
            if state is None:
                row = self._conn.execute(
                    "SELECT COUNT(*) AS n FROM citizens"
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT COUNT(*) AS n FROM citizens WHERE state = ?",
                    (state,),
                ).fetchone()
        return int(row["n"])

    def count_by_state_and_status(self):
        """Return {state: {status: count}} for all citizens in one query."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT state, status, COUNT(*) AS n FROM citizens "
                "GROUP BY state, status"
            ).fetchall()
        counts = {}
        for row in rows:
            counts.setdefault(row["state"], {})[row["status"]] = int(row["n"])
        return counts

    def get_citizen(self, discord_id):
        """Return the full citizen record for a Discord user ID, or None."""
        with self._lock:
            return self._get_row(int(discord_id))

    def get_citizen_by_id(self, citizen_id):
        """Return the citizen record matching a Citizen ID, e.g. DC-ASB-0001."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM citizens WHERE citizen_id = ?",
                (citizen_id.strip().upper(),),
            ).fetchone()
        return dict(row) if row else None

    def search_citizens(self, query):
        """Search citizens by name or Citizen ID (case-insensitive)."""
        q = query.strip()
        if not q:
            return []
        like = f"%{q}%"
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM citizens "
                "WHERE name LIKE ? OR citizen_id LIKE ? "
                "ORDER BY citizen_id",
                (like, like),
            ).fetchall()
        return [dict(r) for r in rows]

    def all_citizens(self):
        """All citizen records ordered by Citizen ID (for the registry view)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM citizens ORDER BY citizen_id"
            ).fetchall()
        return [dict(r) for r in rows]

    # ---- edits ----

    def update_citizen(self, discord_id, actor_id=None, **fields):
        """Edit profile fields (name, gender, state, status).

        Returns the list of (field, old, new) tuples that were applied.
        Every change is written to the audit log. Unknown keys are ignored.
        """
        editable = ("name", "gender", "state", "status")
        with self._lock:
            row = self._get_row(int(discord_id))
            if row is None:
                return []
            changes = []
            with self._conn:
                for key, value in fields.items():
                    if key not in editable or value is None:
                        continue
                    if str(row[key]) == str(value):
                        continue
                    changes.append((key, row[key], value))
                    self._conn.execute(
                        f"UPDATE citizens SET {key} = ? WHERE discord_id = ?",
                        (value, str(int(discord_id))),
                    )
                if changes:
                    self._conn.execute(
                        "UPDATE citizens SET updated_at = ? WHERE discord_id = ?",
                        (_now(), str(int(discord_id))),
                    )
            for key, old, new in changes:
                self.log_audit(
                    actor_id,
                    "edit",
                    discord_id,
                    f"{row['citizen_id']} {key}: {old!r} -> {new!r}",
                )
            return changes

    def set_status(self, discord_id, status, actor_id=None):
        """Change a citizen's status (Citizen / Resident / Visitor / Inactive)."""
        return self.update_citizen(discord_id, actor_id=actor_id, status=status)

    def change_state(self, discord_id, new_state, actor_id=None):
        """Move a citizen to a new state of residence.

        The Citizen ID is permanent and NEVER changes - only the residence
        (state column) is updated. The move is written to the audit log.
        Returns True when a move happened, False when already there.
        """
        if new_state not in STATE_CODES:
            raise ValueError(f"Unknown state: {new_state}")
        with self._lock:
            row = self._get_row(int(discord_id))
            if row is None:
                return False
            if row["state"] == new_state:
                return False
            old_state = row["state"]
            with self._conn:
                self._conn.execute(
                    "UPDATE citizens SET state = ?, updated_at = ? "
                    "WHERE discord_id = ?",
                    (new_state, _now(), str(int(discord_id))),
                )
            self.log_audit(
                actor_id,
                "move",
                discord_id,
                f"{row['citizen_id']} moved from {old_state} to {new_state}",
            )
            return True

    # ---- government appointments ----

    def log_appointment(self, discord_id, role_name, state=None, actor_id=None):
        """Record a new government appointment (replaces the active one)."""
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE appointments SET active = 0 "
                "WHERE discord_id = ? AND role_name = ? AND active = 1",
                (str(int(discord_id)), role_name),
            )
            self._conn.execute(
                "INSERT INTO appointments (discord_id, role_name, state, appointed_at, active) "
                "VALUES (?, ?, ?, ?, 1)",
                (str(int(discord_id)), role_name, state, _now()),
            )
        self.log_audit(
            actor_id,
            "appoint",
            discord_id,
            f"Appointed to {role_name}" + (f" of {state}" if state else ""),
        )

    def void_appointment(self, discord_id, role_name, state=None, actor_id=None):
        """Remove a holder's active appointment. Returns True if one existed."""
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT id FROM appointments "
                "WHERE discord_id = ? AND role_name = ? AND active = 1",
                (str(int(discord_id)), role_name),
            ).fetchone()
            if row is None:
                return False
            self._conn.execute(
                "UPDATE appointments SET active = 0 WHERE id = ?", (row["id"],)
            )
        self.log_audit(
            actor_id,
            "dismiss",
            discord_id,
            f"Dismissed from {role_name}" + (f" of {state}" if state else ""),
        )
        return True

    def appointment_holder(self, role_name, state=None):
        """Current active holder of a role (unique offices), or None."""
        with self._lock:
            if state is None:
                row = self._conn.execute(
                    "SELECT * FROM appointments WHERE role_name = ? AND active = 1 "
                    "ORDER BY appointed_at DESC LIMIT 1",
                    (role_name,),
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT * FROM appointments WHERE role_name = ? AND state = ? "
                    "AND active = 1 ORDER BY appointed_at DESC LIMIT 1",
                    (role_name, state),
                ).fetchone()
        return dict(row) if row is not None else None

    def active_appointments(self, discord_id=None):
        """All active appointments, optionally for one user."""
        with self._lock:
            if discord_id is None:
                rows = self._conn.execute(
                    "SELECT * FROM appointments WHERE active = 1 "
                    "ORDER BY appointed_at ASC"
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM appointments WHERE active = 1 AND discord_id = ? "
                    "ORDER BY appointed_at ASC",
                    (str(int(discord_id)),),
                ).fetchall()
        return [dict(r) for r in rows]

    # ---- audit log ----

    def log_audit(self, actor_id, action, subject_id, details=None):
        """Append one entry to the append-only audit log."""
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO audit_log (actor_id, action, subject_id, details, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    str(int(actor_id)) if actor_id is not None else None,
                    action,
                    str(int(subject_id)) if subject_id is not None else None,
                    details,
                    _now(),
                ),
            )

    def recent_audit(self, limit=10):
        """Most recent audit entries, newest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ---- lifecycle ----

    def close(self):
        with self._lock:
            self._conn.close()
