import signal
import threading
import queue
import time

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
from aki_code.tasks import TaskStore
from aki_code.scheduler import CronScheduler
from aki_code.team import MessageBus, TeamManager
from aki_code.mcp.manager import MCPManager
from aki_code.background import (
    BackgroundManager,
    inject_background_results,
    should_run_background,
)

console = Console()

# Sentinel distinguishing "no input available right now" from a real EOF (None)
# when polling the input queue in the dual-wait loop.
_NO_INPUT = object()


def _request_permission(tool_name: str, tool_input: dict, reason: str) -> bool:
    # ASCII marker: rich's legacy Windows console renders via the GBK codec,
    # which cannot encode symbols like U+26A0 and would raise mid-prompt.
    console.print(f"  [yellow]! {reason}[/yellow]")
    console.print(f"  [dim]{escape(f'{tool_name}({tool_input})')}[/dim]")
    answer = console.input("  [yellow]Allow? (y/n): [/yellow]").strip().lower()
    return answer.startswith("y")


def _deny_permission(tool_name: str, tool_input: dict, reason: str) -> bool:
    """Non-interactive stand-in: a scheduled turn cannot prompt on stdin.

    Used when the agent loop runs on the cron processor thread, where reading
    the terminal would collide with the main REPL. Anything needing approval is
    refused rather than blocking.
    """
    return False


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

    # Turn-end cleanup rides the STOP hook (Lesson 15): when the lead's turn
    # finishes, extract durable memories and consolidate once the store has
    # grown. Best-effort and disk-only — the hook swallows its own errors so a
    # failure never disturbs the finished turn, and the loop stays feature-agnostic.
    def _memory_stop_hook(ctx):
        msgs = ctx.data.get("messages")
        if not msgs:
            return
        try:
            if memory_store.extract_memories(msgs):
                memory_store.maybe_consolidate()
        except Exception as exc:
            console.print(
                f"  [dim]memory extraction skipped: {escape(str(exc))}[/dim]"
            )

    hook_manager.register(HookEvent.STOP, _memory_stop_hook)

    task_store = TaskStore(config.TASKS_DIR)
    background_manager = BackgroundManager()
    scheduler = CronScheduler(config.SCHEDULES_FILE)
    try:
        scheduler.load()
    except ValueError as exc:
        # Report a corrupt schedule file loudly rather than silently starting
        # with no jobs (the course requires the error to surface).
        console.print(f"[bold red]Schedule load error:[/bold red] {exc}")
    bus = MessageBus(config.MAILBOX_DIR)
    # Each teammate builds its own cwd-bound registry, so TeamManager only needs
    # the skill loader (not the lead registry) — no circular wiring.
    team_manager = TeamManager(
        bus=bus,
        task_store=task_store,
        client=client,
        skill_loader=skill_loader,
        permission_manager=permission_manager,
        hook_manager=hook_manager,
        console=console,
    )
    # MCP servers are connected on demand by the model via connect_mcp; the
    # manager holds the connected clients and registers their tools into the
    # lead registry (Lesson 14). Mock servers only — no real transport yet.
    mcp_manager = MCPManager()
    registry = create_registry(
        client=client,
        permission_manager=permission_manager,
        hook_manager=hook_manager,
        todo_manager=todo_manager,
        skill_loader=skill_loader,
        task_store=task_store,
        scheduler=scheduler,
        team_manager=team_manager,
        mcp_manager=mcp_manager,
        ask_permission=_request_permission,
        console=console,
    )
    command_registry = create_command_registry()
    compactor = Compactor(client, todo_manager=todo_manager, console=console)
    messages: list[dict] = []

    # Serializes agent turns: the main REPL holds it while reading input is NOT
    # happening, and the cron processor acquires it (non-blocking) so a scheduled
    # turn never overlaps a user turn on the shared `messages`/console.
    agent_lock = threading.Lock()
    stop_event = threading.Event()

    # On SIGTERM, stop the scheduler and any background process groups before
    # leaving. Best effort: registration can be unavailable on some platforms,
    # and Ctrl+C is already handled by the KeyboardInterrupt paths below.
    def _terminate(signum, frame):
        stop_event.set()
        team_manager.shutdown_all(timeout=1.0)
        background_manager.teardown_all()
        raise SystemExit(0)

    try:
        signal.signal(signal.SIGTERM, _terminate)
    except (ValueError, OSError, AttributeError):
        pass

    registry.setup_all()

    console.print("[bold green]Aki-Code[/bold green] — coding assistant")
    console.print("[dim]/help for commands, /exit to quit[/dim]\n")

    # Consecutive tool rounds without a todo_write; at 3 we nudge the model.
    rounds_since_todo = 0

    def run_agent_turn(active_request: str, interactive: bool) -> bool:
        """Run one full agent turn (model + tool rounds) over the shared messages.

        Shared by the main REPL and the cron processor thread; the caller must
        hold agent_lock. Returns True if the turn completed, False if it was
        interrupted or errored (after rolling back to the pre-call snapshot).
        `interactive` picks the permission callback: a scheduled turn runs on
        another thread and cannot prompt on stdin, so it auto-denies anything
        that would need approval.
        """
        nonlocal rounds_since_todo
        ask = _request_permission if interactive else _deny_permission

        # Recall memories relevant to this request and rebuild the system prompt
        # (skill catalog is static; only the memory section changes per turn).
        recalled = memory_store.load_memories(
            memory_store.select_relevant(active_request)
        )
        system_prompt = build_system_prompt(
            skill_catalog, memory_store.render(recalled), mcp_manager.prompt_state()
        )

        # active_request is restated in every compaction summary so the model
        # never loses sight of the goal after history is trimmed.
        # reactive_retries bounds consecutive reactive compactions this turn, so
        # a context that cannot be shrunk enough errors out instead of looping.
        reactive_retries = 0

        while True:
            # Surface any background jobs that finished since the last turn.
            inject_background_results(messages, background_manager)
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
                    # Turn complete: fire STOP so turn-end cleanup (durable memory
                    # extraction/consolidation) runs off the loop. The hook is
                    # best-effort and swallows its own errors, so a failure here
                    # never disturbs the finished turn.
                    hook_manager.fire(HookEvent.STOP, messages=messages)
                    return True

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
                        if not ask(tool_use.name, tool_use.input, reason):
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
                    # A long-running bash command flagged run_in_background starts
                    # on a worker thread and returns a placeholder immediately; its
                    # result is injected as a notification on a later turn.
                    if should_run_background(tool_use.name, tool_use.input):
                        bg_id = background_manager.start(
                            tool_use.input.get("command", "")
                        )
                        result = f"[Background task {bg_id} started]"
                    else:
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
                return False
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
                return False

    def run_scheduled_turn() -> None:
        """Deliver due cron jobs as one agent turn (called under agent_lock).

        The fired prompts become a single [Scheduled] user message. On failure
        the conversation is restored and the jobs requeued for a later attempt
        (at-least-once delivery); on success one-shot jobs are removed and
        recurring jobs are re-armed.
        """
        fired = scheduler.consume_cron_queue()
        if not fired:
            return
        saved = list(messages)
        content = "\n".join(f"[Scheduled] {job.prompt}" for job in fired)
        block = {"type": "text", "text": content}
        # Merge into a trailing user message when there is one (e.g. a dangling
        # request left by a failed turn) so roles keep alternating; otherwise
        # start a fresh user turn. Replace rather than mutate so `saved` restores
        # cleanly on failure.
        if messages and messages[-1].get("role") == "user":
            prev = messages[-1].get("content")
            merged = (
                prev + [block]
                if isinstance(prev, list)
                else [{"type": "text", "text": prev}, block]
            )
            messages[-1] = {"role": "user", "content": merged}
        else:
            messages.append({"role": "user", "content": [block]})

        console.print(f"  [magenta][cron] {len(fired)} scheduled task(s) firing[/magenta]")
        if run_agent_turn(content, interactive=False):
            scheduler.finalize_delivery(fired)
        else:
            messages[:] = saved
            scheduler.requeue(fired)

    def run_team_turn() -> bool:
        """Deliver pending lead-inbox events as one interactive Lead turn.

        Returns True if a turn ran. Events are drained from the mailbox and
        injected as a single [Team events] user message; on a model failure the
        message stays dangling so the normal retry path offers it again.
        """
        events = team_manager.consume_lead_inbox()
        if not events:
            return False
        text = team_manager.format_team_events(events)
        block = {"type": "text", "text": text}
        if messages and messages[-1].get("role") == "user":
            prev = messages[-1].get("content")
            merged = (
                prev + [block]
                if isinstance(prev, list)
                else [{"type": "text", "text": prev}, block]
            )
            messages[-1] = {"role": "user", "content": merged}
        else:
            messages.append({"role": "user", "content": [block]})
        console.print(f"  [magenta][team] {len(events)} team event(s)[/magenta]")
        run_agent_turn(text, interactive=True)
        return True

    # Start the cron runtime only for the live CLI; importing this module must
    # not spawn threads.
    scheduler.start(agent_lock, run_scheduled_turn)

    # Terminal input runs on its own thread so the loop can also wake on team
    # events without the user pressing enter. A gate keeps exactly one prompt
    # outstanding, so the reader never reads ahead or stacks prompts.
    input_queue: "queue.Queue" = queue.Queue()
    want_input = threading.Event()

    def _input_reader() -> None:
        while not stop_event.is_set():
            want_input.wait()
            if stop_event.is_set():
                break
            want_input.clear()
            input_queue.put(_read_input())  # None signals EOF / exit

    reader = threading.Thread(target=_input_reader, name="input-reader", daemon=True)
    reader.start()
    want_input.set()  # allow the first prompt

    POLL_SECONDS = 0.1
    while True:
        # 1) User input has priority over team events.
        try:
            user_input = input_queue.get_nowait()
        except queue.Empty:
            user_input = _NO_INPUT

        if user_input is not _NO_INPUT:
            if user_input is None:
                console.print("\n[dim]Bye![/dim]")
                break

            exit_requested = False
            with agent_lock:
                # Slash commands are intercepted before anything touches the convo.
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
                        task_store=task_store,
                        mcp_manager=mcp_manager,
                    )
                    if command_registry.dispatch(user_input, ctx).action == CommandAction.EXIT:
                        exit_requested = True
                else:
                    retrying = bool(messages) and messages[-1]["role"] == "user"
                    if retrying:
                        if user_input:
                            messages[-1] = {"role": "user", "content": user_input}
                            active_request = user_input
                        else:
                            prev = messages[-1].get("content")
                            active_request = prev if isinstance(prev, str) else ""
                        run_agent_turn(active_request, interactive=True)
                    elif user_input:
                        hook_manager.fire(HookEvent.ON_USER_INPUT, user_input=user_input)
                        messages.append({"role": "user", "content": user_input})
                        run_agent_turn(user_input, interactive=True)
            if exit_requested:
                break

            # Re-arm the reader: show the retry hint (if any) then the prompt.
            if bool(messages) and messages[-1]["role"] == "user":
                console.print(
                    "[dim]Last request failed. Press Enter to retry, "
                    "or type a new message.[/dim]"
                )
            want_input.set()
            continue

        # 2) No input pending: deliver team events, waking a Lead turn without
        #    the user having to type anything.
        if team_manager.has_lead_mail():
            with agent_lock:
                ran = run_team_turn()
            if ran:
                continue

        # 3) Idle: briefly wait, then re-check both sources.
        time.sleep(POLL_SECONDS)

    stop_event.set()
    want_input.set()  # release the reader if it is parked on the gate
    team_manager.shutdown_all()
    scheduler.stop()
    background_manager.teardown_all()
    registry.teardown_all()


def main() -> None:
    run()


if __name__ == "__main__":
    main()
