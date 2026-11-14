from alembic import context
from sqlalchemy import create_engine

from app import models  # noqa: F401  (register tables)
from app.config import env
from app.db import Base

target_metadata = Base.metadata


def _url() -> str:
    return env("DATABASE_URL", "sqlite:////data/suite.db")


def run_migrations_online():
    engine = create_engine(_url())
    with engine.connect() as conn:
        context.configure(connection=conn, target_metadata=target_metadata, render_as_batch=_url().startswith("sqlite"))
        with context.begin_transaction():
            context.run_migrations()


run_migrations_online()
