"""Run one job to completion, writing its progress and result to the database.

``run_job`` is a plain function: the job manager calls it in a fresh process
(``entry.main``), and tests call it in-process with the LLM and data vendors
patched. It follows the CLI's order for an analysis: build the initial state
(which settles this ticker's earlier decisions), open the checkpoint, stream
the graph, record the decision, clear the checkpoint, save the report tree.

A job that is cancelled is killed by the manager, which records the status
itself; nothing here runs after the kill.
"""

from __future__ import annotations

import logging
import time
import traceback
from dataclasses import asdict
from pathlib import Path

from tradingagents_web.config import build_config
from tradingagents_web.progress import ProgressTracker, sections_from_state
from tradingagents_web.store.db import Store, now
from tradingagents_web.worker.stats import StatsCallbackHandler

logger = logging.getLogger(__name__)

STATS_EVERY = 2.0   # seconds between stats events while a run streams


class Emitter:
    def __init__(self, store: Store, job_id: int):
        self.store, self.job_id = store, job_id

    def __call__(self, type_: str, data: dict | None = None) -> None:
        self.store.append_event(self.job_id, type_, data or {})


def _portfolio(request: dict):
    from tradingagents.portfolio import PortfolioContext

    body = request.get("portfolio")
    return PortfolioContext.model_validate(body) if body is not None else None


def run_analysis(store: Store, job: dict, emit: Emitter) -> tuple[dict, dict]:
    from tradingagents.agents.rating import is_review, run_rating
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    req = job["request"]
    ticker, trade_date, asset_type = req["ticker"], req["trade_date"], req["asset_type"]
    portfolio = _portfolio(req)
    stats = StatsCallbackHandler()
    graph = TradingAgentsGraph(req["analysts"], config=build_config(req), callbacks=[stats])
    tracker = ProgressTracker(req["analysts"])

    init_state = graph.create_run_state(ticker, trade_date, asset_type, portfolio)
    args = graph.propagator.get_graph_args(callbacks=[stats])
    thread = graph.begin_checkpoint(ticker, trade_date, asset_type, portfolio)
    try:
        if thread is not None:
            args.setdefault("config", {}).setdefault("configurable", {})["thread_id"] = thread
            resumed = getattr(graph, "_resuming", False)
            emit("log", {"text": f"체크포인트에서 이어서 실행합니다 ({ticker}, {trade_date})" if resumed
                         else f"새로 시작합니다. 단계마다 체크포인트를 저장합니다 ({ticker}, {trade_date})"})
        for event in tracker.start():
            emit(*event)

        last_stats, last_sent = 0.0, None
        for messages, chunk in graph.stream_run(graph.checkpoint_input(init_state), **args):
            for event in tracker.feed(messages, chunk):
                emit(*event)
            current = stats.get_stats()
            if current != last_sent and time.monotonic() - last_stats >= STATS_EVERY:
                emit("stats", current)
                last_stats, last_sent = time.monotonic(), current

        final_state = tracker.state
        graph.record_decision(ticker, trade_date, final_state)
        graph.clear_checkpoint_on_success(ticker, trade_date, asset_type, portfolio)
    finally:
        graph.end_checkpoint()

    report_file = graph.save_reports(final_state, ticker)
    rating = run_rating(final_state)
    for event in tracker.finish():
        emit(*event)
    emit("stats", stats.get_stats())
    fields = {
        "rating": rating,
        "report_dir": str(Path(report_file).parent),
        "settings": graph.run_settings(),
        "result": {"sections": sections_from_state(final_state), "stats": stats.get_stats()},
    }
    return fields, {"rating": rating, "review": is_review(rating)}


def run_backtest_job(store: Store, job: dict, emit: Emitter) -> tuple[dict, dict]:
    from tradingagents.backtest import run_backtest, summarize
    from tradingagents.memory.log import TradingMemoryLog

    req = job["request"]
    tickers, dates = req["tickers"], req["dates"]
    total = len(tickers) * len(dates)
    store.update_job(job["id"], progress_done=0, progress_total=total)

    def progress(index: int, todo: int, ticker: str, date: str) -> None:
        # Cells already in this run's log are skipped before the first call.
        store.update_job(job["id"], progress_done=total - todo + index - 1)
        emit("cell", {"ticker": ticker, "date": date, "index": index, "of": todo})

    result = run_backtest(tickers, dates, build_config(req), asset_type=req["asset_type"],
                          portfolio=_portfolio(req), selected_analysts=tuple(req["analysts"]),
                          run_id=req["run_id"], progress=progress)
    summary = summarize(result)
    entries = TradingMemoryLog({"memory_log_path": str(result.log_path)}).load_entries()
    cells = [{k: e.get(k) for k in ("ticker", "date", "rating", "pending", "raw", "alpha", "holding")}
             for e in entries]
    fields = {
        "progress_done": total,
        "report_dir": str(result.log_path.parent),
        "result": {
            "summary": asdict(summary),
            "summary_text": summary.render(),
            "cells": cells,
            "cells_run": result.cells_run,
            "skipped": result.skipped,
            "failures": [list(f) for f in result.failures],
            "settlement_failures": [list(f) for f in result.settlement_failures],
        },
    }
    return fields, {"cells_run": result.cells_run, "failures": len(result.failures)}


def run_settle(store: Store, job: dict, emit: Emitter) -> tuple[dict, dict]:
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    req = job["request"]
    graph = TradingAgentsGraph(config=build_config(req))
    ticker = req["ticker"]

    def pending() -> int:
        return sum(1 for e in graph.memory_log.load_entries() if e["ticker"] == ticker and e["pending"])

    before = pending()
    emit("log", {"text": f"{ticker}의 대기 중인 결정 {before}건을 정산합니다"})
    graph.settle_pending(ticker)
    settled = before - pending()
    emit("log", {"text": f"{settled}건을 정산했습니다. 보유 기간이 아직 끝나지 않은 결정은 대기 상태로 남습니다"})
    return {"result": {"settled": settled, "pending_before": before}}, {"settled": settled}


RUNNERS = {"analysis": run_analysis, "backtest": run_backtest_job, "settle": run_settle}


def run_job(job_id: int, db_path: str | Path) -> None:
    store = Store(db_path)
    try:
        job = store.get_job(job_id)
        if job is None or job["status"] != "running":
            return
        emit = Emitter(store, job_id)
        try:
            fields, done = RUNNERS[job["kind"]](store, job, emit)
        except Exception as exc:
            logger.exception("Job %s failed", job_id)
            request = job["request"]
            # A failed backtest continues where it stopped; an analysis does when it checkpointed.
            resumable = job["kind"] == "backtest" or (job["kind"] == "analysis" and request.get("checkpoint"))
            message = f"{type(exc).__name__}: {exc}"
            if store.transition(job_id, ("running",), status="failed", error=message,
                                resumable=resumable, finished_at=now()):
                emit("error", {"message": message, "trace": traceback.format_exc(limit=5)})
            return
        if store.transition(job_id, ("running",), status="completed", finished_at=now(), **fields):
            emit("done", done)
    finally:
        store.close()
