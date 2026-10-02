"""Load the ``.env`` file before anything reads the environment."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

_loaded = False


def load_env() -> Path | None:
    """Apply ``TRADINGAGENTS_WEB_ENV_FILE`` (or ``./.env``) to ``os.environ`` once.

    Variables already set in the environment win over the file, so a value
    given on the command line or by Docker is never replaced. Returns the file
    that was loaded, or None when there was none.
    """
    global _loaded
    if _loaded:
        return None
    _loaded = True
    path = Path(os.environ.get("TRADINGAGENTS_WEB_ENV_FILE") or ".env").expanduser()
    if path.is_file():
        load_dotenv(path, override=False)
        return path
    return None
