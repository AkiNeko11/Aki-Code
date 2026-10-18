"""Inspect and synchronously run saved workflows from the CLI."""
from __future__ import annotations

import json

from aki_code.commands.base import BaseCommand, CommandContext, CommandResult
from aki_code.workflow.model import WorkflowError, WorkflowInputError


class WorkflowsCommand(BaseCommand):
    name = "workflows"
    description = "Saved workflows: list | runs | show RUN_ID | run NAME JSON | resume RUN_ID"

    def execute(self, ctx: CommandContext, args: str) -> CommandResult:
        runtime = ctx.workflow_runtime
        if runtime is None:
            ctx.console.print("Workflows are not configured.", markup=False)
            return CommandResult()
        action, _, rest = args.strip().partition(" ")
        try:
            if action in ("", "list"):
                value = runtime.registry.list()
            elif action == "runs" and not rest.strip():
                value = runtime.store.list_runs()
            elif action == "show" and rest.strip():
                value = runtime.store.get_run(rest.strip())
            elif action == "run" and rest.strip():
                name, _, raw = rest.strip().partition(" ")
                arguments = json.loads(raw) if raw.strip() else {}
                if not isinstance(arguments, dict):
                    raise WorkflowInputError("Workflow arguments must be a JSON object")
                ctx.console.print("Running workflow; waiting for all active steps to finish.", markup=False)
                value = runtime.run(name, arguments)
            elif action == "resume" and rest.strip():
                run_id = rest.strip()
                snapshot = runtime.store.get_run(run_id)
                identity = snapshot["identity"]
                ctx.console.print("Resuming workflow; input and workspace checks apply.", markup=False)
                value = runtime.run(
                    identity["workflow"]["name"], identity["args"],
                    resume_from_run_id=run_id,
                )
            else:
                raise WorkflowInputError("Usage: /workflows [list|runs|show RUN_ID|run NAME JSON|resume RUN_ID]")
            ctx.console.print(json.dumps(value, ensure_ascii=False, indent=2), markup=False)
        except (WorkflowError, ValueError, OSError, KeyError) as exc:
            ctx.console.print(f"Workflow error: {exc}", markup=False)
        except KeyboardInterrupt:
            runtime.cancel()
            ctx.console.print("Workflow interrupted; saved progress is available in /workflows runs.", markup=False)
        return CommandResult()
