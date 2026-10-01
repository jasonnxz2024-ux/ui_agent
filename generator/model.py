"""Auditable output model for generated AiBuilder custom code."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal


@dataclass
class GeneratedUnit:
    source_id: str
    symbol: str
    adapter: str
    widget_type: str
    support: Literal["native", "compound", "partial", "fallback", "unsupported"]
    c_code: str
    warnings: list[str] = field(default_factory=list)
    resources: list[str] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    decisions: dict[str, Any] = field(default_factory=dict)

    def audit_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class GenerationBundle:
    """Files destined for an AiBuilder project's generated/custom directory."""

    custom_h: str
    custom_c: str
    units: list[GeneratedUnit] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    screen_tree: dict[str, Any] = field(default_factory=dict)
    capability_plan: dict[str, Any] = field(default_factory=dict)
    agent_planning: dict[str, Any] = field(default_factory=dict)

    def audit_dict(self) -> dict[str, Any]:
        return {
            "schema": "uagent.lvgl.audit/v1",
            "files": {"custom.h": "custom.h", "custom.c": "custom.c"},
            "warnings": list(self.warnings),
            "units": [unit.audit_dict() for unit in self.units],
            "screen_tree": self.screen_tree,
            "capability_plan": self.capability_plan,
            "agent_planning": self.agent_planning,
        }
