import pytest

from harvester.db import DB
from tests.helpers import make_settings


@pytest.fixture
def s(tmp_path):
    return make_settings(tmp_path)


@pytest.fixture
def db(s):
    return DB(s.db_path)
