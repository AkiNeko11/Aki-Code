from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path

from aki_code.tasks.model import IN_PROGRESS
from aki_code.team.worktree import WorktreeError, resolve_task_cwd


@dataclass
class Lease:
    """One teammate's claim on a working directory for its current task."""

    owner: str
    task_id: str
    cwd: str


class Assignments:
    """The lease map behind teammate cwd routing (Lesson 13).

    When a teammate claims a task, assign() records the resolved directory its
    workspace tools run in. The lease serves two host-side needs: remove_worktree
    refuses to delete a checkout still leased this turn, and a restarted host
    rebuilds leases from the durable task owner+worktree bindings via
    assignment_cwd()/recover(). A lease is dropped when the teammate goes IDLE,
    never on complete_task itself, so tools keep the task cwd until the turn ends.
    """

    def __init__(self) -> None:
        self._leases: dict[str, Lease] = {}
        self._lock = threading.RLock()

    # ── live assignment ─────────────────────────────────────────

    def assign(self, owner: str, task, base_repo: str | Path) -> str:
        """Resolve the task's cwd and record owner's lease, replacing any prior.

        A missing or invalid worktree binding raises rather than silently
        falling back to the repository directory, so a lost checkout is surfaced.
        """
        cwd = resolve_task_cwd(task, base_repo)
        with self._lock:
            self._leases[owner] = Lease(owner=owner, task_id=task.id, cwd=cwd)
        return cwd

    def release(self, owner: str) -> None:
        """Drop owner's lease (called when a teammate returns to IDLE)."""
        with self._lock:
            self._leases.pop(owner, None)

    def lease(self, owner: str) -> Lease | None:
        with self._lock:
            return self._leases.get(owner)

    def owners_on(self, path: str | Path) -> list[str]:
        """Owners whose lease resolves to this directory (removal in-use check)."""
        target = str(Path(path).resolve())
        with self._lock:
            return sorted(
                owner
                for owner, lease in self._leases.items()
                if str(Path(lease.cwd).resolve()) == target
            )

    # ── restart recovery ────────────────────────────────────────

    def assignment_cwd(self, owner: str, *, task_store, base_repo: str | Path) -> str:
        """Recover owner's lease from their durable in_progress task.

        Replaces any stale local lease for the same owner (an owner that moved to
        a new task). Fails loudly if the owner has no in_progress task or the
        worktree binding is missing or invalid.
        """
        task = self._in_progress_task(task_store, owner)
        if task is None:
            raise WorktreeError(f"{owner} has no in_progress task to recover")
        return self.assign(owner, task, base_repo)

    def recover(self, *, task_store, base_repo: str | Path) -> list[Lease]:
        """Rebuild every in_progress lease after a host restart.

        Propagates WorktreeError on the first broken binding instead of skipping
        it, so a lost checkout is never silently ignored.
        """
        with self._lock:
            self._leases.clear()
        for task in task_store.list():
            if task.status == IN_PROGRESS and task.owner:
                self.assign(task.owner, task, base_repo)
        with self._lock:
            return list(self._leases.values())

    @staticmethod
    def _in_progress_task(task_store, owner: str):
        for task in task_store.list():
            if task.owner == owner and task.status == IN_PROGRESS:
                return task
        return None
