"""SQLite storage for jobs, their events, and the few values the web UI keeps."""

from tradingagents_web.store.db import Store, connect

__all__ = ["Store", "connect"]
