from datetime import datetime, timedelta

from sqlalchemy import select

from app import db as dbmod, jobs, scheduler
from app.models import Job, Schedule, utcnow


def mk(client, **kw):
    body = {"name": "s", "task": "noop", "kind": "cron", "cron_expr": "0 9 * * *"}
    body.update(kw)
    r = client.post("/api/schedules", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_default_timezone_is_africa_lagos(client):
    assert client.get("/api/settings/app").json()["default_timezone"] == "Africa/Lagos"
    assert mk(client)["timezone"] == "Africa/Lagos"


def test_cron_in_lagos_converts_to_utc_correctly():
    # Lagos is UTC+1 all year (no DST): 09:00 Lagos == 08:00 UTC.
    after = datetime(2026, 11, 13, 0, 0)
    nxt = scheduler.compute_next("cron", cron_expr="0 9 * * *", tzname="Africa/Lagos", after=after)
    assert nxt == datetime(2026, 11, 13, 8, 0)
    # same expression in New York (UTC-5 in Nov) -> 14:00 UTC
    assert scheduler.compute_next("cron", cron_expr="0 9 * * *", tzname="America/New_York", after=after) == datetime(2026, 11, 13, 14, 0)
    # rolls to next day when already past
    assert scheduler.compute_next("cron", cron_expr="0 9 * * *", tzname="Africa/Lagos", after=datetime(2026, 11, 13, 8, 0)) == datetime(2026, 11, 14, 8, 0)


def test_once_schedule_local_time_converted(client):
    future = (datetime.utcnow() + timedelta(days=2)).replace(microsecond=0, second=0)
    local_iso = (future + timedelta(hours=1)).isoformat()  # naive local Lagos time == UTC+1
    s = mk(client, kind="once", run_at=local_iso, cron_expr="")
    assert s["next_run_at"] == future.isoformat() + "Z"


def test_crud_pause_resume_edit_delete(client):
    s = mk(client, cron_expr="*/5 * * * *")
    sid = s["id"]
    assert s["enabled"] and s["next_run_at"]
    p = client.post(f"/api/schedules/{sid}/pause").json()
    assert p["enabled"] is False and p["next_run_at"] is None
    r = client.post(f"/api/schedules/{sid}/resume").json()
    assert r["enabled"] and r["next_run_at"]
    e = client.put(f"/api/schedules/{sid}", json={"name": "renamed", "cron_expr": "30 6 * * 1", "timezone": "Europe/London"}).json()
    assert e["name"] == "renamed" and e["cron_expr"] == "30 6 * * 1" and e["timezone"] == "Europe/London"
    assert len(client.get("/api/schedules").json()) == 1
    assert client.delete(f"/api/schedules/{sid}").status_code == 200
    assert client.get("/api/schedules").json() == []
    assert client.put(f"/api/schedules/{sid}", json={"name": "x"}).status_code == 404


def test_validation_errors(client):
    assert client.post("/api/schedules", json={"name": "x", "task": "noop", "kind": "cron", "cron_expr": "nonsense"}).status_code == 400
    assert client.post("/api/schedules", json={"name": "x", "task": "noop", "kind": "interval", "interval_seconds": 5}).status_code == 400
    assert client.post("/api/schedules", json={"name": "x", "task": "noop", "kind": "cron", "cron_expr": "* * * * *", "timezone": "Mars/Base"}).status_code == 400
    assert client.post("/api/schedules", json={"name": "x", "task": "rm_rf", "kind": "cron", "cron_expr": "* * * * *"}).status_code == 400


def test_tick_fires_exactly_once_per_slot_and_advances(client, db):
    s = mk(client, kind="interval", cron_expr="", interval_seconds=60)
    row = db.get(Schedule, s["id"])
    row.next_run_at = utcnow() - timedelta(seconds=5)
    db.commit()
    assert scheduler.tick(db) == 1
    assert scheduler.tick(db) == 0                      # same slot is not fired twice
    db.refresh(row)
    assert row.next_run_at > utcnow() and row.last_run_at
    made = db.scalars(select(Job).where(Job.schedule_id == s["id"])).all()
    assert len(made) == 1 and made[0].dedupe_key.startswith("sched:")


def test_paused_schedule_does_not_fire(client, db):
    s = mk(client, kind="interval", cron_expr="", interval_seconds=60)
    row = db.get(Schedule, s["id"])
    row.next_run_at = utcnow() - timedelta(seconds=5)
    db.commit()
    client.post(f"/api/schedules/{s['id']}/pause")
    assert scheduler.tick(db) == 0


def test_once_schedule_runs_then_disables(client, db):
    s = mk(client, kind="once", run_at=(datetime.utcnow() + timedelta(hours=3)).isoformat(), cron_expr="")
    row = db.get(Schedule, s["id"])
    row.next_run_at = utcnow() - timedelta(seconds=1)
    db.commit()
    assert scheduler.tick(db) == 1
    db.refresh(row)
    assert row.enabled is False and row.next_run_at is None


def test_misfire_beyond_grace_is_skipped_not_flooded(client, db):
    s = mk(client, kind="interval", cron_expr="", interval_seconds=60)
    row = db.get(Schedule, s["id"])
    row.next_run_at = utcnow() - timedelta(days=3)   # app was down for days
    db.commit()
    assert scheduler.tick(db) == 0                    # no backlog storm
    db.refresh(row)
    assert row.next_run_at > utcnow()


def test_overdue_schedule_within_grace_runs_after_restart(client, db, dburl):
    """Persistence: schedule survives an engine restart and still fires once when overdue."""
    s = mk(client, kind="interval", cron_expr="", interval_seconds=3600)
    row = db.get(Schedule, s["id"])
    row.next_run_at = utcnow() - timedelta(minutes=10)
    db.commit()
    dbmod.engine.dispose()
    dbmod.configure(dburl)                            # "restart"
    s2 = dbmod.session()
    try:
        assert s2.get(Schedule, s["id"]) is not None
        assert scheduler.tick(s2) == 1
    finally:
        s2.close()


def test_scheduled_job_actually_executes_through_worker(client, db):
    s = mk(client, kind="interval", cron_expr="", interval_seconds=60)
    row = db.get(Schedule, s["id"])
    row.next_run_at = utcnow() - timedelta(seconds=1)
    db.commit()
    w = jobs.Worker(poll=0.02)                        # real worker, scheduler enabled, real noop task
    w.start()
    try:
        import time
        end = time.time() + 10
        while time.time() < end:
            runs = client.get(f"/api/schedules/{s['id']}/runs").json()
            if runs and runs[0]["status"] == "completed":
                break
            time.sleep(0.05)
    finally:
        w.stop()
    assert runs and runs[0]["status"] == "completed"


def test_upcoming_and_previous_executions(client, db):
    s = mk(client, cron_expr="0 9 * * *")
    up = client.get("/api/schedules/upcoming").json()
    assert len(up) == 3 and up[0]["timezone"] == "Africa/Lagos"
    assert up[0]["at_local"].endswith("+01:00") and up[0]["at_utc"] < up[1]["at_utc"]
    client.post(f"/api/schedules/{s['id']}/run-now")
    assert len(client.get(f"/api/schedules/{s['id']}/runs").json()) == 1
