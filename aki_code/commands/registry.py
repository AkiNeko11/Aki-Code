from __future__ import annotations

from aki_code.commands.base import BaseCommand, CommandContext, CommandResult


class CommandRegistry:
    def __init__(self) -> None:
        self._lookup: dict[str, BaseCommand] = {}
        self._order: list[BaseCommand] = []

    def register(self, command: BaseCommand) -> None:
        self._lookup[command.name] = command
        for alias in command.aliases:
            self._lookup[alias] = command
        self._order.append(command)

    def list_commands(self) -> list[BaseCommand]:
        return list(self._order)

    def dispatch(self, raw: str, ctx: CommandContext) -> CommandResult:
        text = raw.strip().lstrip("/")
        name, _, args = text.partition(" ")
        name = name.strip()
        args = args.strip()

        if not name:
            name = "help"

        cmd = self._lookup.get(name)
        if cmd is None:
            ctx.console.print(
                f"[red]Unknown command:[/red] /{name} — type /help for the list."
            )
            return CommandResult()
        return cmd.execute(ctx, args)
