"""Pages, JSON API, login and the event stream, through the HTTP interface."""

from __future__ import annotations

import io
import json

import pytest
from conftest import TRADE_DATE
from fastapi.testclient import TestClient
from tradingagents.default_config import DEFAULT_CONFIG

from tradingagents_web.app import create_app
from tradingagents_web.settings import AppSettings
from tradingagents_web.store.db import Store, now
from tradingagents_web.worker.runner import run_job

PAGES = ["/", "/runs/new", "/runs", "/backtests", "/backtests/new", "/portfolio", "/memory",
         "/settings", "/ui/models?llm_provider=anthropic", "/ui/ticker?ticker=btcusd", "/api/docs"]


@pytest.fixture
def settings(tmp_path):
    # TestClient sends "Host: testserver"; without a token only listed hosts are served.
    return AppSettings(db_path=tmp_path / "web.db", token="", allowed_hosts=["testserver", "127.0.0.1"])


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
    """Run a queued job in this process, as a worker would."""
    store.transition(job_id, ("queued",), status="running", started_at=now())
    run_job(job_id, store.path)
    return store.get_job(job_id)


def _form(**fields):
    base = {"ticker": "NVDA", "trade_date": TRADE_DATE, "analysts": ["market", "news"],
            "llm_provider": "openai", "deep_think_llm": "gpt-6-sol", "quick_think_llm": "gpt-6-luna",
            "output_language": "Korean", "max_debate_rounds": "1", "max_risk_discuss_rounds": "1"}
    base.update(fields)
    return base


@pytest.mark.parametrize("path", PAGES)
def test_every_page_renders(client, path):
    response = client.get(path)
    assert response.status_code == 200, response.text[:500]


def test_the_run_form_queues_a_normalized_request(client, store):
    response = client.post("/runs", data=_form(ticker="btcusd", analysts=["news", "fundamentals", "market"]),
                           follow_redirects=False)
    assert response.status_code == 303
    job = store.get_job(int(response.headers["location"].rsplit("/", 1)[1]))
    assert job["status"] == "queued"
    request = job["request"]
    assert request["ticker"] == "BTC-USD" and request["asset_type"] == "crypto"
    # Canonical order, and the stock-only analyst dropped for crypto.
    assert request["analysts"] == ["market", "news"]
    assert request["output_language"] == "Korean" and request["portfolio"] is None
    assert request["backend_url"] == "https://api.openai.com/v1"


def test_the_same_run_twice_while_queued_is_refused(client):
    assert client.post("/runs", data=_form(), follow_redirects=False).status_code == 303
    again = client.post("/runs", data=_form())
    assert again.status_code == 400
    assert "이미 실행 중이거나 대기 중" in again.text
    assert client.post("/api/runs", json={"ticker": "NVDA", "trade_date": TRADE_DATE,
                                          "analysts": ["market", "news"],
                                          "output_language": "Korean"}).status_code == 409


@pytest.mark.parametrize("fields, message", [
    ({"ticker": "../etc"}, "ticker"),
    ({"trade_date": "2999-01-01"}, "미래"),
    ({"analysts": []}, "제출"),          # no analysts falls back to the defaults, so this one is accepted
])
def test_a_bad_request_shows_the_form_again_with_the_reason(client, fields, message):
    response = client.post("/runs", data=_form(**fields), follow_redirects=False)
    if message == "제출":
        assert response.status_code == 303
        return
    assert response.status_code == 400
    assert message in response.text


def test_a_finished_run_page_shows_rating_report_and_download(client, store, offline):
    location = client.post("/runs", data=_form(), follow_redirects=False).headers["location"]
    job = _run(store, int(location.rsplit("/", 1)[1]))
    assert job["status"] == "completed"

    page = client.get(location)
    assert "Overweight" in page.text
    assert "Portfolio Manager 최종 결정" in page.text
    assert "sse-connect" not in page.text          # nothing left to stream
    download = client.get(f"{location}/report.md")
    assert download.status_code == 200 and "Overweight" in download.text


def test_the_event_stream_replays_and_ends(client, store, offline):
    job_id = client.post("/api/runs", json={"ticker": "NVDA", "trade_date": TRADE_DATE}).json()["id"]
    _run(store, job_id)
    total = len(store.events(job_id))

    with client.stream("GET", f"/runs/{job_id}/stream") as response:
        body = "".join(response.iter_text())
    assert "event: status" in body and "event: section-market_report" in body
    assert body.rstrip().endswith("data: end")

    with client.stream("GET", f"/api/runs/{job_id}/events", headers={"Last-Event-ID": str(total - 1)}) as response:
        tail = "".join(response.iter_text())
    assert f"id: {total}" in tail and f"id: {total - 1}" not in tail
    assert "event: done" in tail


def test_model_written_html_is_not_rendered(client, store):
    job_id = store.create_job("analysis", {"ticker": "NVDA", "trade_date": TRADE_DATE, "analysts": ["market"],
                                           "asset_type": "stock", "llm_provider": "openai",
                                           "deep_think_llm": "a", "quick_think_llm": "b"},
                              ["NVDA"], ticker="NVDA", trade_date=TRADE_DATE)
    store.update_job(job_id, status="completed", rating="Hold", result={"sections": {
        "final_trade_decision": "<script>alert(1)</script> **bold** [x](javascript:alert(2))"}})
    page = client.get(f"/runs/{job_id}").text
    assert "<script>alert(1)</script>" not in page
    assert "<strong>bold</strong>" in page
    assert 'href="javascript:' not in page


def test_the_portfolio_is_saved_and_snapshotted_into_a_run(client, store):
    response = client.post("/portfolio", data={"cash": "25000", "currency": "USD",
                                               "pos_ticker": ["nvda", ""], "pos_quantity": ["120", ""],
                                               "pos_price": ["150", ""]}, follow_redirects=False)
    assert response.status_code == 303
    saved = client.get("/api/portfolio").json()
    assert saved == {"cash": 25000.0, "currency": "USD",
                     "positions": [{"ticker": "NVDA", "quantity": 120.0, "average_price": 150.0}]}

    job_id = client.post("/api/runs", json={"ticker": "NVDA", "trade_date": TRADE_DATE,
                                            "use_portfolio": True}).json()["id"]
    client.put("/api/portfolio", json={"cash": 1, "positions": []})
    # The run keeps the book it was asked with, so a resume hashes the same portfolio.
    assert store.get_job(job_id)["request"]["portfolio"] == saved


def test_a_portfolio_round_trips_through_json_files(client):
    book = {"cash": 10.5, "currency": "KRW", "positions": [{"ticker": "005930.KS", "quantity": 3}]}
    imported = client.post("/portfolio/import", files={"file": ("book.json", io.BytesIO(json.dumps(book).encode()))},
                           follow_redirects=False)
    assert imported.status_code == 303
    exported = client.get("/portfolio/export").json()
    assert exported["positions"][0] == {"ticker": "005930.KS", "quantity": 3.0, "average_price": None}
    bad = client.post("/portfolio/import", files={"file": ("x.json", io.BytesIO(b"{not json"))})
    assert bad.status_code == 400


def test_using_a_portfolio_before_saving_one_is_refused(client):
    response = client.post("/api/runs", json={"ticker": "NVDA", "trade_date": TRADE_DATE, "use_portfolio": True})
    assert response.status_code == 400


def test_saved_defaults_fill_what_a_request_leaves_out(client, store):
    client.post("/settings", data={"llm_provider": "anthropic", "deep_think_llm": "claude-opus-5-5",
                                   "quick_think_llm": "claude-sonnet-5", "output_language": "Japanese",
                                   "max_debate_rounds": "2", "max_risk_discuss_rounds": "1",
                                   "analysts": ["news"], "checkpoint": "on"})
    job_id = client.post("/api/runs", json={"ticker": "AAPL", "trade_date": TRADE_DATE,
                                            "output_language": "Korean"}).json()["id"]
    request = store.get_job(job_id)["request"]
    assert request["llm_provider"] == "anthropic"
    assert request["deep_think_llm"] == "claude-opus-5-5"
    assert request["output_language"] == "Korean"          # the request wins
    assert request["max_debate_rounds"] == 2 and request["analysts"] == ["news"]
    assert request["checkpoint"] is True
    assert request["backend_url"] == "https://api.anthropic.com/"


def test_a_provider_without_models_named_gets_its_own_default_models(client, store):
    job_id = client.post("/api/runs", json={"ticker": "AAPL", "trade_date": TRADE_DATE,
                                            "llm_provider": "anthropic"}).json()["id"]
    request = store.get_job(job_id)["request"]
    assert request["deep_think_llm"].startswith("claude") and request["quick_think_llm"].startswith("claude")


def test_resume_replays_the_stored_request(client, store):
    job_id = client.post("/api/runs", json={"ticker": "NVDA", "trade_date": TRADE_DATE,
                                            "checkpoint": True}).json()["id"]
    store.update_job(job_id, status="failed", resumable=True)
    new_id = client.post(f"/api/runs/{job_id}/resume").json()["id"]
    assert store.get_job(new_id)["request"] == store.get_job(job_id)["request"]
    assert store.get_job(new_id)["resumed_from"] == job_id
    assert not store.get_job(job_id)["resumable"]
    assert client.post(f"/api/runs/{job_id}/resume").status_code == 409


def test_a_backtest_larger_than_the_cap_is_refused(client, settings):
    settings.max_backtest_cells = 4
    response = client.post("/api/backtests", json={"tickers": ["NVDA", "AAPL"], "start": "2026-01-01",
                                                   "end": "2026-01-31", "every_n_days": 7})
    assert response.status_code == 400 and "최대 4개" in response.json()["detail"]


def test_a_backtest_runs_and_its_page_shows_the_grid(client, store, offline):
    job_id = client.post("/api/backtests", json={"tickers": ["NVDA"], "start": "2026-01-05",
                                                 "end": "2026-01-09", "every_n_days": 2,
                                                 "analysts": ["market"]}).json()["id"]
    job = _run(store, job_id)
    assert job["status"] == "completed", job["error"]
    assert job["result"]["cells_run"] == 3
    assert {c["date"] for c in job["result"]["cells"]} == {"2026-01-05", "2026-01-07", "2026-01-09"}
    page = client.get(f"/backtests/{job_id}").text
    assert "티커 × 날짜" in page and "Overw" in page
    # The backtest wrote to a log of its own, not the live memory log.
    assert client.get("/api/memory").json() == []


def test_the_memory_page_lists_decisions_and_settle_queues_a_job(client, store, offline):
    _run(store, client.post("/api/runs", json={"ticker": "NVDA", "trade_date": TRADE_DATE}).json()["id"])
    entries = client.get("/api/memory?ticker=nvda").json()
    assert [(e["ticker"], e["pending"]) for e in entries] == [("NVDA", True)]
    page = client.get("/memory").text
    assert "지금 정산" in page and "NVDA" in page
    response = client.post("/memory/settle", data={"ticker": "NVDA"}, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"].startswith("/jobs/")


def test_checkpoints_are_listed_and_deleted(client, home):
    directory = home / "cache" / "checkpoints"
    directory.mkdir(parents=True)
    (directory / "NVDA.db").write_bytes(b"x" * 10)
    (directory / "NVDA.db-wal").write_bytes(b"y")
    assert [c["ticker"] for c in client.get("/api/checkpoints").json()] == ["NVDA"]
    assert client.delete("/api/checkpoints/nvda").json() == {"deleted": 1}
    assert list(directory.iterdir()) == []


def test_key_status_never_shows_a_key(client, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret-value")
    rows = client.get("/api/settings/keys").json()
    assert {"provider": "openai", "label": "OpenAI", "env": "OPENAI_API_KEY", "set": True, "needs_key": True} in rows
    assert "sk-secret-value" not in client.get("/settings").text


def test_a_cross_site_post_is_refused(client):
    response = client.post("/runs", data=_form(), headers={"Origin": "https://evil.example"})
    assert response.status_code == 403


def test_the_endpoint_is_shown_without_credentials(client, store):
    job_id = client.post("/api/runs", json={"ticker": "NVDA", "trade_date": TRADE_DATE,
                                            "llm_provider": "openai_compatible",
                                            "deep_think_llm": "m", "quick_think_llm": "m",
                                            "backend_url": "https://user:pass@relay.example:8443/v1"}).json()["id"]
    assert client.get(f"/api/runs/{job_id}").json()["request"]["backend_url"] == "https://relay.example:8443"


def test_with_a_token_everything_but_login_needs_it(tmp_path, home):
    app = create_app(AppSettings(db_path=tmp_path / "web.db", token="s3cret"), start_manager=False)
    with TestClient(app) as c:
        assert c.get("/", follow_redirects=False).headers["location"] == "/login?next=/"
        assert c.get("/api/runs").status_code == 401
        assert c.get("/api/runs", headers={"Authorization": "Bearer s3cret"}).status_code == 200
        assert c.post("/login", data={"token": "wrong", "next": "/"}).status_code == 401
        login = c.post("/login", data={"token": "s3cret", "next": "//evil.example"}, follow_redirects=False)
        assert login.headers["location"] == "/"
        assert c.get("/").status_code == 200       # the cookie from the login


def test_default_paths_point_at_the_test_home(home):
    assert DEFAULT_CONFIG["results_dir"].startswith(str(home))


def test_without_a_token_a_rebound_hostname_is_refused(client):
    assert client.get("/", headers={"Host": "attacker.example"}).status_code == 400
    assert client.get("/", headers={"Host": "127.0.0.1:8000"}).status_code == 200


def test_a_completed_backtest_with_pending_cells_reruns_to_settle_them(client, store, offline):
    job_id = client.post("/api/backtests", json={"tickers": ["NVDA"], "start": "2026-01-05",
                                                 "end": "2026-01-07", "every_n_days": 2,
                                                 "analysts": ["market"]}).json()["id"]
    job = _run(store, job_id)
    assert job["status"] == "completed" and job["result"]["summary"]["pending"] == 2
    assert "대기 칸 정산" in client.get(f"/backtests/{job_id}").text

    again = client.post(f"/api/backtests/{job_id}/resume")
    assert again.status_code == 201
    assert client.post(f"/api/backtests/{job_id}/resume").status_code == 409   # one at a time
    rerun = _run(store, again.json()["id"])
    assert rerun["status"] == "completed"
    # Every cell was already in the run's log: nothing re-analyzed, only the settle pass ran.
    assert rerun["result"]["cells_run"] == 0 and rerun["result"]["skipped"] == 2
    assert rerun["request"]["run_id"] == job["request"]["run_id"]
