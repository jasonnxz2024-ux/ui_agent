"""Contracts between semantic detection and LVGL code generation.

The scanner deliberately has no LVGL knowledge.  These small immutable values
are the only data the generator consumes, making every generated decision
available to the audit manifest.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


SupportLevel = Literal["native", "compound", "partial", "fallback", "unsupported"]


@dataclass(frozen=True)
class AdaptedWidget:
    """A widget intent that can be rendered by an LVGL backend.

    ``properties`` is intentionally JSON-shaped.  Keeping it free of scanner
    or Python object references allows an audit report to be persisted and
    regenerated on another machine.
    """

    source_id: str
    adapter: str
    widget_type: str
    support: SupportLevel
    properties: dict[str, Any] = field(default_factory=dict)
    resource_ids: tuple[str, ...] = ()
    event_ids: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
