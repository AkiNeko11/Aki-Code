"""Set a goal and return its task to the normal host execution path."""
import json

from aki_code.commands.base import BaseCommand, CommandAction, CommandResult


class GoalCommand(BaseCommand):
    name = "goal"
    description = "Goal condition and immediate execution: /goal [CONDITION|clear]"

    def execute(self, ctx, args):
        goal = ctx.goal_controller
        if goal is None:
            ctx.console.print("Goals are not configured.", markup=False)
            return CommandResult()
        condition = args.strip()
        if not condition:
            ctx.console.print(json.dumps(goal.status(), ensure_ascii=False, indent=2), markup=False)
        elif condition.casefold() in {"clear", "stop", "off", "reset", "none", "cancel"}:
            goal.clear()
            ctx.console.print("Goal cleared. Running jobs retain their normal host lifecycle.", markup=False)
        else:
            try:
                return CommandResult(CommandAction.RUN_AGENT, goal.set(condition))
            except ValueError as exc:
                ctx.console.print(f"Goal error: {exc}", markup=False)
        return CommandResult()
