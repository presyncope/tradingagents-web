"""The time zone schedules run in and pages show times in."""

from __future__ import annotations

import os
from datetime import date, datetime, tzinfo
from zoneinfo import ZoneInfo


def zone() -> tzinfo:
    """``TRADINGAGENTS_WEB_TZ`` (an IANA name such as Asia/Seoul), else the system's zone."""
    name = os.environ.get("TRADINGAGENTS_WEB_TZ")
    return ZoneInfo(name) if name else datetime.now().astimezone().tzinfo


def today() -> date:
    """Today in the configured zone, never later than the system's today (the graph's limit)."""
    return min(datetime.now(zone()).date(), date.today())
