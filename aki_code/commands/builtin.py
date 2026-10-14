from __future__ import annotations

from aki_code.commands.base import (
    BaseCommand,
    CommandAction,
    CommandContext,
    CommandResult,
)
from aki_code.security.permission import PermissionMode


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


class HooksCommand(BaseCommand):
    name = "hooks"
    aliases = ()
    description = "Toggle hook output: /hooks on|off|status"

    def execute(self, ctx: CommandContext, args: str) -> CommandResult:
        arg = args.strip().lower()
        settings = ctx.settings

        if arg in ("on", "enable"):
            settings.show_hooks = True
            ctx.console.print("[dim]Hook output: ON[/dim]")
        elif arg in ("off", "disable"):
            settings.show_hooks = False
            ctx.console.print("[dim]Hook output: OFF[/dim]")
        elif arg in ("status", ""):
            state = "ON" if settings.show_hooks else "OFF"
            ctx.console.print(
                f"[dim]Hook output: {state}. Usage: /hooks on|off|status[/dim]"
            )
        else:
            ctx.console.print("[red]Usage:[/red] /hooks on|off|status")
        return CommandResult()


class ModeCommand(BaseCommand):
    name = "mode"
    aliases = ("permission", "permissions")
    description = "View/set permission mode: /mode (default|accept-edits)"

    def execute(self, ctx: CommandContext, args: str) -> CommandResult:
        pm = ctx.permission_manager
        arg = args.strip().lower()

        if not arg:
            ctx.console.print(
                f"[dim]Permission mode: {pm.mode.value}. "
                "Usage: /mode (default|accept-edits)[/dim]"
            )
            return CommandResult()

        try:
            new_mode = PermissionMode(arg)
        except ValueError:
            valid = " | ".join(m.value for m in PermissionMode)
            ctx.console.print(f"[red]Unknown mode:[/red] {arg}. Valid: {valid}")
            return CommandResult()

        pm.mode = new_mode
        ctx.console.print(f"[dim]Permission mode: {new_mode.value}[/dim]")
        if new_mode == PermissionMode.ACCEPT_EDITS:
            ctx.console.print(
                "[yellow]File edits inside the workspace are now auto-accepted.[/yellow]"
            )
        return CommandResult()


