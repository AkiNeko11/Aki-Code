from __future__ import annotations

import json
import os
import re
import secrets
import threading
from contextlib import contextmanager
from pathlib import Path

from aki_code import config
from aki_code.tasks.model import COMPLETED, IN_PROGRESS, PENDING, Task

# ids are "task_" + 8 hex chars; validating the shape before it is ever used as
# a filename means a crafted id cannot escape the tasks directory.
_TASK_ID = re.compile(r"^task_[0-9a-f]{8}$")


class TaskStore:
    """Durable, cross-session task store (Lesson 10).

    One JSON file per task under tasks_dir. create() assigns a random id with an
    exclusive write (regenerating on the rare collision); save() writes
    atomically via a temp file + rename so a crash never leaves a half-written
    task. This is the durable counterpart to the in-session TodoWrite checklist.

    Lesson 13 adds concurrency: several teammates may scan and claim at once, so
    discovery + ownership change happen under task_store_lock() and only one
    claimant wins a given task. The lock is in-process for now; a cross-process
    file lock (so another Harness sharing the directory cannot win the same
    claim) is deferred to the worktree phase.
    """

    def __init__(self, tasks_dir: str | Path = config.TASKS_DIR):
        self.tasks_dir = Path(tasks_dir)
        self._lock = threading.RLock()

    @contextmanager
    def task_store_lock(self):
        """Serialize read-modify-write task operations across threads."""
        with self._lock:
            yield

    # ── ids and paths ───────────────────────────────────────────

    def _new_id(self) -> str:
        return "task_" + secrets.token_hex(4)

    def _task_path(self, task_id: str) -> Path:
        tid = str(task_id)
        if not _TASK_ID.match(tid):
            raise KeyError(f"invalid task id '{task_id}'")
        return self.tasks_dir / f"{tid}.json"

    # ── CRUD ────────────────────────────────────────────────────

    def create(self, subject: str, description: str = "") -> Task:
        """Create a pending task with no dependencies; return it (id assigned)."""
        subject = str(subject or "").strip()
        if not subject:
            raise ValueError("subject must be a non-empty string")
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        while True:
            task_id = self._new_id()
            path = self.tasks_dir / f"{task_id}.json"
            try:
                # O_EXCL fails if the id already exists -> regenerate.
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                continue
            task = Task(
                id=task_id,
                subject=subject,
                description=str(description or ""),
            )
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(task.to_dict(), handle, ensure_ascii=False, indent=2)
            return task

    def load(self, task_id: str) -> Task:
        path = self._task_path(task_id)
        if not path.is_file():
            raise KeyError(f"unknown task '{task_id}'")
        data = json.loads(path.read_text(encoding="utf-8"))
        return Task.from_dict(data)

    def save(self, task: Task) -> None:
        """Atomically persist a task (temp file + rename)."""
        path = self._task_path(task.id)
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{task.id}.json.tmp")
        tmp.write_text(
            json.dumps(task.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, path)

    def list(self) -> list[Task]:
        """All tasks sorted by id; malformed files are skipped."""
        if not self.tasks_dir.is_dir():
            return []
        tasks: list[Task] = []
        for path in sorted(self.tasks_dir.glob("task_*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                tasks.append(Task.from_dict(data))
            except (OSError, json.JSONDecodeError, KeyError, TypeError):
                continue
        return tasks

    def exists(self, task_id: str) -> bool:
        try:
            return self._task_path(task_id).is_file()
        except KeyError:
            return False

    # ── Dependencies ────────────────────────────────────────────

    def update_dependencies(self, task_id: str, addBlockedBy) -> Task:
        """Add blockedBy edges, validating the whole change before saving.

        Rules (course): the target and every dependency must exist; the target
        must still be pending and unclaimed; no self-dependency or cycle. Adding
        an edge that already exists is a safe no-op.
        """
        if isinstance(addBlockedBy, str):
            addBlockedBy = [addBlockedBy]
        deps = [str(x) for x in (addBlockedBy or [])]

        with self.task_store_lock():
            task = self.load(task_id)  # target must exist
            for dep_id in deps:
                if not self.exists(dep_id):
                    raise KeyError(f"unknown dependency '{dep_id}'")
            if task.status != PENDING:
                raise ValueError(
                    f"task {task_id} is {task.status}; dependencies can only be "
                    f"added while pending"
                )
            if task.owner is not None:
                raise ValueError(f"task {task_id} is already claimed by {task.owner}")
            if task_id in deps:
                raise ValueError(f"task {task_id} cannot depend on itself")
            if self._creates_cycle(task_id, deps):
                raise ValueError(
                    f"adding {deps} to {task_id} would create a dependency cycle"
                )

            merged = list(task.blockedBy)
            for dep_id in deps:
                if dep_id not in merged:
                    merged.append(dep_id)
            task.blockedBy = merged
            self.save(task)
            return task

    def _creates_cycle(self, task_id: str, new_deps: list[str]) -> bool:
        """True if making `task_id` depend on `new_deps` closes a loop.

        Follows blockedBy edges from each new dependency; reaching task_id means
        task_id must already finish before that dependency, so the new edge would
        create a cycle.
        """
        stack = list(new_deps)
        seen: set[str] = set()
        while stack:
            current = stack.pop()
            if current == task_id:
                return True
            if current in seen:
                continue
            seen.add(current)
            try:
                current_task = self.load(current)
            except KeyError:
                continue
            stack.extend(current_task.blockedBy)
        return False

    def incomplete_dependencies(self, task: Task) -> list[str]:
        """Dependency ids that are not completed (a missing file counts too)."""
        incomplete: list[str] = []
        for dep_id in task.blockedBy:
            try:
                dep = self.load(dep_id)
            except KeyError:
                incomplete.append(dep_id)
                continue
            if dep.status != COMPLETED:
                incomplete.append(dep_id)
        return incomplete

    def can_start(self, task_id: str) -> bool:
        """True when every dependency is completed."""
        return not self.incomplete_dependencies(self.load(task_id))

    # ── Lifecycle ───────────────────────────────────────────────

    def _owner_in_progress(self, owner: str) -> bool:
        """True if `owner` already holds an in_progress task (one at a time)."""
        return any(
            t.owner == owner and t.status == IN_PROGRESS for t in self.list()
        )

    def _validate_claim(self, task: Task, owner: str) -> str | None:
        """Return why `owner` cannot claim `task`, or None if it can."""
        if task.status != PENDING:
            return f"Task {task.id} is {task.status}, cannot claim"
        if task.owner is not None:
            return f"Task {task.id} is already claimed by {task.owner}"
        if self._owner_in_progress(owner):
            return f"Owner {owner} must complete its current task first"
        dependencies = self.incomplete_dependencies(task)
        if dependencies:
            return f"Blocked by: {', '.join(dependencies)}"
        return None

    def claim(self, task_id: str, owner: str = "agent") -> str:
        """pending -> in_progress, setting the owner; a message either way."""
        with self.task_store_lock():
            task = self.load(task_id)
            error = self._validate_claim(task, owner)
            if error:
                return error
            task.owner = owner
            task.status = IN_PROGRESS
            self.save(task)
            return f"Claimed {task_id} ({task.subject})"

    def scan_unclaimed_tasks(self) -> list[Task]:
        """Snapshot of tasks that are pending, unowned, and unblocked.

        Only a point-in-time candidate list: another teammate (or process) may
        be eyeing the same task, so the actual ownership change must happen
        inside claim_task / claim_next_task under the lock.
        """
        with self.task_store_lock():
            return [
                t
                for t in self.list()
                if t.status == PENDING and t.owner is None and self.can_start(t.id)
            ]

    def claim_next_task(self, owner: str) -> Task | None:
        """Atomically claim the first available task for `owner`; None if none.

        Discovery and the ownership change happen under one lock hold, so two
        teammates racing for the same candidate cannot both win it.
        """
        with self.task_store_lock():
            for task in self.scan_unclaimed_tasks():
                if self._validate_claim(task, owner) is not None:
                    continue
                task.owner = owner
                task.status = IN_PROGRESS
                self.save(task)
                return task
            return None

    def complete(self, task_id: str, owner: str = "agent") -> str:
        """in_progress -> completed; report downstream tasks this unblocked."""
        with self.task_store_lock():
            task = self.load(task_id)
            if task.status != IN_PROGRESS:
                return f"Task {task_id} is {task.status}, cannot complete"
            if task.owner != owner:
                return f"Task {task_id} is owned by {task.owner}, not {owner}"

            ready_before = {
                t.id
                for t in self.list()
                if t.status == PENDING and t.blockedBy and self.can_start(t.id)
            }
            task.status = COMPLETED
            self.save(task)
            unblocked = [
                t.subject
                for t in self.list()
                if t.status == PENDING
                and t.blockedBy
                and t.id not in ready_before
                and self.can_start(t.id)
            ]
            message = f"Completed {task_id} ({task.subject})"
            if unblocked:
                message += f"\nUnblocked: {', '.join(unblocked)}"
            return message

    def release(self, task_id: str, owner: str = "agent") -> str:
        """in_progress -> pending, clearing the owner (assignment released).

        Used when a teammate is shut down or errors mid-task: the unfinished task
        goes back to the board so another owner can claim it. The worktree binding
        is kept, so a re-claim resumes in the same checkout.
        """
        with self.task_store_lock():
            task = self.load(task_id)
            if task.status != IN_PROGRESS:
                return f"Task {task_id} is {task.status}, cannot release"
            if task.owner != owner:
                return f"Task {task_id} is owned by {task.owner}, not {owner}"
            task.status = PENDING
            task.owner = None
            self.save(task)
            return f"Released {task_id} ({task.subject})"
