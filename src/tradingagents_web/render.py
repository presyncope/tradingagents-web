"""Templates, and the one way model-written markdown becomes HTML.

Reports are written by an LLM from news and social posts, so they are treated
as untrusted: raw HTML in the markdown is not passed through, and the rendered
HTML is cleaned by nh3 before it is marked safe. Everything else in the
templates is escaped by Jinja.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import nh3
from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt
from markupsafe import Markup

from tradingagents_web.clock import zone
from tradingagents_web.progress import SECTION_TITLES

_md = MarkdownIt("commonmark", {"html": False, "linkify": False}).enable("table")

STATUS_LABELS = {"queued": "대기", "running": "실행 중", "completed": "완료",
                 "failed": "실패", "cancelled": "취소"}
KIND_LABELS = {"analysis": "분석", "backtest": "백테스트", "settle": "정산"}
AGENT_LABELS = {"pending": "대기", "in_progress": "진행 중", "completed": "완료"}
RATING_CLASS = {"Buy": "pos", "Overweight": "pos", "Hold": "neutral",
                "Underweight": "neg", "Sell": "neg", "REVIEW": "warn"}


def markdown(text: str | None) -> Markup:
    if not text:
        return Markup("")
    return Markup(nh3.clean(_md.render(text)))


def when(value: str | None) -> str:
    """An ISO timestamp in the server's display zone (TRADINGAGENTS_WEB_TZ), minutes precision."""
    if not value:
        return ""
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return value
    if moment.tzinfo is not None:
        moment = moment.astimezone(zone())
    return moment.strftime("%Y-%m-%d %H:%M")


def duration(job: dict) -> str:
    if not job.get("started_at"):
        return ""
    start = datetime.fromisoformat(job["started_at"])
    end = datetime.fromisoformat(job["finished_at"]) if job.get("finished_at") else None
    if end is None:
        from datetime import UTC
        end = datetime.now(UTC)
    seconds = int((end - start).total_seconds())
    minutes, seconds = divmod(max(seconds, 0), 60)
    return f"{minutes}분 {seconds}초" if minutes else f"{seconds}초"


def endpoint(url: str | None) -> str:
    """An endpoint without anything that could be a credential: scheme and host only."""
    if not url:
        return ""
    parts = urlsplit(url)
    host = parts.hostname or ""
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{host}{port}" if parts.scheme else url


def size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def templates() -> Jinja2Templates:
    env = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
    env.env.filters.update(
        markdown=markdown, when=when, endpoint=endpoint, size=size,
        status_label=lambda s: STATUS_LABELS.get(s, s),
        kind_label=lambda k: KIND_LABELS.get(k, k),
        agent_label=lambda s: AGENT_LABELS.get(s, s),
        rating_class=lambda r: RATING_CLASS.get(r or "", "neutral"),
        section_title=lambda k: SECTION_TITLES.get(k, k),
        as_item=lambda e: {"type": e["type"], "ts": e["ts"], **e["data"]},
    )
    env.env.globals.update(duration=duration)
    return env
