import pytest

from database.database import DeltaCityDB


@pytest.fixture
def db(tmp_path):
    """A fresh in-file SQLite database for each test."""
    database = DeltaCityDB(tmp_path / "deltacity.db")
    yield database
    database.close()