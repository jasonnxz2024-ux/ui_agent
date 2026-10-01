"""Stable source-instance to browser-node binding."""
from __future__ import annotations
from typing import Any


def browser_identity(node: dict[str, Any]) -> tuple[str | None, float]:
    attrs = node.get("attrs") if isinstance(node.get("attrs"), dict) else {}
    value = attrs.get("data-openhmi-id") or attrs.get("data-figma-node-id") or attrs.get("id")
    if value:
        confidence = 1.0 if attrs.get("data-openhmi-id") else (0.95 if attrs.get("data-figma-node-id") else 0.85)
        return str(value), confidence
    return str(node.get("identity") or f"dom:{node.get('index', 0)}"), float(node.get("identity_confidence", .25))


def bind_instances(model, browser_evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    """Join only explicit semantic IDs; never infer a match from widget kind."""
    browser = browser_evidence if browser_evidence is not None else model.browser_evidence
    nodes: list[dict[str, Any]] = []
    for screen in (browser or {}).get("screens", []):
        nodes.extend(n for n in (screen.get("data", {}) or {}).get("nodes", []) if isinstance(n, dict))
    by_id: dict[str, list[dict[str, Any]]] = {}
    for node in nodes:
        identity, confidence = browser_identity(node)
        if identity and confidence >= .8:
            by_id.setdefault(identity, []).append(node)
    matched = ambiguous = 0
    for instance in model.instances.values():
        if not instance.semantic_id:
            continue
        candidates = by_id.get(instance.semantic_id, [])
        if len(candidates) == 1:
            node = candidates[0]
            identity, confidence = browser_identity(node)
            instance.browser_identity = identity
            rect = node.get("rect") if isinstance(node.get("rect"), dict) else {}
            instance.browser_bounds = [float(rect.get(k, 0)) for k in ("x", "y", "width", "height")]
            instance.browser_confidence = confidence
            instance.binding_status = "matched"
            matched += 1
        elif len(candidates) > 1:
            instance.binding_status = "ambiguous"
            ambiguous += 1
    unbound = sum(1 for i in model.instances.values() if i.semantic_id and i.binding_status == "unbound")
    summary = {"instances": len(model.instances), "semantic_instances": sum(bool(i.semantic_id) for i in model.instances.values()),
               "matched": matched, "ambiguous": ambiguous, "unbound": unbound,
               "browser_nodes": len(nodes), "status": "ok" if not ambiguous else "review"}
    model.binding_summary = summary
    return summary
