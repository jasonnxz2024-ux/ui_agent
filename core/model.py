"""Stable intermediate model shared by every React-to-LVGL adapter."""
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal


VisualEffectKind = Literal["glow", "shadow", "blur", "gradient", "mask", "filter", "clip"]
SupportLevel = Literal["native", "compound", "fallback", "unsupported"]


@dataclass
class Resource:
    id: str
    path: Path
    kind: Literal["image", "svg", "font", "video", "unknown"]
    source: str
    references: list[str] = field(default_factory=list)


@dataclass
class Event:
    id: str
    kind: Literal["click", "drag", "swipe", "change", "timer", "input"]
    handler: str
    target: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class NavigationEdge:
    """A source-backed request to move from one application view to another."""

    id: str
    source_component: str
    trigger: str
    target_route: str
    target_component: str | None = None
    source_route: str = "*"
    action: Literal["state", "route", "link"] = "state"
    confidence: float = 0.0
    status: Literal["confirmed", "verify", "review", "audit"] = "audit"
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class SourceModule:
    """A source file and its resolved local dependencies.

    Imports deliberately remain file-oriented.  Components are connected in a
    second graph so an adapter can distinguish a component it renders from a
    component that merely happens to be imported by its source file.
    """
    id: str
    path: str
    imports: list[str] = field(default_factory=list)
    external_imports: list[str] = field(default_factory=list)
    exports: list[str] = field(default_factory=list)


@dataclass
class Component:
    id: str
    kind: str
    source_file: str
    props: dict[str, Any] = field(default_factory=dict)
    children: list[str] = field(default_factory=list)
    resource_ids: list[str] = field(default_factory=list)
    event_ids: list[str] = field(default_factory=list)


@dataclass
class SourceInstanceIR:
    """One concrete JSX element at a source call-site."""
    instance_id: str
    owner_component: str
    tag: str
    component_ref: str | None = None
    source_file: str = ""
    span: list[int] = field(default_factory=list)
    occurrence: int = 0
    parent_id: str | None = None
    children: list[str] = field(default_factory=list)
    props_raw: dict[str, Any] = field(default_factory=dict)
    semantic_id: str | None = None
    figma_node_id: str | None = None
    browser_identity: str | None = None
    browser_bounds: list[float] | None = None
    browser_confidence: float = 0.0
    binding_status: str = "unbound"


@dataclass
class Screen:
    id: str
    name: str
    width: int
    height: int
    root_component: str


@dataclass(frozen=True)
class ScreenIR:
    """One application route composed from React component evidence.

    ``shared_components`` are layout elements rendered outside the route
    switch (for example a persistent navigation rail).  Keeping this contract
    in the core model lets the C and UIBuilder backends consume the same page
    selection instead of independently guessing screens from filenames.
    """

    id: str
    name: str
    route: str
    root_component: str
    shared_components: tuple[str, ...] = ()


@dataclass
class VisualEffectIR:
    """Stable semantic evidence for a visual effect.

    Effects are intentionally independent from a target renderer.  A CSS
    shadow, SVG filter, or gradient therefore remains inspectable even when a
    later target cannot express it exactly.
    """

    id: str
    kind: VisualEffectKind | str
    source_file: str
    effect_type: VisualEffectKind | str | None = None
    source_offset: int = 0
    target_component: str | None = None
    evidence_ids: list[str] = field(default_factory=list)
    color: str | None = None
    opacity: float | None = None
    spread: float | None = None
    blur_radius: float | None = None
    offset_x: float | None = None
    offset_y: float | None = None
    gradient_type: str | None = None
    angle: float | None = None
    stops: list[dict[str, Any]] = field(default_factory=list)
    mask_path: str | None = None
    clip_path: str | None = None
    filter_id: str | None = None
    primitives: list[dict[str, Any]] = field(default_factory=list)
    bounds: list[float] | None = None
    properties: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # ``effect_type`` is a readable compatibility alias used by reports;
        # ``kind`` stays the canonical field for the JSON contract.
        if self.effect_type and self.kind == "filter":
            self.kind = self.effect_type
        self.effect_type = self.kind


@dataclass
class CapabilityDecision:
    """Evidence-backed target capability choice for one source item."""

    target_id: str = ""
    support: SupportLevel = "unsupported"
    adapter: str = ""
    confidence: float = 0.0
    evidence_ids: list[str] = field(default_factory=list)
    reason: str = ""
    fallback_bounds: list[float] | None = None
    source_id: str | None = None
    strategy: SupportLevel | None = None
    properties: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.source_id and not self.target_id:
            self.target_id = self.source_id
        if not self.source_id:
            self.source_id = self.target_id
        if self.strategy and self.support == "unsupported":
            self.support = self.strategy
        self.strategy = self.support
        self.confidence = max(0.0, min(1.0, float(self.confidence)))


@dataclass
class RenderPlanItem:
    """One renderer operation derived from a capability decision."""

    id: str = ""
    source_id: str = ""
    support: SupportLevel = "unsupported"
    adapter: str = ""
    operation: str = "native"
    evidence_ids: list[str] = field(default_factory=list)
    fallback_bounds: list[float] | None = None
    properties: dict[str, Any] = field(default_factory=dict)


@dataclass
class RenderPlan:
    """Target-neutral render operations and blockers."""

    items: list[RenderPlanItem] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    schema: str = "uagent.render-plan/v1"


@dataclass
class ReactProjectModel:
    source_dir: Path
    resources: dict[str, Resource] = field(default_factory=dict)
    components: dict[str, Component] = field(default_factory=dict)
    instances: dict[str, SourceInstanceIR] = field(default_factory=dict)
    events: dict[str, Event] = field(default_factory=dict)
    navigation: list[NavigationEdge] = field(default_factory=list)
    modules: dict[str, SourceModule] = field(default_factory=dict)
    component_graph: dict[str, list[str]] = field(default_factory=dict)
    entry_modules: list[str] = field(default_factory=list)
    entry_components: list[str] = field(default_factory=list)
    screens: list[Screen] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    video_evidence: dict[str, Any] = field(default_factory=dict)
    source_evidence: dict[str, dict[str, Any]] = field(default_factory=dict)
    browser_evidence: dict[str, Any] = field(default_factory=dict)
    visual_effects: dict[str, VisualEffectIR] = field(default_factory=dict)
    capability_plan: dict[str, CapabilityDecision] = field(default_factory=dict)
    render_plan: RenderPlan = field(default_factory=RenderPlan)
    binding_summary: dict[str, Any] = field(default_factory=dict)
    # JSON-compatible summary produced by the deterministic planning pass.
    # Keeping this as a plain mapping avoids a dependency cycle between the
    # source model and the planning-agent protocol.
    agent_planning: dict[str, Any] = field(default_factory=dict)

    def add_warning(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)
