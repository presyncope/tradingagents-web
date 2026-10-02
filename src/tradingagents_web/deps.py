"""Request-scoped dependencies shared by the page and API routers."""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import HTTPException, Request

from tradingagents_web.jobs.manager import JobManager
from tradingagents_web.settings import AppSettings
from tradingagents_web.store.db import Store


def get_store(request: Request) -> Iterator[Store]:
    """A connection of this request's own; SQLite connections are not shared across threads."""
    store = Store(request.app.state.settings.db_path, init=False)
    try:
        yield store
    finally:
        store.close()


def get_manager(request: Request) -> JobManager:
    manager = request.app.state.manager
    if manager is None:
        raise HTTPException(503, "job manager is not running")
    return manager


def optional_manager(request: Request) -> JobManager | None:
    return request.app.state.manager


def get_settings(request: Request) -> AppSettings:
    return request.app.state.settings
