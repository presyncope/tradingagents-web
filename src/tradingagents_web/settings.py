"""Server settings, read from ``TRADINGAGENTS_WEB_*`` environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

HOME = Path(os.path.expanduser("~")) / ".tradingagents"


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw and raw.strip() else default


@dataclass
class AppSettings:
    db_path: Path = field(
        default_factory=lambda: Path(os.environ.get("TRADINGAGENTS_WEB_DB") or HOME / "webapp.db")
    )
    # Runs at once. Each run is its own process calling the LLM provider.
    max_workers: int = field(default_factory=lambda: _int("TRADINGAGENTS_WEB_MAX_WORKERS", 2))
    # A backtest larger than this many ticker x date cells is refused.
    max_backtest_cells: int = field(
        default_factory=lambda: _int("TRADINGAGENTS_WEB_MAX_BACKTEST_CELLS", 200)
    )
    # Empty means no login, which is allowed only on a loopback address.
    token: str = field(default_factory=lambda: os.environ.get("TRADINGAGENTS_WEB_TOKEN", ""))
    # Host headers accepted while there is no token (DNS-rebinding guard).
    allowed_hosts: list[str] = field(default_factory=lambda: [
        h.strip() for h in (os.environ.get("TRADINGAGENTS_WEB_ALLOWED_HOSTS") or "127.0.0.1,localhost").split(",")
        if h.strip()])
    # Seconds between scheduler passes.
    poll_interval: float = 1.0
    # Register reports the CLI saved under results_dir/reports when the server starts.
    import_on_start: bool = True
    # A portfolio file another tool keeps current (e.g. systematic-trading's
    # toss-export-portfolio); the saved portfolio follows it whenever it changes.
    portfolio_file: Path | None = field(default_factory=lambda: (
        Path(os.environ["TRADINGAGENTS_WEB_PORTFOLIO_FILE"]).expanduser()
        if os.environ.get("TRADINGAGENTS_WEB_PORTFOLIO_FILE") else None))
    # Seconds between checks of portfolio_file.
    portfolio_poll: float = 30.0
