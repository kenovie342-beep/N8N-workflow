import os

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from tests.conftest import PG_URL

URLS = ["sqlite"] + (["pg"] if PG_URL else [])
ROOT = os.path.dirname(os.path.dirname(__file__))
TABLES = {"settings", "app_settings", "jobs", "job_logs", "schedules", "callback_nonces", "alembic_version"}


@pytest.fixture(params=URLS)
def blank(request, tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path}/m.db" if request.param == "sqlite" else PG_URL
    eng = create_engine(url)
    with eng.begin() as c:
        if request.param == "pg":
            c.execute(text("DROP SCHEMA public CASCADE")); c.execute(text("CREATE SCHEMA public"))
    monkeypatch.setenv("DATABASE_URL", url)
    yield url, eng
    eng.dispose()


def cfg():
    c = Config(os.path.join(ROOT, "alembic.ini"))
    c.set_main_option("script_location", os.path.join(ROOT, "alembic"))
    return c


def test_upgrade_creates_all_tables_and_is_idempotent(blank):
    url, eng = blank
    command.upgrade(cfg(), "head")
    command.upgrade(cfg(), "head")     # running twice must be harmless (container restarts)
    assert TABLES <= set(inspect(eng).get_table_names())


def test_migration_preserves_existing_data_on_rerun(blank):
    url, eng = blank
    command.upgrade(cfg(), "head")
    with eng.begin() as c:
        c.execute(text("INSERT INTO app_settings (key, value) VALUES ('keep', 'me')"))
    command.upgrade(cfg(), "head")
    with eng.connect() as c:
        assert c.execute(text("SELECT value FROM app_settings WHERE key='keep'")).scalar() == "me"


def test_migrated_schema_matches_models(blank):
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from app import models  # noqa: F401
    from app.db import Base
    url, eng = blank
    command.upgrade(cfg(), "head")
    with eng.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == [], f"models and migrations drifted: {diff}"


def test_downgrade_roundtrip(blank):
    url, eng = blank
    command.upgrade(cfg(), "head")
    command.downgrade(cfg(), "base")
    assert "jobs" not in set(inspect(eng).get_table_names())
