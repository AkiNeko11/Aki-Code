from __future__ import annotations

from dataclasses import asdict, dataclass, field

# Task lifecycle: pending --claim--> in_progress --complete--> completed.
PENDING = "pending"
IN_PROGRESS = "in_progress"
COMPLETED = "completed"
VALID_STATUSES = (PENDING, IN_PROGRESS, COMPLETED)

# Serialized keys, kept in the course's order so asdict() round-trips cleanly.
TASK_FIELDS = ("id", "subject", "description", "status", "owner", "blockedBy")


@dataclass
class Task:
    """One node in the durable task graph (Lesson 10).

    blockedBy holds the ids of tasks that must complete first; "blocked" is a
    derived condition (any incomplete dependency), never a stored status. owner
    records which agent claimed the task, enabling claim/complete checks and,
    later, multi-agent coordination.
    """

    id: str
    subject: str
    description: str = ""
    status: str = PENDING
    owner: str | None = None
    blockedBy: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    def one_line(self) -> str:
        """Compact summary for list output: id, subject, status, owner, deps."""
        line = f"{self.id}: {self.subject} [{self.status}"
        if self.owner:
            line += f", owner={self.owner}"
        if self.blockedBy:
            line += f", blockedBy={','.join(self.blockedBy)}"
        return line + "]"

    @classmethod
    def from_dict(cls, data: dict) -> "Task":
        """Build a Task from stored JSON, tolerating extra/missing keys."""
        blocked = data.get("blockedBy") or []
        if not isinstance(blocked, list):
            blocked = []
        return cls(
            id=str(data["id"]),
            subject=str(data.get("subject", "")),
            description=str(data.get("description", "") or ""),
            status=str(data.get("status", PENDING)),
            owner=data.get("owner"),
            blockedBy=[str(x) for x in blocked],
        )
