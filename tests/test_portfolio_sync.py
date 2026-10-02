"""The saved portfolio follows a file another tool keeps current (systematic-trading's export)."""

from __future__ import annotations

import json
import os

import pytest
from conftest import TRADE_DATE
from fastapi.testclient import TestClient

from tradingagents_web import services
from tradingagents_web.app import create_app
from tradingagents_web.settings import AppSettings
from tradingagents_web.store.db import Store

DOC = {"cash": 1234.5, "currency": "USD", "source": "toss", "synced_at": "2026-10-02T14:27:17+00:00",
       "account_seq": 1, "positions": [{"ticker": "nvda", "quantity": 10, "average_price": 120.5},
                                       {"ticker": "GOOGL", "quantity": 3.5, "average_price": None}]}


@pytest.fixture
def book(tmp_path):
    path = tmp_path / "portfolio.json"
    path.write_text(json.dumps(DOC))
    return path


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "web.db")
    yield s
    s.close()


def test_the_file_becomes_the_saved_portfolio(store, book):
    status = services.sync_portfolio_file(store, book)
    assert status["error"] is None and status["positions"] == 2 and status["source"] == "toss"
    assert services.get_portfolio(store) == {
        "cash": 1234.5, "currency": "USD",
        "positions": [{"ticker": "NVDA", "quantity": 10.0, "average_price": 120.5},
                      {"ticker": "GOOGL", "quantity": 3.5, "average_price": None}]}


def test_an_unchanged_file_is_not_read_again_and_a_changed_one_is(store, book):
    services.sync_portfolio_file(store, book)
    store.set_value("portfolio", {"cash": 1, "currency": "USD", "positions": []})    # a manual edit
    services.sync_portfolio_file(store, book)
    assert services.get_portfolio(store)["cash"] == 1                                   # file unchanged
    book.write_text(json.dumps({**DOC, "cash": 99.0}))
    os.utime(book, (book.stat().st_atime, book.stat().st_mtime + 5))
    services.sync_portfolio_file(store, book)
    assert services.get_portfolio(store)["cash"] == 99.0


def test_a_broken_or_missing_file_keeps_the_last_portfolio(store, book):
    services.sync_portfolio_file(store, book)
    book.write_text("{not json")
    os.utime(book, (book.stat().st_atime, book.stat().st_mtime + 5))
    assert services.sync_portfolio_file(store, book)["error"]
    assert services.get_portfolio(store)["cash"] == 1234.5
    book.unlink()
    assert "읽을 수 없습니다" in services.sync_portfolio_file(store, book)["error"]
    assert services.get_portfolio(store)["cash"] == 1234.5


def test_pages_show_the_sync_and_runs_snapshot_it(tmp_path, home, book):
    settings = AppSettings(db_path=tmp_path / "web.db", token="", allowed_hosts=["testserver"],
                           portfolio_file=book)
    with TestClient(create_app(settings, start_manager=False)) as client:
        assert client.post("/portfolio/sync", follow_redirects=False).status_code == 303
        page = client.get("/portfolio").text
        assert "자동 동기화 (Toss)" in page and "2026-10-02" in page and "2종목" in page
        assert "Toss" in client.get("/runs/new").text
        job_id = client.post("/api/runs", json={"ticker": "NVDA", "trade_date": TRADE_DATE,
                                                "use_portfolio": True}).json()["id"]
        snapshot = Store(settings.db_path).get_job(job_id)["request"]["portfolio"]
        assert snapshot["positions"][0] == {"ticker": "NVDA", "quantity": 10.0, "average_price": 120.5}
        assert client.get("/api/portfolio/sync").json()["synced_at"] == DOC["synced_at"]


def test_without_a_file_the_sync_button_explains(tmp_path, home):
    settings = AppSettings(db_path=tmp_path / "web.db", token="", allowed_hosts=["testserver"])
    with TestClient(create_app(settings, start_manager=False)) as client:
        assert client.post("/api/portfolio/sync").status_code == 400
        assert "자동 동기화" not in client.get("/portfolio").text
