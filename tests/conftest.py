import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from legomon.config import Config  # noqa: E402
from legomon.db import Store  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def config():
    return Config(ROOT)


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "test.sqlite3")
