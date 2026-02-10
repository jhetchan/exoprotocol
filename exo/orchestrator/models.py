from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class OrchestratorTask:
    task_id: str
    intent: str
    action_kind: str = "read_file"
    target: str | None = None
    action_params: dict[str, Any] = field(default_factory=dict)
    scope_allow: list[str] = field(default_factory=lambda: ["**"])
    scope_deny: list[str] = field(default_factory=list)
    topic_id: str | None = None
    ttl_hours: int = 1
    max_attempts: int = 1
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_submit_payload(self, default_topic_id: str) -> dict[str, Any]:
        return {
            "intent_id": self.task_id,
            "intent": self.intent,
            "topic": self.topic_id or default_topic_id,
            "ttl_hours": max(int(self.ttl_hours), 1),
            "max_attempts": max(int(self.max_attempts), 1),
            "scope": {
                "allow": self.scope_allow or ["**"],
                "deny": self.scope_deny or [],
            },
            "action": {
                "kind": self.action_kind,
                "target": self.target,
                "params": self.action_params if isinstance(self.action_params, dict) else {},
                "mode": "execute",
            },
            "metadata": self.metadata if isinstance(self.metadata, dict) else {},
        }


@dataclass(frozen=True)
class Workflow:
    workflow_id: str
    tasks: list[OrchestratorTask]
    stop_on_error: bool = True


@dataclass(frozen=True)
class AgentRun:
    agent_id: str
    workflow: Workflow
