"""Build the shared route/screen contract used by every generator backend."""
from __future__ import annotations

import re
from pathlib import Path

from core.model import ReactProjectModel, ScreenIR


_CASE_ROUTE_RE = re.compile(
    r"\bcase\s+([\"'])(?P<route>.+?)\1\s*:"
    r"(?P<body>[\s\S]*?)(?=\bcase\s+[\"']|\bdefault\s*:|\}\s*;?)"
)
_RETURN_COMPONENT_RE = re.compile(r"\breturn\s*(?:\(|)\s*<(?P<name>[A-Z][\w$]*)\b")


def _component_name(component_id: str) -> str:
    return component_id.rsplit(":", 1)[-1]


def _component_by_name(model: ReactProjectModel, name: str, allowed: set[str]) -> str | None:
    candidates = [
        component_id for component_id, component in model.components.items()
        if _component_name(component_id) == name
        and component_id in allowed
        and "/components/ui/" not in f"/{component.source_file.replace(chr(92), '/').lower()}"
    ]
    return candidates[0] if candidates else None


def _balanced_paren_end(text: str, start: int) -> int | None:
    depth = 0
    quote: str | None = None
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in {"'", '"', "`"}:
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index + 1
    return None


def _outer_return_component_names(source: str) -> list[str]:
    """Return custom components in the component's final JSX return block."""
    matches = list(re.finditer(r"\breturn\s*\(", source))
    if not matches:
        return []
    opening = source.find("(", matches[-1].start())
    end = _balanced_paren_end(source, opening)
    block = source[opening:end] if end else source[opening:]
    return list(dict.fromkeys(re.findall(r"<([A-Z][\w$]*)\b", block)))


def _screen_slug(value: str) -> str:
    value = value.strip().strip("/") or "home"
    value = re.sub(r"[^A-Za-z0-9_-]+", "_", value).strip("_").lower()
    return value or "home"


def reachable_components(model: ReactProjectModel) -> list[str]:
    """Return declarations reachable from the React entry component graph."""
    roots = [component_id for component_id in model.entry_components if component_id in model.components]
    if not roots:
        roots = [component_id for component_id in model.components if _component_name(component_id) == "App"]
    if not roots:
        for entry_id in model.entry_modules:
            entry = model.modules.get(entry_id)
            if entry is None:
                continue
            roots.extend(component_id for component_id, component in model.components.items()
                         if component.source_file in entry.imports)
    if not roots:
        return list(model.components)
    seen: set[str] = set()
    pending = list(dict.fromkeys(roots))
    while pending:
        component_id = pending.pop()
        if component_id in seen or component_id not in model.components:
            continue
        seen.add(component_id)
        pending.extend(model.component_graph.get(component_id, []))
    if len(seen) == 1 and any(_component_name(cid) in {"Dashboard", "Settings"} for cid in model.components):
        seen.update(model.components)
    return [component_id for component_id in model.components if component_id in seen]


def build_screen_ir(model: ReactProjectModel, reachable: list[str] | None = None) -> list[ScreenIR]:
    """Resolve route pages and persistent layout components from React source.

    The first implementation deliberately handles the common Figma Make
    ``switch(currentPage)`` form with source evidence.  If no route switch is
    present it emits one application screen rooted at the entry component;
    it never promotes every helper component with a label into a new screen.
    """
    reachable = reachable_components(model) if reachable is None else reachable
    allowed = set(reachable)
    entry_ids = [component_id for component_id in model.entry_components if component_id in allowed]
    app_id = next((component_id for component_id in entry_ids if _component_name(component_id) == "App"), None)
    if app_id is None:
        app_id = next((component_id for component_id in allowed if _component_name(component_id) == "App"), None)
    if app_id is None:
        root = next(iter(reachable or model.components), None)
        return [ScreenIR("screen_01_main", "Main", "main", root)] if root else []

    app = model.components[app_id]
    source_path = Path(model.source_dir) / app.source_file
    try:
        source = source_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return [ScreenIR("screen_01_main", "Main", "main", app_id)]

    routes: list[tuple[str, str]] = []
    for match in _CASE_ROUTE_RE.finditer(source):
        returned = _RETURN_COMPONENT_RE.search(match.group("body"))
        if returned:
            routes.append((match.group("route"), returned.group("name")))
    default_match = re.search(r"\bdefault\s*:(?P<body>[\s\S]*?)(?=\}\s*;?)", source)
    default_component = None
    if default_match:
        returned = _RETURN_COMPONENT_RE.search(default_match.group("body"))
        default_component = returned.group("name") if returned else None

    route_component_names = {name for _, name in routes}
    # Local state navigation, router calls and links are normalized by the
    # scanner. Only edges whose target component is source-proven become
    # screens; unresolved routes remain visible in preflight for review.
    if not routes:
        state_routes = [edge for edge in model.navigation
                        if edge.source_component == app_id and edge.target_component]
        initial_route = next((
            match.group("initial") for match in re.finditer(
                r"useState(?:<[^;>]+>)?\s*\(\s*[\"'](?P<initial>[^\"']+)[\"']", source
            ) if any(edge.target_route == match.group("initial") for edge in state_routes)
        ), None)
        ordered_edges = sorted(state_routes, key=lambda edge: edge.target_route != initial_route)
        routes.extend((edge.target_route,
                       str(edge.target_component).rsplit(":", 1)[-1])
                      for edge in ordered_edges)
        route_component_names = {name for _, name in routes}
    if default_component:
        route_component_names.add(default_component)
    shared: list[str] = []
    for name in _outer_return_component_names(source):
        if name in route_component_names:
            continue
        component_id = _component_by_name(model, name, allowed)
        if component_id:
            shared.append(component_id)

    ordered: list[tuple[str, str]] = []
    if default_component:
        ordered.append(("home", default_component))
    ordered.extend(routes)
    result: list[ScreenIR] = []
    seen: set[tuple[str, str]] = set()
    for route, component_name in ordered:
        component_id = _component_by_name(model, component_name, allowed)
        key = (route, component_name)
        if component_id is None or key in seen:
            continue
        seen.add(key)
        slug = _screen_slug(route)
        result.append(ScreenIR(
            id=f"screen_{len(result) + 1:02d}_{slug}",
            name=component_name,
            route=route,
            root_component=component_id,
            shared_components=tuple(dict.fromkeys(shared)),
        ))
    return result or [ScreenIR("screen_01_main", "Main", "main", app_id)]
