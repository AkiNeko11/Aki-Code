import signal
import threading
import queue
import time
from contextlib import nullcontext
from pathlib import Path

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
from aki_code.compact import Compactor
from aki_code.memory import MemoryStore
from aki_code.tasks import TaskStore
from aki_code.scheduler import CronScheduler
from aki_code.team import MessageBus, TeamManager
from aki_code.mcp.manager import MCPManager
from aki_code.agent.harness import AgentHarness
from aki_code.goal import GoalController, PromptGoalEvaluator
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

    # The lead runs on its own HookManager so its permission and turn-end hooks
    # never leak onto subagents/teammates: those still share `hook_manager`
    # (observability + token tracking only) and keep their inline permission until
    # they move to their own harnesses. Both managers feed the same TokenTracker,
    # so usage still aggregates across the whole session.
    lead_hooks = HookManager()
    register_builtin_hooks(lead_hooks, settings)
    lead_hooks.register(HookEvent.POST_LLM_CALL, token_tracker.on_post_llm_call)

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

    lead_hooks.register(HookEvent.STOP, _memory_stop_hook)

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
    # MCP servers are connected on demand by the model via connect_mcp; the
    # manager holds the connected clients and registers their tools into the
    # lead registry (Lesson 14). Mock servers only — no real transport yet. Built
    # before the team so teammates can reach the connected MCP tools too (L15).
    mcp_manager = MCPManager()
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
        mcp_manager=mcp_manager,
    )

    def _goal_event(event):
        state = event["goal"]
        console.print(f"  [dim]goal {escape(state['status'])}: {escape(state['reason'][:350])}[/dim]")

    def _evaluator_usage(response):
        token_tracker.add_from_response(response)
        goal.on_evaluator_response(response)

    goal = GoalController(
        PromptGoalEvaluator(client, config.GOAL_EVALUATOR_MODEL,
                            max_chars=config.GOAL_EVALUATOR_MAX_CHARS,
                            timeout=config.GOAL_EVALUATOR_TIMEOUT,
                            on_response=_evaluator_usage),
        max_stop_blocks=config.MAX_STOP_BLOCKS, max_turns=config.MAX_TURNS,
        pending_work=lambda: (background_manager.has_pending(goal.current_id)
                              or team_manager.has_pending_tasks(goal.state.task_ids)),
        event_sink=_goal_event,
        history_chars=max(2000, config.GOAL_EVALUATOR_MAX_CHARS * 2),
    )
    # These hooks belong only to the lead; subagents/teammates keep their own
    # lifecycle, and workflow steps continue to use the observability-only fork.
    lead_hooks.register(HookEvent.PRE_LLM_CALL, goal.before_model)
    lead_hooks.register(HookEvent.POST_LLM_CALL, goal.on_model_response)
    lead_hooks.register(HookEvent.MESSAGE_APPENDED, goal.record_message)
    lead_hooks.register(HookEvent.BEFORE_STOP, goal.stop_hook)
    lead_hooks.register(HookEvent.TURN_STOPPED, goal.on_turn_stopped)
    # Workflow steps use fresh harnesses and a restricted repository tool set.
    # The outer call waits synchronously; the runtime bounds internal parallelism.
    from aki_code.workflow.examples import register_builtin_workflows
    from aki_code.workflow.model import RunLimits
    from aki_code.workflow.registry import WorkflowRegistry
    from aki_code.workflow.runner import HarnessAgentRunner
    from aki_code.workflow.runtime import WorkflowRuntime
    from aki_code.workflow.store import WorkflowStore

    workflows = WorkflowRegistry()
    register_builtin_workflows(workflows)

    def _workflow_event(event):
        kind = event.get("progress_type", event.get("type", "progress"))
        detail = (event.get("label") or event.get("phase") or event.get("message")
                  or event.get("status") or event.get("name") or "")
        # Intermediate agent conversations stay out of the lead's context;
        # the outer call returns the final result and execution status.
        console.print(f"  [dim]workflow {escape(str(kind))}: {escape(str(detail)[:250])}[/dim]")

    workflow_runtime = WorkflowRuntime(
        workflows,
        HarnessAgentRunner(
            client=client, permission_manager=permission_manager,
            hook_manager=hook_manager, workspace=Path.cwd(), allow_reads=True,
        ),
        WorkflowStore(config.WORKFLOWS_DIR),
        limits=RunLimits(
            max_concurrency=config.WORKFLOW_MAX_CONCURRENCY,
            max_calls=config.WORKFLOW_MAX_CALLS,
            max_turns=config.WORKFLOW_MAX_TURNS,
            max_tokens=config.WORKFLOW_MAX_TOKENS,
            request_timeout=config.WORKFLOW_REQUEST_TIMEOUT,
        ),
        event_sink=_workflow_event,
    )
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
        workflow_runtime=workflow_runtime,
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
        workflow_runtime.cancel()
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

    # The lead's agent loop, extracted into the shared harness (Lesson 15). The
    # closures below inject the lead-specific pieces the harness is agnostic to:
    # streaming model calls, the dynamic registry tool pool, background-job
    # routing, per-turn memory recall in the system prompt, and compaction.
    def _lead_dispatch(tool_use):
        # A long-running bash command flagged run_in_background starts on a worker
        # thread and returns a placeholder immediately; its result is injected as
        # a notification on a later turn.
        if should_run_background(tool_use.name, tool_use.input):
            bg_id = background_manager.start(tool_use.input.get("command", ""), goal_id=goal.current_id)
            return f"[Background task {bg_id} started]"
        before = {task.id for task in task_store.list()} if goal.current_id and tool_use.name == "create_task" else set()
        result = registry.execute(tool_use.name, **tool_use.input)
        if goal.current_id:
            if tool_use.name == "create_task":
                goal.state.task_ids.update(task.id for task in task_store.list() if task.id not in before)
            if tool_use.name in {"spawn_teammate", "send_message", "request_plan", "request_shutdown"}:
                name = tool_use.input.get("name")
                runtime = team_manager.teammates.get(name)
                if runtime is not None:
                    task = runtime.current_task
                    if task is not None:
                        goal.state.task_ids.add(task.id)
        return result

    def _lead_system_prompt(active_request: str) -> str:
        # Recall memories relevant to this request and rebuild the system prompt
        # (skill catalog is static; the memory and MCP sections change per turn).
        recalled = memory_store.load_memories(
            memory_store.select_relevant(active_request)
        )
        return build_system_prompt(
            skill_catalog, memory_store.render(recalled), mcp_manager.prompt_state()
        ) + goal.prompt_section()

    def _background_observed(record):
        if goal.current_id and record.get("goal_id") == goal.current_id:
            goal.observe_message({"role": "user", "content": record["notification"]})

    def _pre_model(msgs):
        # Cron/unrelated deliveries must not consume a deferred goal's results.
        if not goal.suppressed:
            inject_background_results(msgs, background_manager, on_result=_background_observed)

    def _append_user(text, *, record_goal=True):
        block = {"type": "text", "text": text}
        if messages and messages[-1].get("role") == "user":
            previous = messages[-1]["content"]
            blocks = previous + [block] if isinstance(previous, list) else [{"type": "text", "text": previous}, block]
            messages[-1] = {"role": "user", "content": blocks}
        else:
            messages.append({"role": "user", "content": text})
        if record_goal:
            lead_hooks.fire(HookEvent.MESSAGE_APPENDED, message={"role": "user", "content": text})

    def _lead_tool_start(tool_use) -> None:
        console.print(
            f"  [cyan]→ {tool_use.name}[/cyan]{escape(f'({tool_use.input})')}"
        )

    harness = AgentHarness(
        hook_manager=lead_hooks,
        schema_provider=registry.get_schemas,
        dispatch=_lead_dispatch,
        llm_caller=lambda msgs, tools, system: _call_llm(client, msgs, tools, system),
        permission_manager=permission_manager,
        system_prompt_provider=_lead_system_prompt,
        compactor=compactor,
        pre_model=_pre_model,
        on_tool_start=_lead_tool_start,
        on_tool_result=_display_tool_result,
        on_interrupt=lambda: console.print("\n[dim]Interrupted.[/dim]"),
        on_error=lambda e: console.print(f"\n[bold red]Error:[/bold red] {e}"),
        todo_nudge=True,
        max_turns=config.MAX_TURNS,
    )

    def run_agent_turn(active_request: str, interactive: bool, *, apply_goal=True):
        """Run one full agent turn over the shared messages via the harness.

        Shared by the main REPL and the cron/team turn runners; the caller must
        hold agent_lock. Returns a TurnResult, separating execution errors from
        goal completion, deferral and controlled stops. Cron runs outside Goal.
        `interactive` picks the permission callback: a scheduled turn runs on
        another thread and cannot prompt on stdin, so it auto-denies anything
        that would need approval.
        """
        ask = _request_permission if interactive else _deny_permission
        with (nullcontext() if apply_goal else goal.suspend()):
            result = harness.run_turn(messages, active_request, ask=ask)
        if result.status == "limit" and not goal.active:
            console.print(result.reason, markup=False)
        return result


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
        if run_agent_turn(content, interactive=False, apply_goal=False).ok:
            scheduler.finalize_delivery(fired)
        else:
            messages[:] = saved
            scheduler.requeue(fired)

    def run_team_turn() -> bool:
        """Deliver pending lead-inbox events without competing for terminal input.

        Returns True if events were received. A suspended goal records results
        without automatically restarting; unrelated events do not drive its gate.
        """
        events = team_manager.consume_lead_inbox()
        if not events:
            return False
        text = team_manager.format_team_events(events)
        _append_user(text, record_goal=False)
        related = []
        if goal.active:
            related = [event for event in events
                       if (event.get("metadata") or {}).get("task_id") in goal.state.task_ids]
            if related:
                goal.observe_message({"role": "user", "content": team_manager.format_team_events(related)})
        console.print(f"  [magenta][team] {len(events)} team event(s)[/magenta]")
        if not goal.active or goal.automatic_allowed and related:
            # The stdin reader may already be waiting on a prompt.
            run_agent_turn(text, interactive=False, apply_goal=bool(related))
        return True

    def run_background_turn() -> bool:
        records = background_manager.collect_records()
        if not records:
            return False
        text = "\n\n".join(record["notification"] for record in records)
        _append_user(text, record_goal=False)
        related = False
        for record in records:
            if goal.current_id and record.get("goal_id") == goal.current_id:
                related = True
                _background_observed(record)
        console.print(f"  [magenta][background] {len(records)} task result(s) received[/magenta]")
        if related and goal.automatic_allowed:
            run_agent_turn(text, interactive=False)
        elif not goal.active and all(record.get("goal_id") is None for record in records):
            run_agent_turn(text, interactive=False, apply_goal=False)
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
                        workflow_runtime=workflow_runtime,
                        goal_controller=goal,
                    )
                    command = command_registry.dispatch(user_input, ctx)
                    if command.action == CommandAction.EXIT:
                        exit_requested = True
                    elif command.action == CommandAction.RUN_AGENT:
                        hook_manager.fire(HookEvent.ON_USER_INPUT, user_input=command.prompt)
                        _append_user(command.prompt)
                        run_agent_turn(command.prompt, interactive=True)
                else:
                    retrying = bool(messages) and messages[-1]["role"] == "user"
                    if user_input or goal.active:
                        goal.resume_for_user()
                        prompt = user_input or "Continue working toward the active goal using the available results."
                        hook_manager.fire(HookEvent.ON_USER_INPUT, user_input=user_input)
                        _append_user(prompt)
                        run_agent_turn(prompt, interactive=True)
                    elif retrying:
                        previous = messages[-1]["content"]
                        prompt = previous if isinstance(previous, str) else "Retry the previous request using the recorded results."
                        run_agent_turn(prompt, interactive=True)
            if exit_requested:
                break

            # Re-arm the reader: show the retry hint (if any) then the prompt.
            if bool(messages) and messages[-1]["role"] == "user" and not goal.active:
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

        if background_manager.has_ready():
            with agent_lock:
                ran = run_background_turn()
            if ran:
                continue

        # 3) Idle: briefly wait, then re-check both sources.
        time.sleep(POLL_SECONDS)

    stop_event.set()
    workflow_runtime.cancel()
    want_input.set()  # release the reader if it is parked on the gate
    team_manager.shutdown_all()
    scheduler.stop()
    background_manager.teardown_all()
    registry.teardown_all()


def main() -> None:
    run()


if __name__ == "__main__":
    main()
