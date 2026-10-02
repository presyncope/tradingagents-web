"""Turn a web request into the config dict ``TradingAgentsGraph`` runs with.

Precedence, highest first: the request, the defaults saved on the settings
page, the ``TRADINGAGENTS_*`` environment (already folded into
``DEFAULT_CONFIG``), then the package's built-in defaults.

A request is resolved once, when it is submitted, and the resolved form is
what the job stores. Rebuilding the config from it gives the same dict every
time, which a checkpoint resume needs: the checkpoint key hashes the config,
the analyst selection and the portfolio.
"""

from __future__ import annotations

import copy
import os
from datetime import date

from pydantic import BaseModel, Field, field_validator
from tradingagents.dataflows.symbols import normalize_symbol, safe_ticker_component
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.llm_clients.model_catalog import MODEL_OPTIONS

# Analyst keys in the order the CLI runs them. The checkpoint signature lists the
# analysts as given, so the same selection must always reach the graph in this order.
ANALYST_ORDER = ["market", "social", "news", "fundamentals"]
ANALYST_LABELS = {
    "market": "Market (기술적 분석)",
    "social": "Sentiment (소셜 심리)",
    "news": "News (뉴스·거시)",
    "fundamentals": "Fundamentals (재무)",
}
STOCK_ONLY_ANALYSTS = {"fundamentals"}
CRYPTO_SUFFIXES = ("-USD", "-USDT", "-USDC", "-BTC", "-ETH")

OUTPUT_LANGUAGES = ["Korean", "English", "Japanese", "Chinese", "Spanish", "French", "German"]


def _ollama_url() -> str:
    return os.environ.get("OLLAMA_BASE_URL") or "http://localhost:11434/v1"


# (label, provider key, default endpoint). The same table the CLI's provider
# menu uses; region variants are listed as their own entries.
def provider_table() -> list[tuple[str, str, str | None]]:
    return [
        ("OpenAI", "openai", "https://api.openai.com/v1"),
        ("Anthropic", "anthropic", "https://api.anthropic.com/"),
        ("Google", "google", None),
        ("xAI", "xai", "https://api.x.ai/v1"),
        ("DeepSeek", "deepseek", "https://api.deepseek.com"),
        ("Qwen (international)", "qwen", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"),
        ("Qwen (China)", "qwen-cn", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
        ("GLM (Z.AI)", "glm", "https://api.z.ai/api/paas/v4/"),
        ("GLM (BigModel, China)", "glm-cn", "https://open.bigmodel.cn/api/paas/v4/"),
        ("MiniMax (global)", "minimax", "https://api.minimax.io/v1"),
        ("MiniMax (China)", "minimax-cn", "https://api.minimaxi.com/v1"),
        ("OpenRouter", "openrouter", "https://openrouter.ai/api/v1"),
        ("Mistral", "mistral", "https://api.mistral.ai/v1"),
        ("Kimi (Moonshot)", "kimi", "https://api.moonshot.ai/v1"),
        ("Groq", "groq", "https://api.groq.com/openai/v1"),
        ("NVIDIA NIM", "nvidia", "https://integrate.api.nvidia.com/v1"),
        ("Azure OpenAI", "azure", None),
        ("Amazon Bedrock", "bedrock", None),
        ("Ollama", "ollama", _ollama_url()),
        ("OpenAI-compatible (vLLM, LM Studio, ...)", "openai_compatible", None),
    ]


def provider_keys() -> list[str]:
    return [key for _, key, _ in provider_table()]


def provider_default_url(provider: str) -> str | None:
    return next((url for _, key, url in provider_table() if key == provider), None)


def model_options(provider: str, mode: str) -> list[tuple[str, str]]:
    """(label, model id) choices for a provider; empty when only a custom ID fits."""
    options = MODEL_OPTIONS.get(provider, {}).get(mode, [])
    return [(label, value) for label, value in options if value != "custom"]


def detect_asset_type(ticker: str) -> str:
    return "crypto" if ticker.endswith(CRYPTO_SUFFIXES) else "stock"


def normalize_ticker(raw: str) -> str:
    """The canonical Yahoo symbol, checked to be safe as a path component."""
    if not raw or not raw.strip():
        raise ValueError("티커를 입력하세요")
    return safe_ticker_component(normalize_symbol(raw.strip()))


def order_analysts(analysts: list[str], asset_type: str) -> list[str]:
    names = [{"sentiment": "social"}.get(a.strip().lower(), a.strip().lower()) for a in analysts]
    unknown = [a for a in names if a not in ANALYST_ORDER]
    if unknown:
        raise ValueError(f"알 수 없는 analyst: {', '.join(unknown)}")
    ordered = [a for a in ANALYST_ORDER if a in names]
    if asset_type == "crypto":
        ordered = [a for a in ordered if a not in STOCK_ONLY_ANALYSTS]
    if not ordered:
        raise ValueError("analyst를 하나 이상 선택하세요")
    return ordered


def check_trade_date(value: str) -> str:
    try:
        day = date.fromisoformat(value.strip())
    except (AttributeError, ValueError):
        raise ValueError(f"날짜 형식이 아닙니다: {value!r} (YYYY-MM-DD)") from None
    if day > date.today():
        raise ValueError(f"미래 날짜는 분석할 수 없습니다: {value}")
    return day.isoformat()


class LLMChoice(BaseModel):
    """Run settings that can come from the request or the saved defaults. None = not set."""

    llm_provider: str | None = None
    deep_think_llm: str | None = None
    quick_think_llm: str | None = None
    backend_url: str | None = None
    output_language: str | None = None
    max_debate_rounds: int | None = Field(default=None, ge=1, le=5)
    max_risk_discuss_rounds: int | None = Field(default=None, ge=1, le=5)
    openai_reasoning_effort: str | None = None
    anthropic_effort: str | None = None
    google_thinking_level: str | None = None

    @field_validator("*", mode="before")
    @classmethod
    def _blank_is_unset(cls, value):
        return None if isinstance(value, str) and not value.strip() else value


class RunDefaults(LLMChoice):
    """What the settings page saves; fills any field a request leaves unset."""

    analysts: list[str] | None = None
    checkpoint: bool | None = None


class AnalysisRequest(LLMChoice):
    ticker: str
    trade_date: str
    asset_type: str | None = None          # None = detect from the ticker
    analysts: list[str] | None = None
    use_portfolio: bool = False
    checkpoint: bool | None = None


class BacktestRequest(LLMChoice):
    tickers: list[str]
    start: str
    end: str
    every_n_days: int = Field(default=7, ge=1, le=365)
    analysts: list[str] | None = None
    use_portfolio: bool = False


LLM_KEYS = list(LLMChoice.model_fields)


def _pick(key: str, request: BaseModel, defaults: RunDefaults):
    value = getattr(request, key, None)
    if value is None:
        value = getattr(defaults, key, None)
    return value


def default_model(provider: str, mode: str) -> str | None:
    """The model a run uses when none is named: the configured one for the configured
    provider, else the first the catalog lists for this provider."""
    if provider == (DEFAULT_CONFIG.get("llm_provider") or "").lower():
        configured = DEFAULT_CONFIG.get(f"{mode}_think_llm")
        if configured:
            return configured
    options = model_options(provider, mode)
    return options[0][1] if options else None


def resolve_llm(request: LLMChoice, defaults: RunDefaults) -> dict:
    """Every LLM setting filled: request, then saved defaults, then DEFAULT_CONFIG."""
    resolved = {}
    for key in LLM_KEYS:
        value = _pick(key, request, defaults)
        if value is None and key not in ("backend_url", "deep_think_llm", "quick_think_llm"):
            value = DEFAULT_CONFIG.get(key)
        resolved[key] = value
    provider = (resolved["llm_provider"] or "openai").lower()
    if provider not in provider_keys():
        raise ValueError(f"지원하지 않는 LLM 제공자: {provider}")
    resolved["llm_provider"] = provider
    for mode in ("deep", "quick"):
        key = f"{mode}_think_llm"
        resolved[key] = resolved[key] or default_model(provider, mode)
    # The CLI's order: an explicit endpoint, then TRADINGAGENTS_LLM_BACKEND_URL,
    # then the provider's own default.
    resolved["backend_url"] = (
        resolved["backend_url"] or DEFAULT_CONFIG.get("backend_url") or provider_default_url(provider)
    )
    if provider == "openai_compatible" and not resolved["backend_url"]:
        raise ValueError("OpenAI-compatible 제공자는 endpoint URL이 필요합니다")
    for key in ("deep_think_llm", "quick_think_llm"):
        if not resolved[key]:
            raise ValueError(f"{key} 모델을 지정하세요")
    return resolved


def resolve_analysis(request: AnalysisRequest, defaults: RunDefaults,
                     portfolio: dict | None) -> dict:
    """The stored form of an analysis request; every value concrete."""
    ticker = normalize_ticker(request.ticker)
    asset_type = request.asset_type or detect_asset_type(ticker)
    if asset_type not in ("stock", "crypto"):
        raise ValueError(f"asset_type은 stock 또는 crypto입니다: {asset_type}")
    analysts = request.analysts or defaults.analysts or list(ANALYST_ORDER)
    checkpoint = request.checkpoint
    if checkpoint is None:
        checkpoint = defaults.checkpoint
    if checkpoint is None:
        checkpoint = bool(DEFAULT_CONFIG.get("checkpoint_enabled"))
    return {
        "ticker": ticker,
        "trade_date": check_trade_date(request.trade_date),
        "asset_type": asset_type,
        "analysts": order_analysts(analysts, asset_type),
        "checkpoint": bool(checkpoint),
        # The book as it was when the run was asked for; a resume reuses it.
        "portfolio": portfolio if request.use_portfolio else None,
        **resolve_llm(request, defaults),
    }


def resolve_backtest(request: BacktestRequest, defaults: RunDefaults, portfolio: dict | None,
                     max_cells: int) -> dict:
    from tradingagents.backtest import iter_grid

    tickers = list(dict.fromkeys(normalize_ticker(t) for t in request.tickers if t.strip()))
    if not tickers:
        raise ValueError("티커를 하나 이상 입력하세요")
    asset_types = {detect_asset_type(t) for t in tickers}
    if len(asset_types) > 1:
        raise ValueError("주식과 암호화폐는 한 백테스트에 섞을 수 없습니다")
    asset_type = asset_types.pop()
    start, end = check_trade_date(request.start), check_trade_date(request.end)
    if start > end:
        raise ValueError("시작일이 종료일보다 늦습니다")
    dates = iter_grid(start, end, every_n_days=request.every_n_days)
    cells = len(tickers) * len(dates)
    if cells > max_cells:
        raise ValueError(f"셀이 {cells}개입니다. 한 번에 최대 {max_cells}개까지 실행할 수 있습니다")
    analysts = request.analysts or defaults.analysts or list(ANALYST_ORDER)
    return {
        "tickers": tickers,
        "start": start,
        "end": end,
        "every_n_days": request.every_n_days,
        "dates": dates,
        "asset_type": asset_type,
        "analysts": order_analysts(analysts, asset_type),
        "portfolio": portfolio if request.use_portfolio else None,
        **resolve_llm(request, defaults),
    }


def build_config(resolved: dict) -> dict:
    """The graph config for a resolved request: DEFAULT_CONFIG with the run's choices."""
    config = copy.deepcopy(DEFAULT_CONFIG)
    for key in LLM_KEYS:
        config[key] = resolved[key]
    if "checkpoint" in resolved:
        config["checkpoint_enabled"] = resolved["checkpoint"]
    return config


def settle_config(defaults: RunDefaults) -> dict:
    """Config for settling decisions: the saved defaults' model, nothing else run-specific."""
    return build_config(resolve_llm(LLMChoice(), defaults))
