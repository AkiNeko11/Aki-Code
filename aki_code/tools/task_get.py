from __future__ import annotations

import json

from aki_code.tools.base import BaseTool
from aki_code.tasks.store import TaskStore


class GetTaskTool(BaseTool):
    """Read one task's full record (Lesson 10).

    Returns the complete JSON — description, status, owner and dependencies —
    which is what an agent needs to resume a task across sessions, where the
    one-line list summary is not enough.
    """

    name = "get_task"
    description = (
        "Read the full details of a single task by id: description, status, "
        "owner, and blockedBy dependencies. Use this to resume work on a task, "
        "especially across sessions."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "The task id."},
        },
        "required": ["task_id"],
    }

    def __init__(self, task_store: TaskStore):
        self._store = task_store

    def execute(self, *, task_id: str) -> str:
        task = self._store.load(task_id)
        return json.dumps(task.to_dict(), indent=2, ensure_ascii=False)
