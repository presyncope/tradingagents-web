"""What the pages and the JSON API both do, in one place.

Each function takes the request's ``Store`` and, when it starts work, the
``JobManager``. Errors a user can fix raise ``UserError``; the routes turn it
into a form message or a 4xx response.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.llm_clients.api_key_env import get_api_key_env
from tradingagents.memory.log import TradingMemoryLog
from tradingagents.portfolio import PortfolioContext

from tradingagents_web.config import (
    AnalysisRequest,
    BacktestRequest,
    LLMChoice,
    RunDefaults,
    normalize_ticker,
    provider_table,
    resolve_analysis,
    resolve_backtest,
    resolve_llm,
)
from tradingagents_web.store.db import ACTIVE, Store

if TYPE_CHECKING:   # the manager imports this module to fire schedules
    from tradingagents_web.jobs.manager import JobManager


class UserError(ValueError):
    """A request the user can correct; ``status`` is the HTTP code for the API."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# --- saved values ---------------------------------------------------------

def get_defaults(store: Store) -> RunDefaults:
    return RunDefaults.model_validate(store.get_value("defaults", {}))


def save_defaults(store: Store, defaults: RunDefaults) -> RunDefaults:
    if defaults.llm_provider or defaults.deep_think_llm or defaults.quick_think_llm:
        try:
            resolve_llm(LLMChoice(), defaults)
        except ValueError as exc:
            raise UserError(str(exc)) from None
    store.set_value("defaults", defaults.model_dump())
    return defaults


def get_portfolio(store: Store) -> dict | None:
    """The one account's book, or None before it is first saved."""
    return store.get_value("portfolio")


def save_portfolio(store: Store, body: dict) -> dict:
    try:
        portfolio = PortfolioContext.model_validate(body)
        for position in portfolio.positions:
            position.ticker = normalize_ticker(position.ticker)
    except Exception as exc:
        raise UserError(f"포트폴리오 형식이 올바르지 않습니다: {exc}") from None
    value = portfolio.model_dump()
    store.set_value("portfolio", value)
    return value


def _portfolio_for(store: Store, use: bool) -> dict | None:
    if not use:
        return None
    portfolio = get_portfolio(store)
    if portfolio is None:
        raise UserError("저장된 포트폴리오가 없습니다. 포트폴리오 화면에서 먼저 저장하세요")
    return portfolio


# --- jobs -----------------------------------------------------------------

def submit_analysis(store: Store, manager: JobManager | None, request: AnalysisRequest, *,
                    source: str = "web", schedule_id: int | None = None) -> int:
    try:
        resolved = resolve_analysis(request, get_defaults(store), _portfolio_for(store, request.use_portfolio))
    except ValueError as exc:
        raise UserError(str(exc)) from None
    for job in store.list_jobs(kind="analysis", status=list(ACTIVE), ticker=resolved["ticker"], limit=200):
        if job["request"] == resolved:
            raise UserError(f"같은 분석이 이미 실행 중이거나 대기 중입니다 (#{job['id']})", status=409)
    job_id = store.create_job("analysis", resolved, [resolved["ticker"]],
                              ticker=resolved["ticker"], trade_date=resolved["trade_date"],
                              source=source, schedule_id=schedule_id)
    if manager:
        manager.wake()
    return job_id


def submit_backtest(store: Store, manager: JobManager | None, request: BacktestRequest,
                    max_cells: int) -> int:
    try:
        resolved = resolve_backtest(request, get_defaults(store),
                                    _portfolio_for(store, request.use_portfolio), max_cells)
    except ValueError as exc:
        raise UserError(str(exc)) from None
    # The run's folder name under results_dir/backtest; a resume reuses it.
    resolved["run_id"] = "web_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    job_id = store.create_job("backtest", resolved, resolved["tickers"])
    if manager:
        manager.wake()
    return job_id


def submit_settle(store: Store, manager: JobManager | None, ticker: str) -> int:
    try:
        ticker = normalize_ticker(ticker)
        request = {"ticker": ticker, **resolve_llm(LLMChoice(), get_defaults(store))}
    except ValueError as exc:
        raise UserError(str(exc)) from None
    job_id = store.create_job("settle", request, [ticker], ticker=ticker)
    if manager:
        manager.wake()
    return job_id


def pending_cells(job: dict) -> int:
    """Cells of a finished backtest whose holding window had not traded yet."""
    return int(((job.get("result") or {}).get("summary") or {}).get("pending") or 0)


def can_continue(job: dict) -> bool:
    """A stopped job resumes; a completed backtest re-runs to settle its pending cells."""
    if job["status"] in ("failed", "cancelled"):
        return job["resumable"]
    return job["kind"] == "backtest" and job["status"] == "completed" and pending_cells(job) > 0


def resume(store: Store, manager: JobManager | None, job_id: int) -> int:
    """Queue the same request again; the checkpoint (or the backtest's log) does the rest.

    A backtest re-run with the same ``run_id`` skips the cells already in its
    log and then settles every ticker, so it also scores cells that were still
    pending when the backtest first completed.
    """
    job = store.get_job(job_id)
    if job is None:
        raise UserError("작업이 없습니다", status=404)
    if not can_continue(job):
        raise UserError("재개할 수 없는 작업입니다", status=409)
    for active in store.list_jobs(kind=job["kind"], status=list(ACTIVE), limit=200):
        if active["resumed_from"] == job_id:
            raise UserError(f"이미 이어서 실행 중입니다 (#{active['id']})", status=409)
    # The stored request is replayed as is, portfolio snapshot included: the
    # checkpoint key hashes it, so re-reading the saved portfolio could miss it.
    new_id = store.create_job(job["kind"], job["request"], job["tickers"], ticker=job["ticker"],
                              trade_date=job["trade_date"], resumed_from=job_id)
    if job["status"] != "completed":
        store.update_job(job_id, resumable=False)
    if manager:
        manager.wake()
    return new_id


def cancel(store: Store, manager: JobManager, job_id: int) -> None:
    if store.get_job(job_id) is None:
        raise UserError("작업이 없습니다", status=404)
    if not manager.cancel(job_id):
        raise UserError("이미 끝난 작업입니다", status=409)


def require_job(store: Store, job_id: int, kind: str | None = None) -> dict:
    job = store.get_job(job_id)
    if job is None or (kind and job["kind"] != kind):
        raise UserError("작업이 없습니다", status=404)
    return job


def report_file(job: dict) -> Path:
    if not job.get("report_dir"):
        raise UserError("저장된 리포트가 없습니다", status=404)
    path = Path(job["report_dir"]) / "complete_report.md"
    if not path.is_file():
        raise UserError("리포트 파일을 찾을 수 없습니다", status=404)
    return path


# --- comparing runs ----------------------------------------------------------

COMPARE_ROWS = [
    ("rating", "등급"), ("ticker", "티커"), ("trade_date", "분석일"), ("source", "출처"),
    ("llm_provider", "제공자"), ("deep_think_llm", "Deep 모델"), ("quick_think_llm", "Quick 모델"),
    ("analysts", "분석가"), ("max_debate_rounds", "토론 라운드"), ("max_risk_discuss_rounds", "리스크 라운드"),
    ("output_language", "출력 언어"), ("portfolio", "포트폴리오"), ("llm_calls", "LLM 호출"),
    ("tokens", "토큰 (입력 / 출력)"),
]


def _compare_value(job: dict, key: str) -> str:
    request = job.get("request") or {}
    stats = (job.get("result") or {}).get("stats") or {}
    if key in ("rating", "ticker", "trade_date", "source"):
        value = job.get(key)
    elif key == "analysts":
        value = ", ".join(request.get("analysts") or [])
    elif key == "portfolio":
        value = "반영" if request.get("portfolio") else "없음"
    elif key == "llm_calls":
        value = stats.get("llm_calls")
    elif key == "tokens":
        value = f"{stats.get('tokens_in', 0):,} / {stats.get('tokens_out', 0):,}" if stats else None
    else:
        value = request.get(key)
    return "" if value is None else str(value)


def compare_runs(store: Store, ids: list[int]) -> dict:
    """Two to four completed analyses side by side: their settings and each report section."""
    from tradingagents_web.progress import SECTIONS

    ids = list(dict.fromkeys(ids))
    if not 2 <= len(ids) <= 4:
        raise UserError("비교할 실행을 2개에서 4개까지 고르세요")
    runs = [require_job(store, job_id, "analysis") for job_id in ids]
    unfinished = [str(r["id"]) for r in runs if r["status"] != "completed"]
    if unfinished:
        raise UserError(f"완료된 실행만 비교할 수 있습니다 (#{', #'.join(unfinished)})")
    rows = []
    for key, label in COMPARE_ROWS:
        values = [_compare_value(run, key) for run in runs]
        rows.append({"key": key, "label": label, "values": values, "differs": len(set(values)) > 1})
    sections = []
    for key, title in SECTIONS:
        contents = [((run.get("result") or {}).get("sections") or {}).get(key) for run in runs]
        if any(contents):
            sections.append({"key": key, "title": title, "contents": contents,
                             "same": len({(c or "").strip() for c in contents}) == 1})
    return {"runs": runs, "rows": rows, "sections": sections,
            "same_ticker": len({r["ticker"] for r in runs}) == 1}


# --- memory log and checkpoints ---------------------------------------------

def memory_entries(ticker: str | None = None, pending: bool | None = None) -> list[dict]:
    entries = TradingMemoryLog({"memory_log_path": DEFAULT_CONFIG["memory_log_path"]}).load_entries()
    if ticker:
        ticker = ticker.strip().upper()
        entries = [e for e in entries if e["ticker"] == ticker]
    if pending is not None:
        entries = [e for e in entries if e["pending"] == pending]
    return sorted(entries, key=lambda e: (e["date"], e["ticker"]), reverse=True)


def _checkpoint_dir() -> Path:
    return Path(DEFAULT_CONFIG["data_cache_dir"]) / "checkpoints"


def list_checkpoints() -> list[dict]:
    directory = _checkpoint_dir()
    if not directory.is_dir():
        return []
    out = []
    for db in sorted(directory.glob("*.db")):
        size = sum(p.stat().st_size for p in (db, *directory.glob(f"{db.name}-*")) if p.exists())
        out.append({"ticker": db.stem, "bytes": size,
                    "modified": datetime.fromtimestamp(db.stat().st_mtime).isoformat(timespec="seconds")})
    return out


def delete_checkpoints(store: Store, ticker: str | None = None) -> int:
    """Delete one ticker's checkpoint database, or all of them; refused while a run uses it."""
    from tradingagents.graph.checkpointer import clear_all_checkpoints

    running = [j for j in store.list_jobs(status="running", limit=500) if j["kind"] != "settle"]
    if ticker is None:
        if running:
            raise UserError("실행 중인 작업이 있어 체크포인트를 지울 수 없습니다", status=409)
        return clear_all_checkpoints(DEFAULT_CONFIG["data_cache_dir"])
    ticker = normalize_ticker(ticker).upper()
    if any(ticker in j["tickers"] for j in running):
        raise UserError(f"{ticker} 작업이 실행 중이라 체크포인트를 지울 수 없습니다", status=409)
    directory = _checkpoint_dir()
    db = directory / f"{ticker}.db"
    if not db.exists():
        return 0
    for path in (db, *directory.glob(f"{db.name}-*")):
        path.unlink(missing_ok=True)
    return 1


# --- environment ------------------------------------------------------------

def key_status() -> list[dict]:
    """Which providers have their API key set. The values themselves never leave the server."""
    rows = []
    for label, key, _ in provider_table():
        env = get_api_key_env(key)
        rows.append({"provider": key, "label": label, "env": env,
                     "set": bool(env and os.environ.get(env)), "needs_key": env is not None})
    return rows
