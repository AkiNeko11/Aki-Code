from __future__ import annotations

from aki_code.agent.subagent import run_subagent
from aki_code.llm.prompts import build_subagent_prompt
from aki_code.tools.base import BaseTool


class TaskTool(BaseTool):
    """Delegate a self-contained task to a nested subagent (Lesson 06).

    The subagent runs its own agent loop with a fresh message list and returns
    only its final text, keeping the parent's conversation clean.
    """

    name = "task"
    description = (
        "Launch a subagent to handle a self-contained task. The subagent works "
        "in an isolated conversation and returns only a concise final answer. "
        "Use it for multi-step exploration or work whose intermediate output "
        "would clutter the main conversation."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": (
                    "A complete, self-contained description of the task. The "
                    "subagent cannot see the parent conversation, so include all "
                    "necessary context."
                ),
            },
        },
        "required": ["prompt"],
    }

    def __init__(
        self,
        *,
        client,
        registry,
        permission_manager,
        hook_manager,
        ask_permission,
        console=None,
        workspace=None,
    ):
        self._client = client
        self._registry = registry
        self._permission_manager = permission_manager
        self._hook_manager = hook_manager
        self._ask_permission = ask_permission
        self._console = console
        self._workspace = workspace

    def execute(self, *, prompt: str) -> str:
        system = build_subagent_prompt(self._workspace)
        policy_prompt = getattr(self._permission_manager, "prompt_section", None)
        if policy_prompt is not None:
            system += policy_prompt()
        return run_subagent(
            prompt=prompt,
            client=self._client,
            registry=self._registry,
            permission_manager=self._permission_manager,
            hook_manager=self._hook_manager,
            ask_permission=self._ask_permission,
            system=system,
            console=self._console,
        )
