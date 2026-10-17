from __future__ import annotations

import json
import os
import re
import threading
from collections import deque
from datetime import datetime
from pathlib import Path

from aki_code import config
from aki_code.scheduler.cronexpr import cron_matches, validate_cron
from aki_code.scheduler.model import CronJob

_ID_NUM = re.compile(r"^sched_(\d+)$")


class CronScheduler:
    """Registry of scheduled prompts (Lesson 12).

    Storage (Phase A): add/list/cancel plus atomic persistence of the durable
    jobs to a single JSON file. Runtime (Phase B): a scheduler thread polls the
    clock and enqueues due jobs; a caller-supplied processor delivers them on an
    agent turn guarded by an external agent_lock, so a scheduled turn never
    overlaps a user turn. Delivery is at-least-once and does not catch up on
    fire times missed while the process was down.
    """

    def __init__(
        self,
        store_path: str | Path = config.SCHEDULES_FILE,
        *,
        poll_interval: float = config.CRON_POLL_INTERVAL,
        processor_interval: float = config.CRON_PROCESSOR_INTERVAL,
    ):
        self.store_path = Path(store_path)
        self.jobs: dict[str, CronJob] = {}
        self._counter = 0
        self.poll_interval = poll_interval
        self.processor_interval = processor_interval
        # Runtime state (Phase B). cron_lock guards jobs + the queue; the queue
        # holds ids of jobs that are due but not yet delivered.
        self.cron_lock = threading.RLock()
        self.cron_queue: deque[str] = deque()
        self._stop_event = threading.Event()
        self._threads: list[threading.Thread] = []

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
        with self.cron_lock:
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
        with self.cron_lock:
            return [self.jobs[key] for key in sorted(self.jobs)]

    def cancel(self, job_id: str) -> bool:
        """Remove a job by id; True if it existed. Persists if it was durable."""
        with self.cron_lock:
            job = self.jobs.pop(str(job_id), None)
            if job is None:
                return False
            # Drop any not-yet-delivered enqueue so a cancelled job cannot fire.
            self.cron_queue = deque(
                jid for jid in self.cron_queue if jid != job.id
            )
            if job.durable:
                self.persist()
            return True

    # ── Persistence (durable jobs only) ─────────────────────────

    def persist(self) -> None:
        """Atomically write the durable jobs (temp file + os.replace).

        Callers that mutate jobs hold cron_lock; this is also safe to call
        directly because the lock is re-entrant.
        """
        with self.cron_lock:
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
        with self.cron_lock:
            self.jobs = {}
            for entry in raw:
                if not isinstance(entry, dict) or "id" not in entry:
                    continue
                job = CronJob.from_dict(entry)
                # A job that was due but never delivered stays pending; last_fired
                # is kept so the same minute is not re-fired immediately on
                # restart.
                self.jobs[job.id] = job
                match = _ID_NUM.match(job.id)
                if match:
                    self._counter = max(self._counter, int(match.group(1)))

    # ── Firing (scheduler thread) ───────────────────────────────

    def poll_due_jobs(self, moment: datetime) -> None:
        """Enqueue every job whose expression matches `moment` (once per minute)."""
        minute_marker = moment.strftime("%Y-%m-%d %H:%M")
        with self.cron_lock:
            for job in list(self.jobs.values()):
                if job.pending_delivery or job.last_fired == minute_marker:
                    continue
                if cron_matches(job.cron, moment):
                    self._enqueue_due_job(job, minute_marker)

    def _enqueue_due_job(self, job: CronJob, minute_marker: str) -> None:
        """Mark a job pending and queue it; roll back if persisting it fails.

        Called with cron_lock held. A durable job whose state cannot be written
        is restored and NOT queued, so the processor never picks up a job whose
        pending state would be lost on restart.
        """
        prev_pending = job.pending_delivery
        prev_fired = job.last_fired
        job.pending_delivery = True
        job.last_fired = minute_marker
        if job.durable:
            try:
                self.persist()
            except OSError:
                job.pending_delivery = prev_pending
                job.last_fired = prev_fired
                return
        self.cron_queue.append(job.id)

    # ── Delivery (processor thread) ─────────────────────────────

    def has_cron_queue(self) -> bool:
        with self.cron_lock:
            return bool(self.cron_queue)

    def consume_cron_queue(self) -> list[CronJob]:
        """Drain the queue into job objects, skipping any cancelled meanwhile."""
        with self.cron_lock:
            ids = list(self.cron_queue)
            self.cron_queue.clear()
            return [self.jobs[jid] for jid in ids if jid in self.jobs]

    def requeue(self, jobs: list[CronJob]) -> None:
        """Put undelivered jobs back for a later attempt (delivery failed)."""
        with self.cron_lock:
            for job in jobs:
                if job.id in self.jobs:
                    self.cron_queue.append(job.id)

    def finalize_delivery(self, jobs: list[CronJob]) -> None:
        """After a successful turn: delete one-shot jobs, re-arm recurring ones."""
        with self.cron_lock:
            touched_durable = False
            for job in jobs:
                current = self.jobs.get(job.id)
                if current is None:
                    continue
                if current.recurring:
                    current.pending_delivery = False
                    touched_durable = touched_durable or current.durable
                else:
                    del self.jobs[current.id]
                    touched_durable = touched_durable or current.durable
            if touched_durable:
                self.persist()

    # ── Threads ─────────────────────────────────────────────────

    def _scheduler_loop(self) -> None:
        while not self._stop_event.wait(self.poll_interval):
            try:
                self.poll_due_jobs(datetime.now())
            except Exception:
                # A bad clock read or match must not kill the scheduler thread.
                continue

    def _processor_loop(self, agent_lock, run_turn) -> None:
        while not self._stop_event.wait(self.processor_interval):
            if not self.has_cron_queue():
                continue
            # Never overlap a user turn: only run when the agent is idle.
            if not agent_lock.acquire(blocking=False):
                continue
            try:
                if self.has_cron_queue():
                    run_turn()
            except Exception:
                pass
            finally:
                agent_lock.release()

    def start(self, agent_lock, run_turn) -> None:
        """Launch the scheduler + processor daemon threads (CLI runtime only)."""
        self._stop_event.clear()
        scheduler_thread = threading.Thread(
            target=self._scheduler_loop, name="cron-scheduler", daemon=True
        )
        processor_thread = threading.Thread(
            target=self._processor_loop,
            args=(agent_lock, run_turn),
            name="cron-processor",
            daemon=True,
        )
        self._threads = [scheduler_thread, processor_thread]
        for thread in self._threads:
            thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        """Signal the threads to stop and join them."""
        self._stop_event.set()
        for thread in self._threads:
            thread.join(timeout=timeout)
        self._threads = []
