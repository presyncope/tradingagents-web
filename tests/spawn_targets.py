"""Functions the job manager starts in a fresh process during tests.

A spawned process imports these by module name, so they live in a module of
their own; pytest's monkeypatching in the parent does not reach the child.
"""

from __future__ import annotations

import os
import time

from tradingagents_web.store.db import Store, now


def sleeper(job_id: int, db_path: str) -> None:
    """Hold the job running until killed, or for a long while."""
    store = Store(db_path, init=False)
    store.append_event(job_id, "log", {"text": "sleeping"})
    store.close()
    time.sleep(60)


def quick_success(job_id: int, db_path: str) -> None:
    store = Store(db_path, init=False)
    store.transition(job_id, ("running",), status="completed", finished_at=now())
    store.append_event(job_id, "done", {})
    store.close()


def crash(job_id: int, db_path: str) -> None:
    os._exit(3)


def offline_worker(job_id: int, db_path: str) -> None:
    """The real worker, with the scripted model and offline vendors of conftest."""
    import pytest
    from conftest import ScriptedModel, patch_offline
    from tradingagents.default_config import DEFAULT_CONFIG

    from tradingagents_web.worker.runner import run_job

    home = os.environ["TA_WEB_TEST_HOME"]
    patch = pytest.MonkeyPatch()
    patch.setitem(DEFAULT_CONFIG, "results_dir", f"{home}/logs")
    patch.setitem(DEFAULT_CONFIG, "data_cache_dir", f"{home}/cache")
    patch.setitem(DEFAULT_CONFIG, "memory_log_path", f"{home}/memory/log.md")
    delay = float(os.environ.get("TA_WEB_TEST_DELAY", "0"))
    patch_offline(patch, ScriptedModel(delay=delay))
    run_job(job_id, db_path)
