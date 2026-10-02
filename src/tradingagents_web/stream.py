"""Follow a job's stored events as they are written.

Workers write events to the database; a stream polls for the ones after the
last it sent. A client that reconnects passes ``Last-Event-ID`` (the event's
``seq``) and continues from there, so nothing is lost or repeated. The
stream ends after the job's last event.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import Request

from tradingagents_web.store.db import FINISHED, Store

END_TYPES = {"done", "error", "cancelled"}
POLL = 0.5


def start_after(request: Request, after: int) -> int:
    last = request.headers.get("last-event-id", "")
    return int(last) if last.isdigit() else after


async def follow(db_path: str | Path, job_id: int, after: int = 0) -> AsyncIterator[dict]:
    """Yield the job's events after ``after`` until it has finished."""
    store = await asyncio.to_thread(Store, db_path, init=False)
    try:
        while True:
            events = await asyncio.to_thread(store.events, job_id, after, 500)
            for event in events:
                after = event["seq"]
                yield event
                if event["type"] in END_TYPES:
                    return
            if events:
                continue
            job = await asyncio.to_thread(store.get_job, job_id)
            if job is None:
                return
            if job["status"] in FINISHED:
                # Anything written between the last read and the status change.
                for event in await asyncio.to_thread(store.events, job_id, after):
                    yield event
                return
            await asyncio.sleep(POLL)
    finally:
        await asyncio.to_thread(store.close)
