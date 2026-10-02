"""Company name preview, the LLM call estimate, and deleting runs."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import TRADE_DATE
from fastapi.testclient import TestClient
from tradingagents.default_config import DEFAULT_CONFIG

from tradingagents_web import services
from tradingagents_web.app import create_app
from tradingagents_web.config import estimate_llm_calls
from tradingagents_web.settings import AppSettings
from tradingagents_web.store.db import Store, now
from tradingagents_web.worker.runner import run_job


@pytest.fixture
def settings(tmp_path):
    return AppSettings(db_path=tmp_path / "web.db", token="", allowed_hosts=["testserver"])


@pytest.fixture
def client(settings, home):
    with TestClient(create_app(settings, start_manager=False)) as c:
        yield c


@pytest.fixture
def store(settings, client):
    s = Store(settings.db_path)
    yield s
    s.close()


def _run(store: Store, job_id: int) -> dict:
    store.transition(job_id, ("queued",), status="running", started_at=now())
    run_job(job_id, store.path)
    return store.get_job(job_id)


def _analysis(client, store, **fields) -> dict:
    body = {"ticker": "NVDA", "trade_date": TRADE_DATE, "analysts": ["market"], **fields}
    return _run(store, client.post("/api/runs", json=body).json()["id"])


# --- company name preview -----------------------------------------------------------

def test_the_ticker_preview_names_the_company(client, monkeypatch):
    asked = []
    monkeypatch.setattr(services, "instrument_identity", lambda t, wait=0: asked.append(t) or {
        "company_name": "NVIDIA Corporation", "exchange": "NMS", "sector": "Technology"})
    page = client.get("/ui/ticker?ticker=nvda").text
    assert "NVIDIA Corporation" in page and "NMS" in page and "Technology" in page
    assert asked == ["NVDA"]                    # looked up by the normalized symbol
    assert client.get("/api/symbols/resolve?q=nvda").json()["identity"]["company_name"] == "NVIDIA Corporation"


def test_an_unknown_or_slow_lookup_says_so(client, monkeypatch):
    assert "회사 정보를 찾지 못했습니다" in client.get("/ui/ticker?ticker=ZZZZ").text
    monkeypatch.setattr(services, "instrument_identity", lambda t, wait=0: None)
    assert "아직 받지 못했습니다" in client.get("/ui/ticker?ticker=NVDA").text


def test_a_slow_lookup_is_not_waited_for(monkeypatch):
    import threading

    from conftest import REAL_INSTRUMENT_IDENTITY

    release = threading.Event()
    monkeypatch.setattr(services, "resolve_instrument_identity", lambda t: release.wait(5) and {})
    try:
        assert REAL_INSTRUMENT_IDENTITY("NVDA", wait=0.05) is None
    finally:
        release.set()


# --- LLM call estimate ------------------------------------------------------------------

def test_the_estimate_matches_what_real_runs_made():
    # gemini-3.1-flash-lite runs with Market + News at one round each made 13 and 14 calls.
    assert estimate_llm_calls(["market", "news"], 1, 1) == 14
    assert estimate_llm_calls(["market", "social", "news", "fundamentals"], 2, 1) == 3 + 1 + 3 + 3 + 4 + 3 + 3


def test_a_backtest_records_its_estimate_and_the_form_shows_it(client, store):
    job_id = client.post("/api/backtests", json={"tickers": ["NVDA", "AAPL"], "start": "2026-01-05",
                                                 "end": "2026-01-09", "every_n_days": 2,
                                                 "analysts": ["market", "news"], "max_debate_rounds": 1,
                                                 "max_risk_discuss_rounds": 1}).json()["id"]
    estimate = store.get_job(job_id)["request"]["estimate"]
    assert estimate == {"cells": 6, "calls_per_cell": 14, "calls": 84, "reflections_max": 6}
    assert "예상 LLM 호출 약 84회" in client.get(f"/backtests/{job_id}").text
    form = client.get("/backtests/new").text
    assert '"fundamentals": 3' in form and "예상 LLM 호출" in form


# --- deleting runs -----------------------------------------------------------------------

def test_deleting_a_run_keeps_its_files_and_the_import_does_not_revive_it(client, store, offline):
    job = _analysis(client, store)
    report_dir = Path(job["report_dir"])

    response = client.post(f"/runs/{job['id']}/delete", follow_redirects=False)

    assert response.headers["location"] == "/runs?deleted=1"
    assert store.get_job(job["id"]) is None and store.events(job["id"]) == []
    assert (report_dir / "complete_report.md").is_file()
    assert client.post("/api/runs/import").json()["imported"] == []
    assert "1개를 삭제했습니다" in client.get("/runs?deleted=1").text


def test_deleting_with_files_removes_the_report_folder(client, store, offline):
    job = _analysis(client, store)
    result = client.request("DELETE", f"/api/runs/{job['id']}?delete_files=true").json()
    assert result["deleted"] == [job["id"]] and result["files_removed"] == [job["report_dir"]]
    assert not Path(job["report_dir"]).exists()
    # The memory log keeps the decision.
    assert [e["ticker"] for e in client.get("/api/memory").json()] == ["NVDA"]


def test_a_queued_or_running_job_is_not_deleted(client, store):
    job_id = client.post("/api/runs", json={"ticker": "NVDA", "trade_date": TRADE_DATE}).json()["id"]
    assert client.request("DELETE", f"/api/runs/{job_id}").status_code == 409
    assert store.get_job(job_id) is not None


def test_bulk_delete_skips_active_jobs_and_unlinks_resumes(client, store, offline):
    done = _analysis(client, store, checkpoint=True)
    store.update_job(done["id"], status="failed", resumable=True)
    resumed = client.post(f"/api/runs/{done['id']}/resume").json()["id"]

    response = client.post("/runs/delete", data={"ids": [str(done["id"]), str(resumed)]},
                           follow_redirects=False)

    assert response.headers["location"] == "/runs?deleted=1&kept=1"
    assert store.get_job(done["id"]) is None
    assert store.get_job(resumed)["resumed_from"] is None


def test_files_outside_the_results_folder_are_never_removed(client, store, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "complete_report.md").write_text("# Trading Analysis Report: NVDA\n")
    job_id = store.create_job("analysis", {"ticker": "NVDA"}, ["NVDA"], ticker="NVDA", status="completed")
    store.update_job(job_id, report_dir=str(outside))
    result = client.request("DELETE", f"/api/runs/{job_id}?delete_files=true").json()
    assert result["files_removed"] == [] and outside.is_dir()


def test_a_backtest_folder_shared_by_a_resumed_run_stays(client, store, offline):
    job_id = client.post("/api/backtests", json={"tickers": ["NVDA"], "start": "2026-01-05",
                                                 "end": "2026-01-05", "analysts": ["market"]}).json()["id"]
    first = _run(store, job_id)
    again = _run(store, client.post(f"/api/backtests/{job_id}/resume").json()["id"])
    assert first["report_dir"] == again["report_dir"]

    result = client.request("DELETE", f"/api/backtests/{job_id}?delete_files=true").json()

    assert result["files_removed"] == [] and Path(again["report_dir"]).is_dir()
    assert Path(again["report_dir"]).is_relative_to(Path(DEFAULT_CONFIG["results_dir"]))


def test_the_delete_buttons_appear_only_for_finished_jobs(client, store, offline):
    job = _analysis(client, store)
    assert "이 실행 삭제" in client.get(f"/runs/{job['id']}").text
    queued = client.post("/api/runs", json={"ticker": "AAPL", "trade_date": TRADE_DATE}).json()["id"]
    assert "이 실행 삭제" not in client.get(f"/runs/{queued}").text
    assert 'formaction="/runs/delete"' in client.get("/runs").text
