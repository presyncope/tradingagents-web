"""Fixtures that run the real TradingAgents graph with no network and no API key.

``ScriptedModel`` and the ``offline`` patches follow TradingAgents'
``tests/test_graph_end_to_end.py`` (v0.5.2): every tool-using analyst calls
each of its tools once, then every agent answers with a fixed text that carries
an Overweight rating.
"""

from __future__ import annotations

import time

import pandas as pd
import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import RunnableLambda
from pydantic import Field
from tradingagents.agents import context, schemas
from tradingagents.agents.analysts import sentiment_analyst
from tradingagents.dataflows import router
from tradingagents.dataflows.vendors.yahoo import market as yahoo_market
from tradingagents.dataflows.vendors.yahoo import snapshot
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph import trading_graph

import tradingagents_web  # noqa: F401

TRADE_DATE = "2026-01-09"
TEXT = "Report.\n\n**Rating**: Overweight\n\nFINAL TRANSACTION PROPOSAL: **BUY**"

STRUCTURED = {
    schemas.ResearchPlan: schemas.ResearchPlan(
        recommendation=schemas.PortfolioRating.OVERWEIGHT, rationale="r", strategic_actions="a"),
    schemas.TraderProposal: schemas.TraderProposal(action=schemas.TraderAction.BUY, reasoning="r"),
    schemas.PortfolioDecision: schemas.PortfolioDecision(
        rating=schemas.PortfolioRating.OVERWEIGHT, executive_summary="s", investment_thesis="t"),
    schemas.SentimentReport: schemas.SentimentReport(
        overall_band=schemas.SentimentBand.NEUTRAL, overall_score=5.0, confidence="low", narrative="n"),
}

ARGS = {"symbol": "NVDA", "ticker": "NVDA", "curr_date": TRADE_DATE, "as_of_date": TRADE_DATE,
        "start_date": "2026-01-02", "end_date": TRADE_DATE, "indicator": "rsi",
        "topic": "Fed rate cut", "freq": "quarterly"}


class ScriptedModel(BaseChatModel):
    """Calls every bound tool once, then answers with TEXT."""

    structured: bool = False
    tools: tuple = ()
    calls: list = Field(default_factory=list)
    fail_at: int | None = None       # raise on this call
    delay: float = 0.0               # seconds per call, for tests that watch a run

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):
        return self.model_copy(update={"tools": tuple(tools)})

    def with_structured_output(self, schema, **kwargs):
        if not self.structured:
            raise NotImplementedError
        return RunnableLambda(lambda _: self._count() or STRUCTURED[schema])

    def _count(self) -> None:
        self.calls.append(1)
        if len(self.calls) == self.fail_at:
            raise RuntimeError("provider unavailable")

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        self._count()
        if self.delay:
            time.sleep(self.delay)
        if self.tools and not isinstance(messages[-1], ToolMessage):
            calls = [{"name": t.name, "id": f"call_{i}",
                      "args": {k: v for k, v in ARGS.items()
                               if k in t.tool_call_schema.model_json_schema()["properties"]}}
                     for i, t in enumerate(self.tools)]
            message = AIMessage(content="", tool_calls=calls)
        else:
            message = AIMessage(content=TEXT)
        return ChatResult(generations=[ChatGeneration(message=message)])


class _Client:
    def __init__(self, model):
        self.model = model

    def get_llm(self):
        return self.model


def patch_offline(monkeypatch, model: BaseChatModel) -> None:
    """Every data vendor answers locally and every LLM is ``model``."""
    for method, vendors in router.VENDOR_METHODS.items():
        for vendor in vendors:
            monkeypatch.setitem(vendors, vendor, lambda *a, _m=method, **k: f"{_m} data")
    prices = pd.DataFrame({
        "Date": pd.bdate_range(end=TRADE_DATE, periods=60),
        "Open": 100.0, "High": 101.0, "Low": 99.0, "Close": 100.5, "Volume": 1_000_000,
    })
    monkeypatch.setattr(snapshot, "load_ohlcv", lambda *a, **k: prices.copy())
    monkeypatch.setattr(sentiment_analyst, "fetch_stocktwits_messages", lambda *a, **k: "no posts")
    monkeypatch.setattr(sentiment_analyst, "fetch_reddit_posts", lambda *a, **k: "no posts")
    monkeypatch.setattr(yahoo_market.yf, "Ticker",
                        lambda s: type("T", (), {"info": {"longName": "NVIDIA"}})())
    monkeypatch.setattr(trading_graph, "create_llm_client", lambda **k: _Client(model))
    context._identity.cache_clear()


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Keep every file TradingAgents writes under the test's directory."""
    monkeypatch.setitem(DEFAULT_CONFIG, "results_dir", str(tmp_path / "logs"))
    monkeypatch.setitem(DEFAULT_CONFIG, "data_cache_dir", str(tmp_path / "cache"))
    monkeypatch.setitem(DEFAULT_CONFIG, "memory_log_path", str(tmp_path / "memory" / "log.md"))
    monkeypatch.setitem(DEFAULT_CONFIG, "llm_provider", "openai")
    monkeypatch.setitem(DEFAULT_CONFIG, "deep_think_llm", "gpt-6-sol")
    monkeypatch.setitem(DEFAULT_CONFIG, "quick_think_llm", "gpt-6-luna")
    monkeypatch.setitem(DEFAULT_CONFIG, "backend_url", None)
    monkeypatch.setitem(DEFAULT_CONFIG, "checkpoint_enabled", False)
    return tmp_path


@pytest.fixture
def offline(monkeypatch, home):
    model = ScriptedModel()
    patch_offline(monkeypatch, model)
    return model


from tradingagents_web import services as _services  # noqa: E402

REAL_INSTRUMENT_IDENTITY = _services.instrument_identity


@pytest.fixture(autouse=True)
def no_identity_lookup(monkeypatch):
    """Ticker previews ask Yahoo Finance for the company; tests never reach the network."""
    from tradingagents_web import services

    monkeypatch.setattr(services, "instrument_identity", lambda ticker, wait=0: {})


@pytest.fixture(autouse=True)
def no_portfolio_file(monkeypatch):
    """The developer's .env may name a live portfolio file; tests set their own."""
    monkeypatch.delenv("TRADINGAGENTS_WEB_PORTFOLIO_FILE", raising=False)
