"""Run comparison, CLI report import, schedules, and backtest scoring against summarize()."""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest
import spawn_targets
from conftest import TRADE_DATE
from fastapi.testclient import TestClient
from tradingagents.backtest import summarize
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.memory import settlement
from tradingagents.reporting import write_report_tree

from tradingagents_web import cli_import, schedules
from tradingagents_web.app import create_app
from tradingagents_web.config import ScheduleRequest
from tradingagents_web.jobs.manager import JobManager
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
    response = client.post("/api/runs", json=body)
    assert response.status_code == 201, response.text
    return _run(store, response.json()["id"])


# --- comparison -------------------------------------------------------------------

def test_two_runs_compare_side_by_side_with_differences_marked(client, store, offline):
    korean = _analysis(client, store, output_language="Korean")
    english = _analysis(client, store, output_language="English", trade_date="2026-01-08")

    data = client.get(f"/api/runs/compare?ids={korean['id']}&ids={english['id']}").json()
    rows = {row["key"]: row for row in data["rows"]}
    assert rows["output_language"]["values"] == ["Korean", "English"] and rows["output_language"]["differs"]
    assert rows["trade_date"]["differs"] and not rows["llm_provider"]["differs"]
    assert rows["rating"]["values"] == ["Overweight", "Overweight"]
    final = next(s for s in data["sections"] if s["key"] == "final_trade_decision")
    assert len(final["contents"]) == 2 and final["same"]
    assert data["same_ticker"]

    page = client.get(f"/runs/compare?ids={korean['id']}&ids={english['id']}")
    assert page.status_code == 200 and 'class="differs"' in page.text
    # The run page offers the other run of the same ticker.
    assert f'<option value="{english["id"]}"' in client.get(f"/runs/{korean['id']}").text


def test_a_comparison_needs_two_to_four_finished_runs(client, store, offline):
    done = _analysis(client, store)
    queued = client.post("/api/runs", json={"ticker": "AAPL", "trade_date": TRADE_DATE}).json()["id"]
    assert client.get(f"/api/runs/compare?ids={done['id']}").status_code == 400
    assert client.get(f"/api/runs/compare?ids={done['id']}&ids={queued}").status_code == 400
    assert client.get(f"/api/runs/compare?ids={done['id']}&ids=9999").status_code == 404


# --- CLI report import ------------------------------------------------------------------

def _cli_tree(name: str = "MSFT_20260102_093000", ticker: str = "MSFT") -> Path:
    """A report tree as the CLI writes it, through the same writer."""
    state = {
        "trade_date": "2026-01-02",
        "market_report": "Market says up.",
        "news_report": "News is mixed.",
        "investment_debate_state": {"bull_history": "Bull case.", "bear_history": "Bear case."},
        "investment_plan": "Plan: accumulate.",
        "trader_investment_plan": "Buy in tranches.",
        "risk_debate_state": {"aggressive_history": "Go big.", "conservative_history": "Go slow.",
                              "neutral_history": "Balance."},
        "final_trade_decision": "**Rating**: Underweight\n\nTrim the position.",
    }
    settings = {"version": "0.5.2", "llm_provider": "google", "deep_think_llm": "gemini-3.8-flash",
                "quick_think_llm": "gemini-3.8-flash", "analysts": ["market", "news"],
                "max_debate_rounds": 2, "max_risk_discuss_rounds": 1}
    root = Path(DEFAULT_CONFIG["results_dir"]) / "reports" / name
    write_report_tree(state, ticker, root, settings=settings)
    return root


def test_cli_reports_are_imported_once_with_their_header_and_sections(client, store):
    tree = _cli_tree()

    first = client.post("/api/runs/import").json()
    assert len(first["imported"]) == 1 and first["errors"] == []
    job = store.get_job(first["imported"][0])
    assert (job["source"], job["status"], job["ticker"], job["trade_date"]) == ("cli", "completed", "MSFT", "2026-01-02")
    assert job["rating"] == "Underweight"
    assert job["request"]["deep_think_llm"] == "gemini-3.8-flash"
    assert job["request"]["analysts"] == ["market", "news"] and job["request"]["max_debate_rounds"] == 2
    assert job["report_dir"] == str(tree)
    sections = job["result"]["sections"]
    assert sections["market_report"] == "Market says up."
    assert "Bull case." in sections["investment_debate"] and "Go slow." in sections["risk_debate"]

    again = client.post("/api/runs/import").json()
    assert again["imported"] == [] and again["skipped"] == 1

    page = client.get(f"/runs/{job['id']}").text
    assert "CLI에서 가져온 리포트" in page and "Trim the position." in page
    assert client.get(f"/runs/{job['id']}/report.md").status_code == 200
    assert client.get(f"/runs/new?source={job['id']}").status_code == 200


def test_reports_the_web_saved_are_not_imported_again(client, store, offline):
    _analysis(client, store)
    assert client.post("/api/runs/import").json()["imported"] == []


def test_a_folder_that_is_not_a_report_is_listed_as_an_error(client, home):
    bad = Path(DEFAULT_CONFIG["results_dir"]) / "reports" / "junk"
    bad.mkdir(parents=True)
    (bad / "complete_report.md").write_text("not a report")
    result = client.post("/api/runs/import").json()
    assert result["imported"] == [] and result["errors"][0].startswith("junk")


def test_the_import_button_reports_the_count(client):
    _cli_tree()
    response = client.post("/runs/import", follow_redirects=False)
    assert response.headers["location"] == "/runs?imported=1&skipped=0"
    assert "CLI 리포트 1개를 가져왔습니다" in client.get(response.headers["location"]).text


# --- schedules ----------------------------------------------------------------------

@pytest.fixture
def seoul(monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_WEB_TZ", "Asia/Seoul")


def test_the_next_run_skips_to_the_next_chosen_weekday(seoul):
    friday_evening = datetime(2026, 10, 2, 18, 0, tzinfo=schedules.zone())       # a Friday
    nxt = schedules.next_run([0, 1, 2, 3, 4], "17:00", friday_evening)
    assert nxt.astimezone(schedules.zone()) == datetime(2026, 10, 5, 17, 0, tzinfo=schedules.zone())
    friday_noon = datetime(2026, 10, 2, 12, 0, tzinfo=schedules.zone())
    assert schedules.next_run([4], "17:00", friday_noon).astimezone(schedules.zone()).day == 2


def test_a_due_schedule_queues_one_run_per_ticker_then_moves_on(store, seoul, home):
    schedule_id = schedules.create(store, ScheduleRequest(name="close", tickers=["nvda", "aapl"],
                                                          days=list(range(7)), time="17:00",
                                                          analysts=["market"], output_language="Korean"))
    # Pretend it was due a minute ago (and missed for days: it still fires once).
    due = datetime.now(UTC) - timedelta(minutes=1)
    store.update_schedule(schedule_id, next_run_at=due.isoformat(timespec="seconds"))

    queued = schedules.fire_due(store)

    assert len(queued) == 2
    jobs = [store.get_job(j) for j in queued]
    assert {j["ticker"] for j in jobs} == {"NVDA", "AAPL"}
    assert all(j["source"] == "schedule" and j["schedule_id"] == schedule_id for j in jobs)
    assert jobs[0]["request"]["output_language"] == "Korean" and jobs[0]["request"]["analysts"] == ["market"]
    assert jobs[0]["trade_date"] == due.astimezone(schedules.zone()).date().isoformat()
    schedule = store.get_schedule(schedule_id)
    assert schedule["next_run_at"] > now() and schedule["last_jobs"] == queued
    assert schedules.fire_due(store) == []          # not due again until the next day


def test_a_disabled_schedule_does_not_fire(store, seoul, home):
    schedule_id = schedules.create(store, ScheduleRequest(name="off", tickers=["NVDA"], days=[0],
                                                          enabled=False))
    assert store.get_schedule(schedule_id)["next_run_at"] is None
    assert schedules.fire_due(store, datetime.now(UTC) + timedelta(days=8)) == []


def test_schedules_through_the_pages_and_api(client, store, seoul):
    response = client.post("/schedules", data={
        "name": "장 마감", "tickers": "NVDA, AAPL", "days": ["0", "2", "4"], "time": "16:30",
        "enabled": "on", "analysts": ["market", "news"], "llm_provider": "openai",
        "deep_think_llm": "gpt-6-sol", "quick_think_llm": "gpt-6-luna", "output_language": "Korean",
        "max_debate_rounds": "1", "max_risk_discuss_rounds": "1"}, follow_redirects=False)
    assert response.status_code == 303
    schedule_id = int(response.headers["location"].rsplit("/", 1)[1])
    listed = client.get("/schedules").text
    assert "장 마감" in listed and "월, 수, 금 16:30" in listed

    fired = client.post(f"/api/schedules/{schedule_id}/run").json()
    assert len(fired["jobs"]) == 2 and fired["errors"] == []
    again = client.post(f"/api/schedules/{schedule_id}/run").json()
    assert again["jobs"] == [] and len(again["errors"]) == 2      # the same runs are already queued

    client.post(f"/schedules/{schedule_id}/toggle")
    assert client.get(f"/api/schedules/{schedule_id}").json()["enabled"] is False
    bad = client.post("/api/schedules", json={"name": "x", "tickers": ["NVDA"], "days": [], "time": "9:00"})
    assert bad.status_code == 422
    assert client.delete(f"/api/schedules/{schedule_id}").status_code == 204
    assert client.get(f"/api/schedules/{schedule_id}").status_code == 404


def test_the_manager_fires_schedules_on_its_pass(tmp_path, home, seoul):
    store = Store(tmp_path / "web.db")
    schedule_id = schedules.create(store, ScheduleRequest(name="now", tickers=["NVDA"], days=list(range(7))))
    store.update_schedule(schedule_id, next_run_at=(datetime.now(UTC) - timedelta(seconds=5)).isoformat())
    manager = JobManager(tmp_path / "web.db", target=spawn_targets.quick_success)
    try:
        manager.tick()
        jobs = store.list_jobs(schedule_id=schedule_id)
        assert len(jobs) == 1 and jobs[0]["status"] in ("running", "completed")
    finally:
        for proc in manager._procs.values():
            manager._kill(proc)


# --- backtest scoring (roadmap gate 3) ---------------------------------------------------

def test_the_backtest_summary_matches_summarize_on_its_log(client, store, offline, monkeypatch):
    """With prices that settle every cell, the page's numbers are summarize()'s numbers."""
    def closes(symbol, start, end):
        days = pd.bdate_range(start, pd.Timestamp(end) - pd.Timedelta(days=1))
        if symbol.upper() in ("SPY", "^GSPC"):
            return pd.Series(100.0, index=days)                       # the benchmark is flat
        return pd.Series([100.0 * 1.01 ** i for i in range(len(days))], index=days)   # up 1% a day
    monkeypatch.setattr(settlement, "get_closes", closes)

    job_id = client.post("/api/backtests", json={"tickers": ["NVDA"], "start": "2026-01-05",
                                                 "end": "2026-01-07", "every_n_days": 1,
                                                 "analysts": ["market"]}).json()["id"]
    job = _run(store, job_id)

    stored = job["result"]["summary"]
    assert stored["resolved"] == 3 and stored["pending"] == 0
    expected = asdict(summarize(Path(job["report_dir"]) / "trading_memory.md"))
    assert stored == expected
    overweight = stored["by_rating"]["Overweight"]
    assert overweight["count"] == 3 and overweight["hit_rate"] == 1.0 and overweight["mean_alpha"] > 0
    page = client.get(f"/backtests/{job_id}").text
    assert "100%" in page and f"{overweight['mean_alpha'] * 100:+.2f}%" in page


def test_cli_import_of_the_users_tree_layout_parses(tmp_path):
    """The header lines CLI 0.5.2 writes, read back by the importer."""
    tree = tmp_path / "SKHY_20261002_055432"
    (tree / "5_portfolio").mkdir(parents=True)
    (tree / "complete_report.md").write_text(
        "# Trading Analysis Report: SKHY\n\n- Analysis date: 2026-10-02\n- Generated: 2026-10-02 05:54:39\n"
        "- TradingAgents 0.5.2: google, deep gemini-3.8-flash, quick gemini-3.8-flash\n"
        "- Analysts: market, social, news, fundamentals; research debate rounds 1, risk debate rounds 1\n\n")
    (tree / "5_portfolio" / "decision.md").write_text("**Rating**: Overweight")
    parsed = cli_import.parse_tree(tree)
    assert parsed["ticker"] == "SKHY" and parsed["trade_date"] == "2026-10-02"
    assert parsed["rating"] == "Overweight"
    assert parsed["request"]["analysts"] == ["market", "social", "news", "fundamentals"]


def test_a_model_outside_the_catalog_is_shown_as_the_selected_entry():
    from tradingagents_web.pages.forms import model_menu

    menu = model_menu("google", "deep", "gemini-3.1-flash-lite")
    assert menu["options"][0] == ("gemini-3.1-flash-lite (직접 지정)", "gemini-3.1-flash-lite")
    assert menu["selected"] == "gemini-3.1-flash-lite" and menu["custom"] == ""


def test_an_imported_report_shows_every_agent_completed(client, store):
    _cli_tree()
    job_id = client.post("/api/runs/import").json()["imported"][0]
    page = client.get(f"/runs/{job_id}").text
    assert "진행 중" not in page and "진행 기록" not in page


def test_times_are_shown_in_the_configured_zone(seoul):
    from tradingagents_web.render import when

    assert when("2026-10-02T10:32:00+00:00") == "2026-10-02 19:32"


def test_a_database_from_the_first_release_gains_the_new_columns(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, status TEXT NOT NULL,"
                " ticker TEXT, trade_date TEXT, tickers TEXT NOT NULL, request TEXT NOT NULL, settings TEXT,"
                " rating TEXT, report_dir TEXT, result TEXT, resumable INTEGER NOT NULL DEFAULT 0,"
                " resumed_from INTEGER, error TEXT, progress_done INTEGER, progress_total INTEGER, pid INTEGER,"
                " created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT)")
    old.execute("INSERT INTO jobs (kind, status, tickers, request, created_at) "
                "VALUES ('analysis', 'completed', '[\"NVDA\"]', '{}', '2026-10-01T00:00:00+00:00')")
    old.commit()
    old.close()

    store = Store(path)

    job = store.get_job(1)
    assert job["source"] == "web" and job["schedule_id"] is None
    assert store.list_schedules() == []
    assert store.create_job("analysis", {}, ["AAPL"], source="schedule", schedule_id=7) == 2
