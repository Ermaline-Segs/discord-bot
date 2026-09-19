"""Tests for delta_city/identity: city prefixes, name pools, ID sequencing."""
import sqlite3

import pytest

from delta_city import identity


@pytest.fixture
def conn(tmp_path):
    """A raw SQLite connection with no schema (identity must be defensive)."""
    connection = sqlite3.connect(tmp_path / "identity.db")
    yield connection
    connection.close()


# ---- city prefixes ---------------------------------------------------------


def test_city_prefixes_match_spec():
    assert identity.CITY_PREFIXES == {
        "Asaba": "ASB",
        "Warri": "WAR",
        "Ughelli": "UGH",
        "Ozoro": "OZO",
        "Kwale": "KWA",
        "Agbor": "AGB",
    }


def test_city_community_map_matches_spec():
    assert identity.CITY_COMMUNITIES["Asaba"] == ["Anioma"]
    assert set(identity.CITY_COMMUNITIES["Warri"]) == {"Pidgin", "Itsekiri", "Urhobo"}
    assert identity.CITY_COMMUNITIES["Ughelli"] == ["Urhobo"]
    assert identity.CITY_COMMUNITIES["Ozoro"] == ["Isoko"]
    assert set(identity.CITY_COMMUNITIES["Kwale"]) == {"Ukwuani", "Ndokwa"}
    assert identity.CITY_COMMUNITIES["Agbor"] == ["Ika"]


def test_pidgin_is_not_labeled_a_tribe():
    # Pidgin is a language community, not an ethnic tribe; the module
    # stores it under a neutral "community" field, never "tribe".
    assert identity.is_community("Warri", "Pidgin")
    assert not any("tribe" in k.lower() for k in identity._NAME_POOLS.get("Pidgin", {}))


# ---- citizen ID sequencing -------------------------------------------------


def test_first_asaba_id_is_dc_asb_0001(conn):
    assert identity.next_citizen_id(conn, "Asaba") == "DC-ASB-0001"


def test_first_warri_id_is_dc_war_0001(conn):
    assert identity.next_citizen_id(conn, "Warri") == "DC-WAR-0001"


def test_city_sequences_are_independent(conn):
    ids = []
    for _ in range(3):
        ids.append(identity.next_citizen_id(conn, "Asaba"))
        ids.append(identity.next_citizen_id(conn, "Warri"))
    assert ids == [
        "DC-ASB-0001", "DC-WAR-0001",
        "DC-ASB-0002", "DC-WAR-0002",
        "DC-ASB-0003", "DC-WAR-0003",
    ]


def test_sequential_numbering_never_skips_or_repeats(conn):
    ids = [identity.next_citizen_id(conn, "Agbor") for _ in range(10)]
    assert ids == [f"DC-AGB-{n:04d}" for n in range(1, 11)]
    assert len(set(ids)) == len(ids)


def test_reopen_connection_continues_sequence(tmp_path):
    dbfile = tmp_path / "persist.db"
    c1 = sqlite3.connect(dbfile)
    identity.next_citizen_id(c1, "Kwale")
    c1.close()

    c2 = sqlite3.connect(dbfile)
    assert identity.next_citizen_id(c2, "Kwale") == "DC-KWA-0002"
    c2.close()


def test_unknown_city_rejected(conn):
    with pytest.raises(ValueError):
        identity.next_citizen_id(conn, "Lagos")


def test_duplicate_citizen_id_impossible(conn):
    """Pull 100 IDs across all six cities; none may repeat."""
    seen = set()
    for _ in range(16):
        for city in identity.CITY_PREFIXES:
            cid = identity.next_citizen_id(conn, city)
            assert cid not in seen
            seen.add(cid)
    assert len(seen) == 96


def test_ensure_sequences_table_is_idempotent(conn):
    identity._ensure_sequences_table(conn)
    identity._ensure_sequences_table(conn)
    assert identity.next_citizen_id(conn, "Ozoro") == "DC-OZO-0001"


def test_make_citizen_id_formats_four_digits():
    assert identity.make_citizen_id("Asaba", 1) == "DC-ASB-0001"
    assert identity.make_citizen_id("Asaba", 12345) == "DC-ASB-12345"


# ---- name generation -------------------------------------------------------


def test_generate_name_returns_first_and_last():
    name = identity.generate_name("Anioma", "Male", seed=42)
    parts = name.split(" ")
    assert len(parts) == 2
    assert all(part.isalpha() for part in parts)


def test_generate_name_female_pool():
    name = identity.generate_name("Itsekiri", "Female", seed=7)
    assert len(name.split(" ")) == 2


def test_generate_name_other_gender():
    name = identity.generate_name("Ika", "Other", seed=3)
    assert len(name.split(" ")) == 2


def test_generate_name_uses_community_pool():
    male = {identity.generate_name("Anioma", "Male", seed=s) for s in range(40)}
    # Names must come from the Anioma pool, not the default pool.
    pool_firsts = set(identity._NAME_POOLS["Anioma"]["male_first"])
    pool_surnames = set(identity._NAME_POOLS["Anioma"]["surnames"])
    for name in male:
        first, _, surname = name.partition(" ")
        assert first in pool_firsts
        assert surname in pool_surnames


def test_generate_name_unknown_community_falls_back():
    name = identity.generate_name("Atlantis", "Male")
    assert len(name.split(" ")) == 2


# ---- validation helpers ----------------------------------------------------


def test_validate_city_case_insensitive():
    assert identity.validate_city("ASABA") == "Asaba"
    assert identity.validate_city("  warri ") == "Warri"
    assert identity.validate_city("Kanos") is None


def test_validate_community_rejects_wrong_city():
    assert identity.validate_community("Asaba", "Anioma") == "Anioma"
    assert identity.validate_community("Asaba", "Urhobo") is None
    assert identity.validate_community("Warri", "PIDGIN") == "Pidgin"


def test_validate_gender():
    assert identity.validate_gender("female") == "Female"
    assert identity.validate_gender("OTHER") == "Other"
    assert identity.validate_gender("robot") is None