from __future__ import annotations

from aki_code.tools.base import BaseTool
from aki_code.scheduler.scheduler import CronScheduler


class ScheduleCronTool(BaseTool):
    """Register a prompt to fire on a cron schedule (Lesson 12).

    The expression is validated before the job is saved. When it fires (Phase B
    runtime), the prompt is delivered to the agent as a "[Scheduled] ..." user
    message on a turn of its own.
    """

    name = "schedule_cron"
    description = (
        "Schedule a prompt to run on a cron schedule (5 fields: minute hour "
        "day-of-month month day-of-week; supports * , */N , N , N-M , N,M). "
        "When the time matches, the prompt is handed to the agent as a new "
        "request. recurring=true fires every match; recurring=false fires once "
        "then is removed. durable=true survives a restart."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "cron": {
                "type": "string",
                "description": "Cron expression, e.g. '0 9 * * *' or '*/5 * * * *'.",
            },
            "prompt": {
                "type": "string",
                "description": "The instruction to give the agent when it fires.",
            },
            "recurring": {
                "type": "boolean",
                "description": "Repeat on every match (default true) or fire once.",
            },
            "durable": {
                "type": "boolean",
                "description": "Persist across restarts (default true).",
            },
        },
        "required": ["cron", "prompt"],
    }

    def __init__(self, scheduler: CronScheduler):
        self._scheduler = scheduler

    def execute(
        self,
        *,
        cron: str,
        prompt: str,
        recurring: bool = True,
        durable: bool = True,
    ) -> str:
        job = self._scheduler.add(cron, prompt, recurring=recurring, durable=durable)
        kind = "recurring" if job.recurring else "one-shot"
        return f"Scheduled {job.id} ({kind}, {'durable' if job.durable else 'in-memory'}): [{job.cron}] {job.prompt}"
