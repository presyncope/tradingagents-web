"""Register reports the CLI saved, so they appear in the run history beside web runs.

The CLI (and ``TradingAgentsGraph.save_reports``) writes a report tree to
``results_dir/reports/<TICKER>_<stamp>/``: ``complete_report.md`` with a header
naming the analysis date, models and analysts, and one file per section. Each
tree not yet known to the web database becomes a completed analysis with
``source = 'cli'``. Nothing on disk is changed; the files stay where they are.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import UTC, datetime
from pathlib import Path

from tradingagents.agents.rating import parse_rating
from tradingagents.dataflows.symbols import safe_ticker_component
from tradingagents.default_config import DEFAULT_CONFIG

from tradingagents_web.config import detect_asset_type
from tradingagents_web.progress import sections_from_state
from tradingagents_web.store.db import Store

logger = logging.getLogger(__name__)

_TITLE = re.compile(r"^# Trading Analysis Report: (\S+)", re.M)
_DATE = re.compile(r"^- Analysis date: (\d{4}-\d{2}-\d{2})", re.M)
_GENERATED = re.compile(r"^- Generated: (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", re.M)
_MODELS = re.compile(r"^- TradingAgents (\S+): ([^,]+), deep ([^,]+), quick (\S+)", re.M)
_ANALYSTS = re.compile(r"^- Analysts: ([^;]*); research debate rounds (\S+), risk debate rounds (\S+)", re.M)

# Section files under the tree, mapped onto the graph state they were written from.
_FILES = {
    "market_report": "1_analysts/market.md",
    "sentiment_report": "1_analysts/sentiment.md",
    "news_report": "1_analysts/news.md",
    "fundamentals_report": "1_analysts/fundamentals.md",
    "investment_plan": "2_research/manager.md",
    "trader_investment_plan": "3_trading/trader.md",
    "final_trade_decision": "5_portfolio/decision.md",
}
_DEBATE = {"bull_history": "2_research/bull.md", "bear_history": "2_research/bear.md"}
_RISK = {"aggressive_history": "4_risk/aggressive.md", "conservative_history": "4_risk/conservative.md",
         "neutral_history": "4_risk/neutral.md"}


def _read(root: Path, relative: str) -> str:
    path = root / relative
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def _int(value: str) -> int | None:
    return int(value) if value.isdigit() else None


def parse_tree(root: Path) -> dict:
    """What one report tree says about its run: header fields, sections, rating."""
    header = (root / "complete_report.md").read_text(encoding="utf-8")[:4000]
    title = _TITLE.search(header)
    if not title:
        raise ValueError("not a TradingAgents report")
    ticker = safe_ticker_component(title.group(1).upper())
    state = {key: _read(root, rel) for key, rel in _FILES.items()}
    state["investment_debate_state"] = {key: _read(root, rel) for key, rel in _DEBATE.items()}
    state["risk_debate_state"] = {key: _read(root, rel) for key, rel in _RISK.items()}

    request: dict = {"ticker": ticker, "asset_type": detect_asset_type(ticker)}
    settings: dict = {}
    if date := _DATE.search(header):
        request["trade_date"] = date.group(1)
    if models := _MODELS.search(header):
        version, provider, deep, quick = (g.strip() for g in models.groups())
        request.update(llm_provider=provider, deep_think_llm=deep, quick_think_llm=quick)
        settings.update(version=version, llm_provider=provider, deep_think_llm=deep, quick_think_llm=quick)
    if analysts := _ANALYSTS.search(header):
        names = [a.strip() for a in analysts.group(1).split(",") if a.strip()]
        request.update(analysts=names, max_debate_rounds=_int(analysts.group(2)),
                       max_risk_discuss_rounds=_int(analysts.group(3)))
        settings.update(analysts=names, max_debate_rounds=_int(analysts.group(2)),
                        max_risk_discuss_rounds=_int(analysts.group(3)))
    generated = None
    if stamp := _GENERATED.search(header):
        local = datetime.strptime(stamp.group(1), "%Y-%m-%d %H:%M:%S").astimezone()
        generated = local.astimezone(UTC).isoformat(timespec="seconds")
    decision = state["final_trade_decision"]
    return {
        "ticker": ticker,
        "trade_date": request.get("trade_date"),
        "request": request,
        "settings": settings or None,
        "rating": parse_rating(decision) if decision.strip() else None,
        "sections": sections_from_state(state),
        "generated": generated,
    }


def report_root() -> Path:
    return Path(DEFAULT_CONFIG["results_dir"]) / "reports"


def import_reports(store: Store, root: Path | None = None) -> dict:
    """Register every report tree under ``root`` the database does not know yet."""
    root = root or report_root()
    known = {os.path.realpath(p) for p in store.report_dirs()}
    # Runs deleted from the web history while their files stayed on disk.
    known |= set(store.get_value("import_ignore", []))
    # A web run saves its tree just before it records the folder; while one runs
    # for a ticker, that ticker's new trees may be its own and wait for a later scan.
    busy = {t for job in store.running_jobs() for t in job["tickers"]}
    imported, skipped, errors = [], 0, []
    if not root.is_dir():
        return {"imported": imported, "skipped": skipped, "errors": errors}
    for tree in sorted(p for p in root.iterdir() if (p / "complete_report.md").is_file()):
        if os.path.realpath(tree) in known:
            skipped += 1
            continue
        try:
            parsed = parse_tree(tree)
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            errors.append(f"{tree.name}: {exc}")
            continue
        if parsed["ticker"] in busy:
            skipped += 1
            continue
        job_id = store.create_job("analysis", parsed["request"], [parsed["ticker"]],
                                  ticker=parsed["ticker"], trade_date=parsed["trade_date"],
                                  source="cli", status="completed")
        when = parsed["generated"]
        store.update_job(job_id, rating=parsed["rating"], report_dir=str(tree),
                         settings=parsed["settings"], result={"sections": parsed["sections"]},
                         **({"created_at": when, "started_at": when, "finished_at": when} if when else {}))
        imported.append(job_id)
    if errors:
        logger.warning("Could not import %d report(s): %s", len(errors), errors)
    return {"imported": imported, "skipped": skipped, "errors": errors}
