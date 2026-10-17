from __future__ import annotations

import os
import threading

from rich.markup import escape

from aki_code import config
from aki_code.agent.subagent import extract_text
from aki_code.hooks.manager import HookEvent
from aki_code.llm.client import call_with_retry
from aki_code.llm.prompts import build_teammate_prompt
from aki_code.team.bus import (
    MSG_IDLE,
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
from aki_code.team.worktree import WorktreeError, resolve_task_cwd

# Plan gate: mutating tools are blocked unless the plan is not required or has
# been approved. Read-only tools are never gated.
PLAN_GATED_TOOLS = frozenset({"bash", "write_file", "edit_file"})
# Workspace tools operate on a claimed task's directory; a teammate with no task
# must claim one first rather than touch the repository.
WORKSPACE_TOOLS = frozenset({"bash", "read_file", "write_file", "edit_file", "glob"})

GATE_NOT_REQUIRED = "not_required"
GATE_REQUIRED = "required"
GATE_PENDING = "pending"
GATE_APPROVED = "approved"
GATE_REJECTED = "rejected"

DEFAULT_MAX_TURNS = 30
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
        require_plan: bool = False,
        max_turns: int = DEFAULT_MAX_TURNS,
        idle_scan_interval: float = DEFAULT_IDLE_SCAN_INTERVAL,
    ):
        self.name = name
        self.client = client
        self.task_store = task_store
        self.bus = bus
        self.permission_manager = permission_manager
        self.hook_manager = hook_manager
        self.system_prompt = system_prompt or build_teammate_prompt(name)
        self.console = console
        self.workdir = workdir or os.getcwd()
        self.max_turns = max_turns
        self.idle_scan_interval = idle_scan_interval

        self.messages: list[dict] = []
        self.current_task = None
        self.cwd = self.workdir
        self.gate_status = GATE_REQUIRED if require_plan else GATE_NOT_REQUIRED
        self.work_version = 0
        self.gate_task_id: str | None = None
        self._pending_directive = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        # A teammate gets its OWN registry with workspace tools bound to its
        # current task directory (self.cwd), so parallel teammates edit separate
        # checkouts. Imported lazily: team.teammate <- tools <- tools.team <-
        # team.manager <- team.teammate would otherwise be a circular import.
        from aki_code.tools import create_teammate_registry

        self.registry = create_teammate_registry(
            cwd_provider=self._current_cwd,
            task_store=task_store,
            skill_loader=skill_loader,
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
                result = self._agent_loop()
                self._report(self.current_task, result)
                self.current_task = None
                self.cwd = self.workdir  # release the assignment on going IDLE
            elif self._pending_directive:
                self._agent_loop()
                self._pending_directive = False

            inbox = self.bus.wait_for_messages(self.name, timeout=self.idle_scan_interval)
            if inbox:
                if self._handle_messages(inbox):
                    break
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

    def _begin_task(self, task) -> None:
        # Resolve the working directory first so a broken binding raises before
        # any state is mutated.
        cwd = self._resolve_cwd(task)
        self.current_task = task
        self.cwd = cwd
        # Claiming changes task identity, so any prior plan approval is stale.
        self.work_version += 1
        self.gate_task_id = task.id
        self.messages.append({
            "role": "user",
            "content": (
                f"[Auto-claimed task {task.id}] {task.subject}\n\n{task.description}"
            ),
        })

    def _resolve_cwd(self, task) -> str:
        # A task bound to a worktree runs there; an unbound task runs in the
        # repository directory. A missing/invalid binding raises rather than
        # silently falling back to the repo (the caller reports it).
        return resolve_task_cwd(task, self.workdir)

    # ── WORK loop ───────────────────────────────────────────────

    def _agent_loop(self) -> str:
        schemas = teammate_schemas(self.registry)
        for _ in range(self.max_turns):
            self.hook_manager.fire(HookEvent.PRE_LLM_CALL, messages=self.messages)
            response = call_with_retry(
                lambda: self.client.messages.create(
                    model=config.MODEL,
                    max_tokens=config.MAX_TOKENS,
                    system=self.system_prompt,
                    messages=self.messages,
                    tools=schemas,
                )
            )
            self.hook_manager.fire(HookEvent.POST_LLM_CALL, response=response)
            self.messages.append({"role": "assistant", "content": response.content})

            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if not tool_uses:
                return extract_text(response.content) or "(no summary)"

            results = [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use.id,
                    "content": self._run_tool(tool_use),
                }
                for tool_use in tool_uses
            ]
            self.messages.append({"role": "user", "content": results})
        return f"Teammate stopped after {self.max_turns} turns without a final answer."

    def _run_tool(self, tool_use) -> str:
        name = tool_use.name
        if name == "submit_plan":
            return self._submit_plan(str(tool_use.input.get("plan", "")))
        if name in TEAMMATE_EXCLUDED_TOOLS:
            return f"Tool '{name}' is not available to teammates."
        if name in PLAN_GATED_TOOLS and self.gate_status not in (
            GATE_NOT_REQUIRED,
            GATE_APPROVED,
        ):
            return f"Blocked: plan status is {self.gate_status}."
        if name in WORKSPACE_TOOLS and self.current_task is None:
            return "Claim a task before using workspace tools."

        decision, reason = self.permission_manager.check(name, tool_use.input)
        if decision == "deny":
            return f"Denied: {reason}"
        if decision == "ask":
            # A teammate cannot prompt the terminal; hand the decision to the lead.
            return f"Permission needed; a teammate cannot prompt the user: {reason}"

        if self.console is not None:
            self.console.print(
                f"  \\[{self.name}] [cyan]→ {name}[/cyan]"
                f"{escape(f'({tool_use.input})')}"
            )
        self.hook_manager.fire(
            HookEvent.PRE_TOOL_USE,
            tool_name=name,
            tool_input=tool_use.input,
            tool_use_id=tool_use.id,
        )
        output = self.registry.execute(name, **tool_use.input)
        self.hook_manager.fire(
            HookEvent.POST_TOOL_USE,
            tool_name=name,
            tool_input=tool_use.input,
            tool_use_id=tool_use.id,
            result=output,
        )
        return output

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

    # ── IDLE message handling ───────────────────────────────────

    def _handle_messages(self, inbox: list[dict]) -> bool:
        """Process inbox messages; return True if the teammate should stop."""
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
                # Honor only if it matches the task/work version the plan was
                # submitted for; claiming or releasing a task stale-ifies it.
                resp_version = metadata.get("work_version")
                resp_task = metadata.get("task_id")
                version_ok = resp_version is None or resp_version == self.work_version
                task_ok = resp_task is None or resp_task == self.gate_task_id
                if version_ok and task_ok:
                    self.gate_status = (
                        GATE_APPROVED if metadata.get("approve") else GATE_REJECTED
                    )
                continue
            if msg_type == MSG_PLAN_REQUEST:
                # The lead asked for a plan; nudge the model to produce one on
                # the next WORK turn (submit_plan is the model's tool).
                self.messages.append({
                    "role": "user",
                    "content": "[Message from lead] Please submit your plan for approval.",
                })
                self._pending_directive = True
                continue
            # A free-text directive from the lead: take it into context and work
            # a turn to respond (workspace tools stay gated without a task).
            self.messages.append({
                "role": "user",
                "content": f"[Message from {msg.get('from', 'lead')}] {msg.get('content', '')}",
            })
            self._pending_directive = True
        return False


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
    require_plan: bool = False,
    max_turns: int = DEFAULT_MAX_TURNS,
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
        require_plan=require_plan,
        max_turns=max_turns,
        idle_scan_interval=idle_scan_interval,
    )
    runtime.start(initial_task=task)
    return runtime
