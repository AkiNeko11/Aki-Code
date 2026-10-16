from __future__ import annotations

from aki_code.tools.base import BaseTool
from aki_code.tasks.store import TaskStore


class CompleteTaskTool(BaseTool):
    """Complete a claimed task: in_progress -> completed (Lesson 10).

    Only the owner may complete it. Afterwards the store scans for downstream
    tasks that just became unblocked and reports them, so the agent knows what
    is ready next.
    """

    name = "complete_task"
    description = (
        "Mark an in_progress task as completed. Only its owner can complete it. "
        "The result also lists any downstream tasks this unblocked (their "
        "dependencies are now all completed)."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "The task to complete."},
            "owner": {
                "type": "string",
                "description": "Must match the task's owner (defaults to 'agent').",
            },
        },
        "required": ["task_id"],
    }

    def __init__(self, task_store: TaskStore):
        self._store = task_store

    def execute(self, *, task_id: str, owner: str = "agent") -> str:
        return self._store.complete(task_id, owner)
