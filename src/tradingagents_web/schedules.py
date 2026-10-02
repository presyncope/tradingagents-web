"""Recurring analyses: the job manager fires a schedule when its time comes.

A schedule names tickers, weekdays and a time of day in the server's time zone
(``TRADINGAGENTS_WEB_TZ``, else the system's). When it fires, each ticker gets
an analysis job for that day's date, built from the schedule's choices and the
saved defaults as they are at that moment. Then the next time is computed from
now, so a schedule missed while the server was down fires once on start-up
rather than once for every missed day.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta

from tradingagents_web import services
from tradingagents_web.clock import zone
from tradingagents_web.config import (
    AnalysisRequest,
    ScheduleRequest,
    normalize_ticker,
    order_analysts,
)
from tradingagents_web.store.db import Store, now

logger = logging.getLogger(__name__)

WEEKDAYS = ["월", "화", "수", "목", "금", "토", "일"]
RUN_FIELDS = [k for k in ScheduleRequest.model_fields if k not in ("name", "tickers", "days", "time", "enabled")]

__all__ = ["zone", "next_run", "create", "update", "set_enabled", "require", "fire", "fire_due", "describe"]


def next_run(days: list[int], clock: str, after: datetime) -> datetime:
    """The first moment strictly after ``after`` on one of ``days`` at ``clock``, in UTC."""
    tz = zone()
    local = after.astimezone(tz)
    hour, minute = (int(p) for p in clock.split(":"))
    for offset in range(8):
        day = (local + timedelta(days=offset)).date()
        candidate = datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)
        if candidate > local and candidate.weekday() in days:
            return candidate.astimezone(UTC)
    raise ValueError("no weekday selected")


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="seconds")


def validate(request: ScheduleRequest) -> dict:
    """The stored form of a schedule. Raises UserError when it cannot run."""
    try:
        tickers = list(dict.fromkeys(normalize_ticker(t) for t in request.tickers if t.strip()))
        if not tickers:
            raise ValueError("티커를 하나 이상 입력하세요")
        if request.analysts:
            order_analysts(request.analysts, "stock")
    except ValueError as exc:
        raise services.UserError(str(exc)) from None
    run = {k: getattr(request, k) for k in RUN_FIELDS}
    return {"name": request.name.strip(), "tickers": tickers, "days": request.days,
            "time": request.time, "enabled": request.enabled, "request": run}


def create(store: Store, request: ScheduleRequest) -> int:
    values = validate(request)
    first = _iso(next_run(values["days"], values["time"], datetime.now(UTC))) if values["enabled"] else None
    return store.create_schedule(**values, next_run_at=first)


def update(store: Store, schedule_id: int, request: ScheduleRequest) -> None:
    require(store, schedule_id)
    values = validate(request)
    upcoming = _iso(next_run(values["days"], values["time"], datetime.now(UTC))) if values["enabled"] else None
    store.update_schedule(schedule_id, **values, next_run_at=upcoming)


def set_enabled(store: Store, schedule_id: int, enabled: bool) -> None:
    schedule = require(store, schedule_id)
    upcoming = _iso(next_run(schedule["days"], schedule["time"], datetime.now(UTC))) if enabled else None
    store.update_schedule(schedule_id, enabled=enabled, next_run_at=upcoming)


def require(store: Store, schedule_id: int) -> dict:
    schedule = store.get_schedule(schedule_id)
    if schedule is None:
        raise services.UserError("예약이 없습니다", status=404)
    return schedule


def fire(store: Store, schedule: dict, trade_date: str) -> tuple[list[int], list[str]]:
    """Queue one analysis per ticker; returns the job ids and the reasons any were skipped."""
    jobs, errors = [], []
    for ticker in schedule["tickers"]:
        try:
            request = AnalysisRequest(ticker=ticker, trade_date=trade_date, **schedule["request"])
            jobs.append(services.submit_analysis(store, None, request, source="schedule",
                                                 schedule_id=schedule["id"]))
        except ValueError as exc:       # UserError, or a request the model rejects
            errors.append(f"{ticker}: {exc}")
    store.update_schedule(schedule["id"], last_run_at=now(), last_jobs=jobs,
                          last_error="; ".join(errors) or None)
    return jobs, errors


def fire_due(store: Store, moment: datetime | None = None) -> list[int]:
    """Fire every enabled schedule whose time has come; returns the jobs queued."""
    moment = moment or datetime.now(UTC)
    queued: list[int] = []
    for schedule in store.due_schedules(_iso(moment)):
        when = datetime.fromisoformat(schedule["next_run_at"]).astimezone(zone())
        # The day the schedule was due, never later than the server's today.
        trade_date = min(when.date(), date.today()).isoformat()
        try:
            jobs, errors = fire(store, schedule, trade_date)
            queued.extend(jobs)
            if errors:
                logger.warning("Schedule %s skipped: %s", schedule["id"], errors)
        except Exception as exc:     # one broken schedule must not stop the scheduler
            logger.exception("Schedule %s failed", schedule["id"])
            store.update_schedule(schedule["id"], last_error=f"{type(exc).__name__}: {exc}")
        store.update_schedule(schedule["id"],
                              next_run_at=_iso(next_run(schedule["days"], schedule["time"], moment)))
    return queued


def describe(schedule: dict) -> str:
    return f"{', '.join(WEEKDAYS[d] for d in schedule['days'])} {schedule['time']}"
