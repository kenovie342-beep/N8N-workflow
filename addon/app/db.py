from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import env


class Base(DeclarativeBase):
    pass


engine = None
SessionLocal = None


def configure(url: str | None = None):
    """(Re)create the engine. Called at startup and by tests."""
    global engine, SessionLocal
    url = url or env("DATABASE_URL", "sqlite:////data/suite.db")
    kwargs: dict = {}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
    else:
        kwargs["pool_pre_ping"] = True
        kwargs["pool_size"] = 20
        kwargs["max_overflow"] = 20
    engine = create_engine(url, **kwargs)
    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _pragmas(dbapi_conn, _):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.close()

    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    return engine


def session():
    if SessionLocal is None:
        configure()
    return SessionLocal()


def get_db():
    db = session()
    try:
        yield db
    finally:
        db.close()
