from __future__ import annotations

import json
import os
import re
import secrets
from pathlib import Path

from aki_code import config
from aki_code.tasks.model import Task

# ids are "task_" + 8 hex chars; validating the shape before it is ever used as
# a filename means a crafted id cannot escape the tasks directory.
_TASK_ID = re.compile(r"^task_[0-9a-f]{8}$")


class TaskStore:
    """Durable, cross-session task store (Lesson 10).

    One JSON file per task under tasks_dir. create() assigns a random id with an
    exclusive write (regenerating on the rare collision); save() writes
    atomically via a temp file + rename so a crash never leaves a half-written
    task. This is the durable counterpart to the in-session TodoWrite checklist.
    """

    def __init__(self, tasks_dir: str | Path = config.TASKS_DIR):
        self.tasks_dir = Path(tasks_dir)

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
