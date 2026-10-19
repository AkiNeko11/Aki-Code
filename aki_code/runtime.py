import threading
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

from aki_code.agent.lifecycle import StopDecision
from aki_code.security.permission import PermissionMode

def _display_tool_result(result: str, console) -> None:
    preview = result if len(result) <= 500 else result[:500] + "..."
    console.print(f"  [dim]← {escape(preview)}[/dim]")


def _call_llm(client, messages, tools, system, console):
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


class AgentRuntime:
    """Shared session assembly; terminal input and process supervision are hosts."""

    def __init__(self, *, workspace=None, state_dir=None, profile="interactive",
                 client=None, console=None, ask_permission=None, event_sink=None,
                 permission_mode=PermissionMode.DEFAULT):
        console = console if console is not None else Console()
        def emit(event):
            if event_sink is not None:
                event_sink(event)
        client = client if client is not None else get_client()
        workspace = Path(workspace or Path.cwd()).resolve()
        coding = profile == "coding"
        memory_enabled = not coding
        state_dir = Path(state_dir).resolve() if state_dir is not None else None
        def state_path(name, configured):
            return state_dir / name if state_dir is not None else configured
        readable_dirs = [state_path("transcripts", config.TRANSCRIPTS_DIR),
                         state_path("tool-results", config.TOOL_OUTPUTS_DIR)] if coding else []
        permission_manager = PermissionManager(
            allowed_dirs=[str(workspace)], base_dir=workspace, mode=permission_mode,
            readable_dirs=readable_dirs,
        )

        settings = SessionSettings(show_hooks=not coding)
        hook_manager = HookManager()
        register_builtin_hooks(hook_manager, settings)

        token_tracker = TokenTracker()
        hook_manager.register(HookEvent.POST_LLM_CALL, token_tracker.on_post_llm_call)

        # The task tool needs a fully-built registry, permissions and hooks, so it
        # is wired last — create_registry passes this same registry into TaskTool.
        todo_manager = TodoManager()
        skill_loader = SkillLoader(Path(config.SKILLS_DIR).resolve())
        skill_loader.scan()
        # The skill catalog is static for the session; the memory section is
        # recalled per turn, so the full system prompt is rebuilt each request.
        skill_catalog = skill_loader.catalog()
        memory_store = MemoryStore(client, memory_dir=state_path("memory", config.MEMORY_DIR),
                                   on_response=token_tracker.add_from_response)

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

        if memory_enabled:
            lead_hooks.register(HookEvent.STOP, _memory_stop_hook)

        task_store = TaskStore(state_path("tasks", config.TASKS_DIR))
        background_manager = BackgroundManager(event_sink=emit if coding else None)
        scheduler = CronScheduler(state_path("schedules.json", config.SCHEDULES_FILE))
        try:
            scheduler.load()
        except ValueError as exc:
            # Report a corrupt schedule file loudly rather than silently starting
            # with no jobs (the course requires the error to surface).
            console.print(f"[bold red]Schedule load error:[/bold red] {exc}")
        bus = MessageBus(state_path("mailboxes", config.MAILBOX_DIR))
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
            workdir=str(workspace),
        )

        def _goal_event(event):
            emit(event)
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
            max_stop_blocks=config.MAX_STOP_BLOCKS, max_turns=None if coding else config.MAX_TURNS,
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
            emit(event)
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
                hook_manager=hook_manager, workspace=workspace, allow_reads=True,
            ),
            WorkflowStore(state_path("workflows", config.WORKFLOWS_DIR)),
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
            ask_permission=ask_permission,
            console=console,
            workflow_runtime=workflow_runtime, workspace=workspace, profile=profile,
            event_sink=emit if coding else None,
        )
        command_registry = create_command_registry()
        compactor = Compactor(client, todo_manager=todo_manager, console=console,
                              on_response=token_tracker.add_from_response,
                              transcripts_dir=state_path("transcripts", config.TRANSCRIPTS_DIR),
                              tool_outputs_dir=state_path("tool-results", config.TOOL_OUTPUTS_DIR))
        messages: list[dict] = []

        # Serializes agent turns: the main REPL holds it while reading input is NOT
        # happening, and the cron processor acquires it (non-blocking) so a scheduled
        # turn never overlaps a user turn on the shared `messages`/console.
        agent_lock = threading.Lock()
        stop_event = threading.Event()

        # The lead's agent loop, extracted into the shared harness (Lesson 15). The
        # closures below inject the lead-specific pieces the harness is agnostic to:
        # streaming model calls, the dynamic registry tool pool, background-job
        # routing, per-turn memory recall in the system prompt, and compaction.
        def _lead_dispatch(tool_use):
            # A long-running bash command flagged run_in_background starts on a worker
            # thread and returns a placeholder immediately; its result is injected as
            # a notification on a later turn.
            if should_run_background(tool_use.name, tool_use.input):
                bg_id = background_manager.start(tool_use.input.get("command", ""), goal_id=goal.current_id, cwd=str(workspace))
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
            recalled = memory_store.load_memories(memory_store.select_relevant(active_request)) if memory_enabled else []
            prompt = build_system_prompt(
                skill_catalog, memory_store.render(recalled), mcp_manager.prompt_state() if not coding else "",
                workspace=workspace, include_team=not coding
            ) + goal.prompt_section()
            if coding:
                prompt += (
                    "\n\n# Automated task\n"
                    "There is no interactive terminal. Complete the supplied task using "
                    "the available tools. Actions requiring human approval stop the run. "
                    "Run verification commands directly so the tool's exit code reflects "
                    "the actual test process; avoid appending echo or other commands that "
                    "mask failure. Leave code changes in the working tree for patch export; "
                    "do not stage, commit, reset or change Git history. "
                    "Runtime archives referenced in tool results are read-only. "
                    "Use glob to list them and read_file with an exact path, offset "
                    "and a small limit (for example 100 lines) to inspect them. "
                    "Shell access to those archives is denied."
                )
            return prompt

        def _background_observed(record):
            emit({"type": "background_result", "record": record})
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

        if coding:
            def _pending_stop(ctx):
                if ctx.data["decision"].action == "allow" and background_manager.has_pending(None):
                    ctx.data["decision"] = StopDecision("defer", "Waiting for background commands.")
            lead_hooks.register(HookEvent.BEFORE_STOP, _pending_stop)

        def _audit(ctx):
            data = {key: value for key, value in ctx.data.items()
                    if key in {"tool_name", "tool_input", "tool_use_id", "result", "message", "status", "reason", "error"}}
            emit({"type": ctx.event.value, **data})
        if event_sink is not None:
            for hooks in (hook_manager, lead_hooks):
                for event in (HookEvent.PRE_TOOL_USE, HookEvent.POST_TOOL_USE,
                              HookEvent.MESSAGE_APPENDED, HookEvent.ON_ERROR, HookEvent.TURN_STOPPED):
                    hooks.register(event, _audit)

        harness = AgentHarness(
            hook_manager=lead_hooks,
            schema_provider=registry.get_schemas,
            dispatch=_lead_dispatch,
            llm_caller=lambda msgs, tools, system: _call_llm(client, msgs, tools, system, console),
            permission_manager=permission_manager,
            system_prompt_provider=_lead_system_prompt,
            compactor=compactor,
            pre_model=_pre_model,
            on_tool_start=_lead_tool_start,
            on_tool_result=lambda result: _display_tool_result(result, console),
            on_interrupt=lambda: console.print("\n[dim]Interrupted.[/dim]"),
            on_error=lambda e: console.print(f"\n[bold red]Error:[/bold red] {e}"),
            todo_nudge=True,
            text_extractor=lambda blocks: "\n".join(b.text for b in blocks if b.type == "text"),
            max_turns=None if coding else config.MAX_TURNS,
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
            ask = ask_permission if interactive or coding else (lambda *args: False)
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


        self.client = client
        self.permission_manager = permission_manager
        self.workspace = workspace
        self.settings = settings
        self.hook_manager = hook_manager
        self.lead_hooks = lead_hooks
        self.token_tracker = token_tracker
        self.todo_manager = todo_manager
        self.skill_loader = skill_loader
        self.memory_store = memory_store
        self.task_store = task_store
        self.background_manager = background_manager
        self.scheduler = scheduler
        self.team_manager = team_manager
        self.mcp_manager = mcp_manager
        self.goal = goal
        self.workflow_runtime = workflow_runtime
        self.registry = registry
        self.command_registry = command_registry
        self.compactor = compactor
        self.messages = messages
        self.agent_lock = agent_lock
        self.stop_event = stop_event
        self.append_user = _append_user
        self.run_turn = run_agent_turn
        self.run_scheduled_turn = run_scheduled_turn
        self.run_team_turn = run_team_turn
        self.run_background_turn = run_background_turn
        self.harness = harness
        registry.setup_all()

    def close(self):
        self.stop_event.set()
        for cleanup in (self.workflow_runtime.cancel, self.team_manager.shutdown_all,
                        self.scheduler.stop, self.background_manager.teardown_all,
                        self.registry.teardown_all):
            try:
                cleanup()
            except Exception as exc:
                # A failed cleanup must not strand the remaining managers.
                self.harness.on_error(exc)
