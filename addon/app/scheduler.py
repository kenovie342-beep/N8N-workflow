"""Persistent scheduler: next_run_at lives in the DB, so schedules survive restarts."""
import json
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter
from sqlalchemy import select

from . import jobs, store
from .models import Schedule, utcnow

log = logging.getLogger("suite.scheduler")
MISFIRE_GRACE = 3600


def _tz(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"Unknown timezone: {name}")


def to_utc(dt: datetime, tzname: str) -> datetime:
    """Interpret a naive datetime in tzname (or convert an aware one) and return naive UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_tz(tzname))
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def compute_next(kind: str, *, cron_expr: str = "", run_at=None, interval_seconds: int = 0,
                 tzname: str = "Africa/Lagos", after: datetime | None = None) -> datetime | None:
    """Next fire time as naive UTC strictly after `after` (naive UTC)."""
    after = after or utcnow()
    if kind == "once":
        return run_at if run_at and run_at > after else None
    if kind == "interval":
        return after + timedelta(seconds=interval_seconds)
    if kind == "cron":
        tz = _tz(tzname)
        local = after.replace(tzinfo=timezone.utc).astimezone(tz)
        nxt = croniter(cron_expr, local).get_next(datetime)
        return nxt.astimezone(timezone.utc).replace(tzinfo=None)
    raise ValueError("kind must be cron, once or interval")


def validate(kind: str, cron_expr: str, run_at, interval_seconds: int, tzname: str) -> None:
    _tz(tzname)
    if kind == "cron":
        if not cron_expr or not croniter.is_valid(cron_expr):
            raise ValueError("Invalid cron expression")
    elif kind == "once":
        if not run_at:
            raise ValueError("run_at is required for one-time schedules")
    elif kind == "interval":
        if interval_seconds < 30:
            raise ValueError("interval_seconds must be at least 30")
    else:
        raise ValueError("kind must be cron, once or interval")


def apply_fields(s: Schedule, d: dict, default_tz: str) -> None:
    """Apply API fields to a Schedule and recompute next_run_at."""
    for f in ("name", "task", "workflow", "kind", "cron_expr", "interval_seconds", "timezone", "priority"):
        if d.get(f) is not None:
            setattr(s, f, d[f])
    if d.get("payload") is not None:
        s.payload_json = json.dumps(d["payload"])
    s.timezone = s.timezone or default_tz
    if d.get("run_at"):
        r = d["run_at"] if isinstance(d["run_at"], datetime) else datetime.fromisoformat(str(d["run_at"]).replace("Z", "+00:00"))
        s.run_at = to_utc(r, s.timezone)
    validate(s.kind, s.cron_expr, s.run_at, s.interval_seconds, s.timezone)
    s.updated_at = utcnow()
    s.next_run_at = compute_next(s.kind, cron_expr=s.cron_expr, run_at=s.run_at,
                                 interval_seconds=s.interval_seconds, tzname=s.timezone) if s.enabled else None


def tick(db, now: datetime | None = None, grace: int = MISFIRE_GRACE) -> int:
    """Enqueue every due schedule exactly once per fire time. Returns number of jobs created."""
    now = now or utcnow()
    created = 0
    due = db.scalars(select(Schedule).where(Schedule.enabled == True, Schedule.next_run_at <= now)).all()  # noqa: E712
    for s in due:
        fire = s.next_run_at
        key = f"sched:{s.id}:{fire.isoformat()}"
        late = (now - fire).total_seconds()
        skip = s.kind != "once" and late > grace
        already = db.scalars(select(jobs.Job.id).where(jobs.Job.dedupe_key == key)).first()
        if not skip and not already:
            jobs.enqueue(db, s.task, json.loads(s.payload_json), priority=s.priority, dedupe_key=key,
                         schedule_id=s.id, workflow=s.workflow or s.name, commit=False)
            created += 1
            s.last_run_at = now
        elif skip:
            log.warning("Schedule %s misfired by %ds; skipping to next slot", s.id, late)
        if s.kind == "once":
            s.enabled, s.next_run_at = False, None
        else:
            s.next_run_at = compute_next(s.kind, cron_expr=s.cron_expr, interval_seconds=s.interval_seconds,
                                         tzname=s.timezone, after=max(now, fire))
        db.commit()
    return created


def upcoming(db, limit: int = 20, per_schedule: int = 3) -> list[dict]:
    out = []
    for s in db.scalars(select(Schedule).where(Schedule.enabled == True)).all():  # noqa: E712
        after, nxt = utcnow(), s.next_run_at
        for _ in range(per_schedule):
            if not nxt:
                break
            out.append({"schedule_id": s.id, "name": s.name, "task": s.task, "at_utc": nxt.isoformat() + "Z",
                        "at_local": nxt.replace(tzinfo=timezone.utc).astimezone(_tz(s.timezone)).isoformat(),
                        "timezone": s.timezone})
            after = nxt
            nxt = compute_next(s.kind, cron_expr=s.cron_expr, run_at=s.run_at, interval_seconds=s.interval_seconds,
                               tzname=s.timezone, after=after) if s.kind != "once" else None
    out.sort(key=lambda x: x["at_utc"])
    return out[:limit]


__all__ = ["store"]
