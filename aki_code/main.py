from rich.console import Console
from rich.markup import escape

from aki_code import config
from aki_code.llm.client import get_client, call_with_retry
from aki_code.llm.prompts import build_system_prompt
from aki_code.llm.usage import TokenTracker
from aki_code.tools import create_registry
from aki_code.commands import create_command_registry
from aki_code.commands.base import CommandAction, CommandContext
from aki_code.security.permission import PermissionManager
from aki_code.hooks.manager import HookManager, HookEvent
from aki_code.hooks.builtin import register_builtin_hooks
from aki_code.session import SessionSettings
from aki_code.skills.loader import SkillLoader
from aki_code.todo.manager import TodoManager
from aki_code.compact import Compactor, is_prompt_too_long
from aki_code.memory import MemoryStore

console = Console()


def _request_permission(tool_name: str, tool_input: dict, reason: str) -> bool:
    console.print(f"  [yellow]⚠ {reason}[/yellow]")
    console.print(f"  [dim]{escape(f'{tool_name}({tool_input})')}[/dim]")
    answer = console.input("  [yellow]Allow? (y/n): [/yellow]").strip().lower()
    return answer.startswith("y")


def _display_tool_result(result: str) -> None:
    preview = result if len(result) <= 500 else result[:500] + "..."
    console.print(f"  [dim]← {escape(preview)}[/dim]")


def _call_llm(client, messages, tools, system):
    """Call LLM with streaming. Prints text as it arrives, returns full response.

    The whole stream is retried on transient errors (rate limit, connection),
    which almost always surface before any text is emitted.
    """
    def _do_stream():
        has_text = False
        with client.messages.stream(
            model=config.MODEL,
            max_tokens=config.MAX_TOKENS,
            system=system,
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
    permission_manager = PermissionManager()

    settings = SessionSettings()
    hook_manager = HookManager()
    register_builtin_hooks(hook_manager, settings)

    token_tracker = TokenTracker()
    hook_manager.register(HookEvent.POST_LLM_CALL, token_tracker.on_post_llm_call)

    # The task tool needs a fully-built registry, permissions and hooks, so it
    # is wired last — create_registry passes this same registry into TaskTool.
    todo_manager = TodoManager()
    skill_loader = SkillLoader(config.SKILLS_DIR)
    skill_loader.scan()
    # The skill catalog is static for the session; the memory section is
    # recalled per turn, so the full system prompt is rebuilt each request.
    skill_catalog = skill_loader.catalog()
    memory_store = MemoryStore(client, memory_dir=config.MEMORY_DIR)
    system_prompt = build_system_prompt(skill_catalog)
    registry = create_registry(
        client=client,
        permission_manager=permission_manager,
        hook_manager=hook_manager,
        todo_manager=todo_manager,
        skill_loader=skill_loader,
        ask_permission=_request_permission,
        console=console,
    )
    command_registry = create_command_registry()
    compactor = Compactor(client, todo_manager=todo_manager, console=console)
    messages: list[dict] = []

    registry.setup_all()

    console.print("[bold green]Aki-Code[/bold green] — coding assistant")
    console.print("[dim]/help for commands, /exit to quit[/dim]\n")

    # Consecutive tool rounds without a todo_write; at 3 we nudge the model.
    rounds_since_todo = 0

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
                permission_manager=permission_manager,
                todo_manager=todo_manager,
                skill_loader=skill_loader,
                compactor=compactor,
                memory_store=memory_store,
            )
            if command_registry.dispatch(user_input, ctx).action == CommandAction.EXIT:
                break
            continue

        if retrying:
            if user_input:
                messages[-1] = {"role": "user", "content": user_input}
                active_request = user_input
            else:
                prev = messages[-1].get("content")
                active_request = prev if isinstance(prev, str) else ""
        else:
            if not user_input:
                continue
            hook_manager.fire(HookEvent.ON_USER_INPUT, user_input=user_input)
            messages.append({"role": "user", "content": user_input})
            active_request = user_input

        # Recall memories relevant to this request and rebuild the system prompt
        # (skill catalog is static; only the memory section changes per turn).
        recalled = memory_store.load_memories(
            memory_store.select_relevant(active_request)
        )
        system_prompt = build_system_prompt(skill_catalog, memory_store.render(recalled))

        # active_request is restated in every compaction summary so the model
        # never loses sight of the goal after history is trimmed.
        # reactive_retries bounds consecutive reactive compactions this turn, so
        # a context that cannot be shrunk enough errors out instead of looping.
        reactive_retries = 0

        while True:
            # Keep the conversation within budget before every model call.
            messages[:] = compactor.prepare(messages, active_request)
            # Snapshot the clean pre-call state so a mid-round failure (e.g. a
            # tool raising after the assistant turn was appended) rolls back
            # without leaving a dangling tool_use.
            snapshot = len(messages)

            try:
                hook_manager.fire(HookEvent.PRE_LLM_CALL, messages=messages)
                response = _call_llm(
                    client, messages, registry.get_schemas(), system_prompt
                )
                reactive_retries = 0
                hook_manager.fire(HookEvent.POST_LLM_CALL, response=response)

                messages.append({"role": "assistant", "content": response.content})

                tool_uses = [b for b in response.content if b.type == "tool_use"]

                if not tool_uses:
                    break

                tool_results = []
                compact_requested = False
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

                    console.print(
                        f"  [cyan]→ {tool_use.name}[/cyan]"
                        f"{escape(f'({tool_use.input})')}"
                    )
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
                    # The compact tool only signals intent; the real compaction
                    # runs once the whole batch is appended, below.
                    if tool_use.name == "compact":
                        compact_requested = True
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tool_use.id,
                        "content": result,
                    })

                used_todo = any(t.name == "todo_write" for t in tool_uses)
                rounds_since_todo = 0 if used_todo else rounds_since_todo + 1
                if rounds_since_todo >= 3:
                    tool_results.append({
                        "type": "text",
                        "text": "<reminder>Update your todos.</reminder>",
                    })
                    rounds_since_todo = 0

                messages.append({"role": "user", "content": tool_results})

                # Batch is closed: collapse history into a summary now.
                if compact_requested:
                    messages[:] = compactor.compact_history(messages, active_request)

            except KeyboardInterrupt:
                del messages[snapshot:]
                console.print("\n[dim]Interrupted.[/dim]")
                break
            except Exception as e:
                if (
                    is_prompt_too_long(e)
                    and reactive_retries < compactor.max_reactive_retries
                ):
                    messages[:] = compactor.reactive_compact(messages, active_request)
                    reactive_retries += 1
                    continue
                del messages[snapshot:]
                hook_manager.fire(HookEvent.ON_ERROR, error=str(e))
                console.print(f"\n[bold red]Error:[/bold red] {e}")
                break

    registry.teardown_all()


def main() -> None:
    run()


if __name__ == "__main__":
    main()
