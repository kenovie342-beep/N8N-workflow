import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def uid() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    """Naive UTC. All timestamps are stored as naive UTC for SQLite/Postgres parity."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat() + "Z" if dt else None


class Setting(Base):
    """LLM / social / cloudflare / github configuration. Secrets are Fernet-encrypted."""

    __tablename__ = "settings"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    kind: Mapped[str] = mapped_column(String, index=True)  # llm | social | cloudflare | github
    name: Mapped[str] = mapped_column(String)
    config_json: Mapped[str] = mapped_column(Text, default="{}")
    secret_enc: Mapped[str] = mapped_column(Text, default="")
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AppSetting(Base):
    __tablename__ = "app_settings"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    task: Mapped[str] = mapped_column(String, index=True)
    workflow: Mapped[str] = mapped_column(String, default="")
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String, index=True, default="queued")
    priority: Mapped[int] = mapped_column(Integer, default=5)  # higher runs first
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    error: Mapped[str] = mapped_column(Text, default="")
    result_json: Mapped[str] = mapped_column(Text, default="{}")
    dedupe_key: Mapped[str | None] = mapped_column(String, nullable=True)
    schedule_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    executor: Mapped[str] = mapped_column(String, default="local")  # local | github
    remote_run_id: Mapped[str] = mapped_column(String, default="")
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    run_after: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (
        # Duplicate prevention: only one *active* job per dedupe_key.
        Index(
            "uq_jobs_active_dedupe",
            "dedupe_key",
            unique=True,
            sqlite_where=text("status IN ('queued','running') AND dedupe_key IS NOT NULL"),
            postgresql_where=text("status IN ('queued','running') AND dedupe_key IS NOT NULL"),
        ),
        Index("ix_jobs_claim", "status", "priority", "created_at"),
    )


class JobLog(Base):
    __tablename__ = "job_logs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(String, index=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    level: Mapped[str] = mapped_column(String, default="info")
    message: Mapped[str] = mapped_column(Text, default="")


class Schedule(Base):
    __tablename__ = "schedules"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String)
    task: Mapped[str] = mapped_column(String)
    workflow: Mapped[str] = mapped_column(String, default="")
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    kind: Mapped[str] = mapped_column(String, default="cron")  # cron | once | interval
    cron_expr: Mapped[str] = mapped_column(String, default="")
    run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # UTC, for kind=once
    interval_seconds: Mapped[int] = mapped_column(Integer, default=0)
    timezone: Mapped[str] = mapped_column(String, default="Africa/Lagos")
    priority: Mapped[int] = mapped_column(Integer, default=5)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class CallbackNonce(Base):
    __tablename__ = "callback_nonces"
    nonce: Mapped[str] = mapped_column(String, primary_key=True)
    job_id: Mapped[str] = mapped_column(String, default="")
    seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
