from __future__ import annotations

from aki_code.commands.base import (
    BaseCommand,
    CommandAction,
    CommandContext,
    CommandResult,
)


class HelpCommand(BaseCommand):
    name = "help"
    aliases = ("?",)
    description = "Show available commands"

    def execute(self, ctx: CommandContext, args: str) -> CommandResult:
        ctx.console.print("[bold]Commands:[/bold]")
        for cmd in ctx.commands.list_commands():
            label = f"/{cmd.name}"
            if cmd.aliases:
                label += " (" + ", ".join(f"/{a}" for a in cmd.aliases) + ")"
            ctx.console.print(f"  [cyan]{label:<24}[/cyan] [dim]{cmd.description}[/dim]")
        return CommandResult()


class ExitCommand(BaseCommand):
    name = "exit"
    aliases = ("quit",)
    description = "Exit Aki-Code"

    def execute(self, ctx: CommandContext, args: str) -> CommandResult:
        ctx.console.print("[dim]Bye![/dim]")
        return CommandResult(action=CommandAction.EXIT)


class UsageCommand(BaseCommand):
    name = "usage"
    aliases = ("tokens",)
    description = "Show cumulative token usage for this session"

    def execute(self, ctx: CommandContext, args: str) -> CommandResult:
        u = ctx.token_tracker.usage
        ctx.console.print("[bold]Session token usage:[/bold]")
        ctx.console.print(f"  input:         {u.input_tokens:>12,}")
        ctx.console.print(f"  output:        {u.output_tokens:>12,}")
        ctx.console.print(f"  cache read:    {u.cache_read_input_tokens:>12,}")
        ctx.console.print(f"  cache write:   {u.cache_creation_input_tokens:>12,}")
        ctx.console.print(f"  [bold]total:[/bold]        {u.total_tokens:>12,}")
        ctx.console.print(f"  [dim]LLM calls: {u.calls}[/dim]")
        return CommandResult()
