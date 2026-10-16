from __future__ import annotations

from aki_code.tools.base import BaseTool
from aki_code.tasks.store import TaskStore


class CreateTaskTool(BaseTool):
    """Create a node in the durable task graph (Lesson 10).

    Returns the runtime-generated id, which the model must capture to wire
    dependencies later via update_task (edges are added in a second phase,
    because sibling tool calls in one response cannot see each other's new ids).
    """

    name = "create_task"
    description = (
        "Create a new task in the durable, cross-session task store. Returns the "
        "generated task id — keep it, you need it to add dependencies "
        "(update_task) and to claim/complete the task later. New tasks start "
        "pending with no dependencies."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "subject": {
                "type": "string",
                "description": "A brief, imperative title for the task.",
            },
            "description": {
                "type": "string",
                "description": "Full requirements and context for the task.",
            },
        },
        "required": ["subject"],
    }

    def __init__(self, task_store: TaskStore):
        self._store = task_store

    def execute(self, *, subject: str, description: str = "") -> str:
        task = self._store.create(subject, description)
        return f"Created {task.id}: {task.subject}"
