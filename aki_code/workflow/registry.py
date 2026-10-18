"""Host-owned workflow definitions; model input never becomes Python code."""
from __future__ import annotations

import inspect
import re
from copy import deepcopy

from aki_code.workflow.model import WorkflowDefinition, WorkflowInputError
from aki_code.workflow.schema import check_schema


class WorkflowRegistry:
    def __init__(self):
        self._definitions: dict[str, WorkflowDefinition] = {}

    def register(self, definition: WorkflowDefinition) -> None:
        if not isinstance(definition, WorkflowDefinition):
            raise WorkflowInputError("Expected a WorkflowDefinition")
        if not isinstance(definition.name, str) or not re.fullmatch(
            r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", definition.name
        ) or len(definition.name) > 64:
            raise WorkflowInputError("Workflow name must be a lowercase slug of 1-64 characters")
        if definition.name in self._definitions:
            raise WorkflowInputError(f"Workflow already registered: {definition.name}")
        for field in ("description", "version"):
            value = getattr(definition, field)
            if not isinstance(value, str) or not value.strip():
                raise WorkflowInputError(f"Workflow {field} must be a nonempty string")
        if not inspect.iscoroutinefunction(definition.script):
            raise WorkflowInputError("Workflow script must be an async function")
        if not isinstance(definition.phases, (tuple, list)) or not all(
            isinstance(phase, str) and phase.strip() for phase in definition.phases
        ):
            raise WorkflowInputError("Workflow phases must be nonempty strings")
        if definition.read_only is not True:
            raise WorkflowInputError("Only read-only workflows are supported in S16")
        if definition.args_schema is not None:
            check_schema(definition.args_schema)
        self._definitions[definition.name] = definition

    def get(self, name: str) -> WorkflowDefinition:
        if not isinstance(name, str) or name not in self._definitions:
            raise WorkflowInputError(f"Unknown workflow: {name!r}")
        return self._definitions[name]

    def list(self) -> list[dict]:
        return [
            {**definition.metadata(), "args_schema": deepcopy(definition.args_schema)}
            for definition in self._definitions.values()
        ]
