"""Evidence-driven capability and render planning.

The scanner records source facts only.  This pass is the first place where a
target strategy is chosen, which keeps renderer-specific decisions out of the
source evidence layer and makes unresolved evidence visible to preflight.
"""
from __future__ import annotations

from collections import deque
from dataclasses import asdict
import re
from typing import Any

from .model import (
    CapabilityDecision,
    Component,
    ReactProjectModel,
    RenderPlan,
    RenderPlanItem,
    VisualEffectIR,
)


KNOWN_CONTROL_KINDS = {
    "button", "label", "image", "switch", "checkbox", "radio",
    "radio_group", "slider", "input", "list", "tabs", "select",
}


def _component_name(component_id: str) -> str:
    return component_id.rsplit(":", 1)[-1]


def _gauge_groups(component: Component) -> list[dict[str, Any]]:
    """Build ordered, source-backed paint groups for each logical gauge."""
    props = component.props or {}
    paints = [item for item in props.get("arc_paints", []) if isinstance(item, dict)]
    records = [item for item in props.get("svg_arcs", []) if isinstance(item, dict)]
    gauges = [item for item in props.get("gauges", []) if isinstance(item, dict)]
    if not paints:
        return []
    gauge_ids = list(dict.fromkeys(str(item.get("gauge_id") or item.get("gauge") or "gauge_1") for item in paints))
    groups: list[dict[str, Any]] = []
    for gauge_id in gauge_ids:
        source_layers = [dict(item) for item in paints if str(item.get("gauge_id") or item.get("gauge")) == gauge_id]
        source_layers.sort(key=lambda item: (int(item.get("layer_order", 0)), int(item.get("source_offset", 0))))
        record = next((item for item in records if str(item.get("gauge_id")) == gauge_id), {})
        gauge = next((item for item in gauges if str(item.get("gauge_id")) == gauge_id), {})
        geometry = record.get("geometry") or next((item.get("geometry") for item in source_layers if item.get("geometry")), None)
        layers: list[dict[str, Any]] = []
        for source in source_layers:
            source["gauge_id"] = gauge_id
            source["source_geometry"] = geometry
            role = str(source.get("role", "track"))
            filter_id = str(source.get("filter_id") or "").casefold()
            if role == "glow-soft" or (role == "foreground" and filter_id == "glow-soft"):
                soft = dict(source)
                soft["role"] = "glow-soft"
                soft["source_role"] = role
                soft["layer_order"] = len(layers)
                layers.append(soft)
                foreground = dict(source)
                foreground["filter_id"] = None
                foreground["blur"] = None
                foreground["role"] = "foreground"
                foreground["source_role"] = "foreground"
                foreground["layer_order"] = len(layers)
                layers.append(foreground)
            else:
                source["layer_order"] = len(layers)
                layers.append(source)
        value_binding = next((str(item.get("value_binding")) for item in layers if item.get("value_binding")), None)
        source_colors = dict(gauge.get("colors") if isinstance(gauge.get("colors"), dict) else {})
        groups.append({
            "gauge_id": gauge_id,
            "geometry": geometry,
            "source_geometry": geometry,
            "range": {
                "min": gauge.get("min", "0"), "max": gauge.get("max", "100"),
                "value": gauge.get("value", "0"),
            },
            "colors": source_colors,
            "value_binding": value_binding,
            "layers": layers,
            "source_layers": source_layers,
        })
    return groups


def reachable_component_ids(model: ReactProjectModel) -> list[str]:
    """Resolve the source graph without importing a renderer module."""
    roots = [item for item in model.entry_components if item in model.components]
    if not roots:
        roots = [item for item in model.components if _component_name(item) == "App"]
    if not roots:
        return list(model.components)
    seen: set[str] = set()
    queue: deque[str] = deque(dict.fromkeys(roots))
    while queue:
        current = queue.popleft()
        if current in seen or current not in model.components:
            continue
        seen.add(current)
        queue.extend(model.component_graph.get(current, []))
    # A reusable Gauge implementation is source evidence for the Gauge uses
    # found at its parent call site, not a second on-screen instrument.  The
    # structural graph tells us it is a declaration child, so do not count it
    # as an additional output item.
    return [item for item in model.components if item in seen and not (
        model.components[item].props.get("_instantiated_as_template")
        or (_component_name(item) == "Gauge" and item not in roots)
    )]


def _local_bounds(effect: VisualEffectIR, browser_effects: list[dict[str, Any]]) -> list[float] | None:
    if isinstance(effect.bounds, list) and len(effect.bounds) == 4:
        try:
            values = [float(value) for value in effect.bounds]
            if values[2] > 0 and values[3] > 0:
                return values
        except (TypeError, ValueError):
            pass
    for item in browser_effects:
        if item.get("source_id") == effect.id or item.get("effect_id") == effect.id:
            bounds = item.get("bounds")
            if isinstance(bounds, list) and len(bounds) == 4:
                try:
                    values = [float(value) for value in bounds]
                    if values[2] > 0 and values[3] > 0:
                        return values
                except (TypeError, ValueError):
                    pass
    # Source CSS/SVG evidence and browser evidence intentionally use separate
    # ids.  When a source effect has no direct DOM id, a same-kind browser
    # observation is still useful for a local crop; never use it for a native
    # decision.  The renderer only consumes this path for ``fallback`` items.
    effect_kind = str(effect.kind).casefold()
    kind_matches = [item for item in browser_effects
                    if str(item.get("kind", "")).casefold() == effect_kind]
    if effect_kind == "filter":
        kind_matches.extend(item for item in browser_effects
                            if str(item.get("kind", "")).casefold() == "blur")
    for item in kind_matches:
        bounds = item.get("bounds")
        if isinstance(bounds, list) and len(bounds) == 4:
            try:
                values = [float(value) for value in bounds]
                if values[2] > 0 and values[3] > 0:
                    return values
            except (TypeError, ValueError):
                pass
    return None


def _canvas_area(model: ReactProjectModel, browser: dict[str, Any]) -> float | None:
    viewport = browser.get("viewport") or browser.get("data", {}).get("viewport")
    if isinstance(viewport, dict):
        try:
            return float(viewport.get("width", 0)) * float(viewport.get("height", 0))
        except (TypeError, ValueError):
            return None
    for screen in model.screens:
        if screen.width > 0 and screen.height > 0:
            return float(screen.width * screen.height)
    return None


def _safe_fallback_bounds(
    effect: VisualEffectIR,
    model: ReactProjectModel,
    browser: dict[str, Any],
) -> list[float] | None:
    bounds = _local_bounds(effect, list(browser.get("visual_effects", [])))
    if bounds is None:
        return None
    area = _canvas_area(model, browser)
    if area and bounds[2] * bounds[3] >= area * 0.85:
        # A full-canvas raster is explicitly forbidden.  The caller will turn
        # this into an unsupported blocker instead of silently exporting it.
        return None
    return [round(value, 2) for value in bounds]


def _effect_decision(
    effect: VisualEffectIR,
    model: ReactProjectModel,
    browser: dict[str, Any],
) -> CapabilityDecision:
    evidence = list(dict.fromkeys(effect.evidence_ids or [effect.id]))
    kind = str(effect.kind).casefold()
    fallback = _safe_fallback_bounds(effect, model, browser)
    props = dict(effect.properties)
    props.update({
        "effect_kind": kind,
        "filter_id": effect.filter_id,
        "blur_radius": effect.blur_radius,
        "bounds": effect.bounds,
    })

    if kind == "glow":
        if str(effect.filter_id or "").casefold() == "text-glow":
            return CapabilityDecision(
                target_id=effect.id,
                support="compound",
                adapter="VisualEffectAdapter",
                confidence=0.98,
                evidence_ids=evidence,
                reason="Text glow is kept as a text effect and is excluded from Gauge Arc synchronization groups.",
                properties=props,
            )
        return CapabilityDecision(
            target_id=effect.id,
            support="compound",
            adapter="VisualEffectAdapter",
            confidence=0.97,
            evidence_ids=evidence,
            reason="Arc glow is source-backed and can be represented by synchronized layered arcs; keep the foreground dynamic.",
            properties=props,
        )
    if kind == "blur":
        if effect.filter_id and "glow" in str(effect.filter_id).casefold():
            return CapabilityDecision(
                target_id=effect.id,
                support="compound",
                adapter="VisualEffectAdapter",
                confidence=0.97,
                evidence_ids=evidence,
                reason="Gaussian blur is part of a source-backed glow filter; render synchronized layers around the dynamic arc.",
                properties=props,
            )
        if fallback:
            return CapabilityDecision(
                target_id=effect.id,
                support="fallback",
                adapter="VisualEffectAdapter",
                confidence=0.91,
                evidence_ids=evidence,
                reason="The target has no equivalent real blur; crop only the measured local transparent region.",
                fallback_bounds=fallback,
                properties=props,
            )
        return CapabilityDecision(
            target_id=effect.id,
            support="unsupported",
            adapter="VisualEffectAdapter",
            confidence=0.94,
            evidence_ids=evidence,
            reason="Real blur has no measured local bounds; a full-screen raster fallback is forbidden.",
            properties=props,
        )
    if kind == "filter":
        if props.get("drop_shadow"):
            return CapabilityDecision(
                target_id=effect.id,
                support="compound",
                adapter="VisualEffectAdapter",
                confidence=0.88,
                evidence_ids=evidence,
                reason="CSS drop-shadow is source-backed but has no exact LVGL equivalent; report a bounded partial/fallback shadow layer.",
                fallback_bounds=fallback,
                properties={**props, "fallback": "drop-shadow"},
            )
        if effect.filter_id and "glow" in str(effect.filter_id).casefold():
            return CapabilityDecision(
                target_id=effect.id,
                support="compound",
                adapter="VisualEffectAdapter",
                confidence=0.97,
                evidence_ids=evidence,
                reason="Named glow filter is linked to SVG arc evidence and is represented by layered native geometry.",
                properties=props,
            )
        primitive_names = {str(item.get("name", "")).casefold() for item in effect.primitives}
        # A blur plus merge is a bounded, auditable glow recipe.  Keep it as a
        # compound effect even without a browser crop; only unknown filter
        # primitives should become blockers.
        known_filter_primitives = {"fegaussianblur", "femerge", "femergenode"}
        if {"fegaussianblur", "femerge"} <= primitive_names and primitive_names <= known_filter_primitives:
            return CapabilityDecision(
                target_id=effect.id,
                support="compound",
                adapter="VisualEffectAdapter",
                confidence=0.93,
                evidence_ids=evidence,
                reason="SVG feGaussianBlur + feMerge is a source-backed glow recipe; preserve it as a custom layered effect.",
                properties={**props, "glow_recipe": "feGaussianBlur+feMerge"},
            )
        if effect.filter_id and primitive_names <= {"fegaussianblur", "femerge", "femergenode"} and fallback:
            return CapabilityDecision(
                target_id=effect.id,
                support="fallback",
                adapter="VisualEffectAdapter",
                confidence=0.9,
                evidence_ids=evidence,
                reason="The SVG filter is limited to blur primitives; use a local crop while preserving source geometry.",
                fallback_bounds=fallback,
                properties=props,
            )
        return CapabilityDecision(
            target_id=effect.id,
            support="unsupported",
            adapter="VisualEffectAdapter",
            confidence=0.96,
            evidence_ids=evidence,
            reason="SVG filter primitives are not in the supported effect subset and cannot be guessed safely.",
            properties=props,
        )
    if kind == "gradient":
        return CapabilityDecision(
            target_id=effect.id,
            support="compound",
            adapter="VisualEffectAdapter",
            confidence=0.86,
            evidence_ids=evidence,
            reason="Gradient is source-backed; preserve it as a bounded composited layer rather than a plain color.",
            properties=props,
        )
    if kind in {"shadow", "mask", "clip"}:
        return CapabilityDecision(
            target_id=effect.id,
            support="compound",
            adapter="VisualEffectAdapter",
            confidence=0.84,
            evidence_ids=evidence,
            reason=f"{kind} is known source evidence and needs a bounded composition layer.",
            properties=props,
        )
    return CapabilityDecision(
        target_id=effect.id,
        support="unsupported",
        adapter="VisualEffectAdapter",
        confidence=0.95,
        evidence_ids=evidence,
        reason="Unknown visual effect kind; no target strategy is inferred.",
        properties=props,
    )


def _component_decision(component: Component, model: ReactProjectModel) -> CapabilityDecision:
    props = component.props or {}
    evidence = [f"source:{component.id}"]
    kind = component.kind.casefold()
    effects = [effect for effect in model.visual_effects.values()
               if effect.target_component == component.id or
               (effect.target_component is None and effect.source_file == component.source_file)]
    gauge_groups = _gauge_groups(component)
    scene = props.get("gauge_scene") if isinstance(props.get("gauge_scene"), dict) else None
    if scene:
        evidence.extend(str(item) for item in props.get("source_evidence_ids", []) if item)
        gauges = scene.get("gauges") if isinstance(scene.get("gauges"), list) else []
        return CapabilityDecision(
            component.id, "compound", "GaugeSceneAdapter", .99, list(dict.fromkeys(evidence)),
            "One fixed-canvas composite gauge scene preserves all source arcs, ticks, readouts, redline and surrounding dashboard paint.",
            properties={"scene": scene, "gauge_count": len(gauges), "arc_count": len(gauges)},
        )
    svg_arc_count = len(props.get("svg_arcs", [])) if isinstance(props.get("svg_arcs"), list) else 0
    call_gauge_count = sum(1 for item in props.get("gauges", [])
                           if isinstance(item, dict) and item.get("instance"))
    arc_count = (svg_arc_count + call_gauge_count if svg_arc_count and call_gauge_count
                 else svg_arc_count or len(props.get("arcs", [])) or len(props.get("gauges", [])))
    if kind == "gauge" or arc_count:
        support = "compound" if gauge_groups or arc_count > 1 or any(effect.kind == "glow" for effect in effects) else "native"
        reason = "Each source gauge has an ordered synchronized track/glow/foreground paint group." if gauge_groups else ("Multiple source arcs or glow layers require a compound synchronized arc render." if support == "compound" else "Gauge geometry and value semantics are directly represented.")
        return CapabilityDecision(component.id, support, "GaugeAdapter", .98, evidence, reason,
                                  properties={"arc_count": max(arc_count, 1), "gauge_groups": gauge_groups})
    if kind == "chart":
        chart = props.get("chart") if isinstance(props.get("chart"), dict) else {}
        has_data = bool(chart.get("data") or chart.get("series") or props.get("series"))
        return CapabilityDecision(component.id, "native" if has_data else "compound", "RechartsAdapter",
                                  .93 if has_data else .78, evidence,
                                  "Chart data and type are source-backed." if has_data else "Chart structure is known but its runtime data is unresolved.")
    if props.get("table"):
        return CapabilityDecision(component.id, "native", "TableAdapter", .94, evidence, "Table structure is source-backed.")
    if props.get("selects"):
        return CapabilityDecision(component.id, "native", "DropdownAdapter", .94, evidence, "Select/menu options are source-backed.")
    if props.get("scroll"):
        return CapabilityDecision(component.id, "native", "ScrollContainerAdapter", .92, evidence, "Overflow direction is source-backed.")
    if props.get("controls"):
        return CapabilityDecision(component.id, "compound", "GenericControlsAdapter", .9, evidence, "The component contains multiple concrete control intents.")
    if model.component_graph.get(component.id):
        return CapabilityDecision(component.id, "compound", "ScreenCompositionAdapter", .72, evidence, "Container composition is preserved from the source graph.")
    return CapabilityDecision(component.id, "native", "SourceComponent", .55, evidence, "No unsupported target-specific evidence was found.")


def _control_decisions(component: Component) -> list[CapabilityDecision]:
    result: list[CapabilityDecision] = []
    for index, raw in enumerate(component.props.get("controls", []) if isinstance(component.props.get("controls"), list) else [], 1):
        if not isinstance(raw, dict):
            continue
        kind = str(raw.get("kind", "unknown")).casefold()
        if kind in {"gauge", "icon_tile"}:
            # These are consumed by the component-level geometry/snapshot
            # pass, not by the generic control adapter.
            continue
        target = f"{component.id}#control_{index}_{kind}"
        evidence = [f"source:{component.id}:{raw.get('offset', 0)}"]
        if kind not in KNOWN_CONTROL_KINDS:
            result.append(CapabilityDecision(target, "unsupported", "GenericControlsAdapter", .9, evidence,
                                             f"Control kind {kind} is outside the adapter whitelist."))
        elif kind == "image" and not raw.get("resource_id"):
            result.append(CapabilityDecision(target, "unsupported", "GenericControlsAdapter", .93, evidence,
                                             "Image source is dynamic or unresolved; no blank native image is emitted."))
        else:
            result.append(CapabilityDecision(target, "native", "GenericControlsAdapter", .9, evidence,
                                             f"Concrete {kind} control has a registered structural mapping."))
    return result


def _browser_counts(browser: dict[str, Any]) -> dict[str, int | None]:
    if not browser:
        return {"Screen": None, "Gauge": None, "Arc": None, "Navigation": None, "Scroll": None, "VisualEffect": None}
    screens = browser.get("screens") if isinstance(browser.get("screens"), list) else []
    nodes = [node for screen in screens for node in (screen.get("data", {}).get("nodes", []) if isinstance(screen, dict) else [])]
    gauges = 0
    arcs = 0
    scrolls = 0
    effects = len(browser.get("visual_effects", [])) if isinstance(browser.get("visual_effects"), list) else 0
    for node in nodes:
        if not isinstance(node, dict):
            continue
        tag = str(node.get("tag", "")).casefold()
        # The browser host can be taller/wider than the fixed HMI canvas. Its
        # document overflow is evidence about the host, never an LVGL scroll
        # container. Only node-local evidence can contribute to Scroll.
        if tag in {"html", "body"}:
            continue
        attrs = node.get("attrs") if isinstance(node.get("attrs"), dict) else {}
        if tag == "svg":
            # A logical gauge is one SVG, regardless of how many circle/path
            # paint layers its outerHTML contains.  Browser attrs remain a
            # compatibility fallback for older captures without outerHTML.
            outer = str(node.get("outerHTML") or node.get("svg") or "")
            dashed = bool(re.search(r"stroke-dash(?:array|offset)", outer, re.I))
            dashed = dashed or any(key in attrs for key in ("stroke-dasharray", "stroke-dashoffset"))
            if dashed or str(node.get("role", "")).casefold() == "meter":
                # Prefer explicit semantic identities when one SVG contains
                # several instruments (radial + side gauges). Paint layers
                # sharing an id remain one group; an unlabelled SVG keeps the
                # historical one-gauge fallback.
                identities = re.findall(
                    r"(?:data-openhmi-id|data-figma-node-id|aria-label|id)\s*=\s*[\"']([^\"']+)[\"']",
                    outer, re.I,
                )
                semantic = []
                for identity in identities:
                    if re.search(r"(?:gauge|meter|arc|radial)", identity, re.I):
                        key = re.sub(r"(?:[-_:](?:track|glow|foreground|indicator|path|circle))$", "", identity, flags=re.I)
                        if key not in semantic:
                            semantic.append(key)
                count = len(semantic) if len(semantic) > 1 else 1
                gauges += count
                arcs += count
        style = node.get("style") if isinstance(node.get("style"), dict) else {}
        overflow = node.get("overflow") if isinstance(node.get("overflow"), dict) else None
        if overflow is not None:
            scrolls += int(bool(overflow.get("hasOverflow")))
        elif str(style.get("overflow", "")).casefold() in {"auto", "scroll"} or str(style.get("overflowY", "")).casefold() in {"auto", "scroll"} or str(style.get("overflowX", "")).casefold() in {"auto", "scroll"}:
            # Older captures lack extents; retain compatibility for them.
            scrolls += 1
    return {"Screen": len(screens), "Gauge": gauges, "Arc": arcs,
            "Navigation": len(browser.get("navigation_verification", {}).get("verified", [])),
            "Scroll": scrolls, "VisualEffect": effects}


def plan_capabilities(
    model: ReactProjectModel,
    browser_evidence: dict[str, Any] | None = None,
    *,
    include_unreachable: bool = False,
) -> dict[str, CapabilityDecision]:
    """Populate and return the model's capability and render plans."""
    if browser_evidence is not None:
        model.browser_evidence = browser_evidence
    from .identity import bind_instances
    bind_instances(model, model.browser_evidence)
    browser = model.browser_evidence or {}
    model.capability_plan.clear()
    selected = list(model.components) if include_unreachable else reachable_component_ids(model)
    for component_id in selected:
        component = model.components[component_id]
        decision = _component_decision(component, model)
        model.capability_plan[decision.target_id] = decision
        if not isinstance(component.props.get("gauge_scene"), dict):
            for child in _control_decisions(component):
                model.capability_plan[child.target_id] = child
    for effect in model.visual_effects.values():
        decision = _effect_decision(effect, model, browser)
        model.capability_plan[decision.target_id] = decision

    plan = RenderPlan()
    for decision in model.capability_plan.values():
        operation = {
            "native": "native",
            "compound": "compound",
            "fallback": "local-screenshot",
            "unsupported": "blocker",
        }[decision.support]
        if decision.adapter == "GaugeSceneAdapter":
            operation = "custom-draw-scene"
        if decision.properties.get("effect_kind") == "glow" and decision.support == "compound":
            operation = "effect-reference"
        plan.items.append(RenderPlanItem(
            id=decision.target_id,
            source_id=decision.source_id or decision.target_id,
            support=decision.support,
            adapter=decision.adapter,
            operation=operation,
            evidence_ids=list(decision.evidence_ids),
            fallback_bounds=decision.fallback_bounds,
            properties={**decision.properties, "reason": decision.reason, "confidence": decision.confidence},
        ))
        if decision.support == "unsupported" and decision.confidence >= .85:
            plan.blockers.append(f"{decision.target_id}: {decision.reason}")
    for component_id in selected:
        component = model.components[component_id]
        if component.props.get("gauge_scene"):
            continue
        for group in _gauge_groups(component):
            evidence_ids = list(component.props.get("source_evidence_ids", []))
            evidence_ids.extend(
                f"source:{component.source_file}:{item.get('source_offset', 0)}"
                for item in group.get("source_layers", []) if isinstance(item, dict)
            )
            group_id = str(group.get("gauge_id") or "gauge_1")
            plan.items.append(RenderPlanItem(
                id=f"{component.id}#arc-glow-{group_id}",
                source_id=component.id,
                support="compound",
                adapter="GaugeAdapter",
                operation="arc-glow-layers",
                evidence_ids=list(dict.fromkeys(evidence_ids)),
                properties={
                    "gauge_id": group_id,
                    "source_geometry": group.get("source_geometry"),
                    "geometry": group.get("geometry"),
                    "range": group.get("range", {}),
                    "value_binding": group.get("value_binding"),
                    "layers": group.get("layers", []),
                    "ordered_layers": group.get("layers", []),
                    "sync_group": f"{component.id}:{group_id}",
                    "reason": "All generated Arc layers share this gauge's source geometry, range, value and angle binding.",
                    "confidence": .99,
                },
            ))
    model.render_plan = plan
    return model.capability_plan


def capability_plan_dict(model: ReactProjectModel) -> dict[str, Any]:
    """Serialize the plan without exposing implementation-only objects."""
    return {
        "schema": "uagent.capability-plan/v1",
        "decisions": {key: asdict(value) for key, value in model.capability_plan.items()},
        "render_plan": asdict(model.render_plan),
    }


def high_confidence_blockers(model: ReactProjectModel) -> list[str]:
    plan = model.render_plan or RenderPlan()
    present = {item.id for item in plan.items}
    blockers = list(plan.blockers)
    for effect_id, effect in model.visual_effects.items():
        if effect_id not in present and effect.source_file and effect.source_offset >= 0:
            blockers.append(f"{effect_id}: high-confidence source visual effect is missing from RenderPlan")
    return list(dict.fromkeys(blockers))


def capability_candidates(model: ReactProjectModel, *, confidence_threshold: float = .8) -> list[dict[str, Any]]:
    """Expose only unresolved or low-confidence decisions to Agent mode."""
    candidates: list[dict[str, Any]] = []
    for decision in model.capability_plan.values():
        if decision.support != "unsupported" and decision.confidence >= confidence_threshold:
            continue
        candidates.append({
            "target_id": decision.target_id,
            "source_id": decision.source_id,
            "support": decision.support,
            "adapter": decision.adapter,
            "confidence": decision.confidence,
            "evidence_ids": list(decision.evidence_ids),
            "reason": decision.reason,
            "properties": dict(decision.properties),
        })
    return candidates


def source_browser_plan_counts(model: ReactProjectModel) -> dict[str, dict[str, int | None]]:
    """Return comparable source, browser, and plan counts for the UI/audit."""
    source = {"Screen": 0, "Gauge": 0, "Arc": 0, "Navigation": len(model.navigation),
              "Scroll": 0, "VisualEffect": len(model.visual_effects)}
    selected = set(reachable_component_ids(model))
    for component in model.components.values():
        if component.id not in selected:
            continue
        props = component.props or {}
        if props.get("_instantiated_as_template"):
            continue
        if _component_name(component.id) == "Gauge" and props.get("svg_arcs"):
            # Count the call-site instances, not the reusable SVG definition.
            continue
        scene = props.get("gauge_scene") if isinstance(props.get("gauge_scene"), dict) else None
        if scene and isinstance(scene.get("gauges"), list):
            gauge_count = len(scene["gauges"])
        else:
            arcs = len(props.get("svg_arcs", [])) if isinstance(props.get("svg_arcs"), list) else 0
            calls = sum(1 for item in props.get("gauges", [])
                        if isinstance(item, dict) and item.get("instance"))
            gauge_count = arcs + calls if arcs and calls else calls or arcs or (len(props.get("gauges", [])) if isinstance(props.get("gauges"), list) else 0)
        source["Gauge"] += gauge_count
        source["Arc"] += gauge_count
        source["Scroll"] += 1 if props.get("scroll") else 0
    try:
        # Keep the count aligned with the shared route contract without making
        # the core scanner import a renderer at module load time.
        from generator.screen_ir import build_screen_ir
        source["Screen"] = max(1, len(model.screens), len(build_screen_ir(model)))
    except (ImportError, OSError, RuntimeError):
        source["Screen"] = max(1, len(model.screens))
    plan = {key: 0 for key in source}
    for decision in model.capability_plan.values():
        if decision.support == "unsupported":
            continue
        if decision.adapter in {"GaugeAdapter", "GaugeSceneAdapter"}:
            plan["Gauge"] += int(decision.properties.get("arc_count", 1) or 1)
            plan["Arc"] += int(decision.properties.get("arc_count", 1) or 1)
        elif decision.adapter == "ScrollContainerAdapter":
            plan["Scroll"] += 1
        elif decision.target_id in model.visual_effects:
            plan["VisualEffect"] += 1
    plan["Screen"] = source["Screen"]
    plan["Navigation"] = len([edge for edge in model.navigation if edge.target_component or edge.status == "confirmed"])
    return {"source": source, "browser": _browser_counts(model.browser_evidence), "plan": plan}


# Friendly aliases for callers that use the noun from the architecture doc.
build_capability_plan = plan_capabilities
CapabilityPlanner = plan_capabilities
