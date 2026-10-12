from rich.console import Console

from aki_code import config
from aki_code.llm.client import get_client, call_with_retry
from aki_code.llm.prompts import SYSTEM_PROMPT
from aki_code.llm.usage import TokenTracker
from aki_code.tools import create_registry
from aki_code.commands import create_command_registry
from aki_code.commands.base import CommandAction, CommandContext
from aki_code.security.permission import PermissionManager
from aki_code.hooks.manager import HookManager, HookEvent
from aki_code.hooks.builtin import register_builtin_hooks
from aki_code.session import SessionSettings

console = Console()


def _request_permission(tool_name: str, tool_input: dict, reason: str) -> bool:
    console.print(f"  [yellow]⚠ {reason}[/yellow]")
    console.print(f"  [dim]{tool_name}({tool_input})[/dim]")
    answer = console.input("  [yellow]Allow? (y/n): [/yellow]").strip().lower()
    return answer.startswith("y")


def _display_tool_result(result: str) -> None:
    preview = result if len(result) <= 500 else result[:500] + "..."
    console.print(f"  [dim]← {preview}[/dim]")


def _call_llm(client, messages, tools):
    """Call LLM with streaming. Prints text as it arrives, returns full response.

    The whole stream is retried on transient errors (rate limit, connection),
    which almost always surface before any text is emitted.
    """
    def _do_stream():
        has_text = False
        with client.messages.stream(
            model=config.MODEL,
            max_tokens=config.MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=messages,
            tools=tools,
        ) as stream:
            for text in stream.text_stream:
                console.print(text, end="", markup=False)
                has_text = True
            response = stream.get_final_message()
        if has_text:
            console.print()
        return response

    return call_with_retry(_do_stream)


def _read_input() -> str | None:
    """Read one line of input. Returns None on Ctrl+C / EOF (signals exit)."""
    try:
        return console.input("[bold blue]> [/bold blue]").strip()
    except (KeyboardInterrupt, EOFError):
        return None


def run() -> None:
    if not config.API_KEY:
        console.print("[bold red]Error:[/bold red] DEEPSEEK_API_KEY not set.")
        console.print("Copy .env.example to .env and fill in your API key.")
        raise SystemExit(1)

    client = get_client()
    registry = create_registry()
    command_registry = create_command_registry()
    permission_manager = PermissionManager()
    messages: list[dict] = []

    settings = SessionSettings()
    hook_manager = HookManager()
    register_builtin_hooks(hook_manager, settings)

    token_tracker = TokenTracker()
    hook_manager.register(HookEvent.POST_LLM_CALL, token_tracker.on_post_llm_call)

    registry.setup_all()

    console.print("[bold green]Aki-Code[/bold green] — coding assistant")
    console.print("[dim]/help for commands, /exit to quit[/dim]\n")

    while True:
        # A dangling user message means the previous turn failed after retries.
        # Offer to retry it instead of forcing the user to retype.
        retrying = bool(messages) and messages[-1]["role"] == "user"
        if retrying:
            console.print(
                "[dim]Last request failed. Press Enter to retry, "
                "or type a new message.[/dim]"
            )

        user_input = _read_input()
        if user_input is None:
            console.print("\n[dim]Bye![/dim]")
            break

        # Slash commands are intercepted before anything touches the conversation.
        if user_input.startswith("/"):
            ctx = CommandContext(
                console=console,
                messages=messages,
                token_tracker=token_tracker,
                commands=command_registry,
                settings=settings,
            )
            if command_registry.dispatch(user_input, ctx).action == CommandAction.EXIT:
                break
            continue

        if retrying:
            if user_input:
                messages[-1] = {"role": "user", "content": user_input}
        else:
            if not user_input:
                continue
            hook_manager.fire(HookEvent.ON_USER_INPUT, user_input=user_input)
            messages.append({"role": "user", "content": user_input})

        # Snapshot so a failed turn can be rolled back cleanly, keeping the
        # user message and all prior history intact (no dangling tool_use).
        snapshot = len(messages)

        try:
            while True:
                hook_manager.fire(HookEvent.PRE_LLM_CALL, messages=messages)
                response = _call_llm(client, messages, registry.get_schemas())
                hook_manager.fire(HookEvent.POST_LLM_CALL, response=response)

                messages.append({"role": "assistant", "content": response.content})

                tool_uses = [b for b in response.content if b.type == "tool_use"]

                if not tool_uses:
                    break

                tool_results = []
                for tool_use in tool_uses:
                    decision, reason = permission_manager.check(
                        tool_use.name, tool_use.input
                    )

                    if decision == "deny":
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": tool_use.id,
                            "content": f"Denied: {reason}",
                        })
                        continue

                    if decision == "ask":
                        if not _request_permission(tool_use.name, tool_use.input, reason):
                            tool_results.append({
                                "type": "tool_result",
                                "tool_use_id": tool_use.id,
                                "content": "User denied this action.",
                            })
                            continue

                    console.print(f"  [cyan]→ {tool_use.name}[/cyan]({tool_use.input})")
                    hook_manager.fire(
                        HookEvent.PRE_TOOL_USE,
                        tool_name=tool_use.name,
                        tool_input=tool_use.input,
                        tool_use_id=tool_use.id,
                    )
                    result = registry.execute(tool_use.name, **tool_use.input)
                    hook_manager.fire(
                        HookEvent.POST_TOOL_USE,
                        tool_name=tool_use.name,
                        tool_input=tool_use.input,
                        tool_use_id=tool_use.id,
                        result=result,
                    )
                    _display_tool_result(result)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tool_use.id,
                        "content": result,
                    })

                messages.append({"role": "user", "content": tool_results})

        except KeyboardInterrupt:
            del messages[snapshot:]
            console.print("\n[dim]Interrupted.[/dim]")
        except Exception as e:
            del messages[snapshot:]
            hook_manager.fire(HookEvent.ON_ERROR, error=str(e))
            console.print(f"\n[bold red]Error:[/bold red] {e}")

    registry.teardown_all()


def main() -> None:
    run()


if __name__ == "__main__":
    main()
