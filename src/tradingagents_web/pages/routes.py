"""HTML pages. Forms post here; HTMX swaps in the fragments under /ui."""

from __future__ import annotations

import hmac
import json
from collections import defaultdict

from fastapi import APIRouter, Depends, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from sse_starlette import EventSourceResponse, ServerSentEvent
from starlette.concurrency import run_in_threadpool
from tradingagents.default_config import DEFAULT_CONFIG

from tradingagents_web import cli_import, clock, schedules, services
from tradingagents_web.config import (
    ANALYST_CALLS,
    ANALYST_ORDER,
    FIXED_CALLS,
    AnalysisRequest,
    BacktestRequest,
    RunDefaults,
    ScheduleRequest,
    detect_asset_type,
    normalize_ticker,
)
from tradingagents_web.deps import get_manager, get_settings, get_store, optional_manager
from tradingagents_web.jobs.manager import JobManager
from tradingagents_web.pages import forms
from tradingagents_web.progress import SECTIONS, RunView, teams
from tradingagents_web.services import UserError
from tradingagents_web.settings import AppSettings
from tradingagents_web.store.db import ACTIVE, Store
from tradingagents_web.stream import follow, start_after

router = APIRouter(include_in_schema=False)

DIRECTION = {"Buy": 1, "Overweight": 1, "Hold": 0, "Underweight": -1, "Sell": -1}


def page(request: Request, name: str, status_code: int = 200, **context) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(request, name, context, status_code=status_code)


def see_other(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def job_url(job: dict) -> str:
    return {"analysis": f"/runs/{job['id']}", "backtest": f"/backtests/{job['id']}"}.get(
        job["kind"], f"/jobs/{job['id']}")


# --- dashboard ----------------------------------------------------------------

@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, store: Store = Depends(get_store)):
    active = store.list_jobs(status=list(ACTIVE), limit=50)
    recent = store.list_jobs(kind="analysis", status="completed", limit=10)
    resumable = [j for j in store.list_jobs(status=["failed", "cancelled"], limit=50) if j["resumable"]]
    pending = services.memory_entries(pending=True)
    upcoming = sorted((s for s in store.list_schedules() if s["enabled"] and s["next_run_at"]),
                      key=lambda s: s["next_run_at"])[:5]
    return page(request, "dashboard.html", active=active, recent=recent, resumable=resumable,
                pending_count=len(pending), job_url=job_url, upcoming=upcoming,
                describe=schedules.describe)


# --- analysis ---------------------------------------------------------------

def _run_form(request: Request, store: Store, values: dict, error: str | None = None,
              status_code: int = 200, ticker: str = "", trade_date: str | None = None):
    return page(request, "run_new.html", status_code=status_code, values=values, error=error,
                ticker=ticker, trade_date=trade_date or forms.last_weekday(),
                has_portfolio=services.get_portfolio(store) is not None,
                deep=forms.model_menu(values["llm_provider"], "deep", values["deep_think_llm"]),
                quick=forms.model_menu(values["llm_provider"], "quick", values["quick_think_llm"]),
                **forms.choices())


@router.get("/runs/new", response_class=HTMLResponse)
def run_new(request: Request, store: Store = Depends(get_store), source: int | None = None):
    previous = None
    ticker, trade_date = "", None
    if source:
        job = services.require_job(store, source, "analysis")
        previous = job["request"]
        ticker, trade_date = previous["ticker"], previous.get("trade_date")
    return _run_form(request, store, forms.form_values(services.get_defaults(store), previous),
                     ticker=ticker, trade_date=trade_date)


@router.post("/runs")
async def run_create(request: Request, store: Store = Depends(get_store),
                     manager: JobManager | None = Depends(optional_manager)):
    form = await request.form()
    fields = {
        **forms.llm_fields(form),
        "ticker": forms._text(form, "ticker") or "",
        "trade_date": forms._text(form, "trade_date") or "",
        "asset_type": forms._text(form, "asset_type"),
        "analysts": forms.analysts(form),
        "use_portfolio": forms.checked(form, "use_portfolio"),
        "checkpoint": forms.checked(form, "checkpoint"),
    }
    try:
        job_id = await run_in_threadpool(services.submit_analysis, store, manager,
                                        AnalysisRequest(**fields))
    except (UserError, ValueError) as exc:
        values = forms.form_values(services.get_defaults(store), {
            k: v for k, v in fields.items() if v is not None and k not in ("ticker", "trade_date")})
        values["use_portfolio"] = fields["use_portfolio"]
        return _run_form(request, store, values, error=str(exc), status_code=400,
                         ticker=fields["ticker"], trade_date=fields["trade_date"])
    return see_other(f"/runs/{job_id}")


@router.get("/runs", response_class=HTMLResponse)
def run_list(request: Request, store: Store = Depends(get_store), ticker: str = "",
             status: str = "", rating: str = "", offset: int = 0, imported: int | None = None,
             skipped: int = 0, deleted: int | None = None, kept: int = 0):
    jobs = store.list_jobs(kind="analysis", ticker=ticker.strip().upper() or None,
                           status=status or None, rating=rating or None, limit=50, offset=offset)
    timeline = []
    if ticker.strip():
        timeline = sorted((j for j in jobs if j["status"] == "completed"),
                          key=lambda j: j["trade_date"] or "")
    return page(request, "run_list.html", jobs=jobs, ticker=ticker, status=status, rating=rating,
                offset=offset, timeline=timeline, imported=imported, skipped=skipped,
                deleted=deleted, kept=kept,
                report_root=str(cli_import.report_root()))


@router.post("/runs/import")
def run_import(store: Store = Depends(get_store)):
    found = cli_import.import_reports(store)
    return see_other(f"/runs?imported={len(found['imported'])}&skipped={found['skipped']}")


@router.post("/runs/delete")
async def runs_delete(request: Request, store: Store = Depends(get_store)):
    form = await request.form()
    ids = [int(i) for i in form.getlist("ids") if str(i).isdigit()]
    if not ids:
        raise UserError("삭제할 실행을 고르세요")
    result = services.delete_jobs(store, ids, delete_files=forms.checked(form, "delete_files"))
    return see_other(f"/runs?deleted={len(result['deleted'])}&kept={len(result['skipped_active'])}")


@router.post("/runs/{job_id}/delete")
async def run_delete(request: Request, job_id: int, store: Store = Depends(get_store)):
    job = services.require_job(store, job_id)
    form = await request.form()
    services.delete_jobs(store, [job_id], delete_files=forms.checked(form, "delete_files"))
    target = {"analysis": "/runs", "backtest": "/backtests"}.get(job["kind"], "/")
    return see_other(f"{target}?deleted=1")


@router.get("/runs/compare", response_class=HTMLResponse)
def run_compare(request: Request, ids: list[int] = Query(default=[]),
                store: Store = Depends(get_store)):
    return page(request, "run_compare.html", **services.compare_runs(store, ids))


@router.get("/runs/{job_id}", response_class=HTMLResponse)
def run_detail(request: Request, job_id: int, store: Store = Depends(get_store)):
    job = services.require_job(store, job_id, "analysis")
    analysts = job["request"].get("analysts") or []
    view = RunView.from_events(analysts, store.events(job_id))
    sections = (job.get("result") or {}).get("sections") or view.sections
    others = []
    if job["status"] == "completed":
        # Every agent finished; an imported report has no events to say so.
        view.agents = dict.fromkeys(view.agents, "completed")
        others = [j for j in store.list_jobs(kind="analysis", status="completed", ticker=job["ticker"],
                                             limit=30) if j["id"] != job_id]
    return page(request, "run_detail.html", job=job, view=view, teams=teams(analysts),
                sections=SECTIONS, section_content=sections, feed=list(reversed(view.feed)),
                others=others)


@router.get("/runs/{job_id}/stream")
async def run_stream(request: Request, job_id: int, after: int = 0,
                     store: Store = Depends(get_store)):
    job = services.require_job(store, job_id, "analysis")
    analysts = job["request"].get("analysts") or []
    templates = request.app.state.templates
    db_path = request.app.state.settings.db_path

    def fragment(name: str, **context) -> str:
        return templates.get_template(name).render(**context)

    async def events():
        async for event in follow(db_path, job_id, start_after(request, after)):
            kind, data, seq = event["type"], event["data"], event["seq"]
            if kind == "status":
                html = fragment("partials/agents.html", teams=teams(analysts), agents=data["agents"])
                yield ServerSentEvent(html, event="status", id=str(seq))
            elif kind == "section":
                html = fragment("partials/section_body.html", content=data["content"])
                yield ServerSentEvent(html, event=f"section-{data['key']}", id=str(seq))
            elif kind == "stats":
                yield ServerSentEvent(fragment("partials/stats.html", stats=data), event="stats", id=str(seq))
            elif kind in ("message", "tool_call", "log", "error", "cancelled"):
                item = {"type": kind, "ts": event["ts"], **data}
                yield ServerSentEvent(fragment("partials/feed_item.html", item=item), event="feed", id=str(seq))
        yield ServerSentEvent("end", event="end")

    return EventSourceResponse(events(), ping=15)


@router.post("/runs/{job_id}/cancel")
def run_cancel(job_id: int, store: Store = Depends(get_store),
               manager: JobManager = Depends(get_manager)):
    job = services.require_job(store, job_id)
    services.cancel(store, manager, job_id)
    return see_other(job_url(job))


@router.post("/runs/{job_id}/resume")
def run_resume(job_id: int, store: Store = Depends(get_store),
               manager: JobManager | None = Depends(optional_manager)):
    new_id = services.resume(store, manager, job_id)
    return see_other(job_url(store.get_job(new_id)))


@router.get("/runs/{job_id}/report.md")
def run_report(job_id: int, store: Store = Depends(get_store)):
    job = services.require_job(store, job_id, "analysis")
    path = services.report_file(job)
    return FileResponse(path, media_type="text/markdown; charset=utf-8",
                        filename=f"{job['ticker']}_{job['trade_date']}_report.md")


# --- generic job page (settle jobs) -------------------------------------------

@router.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_detail(request: Request, job_id: int, store: Store = Depends(get_store)):
    job = services.require_job(store, job_id)
    if job["kind"] != "settle":
        return see_other(job_url(job))
    return page(request, "job_detail.html", job=job, events=store.events(job_id))


# --- HTMX fragments -------------------------------------------------------------

@router.get("/ui/models", response_class=HTMLResponse)
def ui_models(request: Request, llm_provider: str = "openai"):
    return page(request, "partials/model_selects.html",
                deep=forms.model_menu(llm_provider, "deep", None),
                quick=forms.model_menu(llm_provider, "quick", None), provider=llm_provider)


@router.get("/ui/ticker", response_class=HTMLResponse)
def ui_ticker(request: Request, ticker: str = ""):
    if not ticker.strip():
        return HTMLResponse("")
    try:
        symbol = normalize_ticker(ticker)
    except ValueError as exc:
        return page(request, "partials/ticker_preview.html", error=str(exc))
    return page(request, "partials/ticker_preview.html", symbol=symbol,
                asset_type=detect_asset_type(symbol), raw=ticker.strip().upper(),
                identity=services.instrument_identity(symbol))


# --- backtests ----------------------------------------------------------------

@router.get("/backtests", response_class=HTMLResponse)
def backtest_list(request: Request, store: Store = Depends(get_store), deleted: int | None = None):
    return page(request, "backtest_list.html", jobs=store.list_jobs(kind="backtest", limit=100),
                deleted=deleted)


@router.get("/backtests/new", response_class=HTMLResponse)
def backtest_new(request: Request, store: Store = Depends(get_store),
                 settings: AppSettings = Depends(get_settings)):
    values = forms.form_values(services.get_defaults(store))
    return _backtest_form(request, store, settings, values)


def _backtest_form(request, store, settings, values, error=None, status_code=200, form=None):
    form = form or {}
    return page(request, "backtest_new.html", status_code=status_code, values=values, error=error,
                form=form, max_cells=settings.max_backtest_cells,
                call_weights=ANALYST_CALLS, fixed_calls=FIXED_CALLS,
                has_portfolio=services.get_portfolio(store) is not None,
                deep=forms.model_menu(values["llm_provider"], "deep", values["deep_think_llm"]),
                quick=forms.model_menu(values["llm_provider"], "quick", values["quick_think_llm"]),
                **forms.choices())


@router.post("/backtests")
async def backtest_create(request: Request, store: Store = Depends(get_store),
                          manager: JobManager | None = Depends(optional_manager),
                          settings: AppSettings = Depends(get_settings)):
    form = await request.form()
    raw_tickers = forms._text(form, "tickers") or ""
    fields = {
        **forms.llm_fields(form),
        "tickers": [t for t in raw_tickers.replace(",", " ").split() if t],
        "start": forms._text(form, "start") or "",
        "end": forms._text(form, "end") or "",
        "every_n_days": forms._int(form, "every_n_days") or 7,
        "analysts": forms.analysts(form),
        "use_portfolio": forms.checked(form, "use_portfolio"),
    }
    try:
        job_id = await run_in_threadpool(services.submit_backtest, store, manager, BacktestRequest(**fields),
                                          settings.max_backtest_cells)
    except (UserError, ValueError) as exc:
        values = forms.form_values(services.get_defaults(store), {
            k: v for k, v in fields.items() if v is not None and k in forms.LLM_KEYS + ["analysts"]})
        echo = {"tickers": raw_tickers, "start": fields["start"], "end": fields["end"],
                "every_n_days": fields["every_n_days"]}
        return _backtest_form(request, store, settings, values, error=str(exc), status_code=400,
                              form=echo)
    return see_other(f"/backtests/{job_id}")


def _backtest_view(job: dict) -> dict:
    """The grid and summary a finished backtest shows."""
    result = job.get("result") or {}
    cells = result.get("cells") or []
    dates = job["request"].get("dates") or []
    grid: dict[str, dict[str, dict]] = defaultdict(dict)
    for cell in cells:
        hit = None
        alpha = cell.get("alpha")
        direction = DIRECTION.get(cell.get("rating") or "")
        if not cell.get("pending") and alpha and direction:
            try:
                value = float(str(alpha).rstrip("%"))
                hit = (value > 0) == (direction > 0)
            except ValueError:
                hit = None
        grid[cell["ticker"]][cell["date"]] = {**cell, "hit": hit}
    summary = result.get("summary") or {}
    return {"grid": grid, "dates": dates, "summary": summary, "result": result}


@router.get("/backtests/{job_id}", response_class=HTMLResponse)
def backtest_detail(request: Request, job_id: int, store: Store = Depends(get_store)):
    job = services.require_job(store, job_id, "backtest")
    return page(request, "backtest_detail.html", job=job, events=store.events(job_id)[-30:],
                **_backtest_view(job))


@router.get("/backtests/{job_id}/progress", response_class=HTMLResponse)
def backtest_progress(request: Request, job_id: int, store: Store = Depends(get_store)):
    job = services.require_job(store, job_id, "backtest")
    response = page(request, "partials/backtest_progress.html", job=job,
                    events=store.events(job_id)[-30:])
    if job["status"] not in ACTIVE:
        response.headers["HX-Refresh"] = "true"
    return response


# --- schedules --------------------------------------------------------------------

def _schedule_fields(form) -> dict:
    return {
        **forms.llm_fields(form),
        "name": forms._text(form, "name") or "",
        "tickers": [t for t in (forms._text(form, "tickers") or "").replace(",", " ").split() if t],
        "days": [int(d) for d in form.getlist("days") if str(d).isdigit()],
        "time": forms._text(form, "time") or "",
        "enabled": forms.checked(form, "enabled"),
        "analysts": forms.analysts(form),
        "use_portfolio": forms.checked(form, "use_portfolio"),
        "checkpoint": forms.checked(form, "checkpoint"),
    }


def _schedule_form(request, store, schedule: dict | None, error=None, status_code=200, echo=None):
    previous = (echo or schedule or {}).get("request") if (echo or schedule) else None
    values = forms.form_values(services.get_defaults(store), previous)
    source = echo or schedule or {"name": "", "tickers": [], "days": [0, 1, 2, 3, 4], "time": "17:00",
                                  "enabled": True}
    jobs = store.list_jobs(schedule_id=schedule["id"], limit=20) if schedule else []
    return page(request, "schedule_form.html", status_code=status_code, schedule=schedule, source=source,
                values=values, error=error, jobs=jobs, weekdays=schedules.WEEKDAYS,
                has_portfolio=services.get_portfolio(store) is not None, job_url=job_url,
                tz=str(schedules.zone()),
                deep=forms.model_menu(values["llm_provider"], "deep", values["deep_think_llm"]),
                quick=forms.model_menu(values["llm_provider"], "quick", values["quick_think_llm"]),
                **forms.choices())


def _echo(fields: dict) -> dict:
    """A rejected form shown again as the user filled it."""
    run = {k: v for k, v in fields.items() if k in schedules.RUN_FIELDS and v is not None}
    return {**{k: fields[k] for k in ("name", "tickers", "days", "time", "enabled")}, "request": run}


@router.get("/schedules", response_class=HTMLResponse)
def schedule_list(request: Request, store: Store = Depends(get_store)):
    rows = []
    for schedule in store.list_schedules():
        rows.append({**schedule, "when": schedules.describe(schedule),
                     "recent": store.list_jobs(schedule_id=schedule["id"], limit=5)})
    return page(request, "schedule_list.html", schedules=rows, tz=str(schedules.zone()), job_url=job_url)


@router.get("/schedules/new", response_class=HTMLResponse)
def schedule_new(request: Request, store: Store = Depends(get_store)):
    return _schedule_form(request, store, None)


@router.post("/schedules")
async def schedule_create(request: Request, store: Store = Depends(get_store)):
    fields = _schedule_fields(await request.form())
    try:
        schedule_id = schedules.create(store, ScheduleRequest(**fields))
    except (UserError, ValueError) as exc:
        return _schedule_form(request, store, None, error=str(exc), status_code=400, echo=_echo(fields))
    return see_other(f"/schedules/{schedule_id}")


@router.get("/schedules/{schedule_id}", response_class=HTMLResponse)
def schedule_detail(request: Request, schedule_id: int, store: Store = Depends(get_store)):
    return _schedule_form(request, store, schedules.require(store, schedule_id))


@router.post("/schedules/{schedule_id}")
async def schedule_update(request: Request, schedule_id: int, store: Store = Depends(get_store)):
    schedule = schedules.require(store, schedule_id)
    fields = _schedule_fields(await request.form())
    try:
        schedules.update(store, schedule_id, ScheduleRequest(**fields))
    except (UserError, ValueError) as exc:
        return _schedule_form(request, store, schedule, error=str(exc), status_code=400, echo=_echo(fields))
    return see_other(f"/schedules/{schedule_id}?saved=1")


@router.post("/schedules/{schedule_id}/toggle")
def schedule_toggle(schedule_id: int, store: Store = Depends(get_store)):
    schedule = schedules.require(store, schedule_id)
    schedules.set_enabled(store, schedule_id, not schedule["enabled"])
    return see_other("/schedules")


@router.post("/schedules/{schedule_id}/run")
def schedule_run(schedule_id: int, store: Store = Depends(get_store),
                 manager: JobManager | None = Depends(optional_manager)):
    schedule = schedules.require(store, schedule_id)
    jobs, errors = schedules.fire(store, schedule, clock.today().isoformat())
    if manager:
        manager.wake()
    if not jobs:
        raise UserError("실행된 작업이 없습니다: " + "; ".join(errors))
    return see_other(f"/runs/{jobs[0]}" if len(jobs) == 1 else f"/schedules/{schedule_id}")


@router.post("/schedules/{schedule_id}/delete")
def schedule_delete(schedule_id: int, store: Store = Depends(get_store)):
    schedules.require(store, schedule_id)
    store.delete_schedule(schedule_id)
    return see_other("/schedules")


# --- portfolio ------------------------------------------------------------------

@router.get("/portfolio", response_class=HTMLResponse)
def portfolio_page(request: Request, store: Store = Depends(get_store), saved: int = 0):
    return page(request, "portfolio.html", portfolio=services.get_portfolio(store),
                saved=bool(saved), error=None)


@router.post("/portfolio")
async def portfolio_save(request: Request, store: Store = Depends(get_store)):
    form = await request.form()
    if form.get("action") == "clear":
        store.set_value("portfolio", None)
        return see_other("/portfolio?saved=1")
    positions = []
    for ticker, quantity, price in zip(form.getlist("pos_ticker"), form.getlist("pos_quantity"),
                                       form.getlist("pos_price"), strict=False):
        if not str(ticker).strip():
            continue
        positions.append({"ticker": str(ticker).strip(), "quantity": str(quantity).strip() or "0",
                          "average_price": str(price).strip() or None})
    body = {"cash": forms._text(form, "cash"), "currency": forms._text(form, "currency"),
            "positions": positions}
    try:
        services.save_portfolio(store, body)
    except UserError as exc:
        return page(request, "portfolio.html", status_code=400, portfolio=body, saved=False,
                    error=str(exc))
    return see_other("/portfolio?saved=1")


@router.post("/portfolio/import")
async def portfolio_import(request: Request, file: UploadFile, store: Store = Depends(get_store)):
    try:
        body = json.loads((await file.read()).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return page(request, "portfolio.html", status_code=400,
                    portfolio=services.get_portfolio(store), saved=False, error=f"JSON이 아닙니다: {exc}")
    try:
        services.save_portfolio(store, body)
    except UserError as exc:
        return page(request, "portfolio.html", status_code=400,
                    portfolio=services.get_portfolio(store), saved=False, error=str(exc))
    return see_other("/portfolio?saved=1")


@router.get("/portfolio/export")
def portfolio_export(store: Store = Depends(get_store)):
    portfolio = services.get_portfolio(store)
    if portfolio is None:
        raise UserError("저장된 포트폴리오가 없습니다", status=404)
    return Response(json.dumps(portfolio, ensure_ascii=False, indent=2), media_type="application/json",
                    headers={"Content-Disposition": 'attachment; filename="portfolio.json"'})


# --- memory -----------------------------------------------------------------------

@router.get("/memory", response_class=HTMLResponse)
def memory_page(request: Request, ticker: str = "", pending: str = ""):
    entries = services.memory_entries(ticker or None, True if pending == "1" else None)
    pending_tickers = sorted({e["ticker"] for e in services.memory_entries(pending=True)})
    return page(request, "memory.html", entries=entries, ticker=ticker, pending=pending,
                pending_tickers=pending_tickers, memory_path=DEFAULT_CONFIG["memory_log_path"])


@router.post("/memory/settle")
async def memory_settle(request: Request, store: Store = Depends(get_store),
                        manager: JobManager | None = Depends(optional_manager)):
    form = await request.form()
    job_id = await run_in_threadpool(services.submit_settle, store, manager,
                                    forms._text(form, "ticker") or "")
    return see_other(f"/jobs/{job_id}")


# --- settings -----------------------------------------------------------------------

@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, store: Store = Depends(get_store),
                  settings: AppSettings = Depends(get_settings), saved: int = 0):
    defaults = services.get_defaults(store)
    return _settings(request, store, settings, defaults, saved=bool(saved))


def _settings(request, store, settings, defaults: RunDefaults, saved=False, error=None, status_code=200):
    values = forms.form_values(defaults)
    return page(request, "settings.html", status_code=status_code, values=values, saved=saved,
                error=error, defaults=defaults, keys=services.key_status(),
                checkpoints=services.list_checkpoints(), app_settings=settings,
                paths={k: DEFAULT_CONFIG[k] for k in ("results_dir", "data_cache_dir", "memory_log_path")},
                deep=forms.model_menu(values["llm_provider"], "deep", values["deep_think_llm"]),
                quick=forms.model_menu(values["llm_provider"], "quick", values["quick_think_llm"]),
                **forms.choices())


@router.post("/settings")
async def settings_save(request: Request, store: Store = Depends(get_store),
                        settings: AppSettings = Depends(get_settings)):
    form = await request.form()
    analysts = forms.analysts(form)
    fields = {**forms.llm_fields(form),
              "analysts": [a for a in ANALYST_ORDER if a in (analysts or [])] or None,
              "checkpoint": forms.checked(form, "checkpoint")}
    try:
        services.save_defaults(store, RunDefaults(**fields))
    except (UserError, ValueError) as exc:
        return _settings(request, store, settings, RunDefaults(**{
            k: v for k, v in fields.items() if k in RunDefaults.model_fields}),
            error=str(exc), status_code=400)
    return see_other("/settings?saved=1")


@router.post("/settings/checkpoints/delete")
async def checkpoints_delete(request: Request, store: Store = Depends(get_store)):
    form = await request.form()
    services.delete_checkpoints(store, forms._text(form, "ticker"))
    return see_other("/settings#checkpoints")


# --- login ------------------------------------------------------------------------------

def _safe_next(target: str) -> str:
    return target if target.startswith("/") and not target.startswith("//") else "/"


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/"):
    return page(request, "login.html", next=_safe_next(next), error=None)


@router.post("/login")
async def login(request: Request, settings: AppSettings = Depends(get_settings)):
    form = await request.form()
    token = str(form.get("token", ""))
    target = _safe_next(str(form.get("next", "/")))
    if not settings.token or not hmac.compare_digest(token.encode(), settings.token.encode()):
        return page(request, "login.html", status_code=401, next=target, error="토큰이 맞지 않습니다")
    response = see_other(target)
    response.set_cookie("ta_token", token, httponly=True, samesite="lax", max_age=60 * 60 * 24 * 30)
    return response


@router.post("/logout")
def logout():
    response = see_other("/login")
    response.delete_cookie("ta_token")
    return response
