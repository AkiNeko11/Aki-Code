from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass
class CronJob:
    """A scheduled prompt (Lesson 12).

    cron decides when it fires; prompt is what the agent receives at that time.
    recurring jobs keep firing; one-shot jobs are deleted after delivery.
    durable jobs are persisted to disk and restored on restart. pending_delivery
    marks a job that is due but not yet received by the model, and last_fired
    (a "%Y-%m-%d %H:%M" marker) stops the same minute firing twice.
    """

    id: str
    cron: str
    prompt: str
    recurring: bool
    durable: bool
    pending_delivery: bool = False
    last_fired: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "CronJob":
        return cls(
            id=str(data["id"]),
            cron=str(data.get("cron", "")),
            prompt=str(data.get("prompt", "")),
            recurring=bool(data.get("recurring", True)),
            durable=bool(data.get("durable", True)),
            pending_delivery=bool(data.get("pending_delivery", False)),
            last_fired=data.get("last_fired"),
        )

    def one_line(self) -> str:
        flags = []
        if not self.recurring:
            flags.append("one-shot")
        if self.durable:
            flags.append("durable")
        if self.pending_delivery:
            flags.append("pending")
        suffix = f" ({', '.join(flags)})" if flags else ""
        return f"{self.id}: [{self.cron}] {self.prompt}{suffix}"
