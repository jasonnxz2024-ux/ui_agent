"""Project-agnostic helpers for browser evidence and visual acceptance.

These helpers deliberately operate on serialized browser evidence rather than
project names or copy-specific coordinates.
"""
from __future__ import annotations

import hashlib
import math
import re
from typing import Any


DEFAULT_REGION_KINDS = ("header", "gauge", "control", "status", "background")


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def discover_canvas(data: dict[str, Any] | None, fallback: tuple[int, int] = (1024, 600)) -> dict[str, Any]:
    """Choose a canvas from viewport/root/SVG viewBox evidence.

    A measured root wins over a caller fallback. An SVG viewBox is used when
    it is the only explicit canvas evidence. Dimensions are never forced to a
    legacy device resolution.
    """
    data = data if isinstance(data, dict) else {}
    root = data.get("root") if isinstance(data.get("root"), dict) else {}
    viewport = data.get("viewport") if isinstance(data.get("viewport"), dict) else {}
    width = _number(root.get("width")) or _number(viewport.get("width")) or float(fallback[0])
    height = _number(root.get("height")) or _number(viewport.get("height")) or float(fallback[1])
    source = "root" if root.get("width") and root.get("height") else "viewport"
    if not root.get("width") or not root.get("height"):
        for node in data.get("nodes", []) if isinstance(data.get("nodes"), list) else []:
            attrs = node.get("attrs") if isinstance(node, dict) else {}
            viewbox = attrs.get("viewBox") if isinstance(attrs, dict) else None
            match = re.fullmatch(r"\s*[-+\d.e]+\s+[-+\d.e]+\s+([-+\d.e]+)\s+([-+\d.e]+)\s*", str(viewbox or ""))
            if match:
                width, height, source = float(match.group(1)), float(match.group(2)), "svg-viewBox"
                break
    return {"width": int(round(width)), "height": int(round(height)), "source": source,
            "x": _number(root.get("x")), "y": _number(root.get("y"))}


def overflow_evidence(data: dict[str, Any] | None) -> dict[str, Any]:
    """Return actual scroll extents; CSS ``overflow:auto`` alone is not overflow."""
    data = data if isinstance(data, dict) else {}
    scroll = data.get("scroll") if isinstance(data.get("scroll"), dict) else {}
    client_w = _number(scroll.get("clientWidth"), _number(data.get("viewport", {}).get("width")))
    client_h = _number(scroll.get("clientHeight"), _number(data.get("viewport", {}).get("height")))
    scroll_w = _number(scroll.get("scrollWidth"), client_w)
    scroll_h = _number(scroll.get("scrollHeight"), client_h)
    return {"clientWidth": int(round(client_w)), "clientHeight": int(round(client_h)),
            "scrollWidth": int(round(scroll_w)), "scrollHeight": int(round(scroll_h)),
            "hasOverflowX": scroll_w > client_w + 1, "hasOverflowY": scroll_h > client_h + 1,
            "hasOverflow": scroll_w > client_w + 1 or scroll_h > client_h + 1}


def make_state_samples(screen: dict[str, Any], *, max_samples: int = 8,
                       timestamp: float | None = None) -> list[dict[str, Any]]:
    """Create deterministic metadata samples without duplicating Screen objects."""
    base = {"variant": "default", "type": "default", "timestamp": timestamp,
            "trigger": None, "screen_id": screen.get("id") or screen.get("state")}
    out = [base]
    nodes = screen.get("data", {}).get("nodes", []) if isinstance(screen.get("data"), dict) else []
    for control in interactive_control_specs(nodes, limit=max(0, max_samples - 1)):
        if len(out) >= max_samples:
            break
        out.append({"variant": f"control:{control['identity']}", "type": "interactive", "timestamp": timestamp,
                    "trigger": {"kind": "click", "identity": control["identity"], "control": control["kind"]},
                    "screen_id": base["screen_id"]})
    if len(out) < max_samples:
        out.append({"variant": "time:frame-0", "type": "requestAnimationFrame", "timestamp": timestamp,
                    "trigger": {"kind": "requestAnimationFrame", "frame": 0}, "screen_id": base["screen_id"]})
    return out[:max_samples]


def interactive_control_specs(nodes: list[dict[str, Any]] | None, *, limit: int = 7) -> list[dict[str, Any]]:
    """Return a bounded, deterministic list of explicitly identified controls.

    Discovery is deliberately independent of project labels/text.  Controls
    without a stable DOM identity are skipped so a changing index cannot make
    interaction samples target the wrong element.
    """
    found: list[dict[str, Any]] = []
    for node in nodes or []:
        if not isinstance(node, dict):
            continue
        tag = str(node.get("tag", "")).casefold()
        role = str(node.get("role", "")).casefold()
        attrs = node.get("attrs") if isinstance(node.get("attrs"), dict) else {}
        kind = None
        if tag == "input" and str(node.get("type", "")).casefold() == "checkbox":
            kind = "checkbox"
        elif role == "switch":
            kind = "switch"
        elif tag == "button" and ("aria-pressed" in attrs or node.get("ariaPressed") is not None):
            kind = "button-pressed"
        if not kind:
            continue
        identity = (node.get("identity") or attrs.get("data-openhmi-id") or
                    attrs.get("data-figma-node-id") or attrs.get("id"))
        if not identity:
            continue
        found.append({"identity": str(identity), "kind": kind, "index": node.get("index")})
        if len(found) >= max(0, limit):
            break
    return found


def region_manifest(nodes: list[dict[str, Any]], canvas: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build semantic regions and their crop geometry.

    The old manifest reported DOM rectangle area as ``coverage``.  That is
    useful as evidence of what the browser declared, but is not a visual
    comparison.  Keep the field for backwards compatibility and persist the
    actual node boxes/union bounds so validation can compare screenshots later.
    """
    canvas = canvas or discover_canvas({})
    cw, ch = max(1, _number(canvas.get("width"), 1)), max(1, _number(canvas.get("height"), 1))
    regions: dict[str, dict[str, Any]] = {}
    def add(kind: str, boxes: list[dict[str, Any]]) -> None:
        if not boxes:
            regions[kind] = {"status": "N/A", "pixel": None, "edge": None, "coverage": None,
                              "boxes": [], "union_bounds": None}
            return
        area = 0.0; edge = 0.0; serialized = []
        for node in boxes:
            r = node.get("rect") or {}; w, h = max(0, _number(r.get("width"))), max(0, _number(r.get("height")))
            area += w * h; edge += 2 * (w + h)
            serialized.append({"x": _number(r.get("x")), "y": _number(r.get("y")),
                               "width": w, "height": h})
        left = min(r["x"] for r in serialized); top = min(r["y"] for r in serialized)
        right = max(r["x"] + r["width"] for r in serialized)
        bottom = max(r["y"] + r["height"] for r in serialized)
        regions[kind] = {"status": "measured", "pixel": round(area, 3),
                         "edge": round(edge, 3), "coverage": round(min(1.0, area / (cw * ch)), 6),
                         "count": len(boxes), "boxes": serialized,
                         "union_bounds": {"x": left, "y": top, "width": max(0.0, right-left),
                                          "height": max(0.0, bottom-top)}}
    def node_identity(node: dict[str, Any]) -> str:
        attrs = node.get("attrs") if isinstance(node.get("attrs"), dict) else {}
        return " ".join(str(node.get(k, "")) for k in
                         ("id", "name", "className", "role", "aria", "text")) + " " + \
            " ".join(str(attrs.get(k, "")) for k in ("id", "data-openhmi-id", "aria-label", "role"))

    # Browser evidence is flat but preserves parentIndex.  A full-canvas SVG
    # is commonly a chart container; when it has explicit arc/gauge/readout
    # descendants, use those local boxes instead of making the whole canvas a
    # gauge region.
    semantic_gauge_indices: set[int] = set()
    for index, node in enumerate(nodes):
        tag = str(node.get("tag", "")).casefold()
        identity = node_identity(node)
        attrs = node.get("attrs") if isinstance(node.get("attrs"), dict) else {}
        if (re.search(r"\b(gauge|meter|arc|readout)\b", identity, re.I)
                or (tag in {"path", "circle", "g"} and
                    any(key in attrs for key in ("stroke-dasharray", "stroke-dashoffset")))):
            semantic_gauge_indices.add(index)

    def has_semantic_descendant(index: int) -> bool:
        found = False
        for candidate in semantic_gauge_indices:
            current = nodes[candidate].get("parentIndex") if isinstance(nodes[candidate], dict) else None
            seen: set[int] = set()
            while isinstance(current, int) and current not in seen:
                if current == index:
                    found = True
                    break
                seen.add(current)
                current = nodes[current].get("parentIndex") if current < len(nodes) else None
            if found:
                return True
        return False

    for kind in DEFAULT_REGION_KINDS:
        selected = []
        for index, node in enumerate(nodes):
            text = " ".join(str(node.get(k, "")) for k in ("className", "role", "aria", "text")).casefold()
            tag = str(node.get("tag", "")).casefold()
            if kind == "header" and (tag in {"header", "nav"} or "header" in text): selected.append(node)
            elif kind == "gauge":
                explicit = (node.get("role") == "meter" or "gauge" in text
                            or index in semantic_gauge_indices)
                if tag == "svg" and has_semantic_descendant(index):
                    # Container SVG: its children carry the useful geometry.
                    explicit = False
                if explicit:
                    selected.append(node)
            elif kind == "control" and tag in {"button", "input", "select", "textarea"}: selected.append(node)
            elif kind == "status":
                attrs = node.get("attrs") if isinstance(node.get("attrs"), dict) else {}
                identity = " ".join(str(node.get(k, "")) for k in
                                    ("id", "name", "aria", "className", "text"))
                identity += " " + " ".join(str(attrs.get(k, "")) for k in ("id", "aria-label", "role", "alt", "href", "src"))
                is_svg_image = tag in {"img", "image", "svg"} and ".svg" in identity.casefold()
                if (str(node.get("role", "")).casefold() in {"status", "alert"}
                        or re.search(r"\b(status|lamp|warning|icon)\b", identity, re.I)
                        or is_svg_image):
                    selected.append(node)
            elif kind == "background" and tag in {"body", "main"}: selected.append(node)
        add(kind, selected)
    return {"schema": "uagent.region-manifest/v1", "canvas": {"width": int(cw), "height": int(ch)}, "regions": regions}


def region_gate(manifest: dict[str, Any], required: tuple[str, ...] = DEFAULT_REGION_KINDS) -> list[str]:
    """Return blockers for regions that exist semantically but have no coverage."""
    blockers = []
    for name in required:
        item = (manifest.get("regions") or {}).get(name, {})
        if item.get("status") == "measured" and not item.get("coverage"):
            blockers.append(f"region:{name}:no-coverage")
    return blockers
