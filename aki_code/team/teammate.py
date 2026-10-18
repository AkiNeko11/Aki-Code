from __future__ import annotations

import os
import threading

from rich.markup import escape

from aki_code import config
from aki_code.agent.subagent import extract_text
from aki_code.hooks.manager import HookEvent
from aki_code.llm.client import call_with_retry
from aki_code.llm.prompts import build_teammate_prompt
from aki_code.mcp.naming import is_mcp_tool
from aki_code.team.bus import (
    MSG_IDLE,
    MSG_MESSAGE,
    MSG_PLAN_APPROVAL_REQUEST,
    MSG_PLAN_APPROVAL_RESPONSE,
    MSG_PLAN_REQUEST,
    MSG_RESULT,
    MSG_SHUTDOWN_REQUEST,
    MSG_SHUTDOWN_RESPONSE,
    MessageBus,
)
from aki_code.team.protocol import new_request_id
from aki_code.team.roles import TEAMMATE_EXCLUDED_TOOLS, teammate_schemas
from aki_code.team.assignments import Assignments
from aki_code.team.worktree import WorktreeError, resolve_task_cwd

# Plan gate: mutating tools are blocked unless the plan is not required or has
# been approved. Read-only tools are never gated.
PLAN_GATED_TOOLS = frozenset({"bash", "write_file", "edit_file", "complete_task"})
# Workspace tools operate on a claimed task's directory; a teammate with no task
# must claim one first rather than touch the repository.
WORKSPACE_TOOLS = frozenset({"bash", "read_file", "write_file", "edit_file", "glob"})

GATE_NOT_REQUIRED = "not_required"
GATE_REQUIRED = "required"
GATE_PENDING = "pending"
GATE_APPROVED = "approved"
GATE_REJECTED = "rejected"

DEFAULT_IDLE_SCAN_INTERVAL = 0.5


class TeammateRuntime:
    """A persistent teammate agent (Lesson 13).

    Unlike a one-shot subagent, a teammate keeps its own conversation across
    tasks and cycles WORK <-> IDLE until shut down. It claims tasks from the
    shared board (or receives one at spawn), works them with a gated, non
    interactive tool dispatch, and reports a result plus an idle_notification to
    the lead over the message bus. It never reads the terminal: anything needing
    user approval is refused and surfaced to the lead.
    """

    def __init__(
        self,
        *,
        name: str,
        client,
        task_store,
        skill_loader,
        bus: MessageBus,
        permission_manager,
        hook_manager,
        system_prompt: str | None = None,
        console=None,
        workdir: str | None = None,
        assignments: Assignments | None = None,
        mcp_manager=None,
        require_plan: bool = False,
        idle_scan_interval: float = DEFAULT_IDLE_SCAN_INTERVAL,
    ):
        self.name = name
        self.client = client
        self.task_store = task_store
        self.bus = bus
        self.permission_manager = permission_manager
        self.system_prompt = system_prompt or build_teammate_prompt(name)
        self.console = console
        self.workdir = workdir or os.getcwd()
        self.assignments = assignments or Assignments()
        self.mcp_manager = mcp_manager
        self.idle_scan_interval = idle_scan_interval

        self.messages: list[dict] = []
        self.current_task = None
        self.cwd = self.workdir
        self.gate_status = GATE_REQUIRED if require_plan else GATE_NOT_REQUIRED
        self.work_version = 0
        self.gate_task_id: str | None = None
        self._pending_directive = False
        self._waiting_for_plan = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        # A teammate gets its OWN registry with workspace tools bound to its
        # current task directory (self.cwd), so parallel teammates edit separate
        # checkouts. Imported lazily: team.teammate <- tools <- tools.team <-
        # team.manager <- team.teammate would otherwise be a circular import.
        from aki_code.tools import create_teammate_registry
        from aki_code.agent.harness import AgentHarness

        self.registry = create_teammate_registry(
            cwd_provider=self._current_cwd,
            task_store=task_store,
            skill_loader=skill_loader,
        )

        # The teammate runs the shared harness (Lesson 15) on a forked
        # HookManager: the fork keeps the parent's observability hooks and the
        # shared TokenTracker, while the plan-gate / exclusion / workspace block
        # hook and the non-interactive permission hook stay private to this
        # teammate and never leak onto the lead or a sibling. The block hook is
        # registered before the harness adds the permission hook so a gated or
        # excluded tool is refused without a permission prompt.
        self.hooks = hook_manager.fork()
        self.hooks.register(HookEvent.PRE_TOOL_USE, self._teammate_block_hook)
        self.harness = AgentHarness(
            hook_manager=self.hooks,
            schema_provider=self._schemas,
            dispatch=self._dispatch,
            llm_caller=self._llm,
            permission_manager=permission_manager,
            system_prompt=self.system_prompt,
            pre_model=self._drain_inbox,
            abort_check=lambda: self._stop.is_set(),
            on_tool_start=self._tool_start,
            text_extractor=extract_text,
        )

    def _current_cwd(self) -> str:
        return self.cwd

    # ── lifecycle ───────────────────────────────────────────────

    def start(self, initial_task=None) -> None:
        if initial_task is not None:
            self._begin_task(initial_task)
        self._thread = threading.Thread(
            target=self._run, name=f"teammate-{self.name}", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float | None = 2.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def _run(self) -> None:
        while not self._stop.is_set():
            if self.current_task is not None:
                if not self._waiting_for_plan:
                    self._pending_directive = False
                    self._work_current_task()
            elif self._pending_directive:
                self._work()
                self._pending_directive = False

            if self._stop.is_set():
                break

            inbox = self.bus.wait_for_messages(self.name, timeout=self.idle_scan_interval)
            if inbox:
                if self._process_inbox(inbox, midwork=False):
                    break
                continue

            if self.current_task is not None:
                continue
            task = self.task_store.claim_next_task(self.name)
            if task is not None:
                try:
                    self._begin_task(task)
                except WorktreeError as exc:
                    # A broken worktree binding: report to the lead rather than
                    # dying or silently running in the repo. The task stays
                    # claimed so the lead can rebind or cancel it.
                    self.bus.send(
                        self.name, "lead",
                        f"Cannot start task {task.id}: {exc}", MSG_RESULT,
                        {"task_id": task.id},
                    )
        self._cleanup_on_exit()

    def _work_current_task(self) -> None:
        """Run the claimed task's turn, then report, release or abandon it."""
        task = self.current_task
        result = self._work()
        if self._stop.is_set():
            # Shutdown interrupted the task mid-turn: put it back on the board so
            # another owner can resume it (the worktree binding is preserved).
            self.task_store.release(task.id, self.name)
        elif result.ok and self.gate_status not in (GATE_NOT_REQUIRED, GATE_APPROVED):
            # A model's final "waiting for approval" ends only this turn. Keep
            # its claim and workspace lease, and park without spending API calls
            # until the Lead responds. It has not completed the task.
            self._waiting_for_plan = True
            if self.gate_status != GATE_PENDING:
                self.bus.send(
                    self.name, "lead", f"Task {task.id} awaits an approved plan: {result.text}",
                    MSG_MESSAGE, {"task_id": task.id},
                )
            return
        elif result.ok:
            self._report(task, result.text or "(no output)")
        else:
            # A model/tool error rolled the turn back: tell the lead and release
            # the task rather than leaving it stuck in_progress under a dead owner.
            self.bus.send(
                self.name, "lead",
                f"Task {task.id} failed and was released: {result.text or 'error'}",
                MSG_RESULT, {"task_id": task.id},
            )
            self.task_store.release(task.id, self.name)
        self.current_task = None
        self.cwd = self.workdir  # release the assignment on going IDLE
        self.assignments.release(self.name)

    def _cleanup_on_exit(self) -> None:
        """Thread teardown: never leave a dead teammate holding work or a lease.

        Covers a task claimed but not yet worked when shutdown arrived, and drops
        the assignment lease so the host does not see it blocking worktree
        cleanup. release() is a no-op if the task is no longer in_progress.
        """
        if self.current_task is not None:
            self.task_store.release(self.current_task.id, self.name)
            self.current_task = None
        self.cwd = self.workdir
        self.assignments.release(self.name)

    def _begin_task(self, task) -> None:
        # Resolve and lease the working directory first so a broken binding
        # raises before any state is mutated. assign() records the lease the host
        # checks before removing a worktree, and resolves an unbound task to the
        # repository directory.
        cwd = self.assignments.assign(self.name, task, self.workdir)
        self.current_task = task
        self.cwd = cwd
        self._waiting_for_plan = False
        if self.gate_status != GATE_NOT_REQUIRED:
            self.gate_status = GATE_REQUIRED
        # Claiming changes task identity, so any prior plan approval is stale.
        self.work_version += 1
        self.gate_task_id = task.id
        self.messages.append({
            "role": "user",
            "content": (
                f"[Auto-claimed task {task.id}] {task.subject}\n\n{task.description}"
            ),
        })

    # ── WORK loop (shared harness) ──────────────────────────────

    def _work(self):
        """Run one harness turn over the teammate's own persistent messages.

        ask=None makes the permission hook non-interactive: anything that would
        prompt the user is refused with a message the lead can act on, instead of
        blocking on a terminal the teammate does not own.
        """
        return self.harness.run_turn(
            self.messages,
            ask=None,
            ask_block_message=(
                "Permission needed; a teammate cannot prompt the user: {reason}"
            ),
        )

    def _llm(self, messages, tools, system):
        return call_with_retry(
            lambda: self.client.messages.create(
                model=config.MODEL,
                max_tokens=config.MAX_TOKENS,
                system=system,
                messages=messages,
                tools=tools,
            )
        )

    def _schemas(self) -> list:
        """Teammate tool schemas plus any MCP tools the lead has connected.

        A teammate builds its own registry, so it does not hold the MCP adapters;
        they are merged in from the shared MCPManager and gated by host policy
        like any other tool.
        """
        schemas = teammate_schemas(self.registry)
        if self.mcp_manager is not None:
            schemas = schemas + self.mcp_manager.tool_schemas()
        return schemas

    def _dispatch(self, tool_use) -> str:
        # submit_plan is the teammate's own protocol tool, handled here rather
        # than by the registry: it needs this teammate's gate and work version.
        if tool_use.name == "submit_plan":
            return self._submit_plan(str(tool_use.input.get("plan", "")))
        # MCP tools live in the shared manager, not this teammate's own registry.
        if self.mcp_manager is not None and is_mcp_tool(tool_use.name):
            return self.mcp_manager.execute(tool_use.name, tool_use.input)
        return self.registry.execute(tool_use.name, **tool_use.input)

    def _tool_start(self, tool_use) -> None:
        if self.console is not None:
            self.console.print(
                f"  \\[{self.name}] [cyan]→ {tool_use.name}[/cyan]"
                f"{escape(f'({tool_use.input})')}"
            )

    def _teammate_block_hook(self, ctx):
        """PreToolUse gate: exclusion, plan gate, workspace-requires-task.

        Runs before the harness's permission hook and short-circuits it via the
        block flag, mirroring the order the old inline dispatch used. submit_plan
        is in none of these sets, so it flows through to _dispatch.
        """
        if ctx.data.get("block"):
            return ctx
        name = ctx.data.get("tool_name")
        if name in TEAMMATE_EXCLUDED_TOOLS:
            ctx.data["block"] = f"Tool '{name}' is not available to teammates."
        elif name in PLAN_GATED_TOOLS and self.gate_status not in (
            GATE_NOT_REQUIRED,
            GATE_APPROVED,
        ):
            ctx.data["block"] = f"Blocked: plan status is {self.gate_status}."
        elif name in WORKSPACE_TOOLS and self.current_task is None:
            ctx.data["block"] = "Claim a task before using workspace tools."
        return ctx

    def _drain_inbox(self, messages) -> None:
        """pre_model hook: read the mailbox before each model call.

        This is what keeps a shutdown or a lead directive from being starved by a
        long run of tool rounds — the teammate no longer only checks its inbox
        between tasks. A shutdown sets _stop, which abort_check turns into a clean
        end of the turn.
        """
        inbox = self.bus.read_inbox(self.name)
        if inbox:
            self._process_inbox(inbox, midwork=True)

    def _report(self, task, result_text: str) -> None:
        self.task_store.complete(task.id, self.name)
        self.bus.send(
            self.name, "lead", result_text or "(no output)", MSG_RESULT,
            {"task_id": task.id},
        )
        self.bus.send(
            self.name, "lead", "Waiting for more work.", MSG_IDLE,
            {"task_id": task.id},
        )

    # ── plan submission (teammate side) ─────────────────────────

    def _submit_plan(self, plan: str) -> str:
        """Send the plan to the lead for approval and pend the gate.

        The approval only takes effect if it comes back for the same task and
        work version, so claiming or releasing a task invalidates a stale plan.
        """
        if not plan.strip():
            return "Cannot submit an empty plan."
        request_id = new_request_id("plan")
        self.gate_status = GATE_PENDING
        self.bus.send(
            self.name, "lead", plan.strip(), MSG_PLAN_APPROVAL_REQUEST,
            {
                "request_id": request_id,
                "work_version": self.work_version,
                "task_id": self.gate_task_id,
            },
        )
        return (
            f"Plan submitted for approval (request_id={request_id}). Shell and "
            f"file-editing tools stay blocked until the lead approves."
        )

    # ── message handling (IDLE loop and mid-turn drain) ─────────

    def _process_inbox(self, inbox: list[dict], *, midwork: bool) -> bool:
        """Handle mailbox messages; return True if the teammate should stop.

        midwork=True means we are inside a harness turn (drained by pre_model): a
        directive is merged into the trailing user message so the model sees it on
        the very next call, and no separate follow-up turn is queued. In IDLE
        (midwork=False) a directive is appended and _pending_directive is set so
        the loop runs a turn to respond.
        """
        for msg in inbox:
            msg_type = msg.get("type")
            metadata = msg.get("metadata") or {}
            if msg_type == MSG_SHUTDOWN_REQUEST:
                self.bus.send(
                    self.name, msg.get("from", "lead"), "shutdown acknowledged",
                    MSG_SHUTDOWN_RESPONSE, {"request_id": metadata.get("request_id")},
                )
                self._stop.set()
                return True
            if msg_type == MSG_PLAN_APPROVAL_RESPONSE:
                if self._apply_plan_approval(metadata):
                    self._waiting_for_plan = False
                    self._inject_user_text(
                        f"[Plan {self.gate_status} by lead] {msg.get('content', '')}\n"
                        "Continue the claimed task under the current plan decision."
                    )
                continue
            if msg_type == MSG_PLAN_REQUEST:
                # The lead asked for a plan; nudge the model to produce one
                # (submit_plan is the model's tool).
                self._inject_user_text(
                    "[Message from lead] Please submit your plan for approval."
                )
            else:
                # A free-text directive from the lead: take it into context.
                self._inject_user_text(
                    f"[Message from {msg.get('from', 'lead')}] {msg.get('content', '')}"
                )
            if not midwork:
                self._waiting_for_plan = False
                self._pending_directive = True
        return False

    def _apply_plan_approval(self, metadata: dict) -> bool:
        # Honor only if it matches the task/work version the plan was submitted
        # for; claiming or releasing a task stale-ifies it.
        resp_version = metadata.get("work_version")
        resp_task = metadata.get("task_id")
        version_ok = resp_version is None or resp_version == self.work_version
        task_ok = resp_task is None or resp_task == self.gate_task_id
        if version_ok and task_ok:
            self.gate_status = (
                GATE_APPROVED if metadata.get("approve") else GATE_REJECTED
            )
            return True
        return False

    def _inject_user_text(self, text: str) -> None:
        """Add a lead directive, merging into a trailing user message if present.

        Mid-turn the last message is a user tool_result batch, so appending a new
        user message would put two user turns in a row (the endpoint rejects
        that); merging keeps roles alternating, exactly as the lead loop does for
        background and team notifications.
        """
        if self.messages and self.messages[-1].get("role") == "user":
            prev = self.messages[-1].get("content")
            block = {"type": "text", "text": text}
            if isinstance(prev, list):
                prev.append(block)
            else:
                self.messages[-1] = {
                    "role": "user",
                    "content": [{"type": "text", "text": prev}, block],
                }
        else:
            self.messages.append({"role": "user", "content": text})


def spawn_teammate(
    *,
    name: str,
    task_id: str,
    client,
    task_store,
    skill_loader,
    bus: MessageBus,
    permission_manager,
    hook_manager,
    console=None,
    workdir: str | None = None,
    assignments: Assignments | None = None,
    mcp_manager=None,
    require_plan: bool = False,
    idle_scan_interval: float = DEFAULT_IDLE_SCAN_INTERVAL,
) -> TeammateRuntime:
    """Claim the initial task, then start a teammate thread for it.

    The working directory is validated and the task claimed before the thread
    starts: if the worktree binding is broken or the task cannot be claimed
    (already taken, blocked, or the owner is busy) no teammate is spawned and a
    ValueError carrying the reason is raised.
    """
    base = workdir or os.getcwd()
    task = task_store.load(task_id)  # raises KeyError if unknown
    try:
        resolve_task_cwd(task, base)  # fail before claiming on a broken binding
    except WorktreeError as exc:
        raise ValueError(str(exc)) from exc
    claim_message = task_store.claim(task_id, name)
    if not claim_message.startswith("Claimed"):
        raise ValueError(claim_message)
    runtime = TeammateRuntime(
        name=name,
        client=client,
        task_store=task_store,
        skill_loader=skill_loader,
        bus=bus,
        permission_manager=permission_manager,
        hook_manager=hook_manager,
        console=console,
        workdir=workdir,
        assignments=assignments,
        mcp_manager=mcp_manager,
        require_plan=require_plan,
        idle_scan_interval=idle_scan_interval,
    )
    runtime.start(initial_task=task)
    return runtime
