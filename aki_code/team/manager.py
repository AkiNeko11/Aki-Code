from __future__ import annotations

import os
import threading

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
from aki_code.team.protocol import (
    STATUS_APPROVED,
    STATUS_PENDING,
    STATUS_REJECTED,
    TYPE_PLAN_APPROVAL,
    TYPE_SHUTDOWN,
    ProtocolState,
    new_request_id,
)
from aki_code.team.teammate import TeammateRuntime, spawn_teammate

LEAD = "lead"


class TeamManager:
    """Lead-side coordinator for an agent team (Lesson 13).

    Owns the message bus, the spawned teammates, and the in-flight control
    protocols. The Lead never polls teammates: their result / idle / plan events
    land in the lead mailbox, consume_lead_inbox() drains them (matching typed
    responses to their requests) and formats them as a [Team events] block that
    the runtime injects to wake a new Lead turn.
    """

    def __init__(
        self,
        *,
        bus: MessageBus,
        task_store,
        client,
        skill_loader,
        permission_manager,
        hook_manager,
        console=None,
        workdir: str | None = None,
    ):
        self.bus = bus
        self.task_store = task_store
        self.client = client
        self.skill_loader = skill_loader
        self.permission_manager = permission_manager
        self.hook_manager = hook_manager
        self.console = console
        self.workdir = workdir or os.getcwd()
        self.teammates: dict[str, TeammateRuntime] = {}
        self.pending: dict[str, ProtocolState] = {}
        self._lock = threading.RLock()

    # ── spawning ────────────────────────────────────────────────

    def spawn(self, name: str, task_id: str, require_plan: bool = False) -> str:
        with self._lock:
            if name in self.teammates:
                return f"Teammate '{name}' already exists"
            try:
                runtime = spawn_teammate(
                    name=name,
                    task_id=task_id,
                    client=self.client,
                    task_store=self.task_store,
                    skill_loader=self.skill_loader,
                    bus=self.bus,
                    permission_manager=self.permission_manager,
                    hook_manager=self.hook_manager,
                    console=self.console,
                    workdir=self.workdir,
                    require_plan=require_plan,
                )
            except ValueError as exc:
                return str(exc)
            self.teammates[name] = runtime
            return f"Teammate '{name}' spawned on task {task_id}"

    def list_teammates(self) -> str:
        with self._lock:
            if not self.teammates:
                return "(no teammates)"
            lines = []
            for name, rt in self.teammates.items():
                task = rt.current_task.id if rt.current_task else "-"
                state = "working" if rt.current_task else "idle"
                lines.append(f"{name}: {state} (task {task}, gate {rt.gate_status})")
            return "\n".join(lines)

    # ── lead inbox delivery ─────────────────────────────────────

    def has_lead_mail(self) -> bool:
        return self.bus.peek(LEAD)

    def consume_lead_inbox(self) -> list[dict]:
        messages = self.bus.read_inbox(LEAD)
        for message in messages:
            self._track_incoming(message)
        return messages

    def _track_incoming(self, message: dict) -> None:
        msg_type = message.get("type")
        metadata = message.get("metadata") or {}
        request_id = metadata.get("request_id")
        with self._lock:
            if msg_type == MSG_SHUTDOWN_RESPONSE and request_id in self.pending:
                state = self.pending[request_id]
                if state.type == TYPE_SHUTDOWN and state.status == STATUS_PENDING:
                    state.status = STATUS_APPROVED
            elif msg_type == MSG_PLAN_APPROVAL_REQUEST and request_id:
                self.pending[request_id] = ProtocolState(
                    request_id=request_id,
                    type=TYPE_PLAN_APPROVAL,
                    sender=message.get("from", ""),
                    target=LEAD,
                    status=STATUS_PENDING,
                    payload=message.get("content", ""),
                    work_version=metadata.get("work_version"),
                    task_id=metadata.get("task_id"),
                )

    def format_team_events(self, messages: list[dict]) -> str:
        lines = []
        for m in messages:
            frm = m.get("from", "?")
            mtype = m.get("type")
            content = m.get("content", "")
            meta = m.get("metadata") or {}
            if mtype == MSG_RESULT:
                lines.append(f"- {frm} completed task {meta.get('task_id')}: {content}")
            elif mtype == MSG_IDLE:
                lines.append(f"- {frm} is idle and ready for more work")
            elif mtype == MSG_PLAN_APPROVAL_REQUEST:
                lines.append(
                    f"- {frm} requests plan approval (request_id={meta.get('request_id')}, "
                    f"task={meta.get('task_id')}): {content}"
                )
            elif mtype == MSG_SHUTDOWN_RESPONSE:
                lines.append(f"- {frm} acknowledged shutdown (request_id={meta.get('request_id')})")
            else:
                lines.append(f"- {frm} [{mtype}]: {content}")
        return "[Team events]\n" + "\n".join(lines)

    # ── plan protocol (Lead side) ───────────────────────────────

    def request_plan(self, name: str) -> str:
        with self._lock:
            if name not in self.teammates:
                return f"No such teammate: {name}"
        self.bus.send(LEAD, name, "Please submit your plan for approval.", MSG_PLAN_REQUEST)
        return f"Requested a plan from {name}"

    def approve_plan(self, request_id: str, approve: bool, feedback: str = "") -> str:
        with self._lock:
            state = self.pending.get(request_id)
            if state is None:
                return f"No pending request with id {request_id}"
            if state.type != TYPE_PLAN_APPROVAL:
                return f"Request {request_id} is not a plan approval"
            if state.status != STATUS_PENDING:
                return f"Request {request_id} is already {state.status}"
            state.status = STATUS_APPROVED if approve else STATUS_REJECTED
            sender = state.sender
            work_version = state.work_version
            task_id = state.task_id
        self.bus.send(
            LEAD, sender, feedback or ("approved" if approve else "rejected"),
            MSG_PLAN_APPROVAL_RESPONSE,
            {
                "request_id": request_id,
                "approve": bool(approve),
                "work_version": work_version,
                "task_id": task_id,
            },
        )
        return f"Plan {request_id} {'approved' if approve else 'rejected'}"

    # ── shutdown protocol ───────────────────────────────────────

    def shutdown_teammate(self, name: str) -> str:
        with self._lock:
            if name not in self.teammates:
                return f"No such teammate: {name}"
            request_id = new_request_id("shutdown")
            self.pending[request_id] = ProtocolState(
                request_id=request_id, type=TYPE_SHUTDOWN, sender=LEAD, target=name,
            )
        self.bus.send(LEAD, name, "shutdown", MSG_SHUTDOWN_REQUEST, {"request_id": request_id})
        return f"Shutdown requested for {name} (request_id={request_id})"

    def shutdown_all(self, timeout: float = 3.0) -> None:
        with self._lock:
            names = list(self.teammates)
        for name in names:
            self.shutdown_teammate(name)
        with self._lock:
            runtimes = list(self.teammates.values())
            self.teammates.clear()
        for runtime in runtimes:
            runtime.stop(timeout=timeout)
