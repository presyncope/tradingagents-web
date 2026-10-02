"""The function a job's process starts in."""

from __future__ import annotations

import logging

from tradingagents_web.worker.runner import run_job


def main(job_id: int, db_path: str) -> None:
    logging.basicConfig(level=logging.INFO, format=f"[job {job_id}] %(levelname)s %(name)s: %(message)s")
    run_job(job_id, db_path)
