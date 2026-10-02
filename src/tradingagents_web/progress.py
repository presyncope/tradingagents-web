"""From a running graph to events, and from events back to what the page shows.

``ProgressTracker`` sits in the worker: it reads each ``(messages, state)`` pair
``TradingAgentsGraph.stream_run`` yields and returns the events to store.
``RunView`` sits in the server: it folds stored events into the state of the
run page, so a page opened mid-run shows everything that happened so far, and
the SSE stream sends only what follows.

Agent statuses and report sections are derived from the accumulated graph
state, never from the order chunks arrive in, so a run resumed from a
checkpoint (whose first chunk already carries earlier reports) shows the
right statuses at once. The rules follow the CLI's live view.
"""

from __future__ import annotations

from typing import Any

PENDING, IN_PROGRESS, COMPLETED = "pending", "in_progress", "completed"

ANALYST_AGENTS = {
    "market": ("Market Analyst", "market_report"),
    "social": ("Sentiment Analyst", "sentiment_report"),
    "news": ("News Analyst", "news_report"),
    "fundamentals": ("Fundamentals Analyst", "fundamentals_report"),
}
RESEARCH_TEAM = ["Bull Researcher", "Bear Researcher", "Research Manager"]
RISK_TEAM = ["Aggressive Analyst", "Neutral Analyst", "Conservative Analyst"]

# Report sections in reading order: (key, title).
SECTIONS = [
    ("market_report", "Market Analyst"),
    ("sentiment_report", "Sentiment Analyst"),
    ("news_report", "News Analyst"),
    ("fundamentals_report", "Fundamentals Analyst"),
    ("investment_debate", "Research Team: Bull/Bear 토론과 Research Manager"),
    ("trader_investment_plan", "Trader"),
    ("risk_debate", "Risk Management: Aggressive/Conservative/Neutral 토론"),
    ("final_trade_decision", "Portfolio Manager 최종 결정"),
]
SECTION_TITLES = dict(SECTIONS)

MESSAGE_LIMIT = 4000     # characters of one message kept in the feed
FEED_LIMIT = 300         # feed items a page shows


def teams(analysts: list[str]) -> list[tuple[str, list[str]]]:
    return [
        ("Analyst Team", [ANALYST_AGENTS[a][0] for a in analysts if a in ANALYST_AGENTS]),
        ("Research Team", RESEARCH_TEAM),
        ("Trading Team", ["Trader"]),
        ("Risk Management", RISK_TEAM),
        ("Portfolio Management", ["Portfolio Manager"]),
    ]


def _joined(parts: list[tuple[str, str | None]]) -> str:
    return "\n\n".join(f"### {name}\n\n{text.strip()}" for name, text in parts if text and text.strip())


def sections_from_state(state: dict) -> dict[str, str]:
    """Report sections present in a (partial or final) graph state."""
    out: dict[str, str] = {}
    for key in ("market_report", "sentiment_report", "news_report", "fundamentals_report",
                "trader_investment_plan", "final_trade_decision"):
        if (state.get(key) or "").strip():
            out[key] = state[key].strip()
    debate = state.get("investment_debate_state") or {}
    research = _joined([("Bull Researcher", debate.get("bull_history")),
                        ("Bear Researcher", debate.get("bear_history")),
                        ("Research Manager", state.get("investment_plan"))])
    if research:
        out["investment_debate"] = research
    risk = state.get("risk_debate_state") or {}
    risk_text = _joined([("Aggressive Analyst", risk.get("aggressive_history")),
                         ("Conservative Analyst", risk.get("conservative_history")),
                         ("Neutral Analyst", risk.get("neutral_history"))])
    if risk_text:
        out["risk_debate"] = risk_text
    return out


def statuses_from_state(state: dict, analysts: list[str]) -> dict[str, str]:
    """Each agent's status, read off what the state already holds."""
    status: dict[str, str] = {}
    all_filed = bool(analysts)
    for key in analysts:
        name, report = ANALYST_AGENTS[key]
        filed = bool((state.get(report) or "").strip())
        status[name] = COMPLETED if filed else IN_PROGRESS
        all_filed = all_filed and filed

    plan = (state.get("investment_plan") or "").strip()
    research = COMPLETED if plan else (IN_PROGRESS if all_filed else PENDING)
    for name in RESEARCH_TEAM:
        status[name] = research

    trader_plan = (state.get("trader_investment_plan") or "").strip()
    status["Trader"] = COMPLETED if trader_plan else (IN_PROGRESS if plan else PENDING)

    final = (state.get("final_trade_decision") or "").strip()
    risk = state.get("risk_debate_state") or {}
    for name, field in zip(RISK_TEAM, ("aggressive_history", "neutral_history",
                                       "conservative_history"), strict=True):
        if final:
            status[name] = COMPLETED
        elif trader_plan or (risk.get(field) or "").strip():
            status[name] = IN_PROGRESS
        else:
            status[name] = PENDING
    risk_started = any((risk.get(f) or "").strip() for f in
                       ("aggressive_history", "neutral_history", "conservative_history"))
    status["Portfolio Manager"] = COMPLETED if final else (IN_PROGRESS if risk_started else PENDING)
    return status


def _text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, dict):
        return str(content.get("text", "")).strip()
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")).strip())
            elif isinstance(item, str):
                parts.append(item.strip())
        return " ".join(p for p in parts if p)
    return str(content).strip()


def _role(message: Any) -> str:
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    if isinstance(message, ToolMessage):
        return "data"
    if isinstance(message, AIMessage):
        return "agent"
    if isinstance(message, HumanMessage):
        return "user"
    return "system"


def _clip(text: str, limit: int = MESSAGE_LIMIT) -> str:
    return text if len(text) <= limit else text[:limit] + " …"


class ProgressTracker:
    """Folds the stream of one run into events, emitting only what changed."""

    def __init__(self, analysts: list[str]):
        self.analysts = analysts
        self.state: dict = {}
        self._seen_messages: set = set()
        self._statuses: dict[str, str] = {}
        self._sections: dict[str, str] = {}

    def start(self) -> list[tuple[str, dict]]:
        """The analysts start together, so they are in progress from the first moment."""
        return self._status_events()

    def feed(self, messages: list, chunk: dict | None) -> list[tuple[str, dict]]:
        events: list[tuple[str, dict]] = []
        for message in messages:
            key = getattr(message, "id", None) or (type(message).__name__, str(getattr(message, "content", "")))
            if key in self._seen_messages:
                continue
            self._seen_messages.add(key)
            text = _text(getattr(message, "content", None))
            role = _role(message)
            if text and not (role == "user" and text == "Continue"):
                events.append(("message", {"role": role, "text": _clip(text)}))
            for call in getattr(message, "tool_calls", None) or []:
                name = call["name"] if isinstance(call, dict) else call.name
                args = call["args"] if isinstance(call, dict) else call.args
                events.append(("tool_call", {"name": name, "args": _clip(str(args), 300)}))
        if chunk:
            self.state.update(chunk)
            events.extend(self._status_events())
            for key, content in sections_from_state(self.state).items():
                if self._sections.get(key) != content:
                    self._sections[key] = content
                    events.append(("section", {"key": key, "content": content}))
        return events

    def _status_events(self) -> list[tuple[str, dict]]:
        statuses = statuses_from_state(self.state, self.analysts)
        if statuses == self._statuses:
            return []
        self._statuses = statuses
        return [("status", {"agents": statuses})]

    def finish(self) -> list[tuple[str, dict]]:
        """Every agent completed, as the CLI shows a finished run."""
        statuses = dict.fromkeys(statuses_from_state(self.state, self.analysts), COMPLETED)
        if statuses == self._statuses:
            return []
        self._statuses = statuses
        return [("status", {"agents": statuses})]


class RunView:
    """What the run page shows, rebuilt from the job's stored events."""

    def __init__(self, analysts: list[str]):
        self.analysts = analysts
        self.agents: dict[str, str] = statuses_from_state({}, analysts)
        self.sections: dict[str, str] = {}
        self.feed: list[dict] = []
        self.stats: dict = {}
        self.last_seq = 0
        self.finished: dict | None = None

    def apply(self, event: dict) -> None:
        kind, data = event["type"], event["data"]
        self.last_seq = max(self.last_seq, event["seq"])
        if kind == "status":
            self.agents = data["agents"]
        elif kind == "section":
            self.sections[data["key"]] = data["content"]
        elif kind == "stats":
            self.stats = data
        elif kind in ("message", "tool_call", "log", "error", "cancelled"):
            self.feed.append({"type": kind, "ts": event["ts"], **data})
            del self.feed[:-FEED_LIMIT]
        if kind in ("done", "error", "cancelled"):
            self.finished = {"type": kind, **data}

    @classmethod
    def from_events(cls, analysts: list[str], events: list[dict]) -> RunView:
        view = cls(analysts)
        for event in events:
            view.apply(event)
        return view
