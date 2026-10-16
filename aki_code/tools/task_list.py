from __future__ import annotations

from aki_code.tools.base import BaseTool
from aki_code.tasks.store import TaskStore


class ListTasksTool(BaseTool):
    """List every task as a one-line summary (Lesson 10).

    Cheap overview for choosing what to work on; get_task returns the full
    record for a single id.
    """

    name = "list_tasks"
    description = (
        "List all tasks in the store as one-line summaries (id, subject, status, "
        "owner, blockedBy). Use get_task for a single task's full details."
    )
    input_schema = {
        "type": "object",
        "properties": {},
    }

    def __init__(self, task_store: TaskStore):
        self._store = task_store

    def execute(self, **_) -> str:
        tasks = self._store.list()
        if not tasks:
            return "(no tasks)"
        return "\n".join(t.one_line() for t in tasks)
