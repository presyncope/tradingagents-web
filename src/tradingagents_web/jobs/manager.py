"""Start queued jobs in their own processes, reap them, cancel them.

Rules the scheduler keeps:

- At most ``max_workers`` jobs run at once.
- Two jobs that share a ticker never run at once, whatever their kind or date.
  A ticker has one checkpoint database for all its dates, and every analysis
  settles that ticker's pending memory-log entries before it starts; a backtest
  shares the cache directory with live runs. A job for a busy ticker waits.
- A job that ends without recording an outcome (killed, crashed) is failed by
  the manager, since the worker could not do it.

The manager is a per-process singleton: run the server with one worker process.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import multiprocessing
import os
import threading
from collections.abc import Callable
from pathlib import Path

from tradingagents_web import schedules
from tradingagents_web.store.db import Store, now
from tradingagents_web.worker.entry import main as worker_main

logger = logging.getLogger(__name__)


def _alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _resumable(job: dict) -> bool:
    if job["kind"] == "backtest":
        return True
    return job["kind"] == "analysis" and bool(job["request"].get("checkpoint"))


class JobManager:
    def __init__(self, db_path: str | Path, max_workers: int = 2,
                 target: Callable[[int, str], None] = worker_main, poll_interval: float = 1.0):
        self.db_path = str(db_path)
        self.max_workers = max_workers
        self.target = target
        self.poll_interval = poll_interval
        self.store = Store(db_path)
        self._lock = threading.Lock()
        self._procs: dict[int, multiprocessing.process.BaseProcess] = {}
        self._ctx = multiprocessing.get_context("spawn")
        self._task: asyncio.Task | None = None

    # --- lifecycle --------------------------------------------------------

    def recover(self) -> None:
        """After a restart: a job whose process is gone did not finish."""
        with self._lock:
            for job in self.store.running_jobs():
                if job["id"] not in self._procs and not _alive(job["pid"]):
                    self._fail(job, "서버가 재시작되어 작업이 중단되었습니다")

    async def start(self) -> None:
        self.recover()
        self._task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self.tick)
            except Exception:
                logger.exception("Scheduler pass failed")
            await asyncio.sleep(self.poll_interval)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        with self._lock:
            for job_id, proc in list(self._procs.items()):
                job = self.store.get_job(job_id)
                self._kill(proc)
                if job and job["status"] == "running":
                    self._fail(job, "서버가 종료되어 작업이 중단되었습니다")
            self._procs.clear()
        self.store.close()

    # --- scheduling -------------------------------------------------------

    def tick(self) -> None:
        with self._lock:
            self._reap()
            self._fire_schedules()
            self._schedule()

    def _fire_schedules(self) -> None:
        try:
            queued = schedules.fire_due(self.store)
        except Exception:
            logger.exception("Firing schedules failed")
            return
        if queued:
            logger.info("Schedules queued jobs %s", queued)

    def _reap(self) -> None:
        for job_id, proc in list(self._procs.items()):
            if proc.is_alive():
                continue
            proc.join()
            del self._procs[job_id]
            job = self.store.get_job(job_id)
            if job and job["status"] == "running":
                self._fail(job, f"작업 프로세스가 결과 없이 종료되었습니다 (exit code {proc.exitcode})")
        for job in self.store.running_jobs():
            if job["id"] not in self._procs and not _alive(job["pid"]):
                self._fail(job, "작업 프로세스를 찾을 수 없습니다")

    def _schedule(self) -> None:
        running = self.store.running_jobs()
        busy = {t for job in running for t in job["tickers"]}
        slots = self.max_workers - len(running)
        for job in self.store.queued_jobs():
            if slots <= 0:
                break
            tickers = set(job["tickers"])
            if tickers & busy:
                continue
            if not self.store.transition(job["id"], ("queued",), status="running", started_at=now()):
                continue
            proc = self._ctx.Process(target=self.target, args=(job["id"], self.db_path),
                                     name=f"tradingagents-job-{job['id']}", daemon=False)
            proc.start()
            self._procs[job["id"]] = proc
            self.store.update_job(job["id"], pid=proc.pid)
            busy |= tickers
            slots -= 1

    def _fail(self, job: dict, message: str) -> None:
        if self.store.transition(job["id"], ("running",), status="failed", error=message,
                                 resumable=_resumable(job), finished_at=now()):
            self.store.append_event(job["id"], "error", {"message": message})

    @staticmethod
    def _kill(proc) -> None:
        proc.terminate()
        proc.join(5)
        if proc.is_alive():
            proc.kill()
            proc.join(5)

    # --- requests ---------------------------------------------------------

    def cancel(self, job_id: int) -> bool:
        """Stop a queued or running job; False when it had already finished."""
        with self._lock:
            job = self.store.get_job(job_id)
            if job is None:
                return False
            if job["status"] == "queued":
                if self.store.transition(job_id, ("queued",), status="cancelled", finished_at=now()):
                    self.store.append_event(job_id, "cancelled", {"message": "실행 전에 취소했습니다"})
                    return True
                return False
            if job["status"] != "running":
                return False
            # Recorded before the kill: the worker's own finish is guarded on
            # "running", so it cannot overwrite the cancel if it races us.
            resumable = _resumable(job)
            self.store.transition(job_id, ("running",), status="cancelled", finished_at=now(),
                                  resumable=resumable)
            note = "취소했습니다." + (" 체크포인트에서 재개할 수 있습니다." if resumable else "")
            self.store.append_event(job_id, "cancelled", {"message": note})
            proc = self._procs.pop(job_id, None)
            if proc is not None:
                self._kill(proc)
            elif _alive(job["pid"]):
                with contextlib.suppress(ProcessLookupError):
                    os.kill(job["pid"], 15)
            return True

    def wake(self) -> None:
        """Schedule now rather than at the next pass, e.g. right after a submit."""
        self.tick()
