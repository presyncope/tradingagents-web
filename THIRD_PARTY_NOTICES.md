# Third-party notices

This repository includes or adapts the following works. Their license texts are in `LICENSES/`.

| Work | Where | License |
| --- | --- | --- |
| [htmx](https://htmx.org) 2.0.4 | `src/tradingagents_web/static/htmx.min.js` (unmodified) | 0BSD, `LICENSES/0BSD-htmx.txt` |
| [htmx-ext-sse](https://github.com/bigskysoftware/htmx-extensions) 2.2.2 | `src/tradingagents_web/static/htmx-ext-sse.js` (unmodified) | 0BSD, `LICENSES/0BSD-htmx-ext-sse.txt` |
| [Pico CSS](https://picocss.com) 2.0.6 | `src/tradingagents_web/static/pico.min.css` (unmodified) | MIT, `LICENSES/MIT-pico.txt` |
| [TradingAgents](https://github.com/TauricResearch/TradingAgents) v0.5.2, Copyright Tauric Research | see below | Apache-2.0, `LICENSES/Apache-2.0-TradingAgents.txt` |

Parts adapted from TradingAgents (modified for this project):

- `src/tradingagents_web/worker/stats.py`: copied from `cli/stats_handler.py`, reformatted.
- `src/tradingagents_web/config.py`: the provider and endpoint table follows `cli/prompts.py`.
- `src/tradingagents_web/progress.py`: agent status rules follow the CLI's live view in `cli/display.py` and `cli/run.py`.
- `tests/conftest.py`: the scripted model and offline patches follow `tests/test_graph_end_to_end.py`.

TradingAgents itself is a dependency installed from its repository, not included here.
