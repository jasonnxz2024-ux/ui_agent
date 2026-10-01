"""Runtime-first, auditable conversion planning.

The planner is deliberately target-neutral.  It consumes the scanner model
and optional browser/layout evidence, classifies reachable source and runtime
scenario nodes, and returns an audit record.  It never emits C, calls a cloud
service, or invents renderer callbacks.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field
import json
from pathlib import Path
from typing import Any, Literal, Mapping, Protocol

from .model import CapabilityDecision, ReactProjectModel, VisualEffectIR


AgentSupport = Literal["native", "custom", "partial", "fallback", "unsupported"]
PLANNING_CATEGORIES = ("Screen", "Scroll", "VisualEffect")
_BROWSER_LAYOUT_FILES = (
    Path("browser-layout-v2.json"),
    Path("browser-layout.json"),
    Path("openhmi") / "browser-layout.json",
)


@dataclass
class PlanningDecision:
    """One auditable choice for a source or runtime scenario node."""

    node_id: str
    kind: str
    support: AgentSupport
    reason: str
    evidence: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    source_id: str | None = None
    properties: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AgentPlanningReport:
    """Stable v1 output of a planning agent."""

    schema: str = "uagent.agent-planning/v1"
    status: str = "ready"
    enabled: bool = True
    mode: str = "runtime-first-local"
    evidence_mode: str = "source-fallback"
    counts: dict[str, dict[str, int | None]] = field(default_factory=dict)
    decisions: list[PlanningDecision] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["decisions"] = [item.to_dict() for item in self.decisions]
        return payload


class PlanningAgent(Protocol):
    """Explicit interface for capability/completeness planning only."""

    def plan(
        self,
        model: ReactProjectModel,
        *,
        browser_evidence: Mapping[str, Any] | None = None,
        screen_tree: Mapping[str, Any] | None = None,
    ) -> AgentPlanningReport:
        """Return an auditable plan without generating target source code."""


def load_browser_evidence(source_dir: str | Path) -> dict[str, Any] | None:
    """Load the first standard browser/layout receipt without project rules."""
    root = Path(source_dir)
    for relative in _BROWSER_LAYOUT_FILES:
        path = root / relative
        if not path.is_file():
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        if isinstance(value, dict):
            return value
    return None


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _screen_records(browser: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw_screens = browser.get("screens")
    if isinstance(raw_screens, list):
        records: list[dict[str, Any]] = []
        for index, raw in enumerate(raw_screens):
            if not isinstance(raw, dict):
                continue
            data = raw.get("data") if isinstance(raw.get("data"), dict) else raw
            records.append({"index": index, "record": raw, "data": data})
        return records
    data = browser.get("data")
    if isinstance(data, dict) and isinstance(data.get("nodes"), list):
        return [{"index": 0, "record": {"state": "default"}, "data": data}]
    if isinstance(browser.get("nodes"), list):
        return [{"index": 0, "record": {"state": "default"}, "data": dict(browser)}]
    return []


def _metrics_overflow(metrics: Mapping[str, Any]) -> tuple[bool, bool]:
    explicit_x = metrics.get("hasOverflowX")
    explicit_y = metrics.get("hasOverflowY")
    if explicit_x is not None or explicit_y is not None:
        return bool(explicit_x), bool(explicit_y)
    if metrics.get("hasOverflow") is not None:
        return bool(metrics.get("hasOverflow")), bool(metrics.get("hasOverflow"))
    client_w = _number(metrics.get("clientWidth"))
    client_h = _number(metrics.get("clientHeight"))
    scroll_w = _number(metrics.get("scrollWidth"), client_w)
    scroll_h = _number(metrics.get("scrollHeight"), client_h)
    return scroll_w > client_w + 1, scroll_h > client_h + 1


def _host_overflow(data: Mapping[str, Any]) -> dict[str, Any]:
    raw = data.get("overflow")
    if not isinstance(raw, Mapping):
        raw = data.get("scroll") if isinstance(data.get("scroll"), Mapping) else {}
    x, y = _metrics_overflow(raw)
    return {
        "x": x,
        "y": y,
        "source": "document.body/document.documentElement",
        "ignored_for_internal_scroll": True,
    }


def _node_internal_scroll(node: Mapping[str, Any]) -> tuple[bool, str]:
    """Detect a real node-local overflow while excluding body/html hosts."""
    tag = str(node.get("tag", "")).casefold()
    if tag in {"html", "body"}:
        return False, "host-element"
    raw = node.get("overflow")
    if isinstance(raw, Mapping):
        x, y = _metrics_overflow(raw)
        if x or y:
            return True, "node-overflow"
    metrics = node.get("scroll")
    if isinstance(metrics, Mapping):
        x, y = _metrics_overflow(metrics)
        if x or y:
            return True, "node-scroll-extents"
    # Some capture producers put extents directly on the node.
    if any(key in node for key in ("scrollWidth", "scrollHeight", "clientWidth", "clientHeight")):
        x, y = _metrics_overflow(node)
        if x or y:
            return True, "node-scroll-extents"
    if node.get("internalScroll") is True or node.get("hasInternalScroll") is True:
        return True, "explicit-node-evidence"
    return False, "no-node-overflow-evidence"


def _browser_effects(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize top-level and generic node style effects from a capture."""
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    style_keys = {
        "filter": "filter",
        "boxShadow": "shadow",
        "textShadow": "shadow",
        "backgroundImage": "gradient",
        "maskImage": "mask",
        "clipPath": "clip",
    }
    for screen in records:
        screen_index = int(screen.get("index", 0))
        data = screen.get("data") if isinstance(screen.get("data"), Mapping) else {}
        top_level = data.get("visual_effects") if isinstance(data.get("visual_effects"), list) else []
        if screen_index == 0:
            # The capture writer stores this list at the document root.
            root_effects = screen.get("root_visual_effects")
            if isinstance(root_effects, list):
                top_level = [*root_effects, *top_level]
        for raw in top_level:
            if not isinstance(raw, Mapping):
                continue
            item = dict(raw)
            source_id = item.get("source_id") or item.get("effect_id")
            effect_id = str(source_id or f"browser:{screen_index}:effect:{len(result)}")
            if not source_id:
                effect_id = f"browser:{screen_index}:{effect_id}"
            item["effect_id"] = effect_id
            item["source_id"] = str(source_id or effect_id)
            item["screen_index"] = screen_index
            if effect_id not in seen:
                result.append(item)
                seen.add(effect_id)
        nodes = data.get("nodes") if isinstance(data.get("nodes"), list) else []
        for node in nodes:
            if not isinstance(node, Mapping):
                continue
            style = node.get("style") if isinstance(node.get("style"), Mapping) else {}
            rect = node.get("rect") if isinstance(node.get("rect"), Mapping) else {}
            node_index = node.get("index", len(result))
            for key, kind in style_keys.items():
                value = str(style.get(key, "") or "").strip()
                if not value or value in {"none", "none none"}:
                    continue
                effect_id = f"browser:{screen_index}:node:{node_index}:{kind}"
                if effect_id in seen:
                    continue
                result.append({
                    "effect_id": effect_id,
                    "source_id": str(node.get("identity") or effect_id),
                    "kind": kind,
                    "value": value,
                    "bounds": [
                        _number(rect.get("x")), _number(rect.get("y")),
                        _number(rect.get("width")), _number(rect.get("height")),
                    ],
                    "screen_index": screen_index,
                    "node_index": node_index,
                })
                seen.add(effect_id)
    # ``capture_layout`` puts this at the root, not inside data.
    return result


def merge_runtime_evidence(
    model: ReactProjectModel,
    browser_evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge source facts with generic browser facts, preferring runtime data."""
    browser = browser_evidence if browser_evidence is not None else model.browser_evidence
    browser_dict = dict(browser) if isinstance(browser, Mapping) else {}
    records = _screen_records(browser_dict)
    # Root-level visual_effects is the canonical capture-layout location.
    root_effects = browser_dict.get("visual_effects")
    for record in records:
        if record.get("index") == 0 and isinstance(root_effects, list):
            record["root_visual_effects"] = root_effects
    internal_scrolls: list[dict[str, Any]] = []
    for screen in records:
        data = screen.get("data") if isinstance(screen.get("data"), Mapping) else {}
        nodes = data.get("nodes") if isinstance(data.get("nodes"), list) else []
        for node_index, raw in enumerate(nodes):
            if not isinstance(raw, Mapping):
                continue
            has_scroll, evidence_kind = _node_internal_scroll(raw)
            if not has_scroll:
                continue
            identity = raw.get("identity")
            attrs = raw.get("attrs") if isinstance(raw.get("attrs"), Mapping) else {}
            identity = identity or attrs.get("data-openhmi-id") or attrs.get("data-figma-node-id") or attrs.get("id")
            internal_scrolls.append({
                "id": str(identity or f"browser:{screen.get('index', 0)}:scroll:{node_index}"),
                "screen_index": screen.get("index", 0),
                "node_index": node_index,
                "evidence": evidence_kind,
            })
    effects = _browser_effects(records)
    # A valid browser receipt with zero semantic nodes is still browser input;
    # its zero counts must remain visible to the completeness gate.
    browser_present = bool(browser_dict) and bool(records or root_effects is not None)
    host_overflow = [_host_overflow(
        screen.get("data") if isinstance(screen.get("data"), Mapping) else {}
    ) for screen in records]
    return {
        "mode": "runtime-first" if browser_present else "source-fallback",
        "browser_present": browser_present,
        "screens": records,
        "internal_scrolls": internal_scrolls,
        "visual_effects": effects,
        "host_overflow": host_overflow,
        "counts": {
            "Screen": len(records) if browser_present else None,
            "Scroll": len(internal_scrolls) if browser_present else None,
            "VisualEffect": len(effects) if browser_present else None,
        },
    }


def _map_support(decision: CapabilityDecision | None) -> AgentSupport:
    if decision is None:
        return "unsupported"
    if decision.support == "native":
        return "native" if decision.confidence >= 0.8 else "partial"
    if decision.support == "fallback":
        return "fallback"
    if decision.support == "unsupported":
        return "unsupported"
    if decision.adapter in {"GaugeAdapter", "GaugeSceneAdapter", "VisualEffectAdapter"}:
        return "custom"
    return "partial"


def _primitive_names(effect: VisualEffectIR, model: ReactProjectModel) -> set[str]:
    primitives = list(effect.primitives)
    if not primitives and effect.filter_id:
        linked = next((item for item in model.visual_effects.values()
                       if item.kind == "filter" and item.filter_id == effect.filter_id), None)
        if linked is not None:
            primitives = list(linked.primitives)
    return {str(item.get("name", "")).casefold() for item in primitives if isinstance(item, Mapping)}


def _effect_decision(
    effect: VisualEffectIR,
    model: ReactProjectModel,
    current: CapabilityDecision | None,
) -> tuple[AgentSupport, str, list[str]]:
    kind = str(effect.kind).casefold()
    evidence = list(dict.fromkeys(effect.evidence_ids or [effect.id]))
    primitive_names = _primitive_names(effect, model)
    known_filter_primitives = {"fegaussianblur", "femerge", "femergenode"}
    if kind in {"filter", "blur"}:
        unknown = primitive_names - known_filter_primitives
        if unknown:
            return (
                "unsupported",
                f"SVG filter contains unknown primitive(s): {', '.join(sorted(unknown))}; no target behavior is guessed.",
                evidence,
            )
        if {"fegaussianblur", "femerge"} <= primitive_names:
            return (
                "custom",
                "SVG feGaussianBlur + feMerge is retained as an auditable custom glow recipe with source geometry.",
                evidence,
            )
        if primitive_names == {"fegaussianblur"}:
            return (
                "partial",
                "SVG Gaussian blur is known, but the merge/compositing recipe is incomplete; preserve it as partial evidence.",
                evidence,
            )
        if effect.filter_id and "glow" in str(effect.filter_id).casefold():
            return "custom", "Named glow filter is source-backed and maps to a custom layered effect recipe.", evidence
    if kind == "clip":
        support: AgentSupport = "custom" if effect.clip_path or effect.mask_path else "partial"
        return support, "clipPath is preserved as source evidence; the target needs a bounded custom/partial clip composition.", evidence
    if kind == "mask":
        return "partial", "Mask evidence is retained for a bounded composition pass; exact target semantics are not assumed.", evidence
    mapped = _map_support(current)
    reason = current.reason if current is not None and current.reason else "Source visual-effect evidence was retained by the capability planner."
    return mapped, reason, evidence


def _screen_nodes(model: ReactProjectModel) -> list[tuple[str, str, list[str], dict[str, Any]]]:
    from generator.screen_ir import build_screen_ir, reachable_components

    nodes = []
    for screen in build_screen_ir(model, reachable_components(model)):
        nodes.append((
            f"screen:{screen.id}",
            "Screen",
            [f"source:screen:{screen.id}", f"source:{screen.root_component}"],
            {"route": screen.route, "root": screen.root_component, "name": screen.name},
        ))
    return nodes


class RuntimeFirstPlanningAgent:
    """Default deterministic local implementation of :class:`PlanningAgent`."""

    name = "runtime-first-local"

    def plan(
        self,
        model: ReactProjectModel,
        *,
        browser_evidence: Mapping[str, Any] | None = None,
        screen_tree: Mapping[str, Any] | None = None,
    ) -> AgentPlanningReport:
        from .capability import reachable_component_ids, source_browser_plan_counts

        runtime = merge_runtime_evidence(model, browser_evidence)
        source_counts = source_browser_plan_counts(model)["source"]
        decisions: list[PlanningDecision] = []
        covered = Counter()

        source_screen_nodes = _screen_nodes(model)
        for node_id, kind, evidence, properties in source_screen_nodes:
            decisions.append(PlanningDecision(
                node_id=node_id, kind=kind, support="custom",
                reason="Screen route and root are retained from the generated screen tree.",
                evidence=list(dict.fromkeys(evidence)), properties=properties,
            ))
            covered[kind] += 1
        runtime_screen_count = len(runtime["screens"])
        for index in range(len(source_screen_nodes), runtime_screen_count):
            reason = "Runtime capture contains a Screen scenario with no reachable source screen node."
            decisions.append(PlanningDecision(
                node_id=f"browser:screen:{index}", kind="Screen", support="unsupported",
                reason=reason, evidence=[f"browser:screen:{index}"],
                blockers=[reason], properties={"screen_index": index},
            ))

        reachable = set(reachable_component_ids(model))
        for component_id in reachable:
            component = model.components[component_id]
            current = model.capability_plan.get(component_id)
            support = _map_support(current)
            reason = current.reason if current is not None else "Reachable source component has no capability decision."
            evidence = list(dict.fromkeys((current.evidence_ids if current else []) or [f"source:{component_id}"]))
            blockers = []
            if support == "unsupported":
                blockers.append(f"{component_id}: {reason}")
            decisions.append(PlanningDecision(
                node_id=component_id, kind="Component", support=support,
                reason=reason, evidence=evidence, blockers=blockers,
                source_id=component_id,
                properties={"component_kind": component.kind, "adapter": current.adapter if current else ""},
            ))

        # Capability decisions for concrete controls are source nodes too.
        for target_id, current in model.capability_plan.items():
            if target_id in model.components or target_id in model.visual_effects:
                continue
            if "#control_" not in target_id:
                continue
            support = _map_support(current)
            blockers = [f"{target_id}: {current.reason}"] if support == "unsupported" else []
            decisions.append(PlanningDecision(
                node_id=target_id, kind="Control", support=support,
                reason=current.reason, evidence=list(current.evidence_ids), blockers=blockers,
                source_id=current.source_id, properties={"adapter": current.adapter},
            ))

        scroll_sources = [component for component_id, component in model.components.items()
                          if component_id in reachable and component.props.get("scroll")]
        runtime_scrolls = list(runtime["internal_scrolls"])
        for index, component in enumerate(scroll_sources):
            current = model.capability_plan.get(component.id)
            evidence = list(dict.fromkeys((current.evidence_ids if current else []) or [f"source:{component.id}"]))
            properties = {"axis": (component.props.get("scroll") or {}).get("axis")}
            if index < len(runtime_scrolls):
                runtime_item = runtime_scrolls[index]
                evidence.append(f"browser:scroll:{runtime_item['id']}")
                properties["runtime_scroll"] = runtime_item
            support = "native" if current is None or current.support == "native" else _map_support(current)
            decisions.append(PlanningDecision(
                node_id=f"scroll:{component.id}", kind="Scroll", support=support,
                reason="Internal node scroll is source-backed and host overflow is not used as its evidence.",
                evidence=list(dict.fromkeys(evidence)), source_id=component.id, properties=properties,
            ))
            if support != "unsupported":
                covered["Scroll"] += 1
        for index in range(len(scroll_sources), len(runtime_scrolls)):
            item = runtime_scrolls[index]
            reason = "Runtime capture contains an internal Scroll node with no reachable source node."
            decisions.append(PlanningDecision(
                node_id=f"browser:scroll:{item['id']}", kind="Scroll", support="unsupported",
                reason=reason, evidence=[f"browser:scroll:{item['id']}"],
                blockers=[reason], properties=item,
            ))

        effects = list(model.visual_effects.values())
        runtime_effects = list(runtime["visual_effects"])
        used_runtime: set[int] = set()
        for effect in effects:
            current = model.capability_plan.get(effect.id)
            support, reason, evidence = _effect_decision(effect, model, current)
            blockers = [f"{effect.id}: {reason}"] if support == "unsupported" else []
            matching_index = None
            for index, item in enumerate(runtime_effects):
                if index in used_runtime:
                    continue
                if str(item.get("source_id")) == effect.id or str(item.get("effect_id")) == effect.id:
                    matching_index = index
                    break
                if str(item.get("kind", "")).casefold() == str(effect.kind).casefold() and matching_index is None:
                    matching_index = index
            if matching_index is not None:
                used_runtime.add(matching_index)
                evidence.append(f"browser:visual-effect:{runtime_effects[matching_index].get('effect_id', matching_index)}")
            decisions.append(PlanningDecision(
                node_id=effect.id, kind="VisualEffect", support=support,
                reason=reason, evidence=list(dict.fromkeys(evidence)), blockers=blockers,
                source_id=effect.target_component, properties={
                    "effect_kind": effect.kind,
                    "filter_id": effect.filter_id,
                    "primitive_names": sorted(_primitive_names(effect, model)),
                    "runtime_match": runtime_effects[matching_index] if matching_index is not None else None,
                },
            ))
            if support != "unsupported":
                covered["VisualEffect"] += 1
        for index, item in enumerate(runtime_effects):
            if index in used_runtime:
                continue
            reason = "Runtime capture contains a VisualEffect with no source-backed effect node."
            decisions.append(PlanningDecision(
                node_id=str(item.get("effect_id") or f"browser:visual-effect:{index}"),
                kind="VisualEffect", support="unsupported", reason=reason,
                evidence=[f"browser:visual-effect:{item.get('effect_id', index)}"],
                blockers=[reason], properties=dict(item),
            ))

        browser_counts = runtime["counts"]
        planned_counts: dict[str, int] = {
            category: int(covered.get(category, 0)) for category in PLANNING_CATEGORIES
        }
        counts: dict[str, dict[str, int | None]] = {}
        for category in PLANNING_CATEGORIES:
            counts[category] = {
                "source": int(source_counts.get(category, 0) or 0),
                "browser": browser_counts.get(category),
                "planned": planned_counts[category],
            }

        blockers: list[str] = []
        for category in PLANNING_CATEGORIES:
            source_count = counts[category]["source"] or 0
            planned_count = counts[category]["planned"] or 0
            browser_count = counts[category]["browser"]
            if source_count > planned_count:
                blockers.append(
                    f"Completeness blocker: {category} source={source_count} > planned={planned_count}"
                )
            if isinstance(browser_count, int) and browser_count > planned_count:
                blockers.append(
                    f"Completeness blocker: {category} browser={browser_count} > planned={planned_count}"
                )
        for decision in decisions:
            blockers.extend(decision.blockers)
        blockers = list(dict.fromkeys(blockers))
        warnings: list[str] = []
        if not runtime["browser_present"]:
            warnings.append("No browser/layout receipt found; planning uses source evidence only.")
        if runtime["host_overflow"]:
            warnings.append("Host body/html overflow is recorded for diagnostics and excluded from internal Scroll counts.")
        unsupported = sum(item.support == "unsupported" for item in decisions)
        partial = sum(item.support == "partial" for item in decisions)
        custom = sum(item.support == "custom" for item in decisions)
        report = AgentPlanningReport(
            status="blocked" if blockers else "ready",
            enabled=True,
            mode=self.name,
            evidence_mode=runtime["mode"],
            counts=counts,
            decisions=decisions,
            blockers=blockers,
            warnings=warnings,
            summary={
                "decision_count": len(decisions),
                "custom": custom,
                "partial": partial,
                "unsupported": unsupported,
                "blocker_count": len(blockers),
                "host_overflow": runtime["host_overflow"],
                "screen_tree_schema": (screen_tree or {}).get("schema") if isinstance(screen_tree, Mapping) else None,
            },
        )
        return report


def run_planning_agent(
    model: ReactProjectModel,
    *,
    browser_evidence: Mapping[str, Any] | None = None,
    enabled: bool = True,
    agent: PlanningAgent | None = None,
) -> dict[str, Any]:
    """Run the configured planner and attach a JSON-compatible audit summary."""
    if browser_evidence is not None:
        model.browser_evidence = dict(browser_evidence)
    if not enabled:
        report = AgentPlanningReport(
            status="disabled", enabled=False, mode="runtime-first-local",
            warnings=["Agent planning is disabled by configuration."],
        )
    else:
        selected = agent or RuntimeFirstPlanningAgent()
        report = selected.plan(model, browser_evidence=model.browser_evidence)
    model.agent_planning = report.to_dict()
    return model.agent_planning


# Friendly aliases for callers that use either the architecture name or the
# concrete implementation name.
LocalPlanningAgent = RuntimeFirstPlanningAgent
build_agent_plan = run_planning_agent


__all__ = [
    "AgentPlanningReport", "AgentSupport", "LocalPlanningAgent",
    "PlanningAgent", "PlanningDecision", "RuntimeFirstPlanningAgent",
    "build_agent_plan", "load_browser_evidence", "merge_runtime_evidence",
    "run_planning_agent",
]
