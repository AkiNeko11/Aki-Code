from __future__ import annotations

import json
import os
import re
from pathlib import Path

from aki_code import config
from aki_code.scheduler.cronexpr import validate_cron
from aki_code.scheduler.model import CronJob

_ID_NUM = re.compile(r"^sched_(\d+)$")


class CronScheduler:
    """Registry of scheduled prompts (Lesson 12).

    Phase A covers storage: add/list/cancel plus atomic persistence of the
    durable jobs to a single JSON file. The scheduler/queue threads that
    actually fire jobs and drive delivery are added in Phase B.
    """

    def __init__(self, store_path: str | Path = config.SCHEDULES_FILE):
        self.store_path = Path(store_path)
        self.jobs: dict[str, CronJob] = {}
        self._counter = 0

    # ── ids ─────────────────────────────────────────────────────

    def _next_id(self) -> str:
        self._counter += 1
        return f"sched_{self._counter:04d}"

    # ── CRUD ────────────────────────────────────────────────────

    def add(
        self,
        cron: str,
        prompt: str,
        recurring: bool = True,
        durable: bool = True,
    ) -> CronJob:
        """Validate and register a job; persist immediately if durable."""
        validate_cron(cron)  # raises ValueError on a bad expression
        prompt = str(prompt or "").strip()
        if not prompt:
            raise ValueError("prompt must be a non-empty string")
        job = CronJob(
            id=self._next_id(),
            cron=" ".join(str(cron).split()),
            prompt=prompt,
            recurring=bool(recurring),
            durable=bool(durable),
        )
        self.jobs[job.id] = job
        if job.durable:
            self.persist()
        return job

    def list(self) -> list[CronJob]:
        return [self.jobs[key] for key in sorted(self.jobs)]

    def cancel(self, job_id: str) -> bool:
        """Remove a job by id; True if it existed. Persists if it was durable."""
        job = self.jobs.pop(str(job_id), None)
        if job is None:
            return False
        if job.durable:
            self.persist()
        return True

    # ── Persistence (durable jobs only) ─────────────────────────

    def persist(self) -> None:
        """Atomically write the durable jobs (temp file + os.replace)."""
        data = [job.to_dict() for job in self.jobs.values() if job.durable]
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.store_path.with_name(self.store_path.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.store_path)

    def load(self) -> None:
        """Restore durable jobs from disk. Raises on a corrupt file (the caller
        reports it rather than silently ignoring it)."""
        if not self.store_path.is_file():
            return
        try:
            raw = json.loads(self.store_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(
                f"could not read {self.store_path.name}: {exc}"
            ) from exc
        if not isinstance(raw, list):
            raise ValueError(f"{self.store_path.name} must contain a JSON array")
        self.jobs = {}
        for entry in raw:
            if not isinstance(entry, dict) or "id" not in entry:
                continue
            job = CronJob.from_dict(entry)
            # A job that was due but never delivered stays pending; last_fired is
            # kept so the same minute is not re-fired immediately on restart.
            self.jobs[job.id] = job
            match = _ID_NUM.match(job.id)
            if match:
                self._counter = max(self._counter, int(match.group(1)))
