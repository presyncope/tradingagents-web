"""Web UI for the TradingAgents multi-agent analysis framework.

The environment is loaded here, on first import of the package, because
``tradingagents.default_config`` reads ``TRADINGAGENTS_*`` variables when it is
imported. Python runs this file before any module of the package, so no
``tradingagents`` import made from here can run before ``.env`` is applied.
"""

from tradingagents_web.env import load_env

__version__ = "0.1.0"

load_env()
