"""Load the ``.env`` files before anything reads the environment."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

_loaded = False


def load_env() -> list[Path]:
    """Apply the environment files to ``os.environ`` once; returns the files loaded.

    Read in this order, each without replacing a value already set:

    1. ``TRADINGAGENTS_WEB_ENV_FILE`` when the shell (or Docker) sets it,
    2. ``./.env``,
    3. ``TRADINGAGENTS_WEB_ENV_FILE`` when ``./.env`` names it, so the web
       server's own ``.env`` can hold its settings and point at the
       TradingAgents ``.env`` that holds the API keys.

    Variables already in the environment always win, so a value given on the
    command line or by Docker is never replaced.
    """
    global _loaded
    if _loaded:
        return []
    _loaded = True
    loaded: list[Path] = []

    def load(raw: str) -> None:
        path = Path(raw).expanduser()
        if path.is_file() and all(path.resolve() != p.resolve() for p in loaded):
            load_dotenv(path, override=False)
            loaded.append(path)

    first = os.environ.get("TRADINGAGENTS_WEB_ENV_FILE")
    if first:
        load(first)
    load(".env")
    named = os.environ.get("TRADINGAGENTS_WEB_ENV_FILE")
    if named and named != first:
        load(named)
    return loaded
