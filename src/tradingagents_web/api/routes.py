"""The JSON API. Every route calls the same services the pages use."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Query, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sse_starlette import EventSourceResponse, ServerSentEvent

from tradingagents_web import cli_import, clock, schedules, services
from tradingagents_web.config import (
    ANALYST_LABELS,
    ANALYST_ORDER,
    AnalysisRequest,
    BacktestRequest,
    RunDefaults,
    ScheduleRequest,
    detect_asset_type,
    model_options,
    normalize_ticker,
    provider_table,
)
from tradingagents_web.deps import get_manager, get_settings, get_store, optional_manager
from tradingagents_web.jobs.manager import JobManager
from tradingagents_web.render import endpoint
from tradingagents_web.services import UserError
from tradingagents_web.settings import AppSettings
from tradingagents_web.store.db import Store
from tradingagents_web.stream import follow, start_after

router = APIRouter(prefix="/api")


def public(job: dict, *, full: bool = False) -> dict:
    """A job as the API shows it: the endpoint reduced to scheme and host, which can't carry a key."""
    out = {k: v for k, v in job.items() if k not in ("pid",)}
    request = dict(out.get("request") or {})
    if request.get("backend_url"):
        request["backend_url"] = endpoint(request["backend_url"])
    out["request"] = request
    if not full:
        out.pop("result", None)
    return out


class Created(BaseModel):
    id: int


class SettleRequest(BaseModel):
    ticker: str


# --- analyses ---------------------------------------------------------------------

@router.post("/runs", response_model=Created, status_code=201)
def create_run(body: AnalysisRequest, store: Store = Depends(get_store),
               manager: JobManager | None = Depends(optional_manager)):
    return {"id": services.submit_analysis(store, manager, body)}


@router.get("/runs")
def list_runs(store: Store = Depends(get_store), ticker: str | None = None, status: str | None = None,
              rating: str | None = None, limit: int = 50, offset: int = 0):
    jobs = store.list_jobs(kind="analysis", ticker=ticker.upper() if ticker else None, status=status,
                           rating=rating, limit=min(limit, 200), offset=offset)
    return [public(j) for j in jobs]


@router.get("/runs/compare")
def compare_runs(ids: list[int] = Query(...), store: Store = Depends(get_store)):
    """Two to four completed analyses: settings rows (``differs`` marks a difference) and sections."""
    result = services.compare_runs(store, ids)
    return {**result, "runs": [public(r) for r in result["runs"]]}


@router.post("/runs/import")
def import_cli_reports(store: Store = Depends(get_store)):
    """Register report trees the CLI saved under results_dir/reports that are not known yet."""
    return cli_import.import_reports(store)


@router.get("/runs/{job_id}")
def get_run(job_id: int, store: Store = Depends(get_store)):
    return public(services.require_job(store, job_id, "analysis"))


@router.get("/runs/{job_id}/report")
def get_run_report(job_id: int, store: Store = Depends(get_store)):
    job = services.require_job(store, job_id, "analysis")
    if job["status"] != "completed":
        raise UserError("완료된 실행만 리포트가 있습니다", status=409)
    return {"rating": job["rating"], "sections": (job.get("result") or {}).get("sections", {}),
            "stats": (job.get("result") or {}).get("stats"), "settings": job.get("settings")}


@router.get("/runs/{job_id}/report.md")
def get_run_report_file(job_id: int, store: Store = Depends(get_store)):
    job = services.require_job(store, job_id, "analysis")
    return FileResponse(services.report_file(job), media_type="text/markdown; charset=utf-8")


@router.post("/runs/{job_id}/cancel", status_code=204)
def cancel_run(job_id: int, store: Store = Depends(get_store), manager: JobManager = Depends(get_manager)):
    services.cancel(store, manager, job_id)


@router.post("/runs/{job_id}/resume", response_model=Created, status_code=201)
def resume_run(job_id: int, store: Store = Depends(get_store),
               manager: JobManager | None = Depends(optional_manager)):
    return {"id": services.resume(store, manager, job_id)}


async def _event_stream(request: Request, store: Store, job_id: int, kind: str, after: int):
    services.require_job(store, job_id, kind)
    db_path = request.app.state.settings.db_path

    async def events():
        async for event in follow(db_path, job_id, start_after(request, after)):
            yield ServerSentEvent(json.dumps(event["data"], ensure_ascii=False), event=event["type"],
                                  id=str(event["seq"]))
        yield ServerSentEvent("{}", event="end")

    return EventSourceResponse(events(), ping=15)


@router.get("/runs/{job_id}/events")
async def run_events(request: Request, job_id: int, after: int = 0, store: Store = Depends(get_store)):
    """Server-sent events: status, message, tool_call, section, stats, log, done, error, cancelled."""
    return await _event_stream(request, store, job_id, "analysis", after)


# --- backtests ----------------------------------------------------------------------

@router.post("/backtests", response_model=Created, status_code=201)
def create_backtest(body: BacktestRequest, store: Store = Depends(get_store),
                    manager: JobManager | None = Depends(optional_manager),
                    settings: AppSettings = Depends(get_settings)):
    return {"id": services.submit_backtest(store, manager, body, settings.max_backtest_cells)}


@router.get("/backtests")
def list_backtests(store: Store = Depends(get_store), limit: int = 50, offset: int = 0):
    return [public(j) for j in store.list_jobs(kind="backtest", limit=min(limit, 200), offset=offset)]


@router.get("/backtests/{job_id}")
def get_backtest(job_id: int, store: Store = Depends(get_store)):
    return public(services.require_job(store, job_id, "backtest"), full=True)


@router.get("/backtests/{job_id}/summary")
def get_backtest_summary(job_id: int, store: Store = Depends(get_store)):
    job = services.require_job(store, job_id, "backtest")
    result = job.get("result") or {}
    if job["status"] != "completed":
        raise UserError("완료된 백테스트만 요약이 있습니다", status=409)
    return {"summary": result.get("summary"), "text": result.get("summary_text"),
            "failures": result.get("failures"), "cells": result.get("cells")}


@router.get("/backtests/{job_id}/events")
async def backtest_events(request: Request, job_id: int, after: int = 0, store: Store = Depends(get_store)):
    return await _event_stream(request, store, job_id, "backtest", after)


@router.post("/backtests/{job_id}/cancel", status_code=204)
def cancel_backtest(job_id: int, store: Store = Depends(get_store), manager: JobManager = Depends(get_manager)):
    services.require_job(store, job_id, "backtest")
    services.cancel(store, manager, job_id)


@router.post("/backtests/{job_id}/resume", response_model=Created, status_code=201)
def resume_backtest(job_id: int, store: Store = Depends(get_store),
                    manager: JobManager | None = Depends(optional_manager)):
    services.require_job(store, job_id, "backtest")
    return {"id": services.resume(store, manager, job_id)}


# --- portfolio -------------------------------------------------------------------------

@router.get("/portfolio")
def get_portfolio(store: Store = Depends(get_store)):
    return services.get_portfolio(store)


@router.put("/portfolio")
def put_portfolio(body: dict, store: Store = Depends(get_store)):
    return services.save_portfolio(store, body)


@router.delete("/portfolio", status_code=204)
def delete_portfolio(store: Store = Depends(get_store)):
    store.set_value("portfolio", None)


@router.post("/portfolio/import")
async def import_portfolio(file: UploadFile, store: Store = Depends(get_store)):
    try:
        body = json.loads((await file.read()).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UserError(f"JSON이 아닙니다: {exc}") from None
    return services.save_portfolio(store, body)


# --- memory log and checkpoints ----------------------------------------------------------

@router.get("/memory")
def get_memory(ticker: str | None = None, pending: bool | None = None):
    return services.memory_entries(ticker, pending)


@router.post("/memory/settle", response_model=Created, status_code=201)
def settle(body: SettleRequest, store: Store = Depends(get_store),
           manager: JobManager | None = Depends(optional_manager)):
    return {"id": services.submit_settle(store, manager, body.ticker)}


@router.get("/jobs/{job_id}")
def get_job(job_id: int, store: Store = Depends(get_store)):
    return public(services.require_job(store, job_id), full=True)


@router.get("/checkpoints")
def get_checkpoints():
    return services.list_checkpoints()


@router.delete("/checkpoints")
def delete_all_checkpoints(store: Store = Depends(get_store)):
    return {"deleted": services.delete_checkpoints(store)}


@router.delete("/checkpoints/{ticker}")
def delete_checkpoint(ticker: str, store: Store = Depends(get_store)):
    return {"deleted": services.delete_checkpoints(store, ticker)}


# --- catalog and settings -------------------------------------------------------------------

@router.get("/catalog/providers")
def providers():
    return [{"key": key, "label": label} for label, key, _ in provider_table()]


@router.get("/catalog/models")
def models(provider: str, mode: str = "deep"):
    return [{"id": value, "label": label} for label, value in model_options(provider, mode)]


@router.get("/catalog/analysts")
def analysts():
    return [{"key": key, "label": ANALYST_LABELS[key]} for key in ANALYST_ORDER]


@router.get("/symbols/resolve")
def resolve_symbol(q: str):
    try:
        symbol = normalize_ticker(q)
    except ValueError as exc:
        raise UserError(str(exc)) from None
    return {"input": q, "symbol": symbol, "asset_type": detect_asset_type(symbol)}


@router.get("/settings")
def get_defaults(store: Store = Depends(get_store)):
    return services.get_defaults(store)


@router.put("/settings")
def put_defaults(body: RunDefaults, store: Store = Depends(get_store)):
    return services.save_defaults(store, body)


@router.get("/settings/keys")
def get_keys():
    return services.key_status()


# --- schedules ---------------------------------------------------------------------------

@router.get("/schedules")
def list_schedules(store: Store = Depends(get_store)):
    return store.list_schedules()


@router.post("/schedules", response_model=Created, status_code=201)
def create_schedule(body: ScheduleRequest, store: Store = Depends(get_store)):
    return {"id": schedules.create(store, body)}


@router.get("/schedules/{schedule_id}")
def get_schedule(schedule_id: int, store: Store = Depends(get_store)):
    schedule = schedules.require(store, schedule_id)
    jobs = [public(j) for j in store.list_jobs(schedule_id=schedule_id, limit=50)]
    return {**schedule, "jobs": jobs}


@router.put("/schedules/{schedule_id}")
def put_schedule(schedule_id: int, body: ScheduleRequest, store: Store = Depends(get_store)):
    schedules.update(store, schedule_id, body)
    return store.get_schedule(schedule_id)


@router.delete("/schedules/{schedule_id}", status_code=204)
def delete_schedule(schedule_id: int, store: Store = Depends(get_store)):
    schedules.require(store, schedule_id)
    store.delete_schedule(schedule_id)


@router.post("/schedules/{schedule_id}/run")
def run_schedule(schedule_id: int, store: Store = Depends(get_store),
                 manager: JobManager | None = Depends(optional_manager)):
    """Fire now, for today's date; the regular next run is unchanged."""
    jobs, errors = schedules.fire(store, schedules.require(store, schedule_id), clock.today().isoformat())
    if manager:
        manager.wake()
    return {"jobs": jobs, "errors": errors}
