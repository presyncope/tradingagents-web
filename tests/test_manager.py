"""The scheduler with real worker processes."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
import spawn_targets
from conftest import TRADE_DATE

from tradingagents_web.config import AnalysisRequest, RunDefaults, resolve_analysis
from tradingagents_web.jobs.manager import JobManager
from tradingagents_web.store.db import Store


def _request(ticker: str, trade_date: str = TRADE_DATE, checkpoint: bool = False) -> dict:
    return resolve_analysis(AnalysisRequest(ticker=ticker, trade_date=trade_date, checkpoint=checkpoint),
                            RunDefaults(), portfolio=None)


def _queue(store: Store, ticker: str, trade_date: str = TRADE_DATE, **kw) -> int:
    request = _request(ticker, trade_date, **kw)
    return store.create_job("analysis", request, [request["ticker"]], ticker=request["ticker"],
                            trade_date=request["trade_date"])


def _wait(predicate, timeout: float = 60.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.1)
    raise AssertionError("timed out")


@pytest.fixture
def manager_for(tmp_path, home):
    managers = []

    def make(target, max_workers=2):
        manager = JobManager(tmp_path / "web.db", max_workers=max_workers, target=target)
        managers.append(manager)
        return manager

    yield make
    for manager in managers:
        for proc in manager._procs.values():
            manager._kill(proc)


def test_two_jobs_for_one_ticker_never_run_at_once(tmp_path, manager_for):
    manager = manager_for(spawn_targets.sleeper, max_workers=3)
    store = Store(tmp_path / "web.db")
    first = _queue(store, "NVDA", "2026-01-08")
    second = _queue(store, "NVDA", "2026-01-09")      # same ticker, other date
    other = _queue(store, "AAPL")

    manager.tick()

    assert store.get_job(first)["status"] == "running"
    assert store.get_job(second)["status"] == "queued"
    assert store.get_job(other)["status"] == "running"

    manager.cancel(first)
    manager.tick()
    assert store.get_job(second)["status"] == "running"


def test_the_worker_limit_holds(tmp_path, manager_for):
    manager = manager_for(spawn_targets.sleeper, max_workers=1)
    store = Store(tmp_path / "web.db")
    a, b = _queue(store, "NVDA"), _queue(store, "AAPL")
    manager.tick()
    assert [store.get_job(j)["status"] for j in (a, b)] == ["running", "queued"]


def test_cancel_kills_the_process_and_offers_resume_when_checkpointed(tmp_path, manager_for):
    manager = manager_for(spawn_targets.sleeper)
    store = Store(tmp_path / "web.db")
    job_id = _queue(store, "NVDA", checkpoint=True)
    manager.tick()
    _wait(lambda: any(e["type"] == "log" for e in store.events(job_id)))
    proc = manager._procs[job_id]

    assert manager.cancel(job_id)

    job = store.get_job(job_id)
    assert job["status"] == "cancelled" and job["resumable"]
    assert not proc.is_alive()
    assert store.events(job_id)[-1]["type"] == "cancelled"
    assert not manager.cancel(job_id)          # already finished


def test_a_queued_job_cancels_without_starting(tmp_path, manager_for):
    manager = manager_for(spawn_targets.sleeper)
    store = Store(tmp_path / "web.db")
    job_id = _queue(store, "NVDA")
    assert manager.cancel(job_id)
    manager.tick()
    assert store.get_job(job_id)["status"] == "cancelled"
    assert job_id not in manager._procs


def test_a_worker_that_dies_without_a_result_is_failed(tmp_path, manager_for):
    manager = manager_for(spawn_targets.crash)
    store = Store(tmp_path / "web.db")
    job_id = _queue(store, "NVDA", checkpoint=True)
    manager.tick()
    _wait(lambda: not manager._procs[job_id].is_alive())

    manager.tick()

    job = store.get_job(job_id)
    assert job["status"] == "failed" and "exit code 3" in job["error"]
    assert job["resumable"]


def test_a_finished_worker_is_reaped_and_keeps_its_result(tmp_path, manager_for):
    manager = manager_for(spawn_targets.quick_success)
    store = Store(tmp_path / "web.db")
    job_id = _queue(store, "NVDA")
    manager.tick()
    _wait(lambda: not manager._procs[job_id].is_alive())
    manager.tick()
    assert store.get_job(job_id)["status"] == "completed"
    assert job_id not in manager._procs


def test_after_a_restart_a_job_whose_process_is_gone_is_failed(tmp_path, home):
    store = Store(tmp_path / "web.db")
    job_id = _queue(store, "NVDA", checkpoint=True)
    store.transition(job_id, ("queued",), status="running", pid=2**22 + 12345)   # no such process

    JobManager(tmp_path / "web.db").recover()

    job = store.get_job(job_id)
    assert job["status"] == "failed" and job["resumable"]
    assert "재시작" in job["error"]


def test_a_spawned_worker_runs_the_real_graph_to_a_saved_report(tmp_path, monkeypatch, manager_for):
    monkeypatch.setenv("TA_WEB_TEST_HOME", str(tmp_path))
    manager = manager_for(spawn_targets.offline_worker)
    store = Store(tmp_path / "web.db")
    job_id = _queue(store, "NVDA")

    manager.tick()
    _wait(lambda: store.get_job(job_id)["status"] != "running", timeout=120)

    job = store.get_job(job_id)
    assert job["status"] == "completed", job["error"]
    assert job["rating"] == "Overweight"
    assert (Path(job["report_dir"]) / "complete_report.md").is_file()
    assert Path(job["report_dir"]).is_relative_to(tmp_path)
    assert store.events(job_id)[-1]["type"] == "done"
