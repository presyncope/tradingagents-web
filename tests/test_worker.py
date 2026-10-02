"""The worker runs the real graph end to end and leaves what the CLI leaves."""

from __future__ import annotations

from pathlib import Path

from conftest import TRADE_DATE, ScriptedModel, patch_offline
from tradingagents.memory.log import TradingMemoryLog

from tradingagents_web.config import AnalysisRequest, RunDefaults, build_config, resolve_analysis
from tradingagents_web.store.db import Store, now
from tradingagents_web.worker.runner import run_job


def _queue_analysis(store: Store, ticker: str = "nvda", **overrides) -> int:
    request = AnalysisRequest(ticker=ticker, trade_date=TRADE_DATE, **overrides)
    resolved = resolve_analysis(request, RunDefaults(), portfolio=None)
    job_id = store.create_job("analysis", resolved, [resolved["ticker"]],
                              ticker=resolved["ticker"], trade_date=resolved["trade_date"])
    store.transition(job_id, ("queued",), status="running", started_at=now())
    return job_id


def test_an_analysis_job_completes_with_the_cli_report_tree_and_memory_entry(tmp_path, offline):
    store = Store(tmp_path / "web.db")
    job_id = _queue_analysis(store)

    run_job(job_id, store.path)

    job = store.get_job(job_id)
    assert job["status"] == "completed", job["error"]
    assert job["rating"] == "Overweight"
    report_dir = Path(job["report_dir"])
    assert (report_dir / "complete_report.md").is_file()
    assert (report_dir / "1_analysts" / "market.md").is_file()
    assert (report_dir / "5_portfolio" / "decision.md").is_file()
    assert set(job["result"]["sections"]) >= {"market_report", "investment_debate",
                                              "trader_investment_plan", "final_trade_decision"}
    assert job["settings"]["analysts"] == ["market", "social", "news", "fundamentals"]

    log = TradingMemoryLog(build_config(job["request"]))
    assert [(e["ticker"], e["date"], e["rating"]) for e in log.load_entries()] == [
        ("NVDA", TRADE_DATE, "Overweight")]

    events = store.events(job_id)
    types = [e["type"] for e in events]
    assert types[-1] == "done" and events[-1]["data"] == {"rating": "Overweight", "review": False}
    assert "message" in types and "tool_call" in types and "section" in types
    final_status = [e for e in events if e["type"] == "status"][-1]["data"]["agents"]
    assert set(final_status.values()) == {"completed"}


def test_a_failed_checkpointed_run_resumes_where_it_stopped(tmp_path, monkeypatch, home):
    failing = ScriptedModel(fail_at=12)          # past the analysts, before the decision
    patch_offline(monkeypatch, failing)
    store = Store(tmp_path / "web.db")
    first = _queue_analysis(store, checkpoint=True)

    run_job(first, store.path)

    job = store.get_job(first)
    assert job["status"] == "failed" and job["resumable"]
    assert "provider unavailable" in job["error"]
    assert store.events(first)[-1]["type"] == "error"

    resumed_model = ScriptedModel()
    patch_offline(monkeypatch, resumed_model)
    second = store.create_job("analysis", job["request"], job["tickers"], ticker=job["ticker"],
                              trade_date=job["trade_date"], resumed_from=first)
    store.transition(second, ("queued",), status="running", started_at=now())
    run_job(second, store.path)

    assert store.get_job(second)["status"] == "completed"
    logs = [e["data"]["text"] for e in store.events(second) if e["type"] == "log"]
    assert any("이어서" in text for text in logs), logs
    fresh = ScriptedModel()
    patch_offline(monkeypatch, fresh)
    third = _queue_analysis(store, checkpoint=False, ticker="AAPL")
    run_job(third, store.path)
    # The resumed run made only the calls the failed one had not completed.
    assert len(resumed_model.calls) < len(fresh.calls)


def test_a_cancelled_job_is_not_overwritten_by_the_worker(tmp_path, offline):
    store = Store(tmp_path / "web.db")
    job_id = _queue_analysis(store)
    store.transition(job_id, ("running",), status="cancelled")

    run_job(job_id, store.path)

    assert store.get_job(job_id)["status"] == "cancelled"
    assert store.events(job_id) == []
