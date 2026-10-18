"""The synchronous Lead tool for registered, read-only workflows."""
from __future__ import annotations

import json

from aki_code.tools.base import BaseTool
from aki_code.workflow.model import WorkflowError


class WorkflowTool(BaseTool):
    name = "workflow"
    input_schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Registered workflow name"},
            "args": {"type": "object", "description": "Workflow-specific JSON arguments"},
            "resume_from_run_id": {"type": "string", "description": "Resume a saved run with identical inputs"},
        },
        "required": ["name"],
        "additionalProperties": False,
    }

    def __init__(self, runtime, registry):
        self.runtime = runtime
        catalog = [
            {"name": item["name"], "description": item["description"], "args_schema": item["args_schema"]}
            for item in registry.list()
        ]
        self.description = (
            "Run a trusted read-only workflow and wait for its final result. "
            "Steps may run concurrently inside the workflow. Supply args according to the "
            "selected workflow's args_schema. Registered workflows: "
            + json.dumps(catalog, ensure_ascii=False, separators=(",", ":"))
        )

    def execute(self, name, args=None, resume_from_run_id=None) -> str:
        try:
            result = self.runtime.run(name, args=args, resume_from_run_id=resume_from_run_id)
        except WorkflowError as exc:
            result = {"status": "error", "error": str(exc)}
        return json.dumps(result, ensure_ascii=False, allow_nan=False)
