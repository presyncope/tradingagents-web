"""Reading the HTML forms into request models, and the values the forms show."""

from __future__ import annotations

from datetime import date, timedelta

from starlette.datastructures import FormData
from tradingagents.default_config import DEFAULT_CONFIG

from tradingagents_web.config import (
    ANALYST_LABELS,
    ANALYST_ORDER,
    LLM_KEYS,
    OUTPUT_LANGUAGES,
    RunDefaults,
    default_model,
    model_options,
    provider_table,
)

EFFORTS = {
    "openai_reasoning_effort": ["low", "medium", "high"],
    "anthropic_effort": ["low", "medium", "high"],
    "google_thinking_level": ["minimal", "high"],
}


def _text(form: FormData, key: str) -> str | None:
    value = form.get(key)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _int(form: FormData, key: str) -> int | None:
    value = _text(form, key)
    return int(value) if value and value.isdigit() else None


def llm_fields(form: FormData) -> dict:
    """The LLM part of any form; a typed custom model ID wins over the menu."""
    fields = {
        "llm_provider": _text(form, "llm_provider"),
        "deep_think_llm": _text(form, "deep_custom") or _text(form, "deep_think_llm"),
        "quick_think_llm": _text(form, "quick_custom") or _text(form, "quick_think_llm"),
        "backend_url": _text(form, "backend_url"),
        "output_language": _text(form, "language_custom") or _text(form, "output_language"),
        "max_debate_rounds": _int(form, "max_debate_rounds"),
        "max_risk_discuss_rounds": _int(form, "max_risk_discuss_rounds"),
    }
    for key in EFFORTS:
        fields[key] = _text(form, key)
    return fields


def analysts(form: FormData) -> list[str] | None:
    chosen = [a for a in form.getlist("analysts") if isinstance(a, str)]
    return chosen or None


def checked(form: FormData, key: str) -> bool:
    return form.get(key) in ("on", "true", "1", "yes")


def last_weekday() -> str:
    day = date.today()
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day.isoformat()


def form_values(defaults: RunDefaults, previous: dict | None = None) -> dict:
    """What a run form starts with: a previous run's choices, else the saved defaults."""
    source = previous or {}
    values = {}
    for key in LLM_KEYS:
        value = source.get(key)
        if value is None:
            value = getattr(defaults, key, None)
        if value is None and key not in ("backend_url", "deep_think_llm", "quick_think_llm"):
            value = DEFAULT_CONFIG.get(key)
        values[key] = value
    values["llm_provider"] = (values["llm_provider"] or "openai").lower()
    for mode in ("deep", "quick"):
        key = f"{mode}_think_llm"
        values[key] = values[key] or default_model(values["llm_provider"], mode)
    if not previous:
        # A saved default endpoint is shown; the provider's own default is not
        # pre-filled, so switching provider does not carry the old endpoint.
        values["backend_url"] = defaults.backend_url
    values["analysts"] = source.get("analysts") or defaults.analysts or list(ANALYST_ORDER)
    checkpoint = source.get("checkpoint")
    if checkpoint is None:
        checkpoint = defaults.checkpoint
    if checkpoint is None:
        checkpoint = bool(DEFAULT_CONFIG.get("checkpoint_enabled"))
    values["checkpoint"] = checkpoint
    values["use_portfolio"] = bool(source.get("portfolio"))
    return values


def choices() -> dict:
    """Menus every run form offers."""
    return {
        "providers": [(label, key) for label, key, _ in provider_table()],
        "analyst_choices": [(key, ANALYST_LABELS[key]) for key in ANALYST_ORDER],
        "languages": OUTPUT_LANGUAGES,
        "efforts": EFFORTS,
        "rounds": [1, 2, 3, 4, 5],
    }


def model_menu(provider: str, mode: str, selected: str | None) -> dict:
    """Options for one model select, the chosen model first when the catalog does not list it.

    Any model ID the provider serves is accepted (e.g. a Flash-Lite model as the
    deep model), so a saved choice outside the menu is shown as an entry of its own
    rather than hidden in the free-text box behind a different selection.
    """
    options = model_options(provider, mode)
    listed = {value for _, value in options}
    if selected and selected not in listed:
        options = [(f"{selected} (직접 지정)", selected), *options]
    if not selected and options:
        selected = options[0][1]
    return {"options": options, "selected": selected, "custom": "", "mode": mode}
