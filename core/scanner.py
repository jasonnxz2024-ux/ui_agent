"""Evidence-based React/Figma Make scanner.

This deliberately small parser records only syntax it can locate
unambiguously. Semantic detection happens inside each component body, so an
import of recharts cannot accidentally classify every component in its file as
a chart.
"""
from __future__ import annotations

from pathlib import Path
import os
import hashlib
import random
import re
from typing import Iterable

from .model import Component, Event, NavigationEdge, ReactProjectModel, Resource, SourceModule, SourceInstanceIR, VisualEffectIR

EXTS = {".tsx", ".ts", ".jsx", ".js"}
ASSET_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".svg", ".ttf", ".woff", ".woff2"}
NON_BUSINESS_DIRS = {
    "node_modules", ".git", "dist", "build", ".next", "simulator",
    "docs", "doc", "reference", "references",
}
NON_SOURCE_DIRS = NON_BUSINESS_DIRS | {"assets"}
IMPORT_RE = re.compile(r"\bimport\s+(?:(?P<bindings>[\s\S]*?)\s+from\s+)?[\"'](?P<source>[^\"']+)[\"']\s*;?")
COMPONENT_RE = re.compile(
    r"(?:export\s+)?(?:default\s+)?function\s+(?P<function>[A-Z]\w*)\s*[^\{]*\{|"
    r"(?:export\s+)?(?:const|let)\s+(?P<arrow>[A-Z]\w*)\s*(?::[^=\r\n]+)?=\s*"
    r"(?:async\s*)?(?:\([^\r\n]*?\)|[A-Za-z_$][\w$]*)\s*=>\s*\{"
)
COMPONENT_EXPR_RE = re.compile(
    r"(?:export\s+)?(?:const|let)\s+(?P<name>[A-Z]\w*)\s*(?::[^=\r\n]+)?=\s*"
    r"(?:async\s*)?(?:\([^\r\n]*?\)|[A-Za-z_$][\w$]*)\s*=>\s*\("
)
STATE_DECL_RE = re.compile(
    r"(?:const|let)\s*\[\s*(?P<state>\w+)\s*,\s*(?P<setter>\w+)\s*\]\s*=\s*"
    r"(?:(?:React\.)?useState(?:<[^>]+>)?\s*\((?P<initial>[\s\S]*?)\)|"
    r"(?:React\.)?useReducer\s*\((?P<reducer>[^,]+),\s*(?P<reducer_initial>[\s\S]*?)\))\s*;"
)
ARRAY_DECL_RE = re.compile(r"(?:export\s+)?const\s+(?P<name>\w+)\s*(?::[^=]+)?=\s*\[")
ARRAY_FROM_RE = re.compile(
    r"(?:const|let)\s+(?P<name>\w+)\s*(?::[^=]+)?=\s*Array\.from\s*"
    r"\(\s*\{\s*length\s*:\s*(?P<length>\d+)\s*\}\s*,"
)


def _kind(path: Path) -> str:
    if path.suffix.lower() in {".ttf", ".woff", ".woff2"}: return "font"
    if path.suffix.lower() == ".svg": return "svg"
    if path.suffix.lower() in ASSET_EXTS: return "image"
    return "unknown"


def _relative(path: Path, root: Path) -> str:
    return str(path.relative_to(root)).replace("\\", "/")


def _balanced_end(text: str, start: int, opener: str = "{", closer: str = "}") -> int | None:
    """Offset after a matching delimiter, skipping quoted strings."""
    depth, quote, escaped = 0, None, False
    for index in range(start, len(text)):
        char = text[index]
        if quote:
            if escaped: escaped = False
            elif char == "\\": escaped = True
            elif char == quote: quote = None
            continue
        if char in "'\"`": quote = char
        elif char == opener: depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0: return index + 1
    return None


def _balanced_end_loose(text: str, start: int, opener: str = "{", closer: str = "}") -> int | None:
    """Fallback delimiter scan for JSX bodies.

    JSX text may contain apostrophes/quotes (for example ``don't``) that look
    like JavaScript string delimiters to the conservative scanner above.  A
    loose brace count is safer than dropping the whole entry component; the
    semantic audit still records any uncertain body details.
    """
    depth = 0
    for index in range(max(0, start), len(text)):
        char = text[index]
        if char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return index + 1
    return None


def _compact(expression: str, limit: int = 240) -> str:
    return re.sub(r"\s+", " ", expression).strip()[:limit]


def _literal(value: str) -> object:
    value = value.strip().rstrip(",")
    if value in {"true", "false"}: return value == "true"
    if value == "null": return None
    if re.fullmatch(r"-?\d+(?:\.\d+)?", value): return float(value) if "." in value else int(value)
    quoted = re.fullmatch(r"[\"'](.*)[\"']", value, re.S)
    return quoted.group(1) if quoted else _compact(value, 120)


def _array_evidence(name: str, literal: str) -> dict[str, object]:
    items: list[dict[str, object]] = []
    for object_text in re.findall(r"\{([^{}]*)\}", literal):
        item: dict[str, object] = {}
        for key, value in re.findall(r"([A-Za-z_$][\w$]*)\s*:\s*([^,}\n]+)", object_text):
            item[key] = _literal(value)
        if item: items.append(item)
    return {"name": name, "length": len(items), "items": items, "expression": _compact(literal)}


def _array_from_evidence(name: str, length: int, expression: str) -> dict[str, object]:
    """Lock simple Array.from + Math.random mock data to a stable import seed."""
    object_match = re.search(r"=>\s*\(\s*\{([\s\S]*?)\}\s*\)", expression)
    fields = re.findall(r"([A-Za-z_$][\w$]*)\s*:\s*([^,\n}]+)", object_match.group(1) if object_match else "")
    seed = int(hashlib.sha1((name + expression).encode("utf-8")).hexdigest()[:8], 16)
    rng = random.Random(seed)
    items: list[dict[str, object]] = []
    for index in range(length):
        item: dict[str, object] = {}
        for key, raw in fields:
            raw = raw.strip()
            random_value = re.fullmatch(r"(-?\d+(?:\.\d+)?)\s*\+\s*Math\.random\(\)\s*\*\s*(\d+(?:\.\d+)?)", raw)
            if random_value:
                base, scale = map(float, random_value.groups())
                item[key] = round(base + rng.random() * scale, 3)
            elif key in {"index", "id"} and raw == "i":
                item[key] = index
        items.append(item)
    return {
        "name": name,
        "length": length,
        "items": items,
        "expression": _compact(expression),
        "generated": True,
        "seed": seed,
        "random_locked": "Math.random()" in expression,
    }


def _local_module(root: Path, current: Path, specifier: str) -> Path | None:
    if not specifier.startswith("."): return None
    base = (current.parent / specifier).resolve()
    candidates = [base] + [base.with_suffix(ext) for ext in EXTS] + [base / f"index{ext}" for ext in EXTS]
    return next((candidate for candidate in candidates if candidate.is_file() and candidate.suffix.lower() in EXTS and root in candidate.parents), None)


def _bindings(bindings: str | None) -> list[str]:
    if not bindings: return []
    result: list[str] = []
    default = bindings.split(",", 1)[0].strip()
    if default and not default.startswith(("{", "*")): result.append(default)
    named = re.search(r"\{([^}]+)\}", bindings)
    if named: result.extend(entry.strip().split(" as ")[-1].strip() for entry in named.group(1).split(","))
    namespace = re.search(r"\*\s+as\s+(\w+)", bindings)
    if namespace: result.append(namespace.group(1))
    return [entry for entry in result if re.fullmatch(r"[A-Za-z_$][\w$]*", entry)]


def _component_spans(text: str) -> Iterable[tuple[str, int, int]]:
    for match in COMPONENT_RE.finditer(text):
        name = match.group("function") or match.group("arrow")
        if match.group("function"):
            # Function parameters may themselves use object destructuring.
            # Find the matching parameter paren before looking for the body.
            paren = text.find("(", match.start(), match.end() + 1)
            paren_end = _balanced_end(text, paren, "(", ")") if paren >= 0 else None
            brace = text.find("{", paren_end or match.end())
        else:
            # The arrow-expression branch is deliberately terminated by the
            # actual body brace, not a destructured argument brace.
            brace = match.end() - 1
        end = _balanced_end(text, brace) if brace >= 0 else None
        if end is None and brace >= 0:
            end = _balanced_end_loose(text, brace)
        if name and end: yield name, match.start(), end
    for match in COMPONENT_EXPR_RE.finditer(text):
        opening = text.find("(", match.start(), match.end() + 1)
        end = _balanced_end(text, opening, "(", ")") if opening >= 0 else None
        if end:
            yield match.group("name"), match.start(), end


def _jsx_prop(body: str, tag: str, prop: str) -> str | None:
    match = re.search(rf"<{re.escape(tag)}\b[\s\S]*?\b{re.escape(prop)}\s*=\s*(?:\{{|[\"'])", body)
    if not match: return None
    value_start = match.end() - 1
    if body[value_start] in "\"'":
        end = body.find(body[value_start], value_start + 1)
        return body[value_start + 1:end] if end >= 0 else None
    end = _balanced_end(body, value_start)
    return _compact(body[value_start + 1:end - 1]) if end else None


def _jsx_opening_end(body: str, start: int) -> int | None:
    """Locate the end of a JSX opening tag, including arrow-function props."""
    brace_depth, quote, escaped = 0, None, False
    for index in range(start, len(body)):
        char = body[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in "'\"`":
            quote = char
        elif char == "{":
            brace_depth += 1
        elif char == "}" and brace_depth:
            brace_depth -= 1
        elif char == ">" and brace_depth == 0:
            return index
    return None


def _opening_tag_prop(body: str, start: int, prop: str) -> str | None:
    """Read one prop from one JSX opening tag (not a later tag of same type)."""
    end = _jsx_opening_end(body, start)
    if end is None: return None
    opening = body[start:end + 1]
    match = re.search(rf"\b{re.escape(prop)}\s*=\s*(?:\{{|[\"'])", opening)
    if not match: return None
    value_start = match.end() - 1
    if opening[value_start] in "\"'":
        value_end = opening.find(opening[value_start], value_start + 1)
        return opening[value_start + 1:value_end] if value_end >= 0 else None
    value_end = _balanced_end(opening, value_start)
    return _compact(opening[value_start + 1:value_end - 1]) if value_end else None


def _jsx_props(opening: str) -> dict[str, object]:
    """Extract literal JSX props without pretending arbitrary expressions are values."""
    attrs: dict[str, object] = {}
    tag_match = re.match(r"\s*<([A-Za-z][\w.:-]*)", opening)
    if not tag_match:
        return attrs
    cursor = tag_match.end()
    limit = len(opening) - 1
    while cursor < limit:
        while cursor < limit and opening[cursor].isspace():
            cursor += 1
        if cursor >= limit or opening[cursor] == "/":
            break
        name_match = re.match(r"([A-Za-z_$][\w$:-]*)", opening[cursor:])
        if not name_match:
            cursor += 1
            continue
        name = name_match.group(1)
        cursor += len(name)
        while cursor < limit and opening[cursor].isspace():
            cursor += 1
        if cursor >= limit or opening[cursor] != "=":
            attrs[name] = True
            continue
        cursor += 1
        while cursor < limit and opening[cursor].isspace():
            cursor += 1
        if cursor >= limit:
            break
        if opening[cursor] in "\"'":
            quote = opening[cursor]
            end = cursor + 1
            while end < limit and opening[end] != quote:
                end += 1
            raw = opening[cursor:end + 1]
            cursor = min(end + 1, limit)
        elif opening[cursor] == "{":
            end = _balanced_end(opening, cursor)
            if end is None:
                break
            raw = opening[cursor:end]
            cursor = end
        else:
            value_match = re.match(r"[^\s>]+", opening[cursor:])
            raw = value_match.group(0) if value_match else ""
            cursor += len(raw)
        attrs[name] = _literal(raw[1:-1] if raw.startswith(("{",)) and raw.endswith("}") else raw)
    return attrs


def _source_instances(model: ReactProjectModel, texts: dict[Path, str], exports: dict[tuple[str, str], str], imports: dict[str, dict[str, tuple[str, str]]], root: Path) -> None:
    """Build a deterministic JSX instance tree for every scanned definition body."""
    for component in model.components.values():
        text = texts[(root / component.source_file).resolve()]
        start, end = component.props.get("span", [0, len(text)])
        body = text[int(start):int(end)]
        stack: list[tuple[str, str]] = []
        occurrence: dict[str, int] = {}
        cursor = 0
        while cursor < len(body):
            match = re.search(r"</?([A-Za-z][\w.:-]*)", body[cursor:])
            if not match:
                break
            tag_start = cursor + match.start()
            absolute = int(start) + tag_start
            closing = body[tag_start:tag_start + 2] == "</"
            tag = match.group(1)
            if closing:
                for index in range(len(stack) - 1, -1, -1):
                    if stack[index][0] == tag:
                        del stack[index:]
                        break
                close_end = body.find(">", tag_start + 2)
                cursor = len(body) if close_end < 0 else close_end + 1
                continue
            opening_end = _jsx_opening_end(body, tag_start)
            if opening_end is None:
                cursor = tag_start + len(match.group(0))
                continue
            raw = body[tag_start:opening_end + 1]
            props = _jsx_props(raw)
            key = f"{component.source_file}:{component.id}:{absolute}:{tag}"
            occurrence[tag] = occurrence.get(tag, 0) + 1
            digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
            instance_id = f"instance:{digest}"
            target = imports.get(component.source_file, {}).get(tag)
            component_ref = exports.get(target) if target else exports.get((component.source_file, tag))
            semantic = next((str(props.get(name)) for name in ("data-openhmi-id", "data-figma-node-id", "id")
                              if props.get(name) not in (None, True, "")), None)
            figma_id = str(props.get("data-figma-node-id")) if props.get("data-figma-node-id") not in (None, True, "") else None
            parent_id = stack[-1][1] if stack else None
            node = SourceInstanceIR(instance_id=instance_id, owner_component=component.id, tag=tag,
                                     component_ref=component_ref, source_file=component.source_file,
                                     span=[absolute, int(start) + opening_end + 1], occurrence=occurrence[tag],
                                     parent_id=parent_id, props_raw=props, semantic_id=semantic,
                                     figma_node_id=figma_id)
            model.instances[instance_id] = node
            if parent_id and parent_id in model.instances:
                model.instances[parent_id].children.append(instance_id)
            cursor = opening_end + 1
            if not raw.rstrip().endswith("/>"):
                stack.append((tag, instance_id))


def _component_kind(body: str) -> str:
    # A fixed canvas that paints several coordinated instruments is one
    # semantic scene.  It must not fall through to the reusable Gauge path:
    # doing so loses the tick loop, center readouts and non-gauge decorations.
    fixed_svg = bool(re.search(
        r"<svg\b[^>]*\b(?:width\s*=\s*\{?\d+|viewBox\s*=\s*[\"']\s*0\s+0\s+\d+\s+\d+)",
        body, re.I,
    ))
    center_count = len(re.findall(r"\bCX_[A-Za-z][A-Za-z0-9_]*\s*=\s*-?\d+(?:\.\d+)?", body))
    arc_count = len(re.findall(r"\barc\s*\(", body)) + len(re.findall(r"<circle\b[^>]*\bstroke-dash(?:array|offset)", body, re.I))
    tick_loop = bool(re.search(r"Array\.from\s*\(\s*\{\s*length\s*:\s*[^}]+\}\s*,|\.map\s*\(\s*\([^)]*\)\s*=>\s*\(\s*<line|<Ticks\b", body, re.S))
    readouts = len(re.findall(r"<text\b[^>]*\btextAnchor\s*=", body))
    if fixed_svg and center_count >= 2 and arc_count >= 2 and tick_loop and readouts >= 2 and re.search(r"(?:stroke|filter)\s*=|\b(?:Angle|Pct)\s*=", body):
        return "composite_gauge_scene"
    # Gauge components commonly render an inline SVG circle whose dash offset
    # is driven by a numeric value.  Keep this semantic signal ahead of the
    # generic component fallback so every Make project can map it to lv_arc.
    if re.search(r"<svg\b[\s\S]*?<circle\b", body) and re.search(r"(?:strokeDash(?:array|offset)|aria-label|role\s*=\s*[\"']meter)", body):
        return "gauge"
    # Reusable gauges may render a ``<g>`` group into a parent SVG instead of
    # owning an inline <svg>.  The class + arc call is structural evidence and
    # avoids relying on the component's function name.
    if re.search(r"className\s*=\s*[\"']gauge-group[\"']", body) and re.search(r"\barc\s*\(", body):
        return "gauge"
    if re.search(r"\bCX_[A-Z]+\s*=\s*\d+", body) and re.search(r"<path\b[\s\S]*?d\s*=\s*\{?\s*arc\s*\(", body):
        return "gauge"
    if re.search(r"<(?:LineChart|AreaChart|BarChart|PieChart|RadarChart|ScatterChart)\b", body): return "chart"
    if (re.search(r"<motion\.", body) and re.search(r"\bonDrag(?:End)?\s*=", body)) or re.search(r"\buseEmblaCarousel\s*\(", body): return "carousel"
    if re.search(r"<(?:Table|table)\b", body): return "table"
    if re.search(r"<(?:Select|DropdownMenu)(?:Trigger|Content)?\b", body): return "dropdown"
    if re.search(r"\boverflow-[xy]-(?:auto|scroll)\b|<ScrollArea\b", body): return "scroll"
    return "component"


def _plain_jsx_text(value: str) -> str:
    """Return a compact, safe label from simple JSX content.

    Dynamic values are intentionally represented as an editable ellipsis rather
    than evaluating source code.  This keeps the intermediate model safe and
    makes the remaining work explicit in the audit file.
    """
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"\{[^{}]*\}", "…", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value or "…"


def _tag_blocks(body: str, tag: str) -> list[tuple[int, str, str]]:
    """Find non-self-closing JSX tag blocks without treating component names as prefixes."""
    result: list[tuple[int, str, str]] = []
    pattern = re.compile(rf"<{re.escape(tag)}\b")
    close = f"</{tag}>"
    for match in pattern.finditer(body):
        opening_end = _jsx_opening_end(body, match.start())
        if opening_end is None or body[match.start():opening_end + 1].rstrip().endswith("/>"):
            continue
        closing = body.find(close, opening_end + 1)
        if closing >= 0:
            result.append((match.start(), body[match.start():opening_end + 1], body[opening_end + 1:closing]))
    return result


def _state_initial(component: Component, name: str | None) -> object | None:
    if not name:
        return None
    for state in component.props.get("state", []):
        if isinstance(state, dict) and state.get("name") == name:
            return state.get("initial")
    return None


def _array_named(component: Component, name: str | None) -> dict[str, object] | None:
    if not name:
        return None
    for array in component.props.get("arrays", []):
        if isinstance(array, dict) and array.get("name") == name:
            return array
    return None


# lucide-react icons are inline SVG React components, not files in the asset
# directory. Keep the names in the intermediate model so a later backend can
# choose a native symbol instead of turning the component into a missing-glyph
# square.  This list is intentionally broad but only records names actually
# present in JSX or an array's ``icon`` field.
LUCIDE_ICON_NAMES = {
    "Settings", "Languages", "Wrench", "MoreHorizontal", "Scissors", "Home",
    "ArrowLeft", "HelpCircle", "Clock", "Wifi", "WifiOff", "Lock", "Check",
    "ChevronRight", "FileText", "Droplet", "Droplets", "Sparkles", "RefreshCw",
    "Plus", "Minus", "X", "Menu", "Search", "Upload", "Download", "Info",
}


def _icon_for_control(component: Component, body: str, offset: int, content: str,
                      mapped_index: int = 0) -> tuple[str | None, str | None]:
    """Return (icon name, evidence source) for one concrete JSX control.

    Figma Make commonly renders a mapped option as ``<Icon />`` where Icon is
    assigned from ``option.icon``.  The icon is therefore recoverable from the
    already-parsed array evidence; no screenshot or visual guess is involved.
    """
    direct = re.findall(r"<([A-Z][A-Za-z0-9]*)\b", content)
    for name in direct:
        if name in LUCIDE_ICON_NAMES:
            return name, "jsx"
    if re.search(r"<Icon\b", content) or re.search(r"\{\s*[^{}]+\.icon\s*\}", content):
        before = body[:offset]
        maps = list(re.finditer(r"\b([A-Za-z_$][\w$]*)\.map\s*\(", before))
        if maps:
            source = maps[-1].group(1)
            evidence = _array_named(component, source)
            items = evidence.get("items", []) if evidence else []
            icons = [str(item.get("icon")) for item in items
                     if isinstance(item, dict) and str(item.get("icon", "")) in LUCIDE_ICON_NAMES]
            if mapped_index < len(icons):
                return icons[mapped_index], f"array:{source}.icon"
    return None, None


def _icon_props(name: str | None, content: str, component: Component, body: str,
                offset: int, mapped_index: int = 0) -> dict[str, object]:
    """Preserve presentation props from the React icon instead of recoloring it.

    Direct Lucide JSX commonly supplies ``color``, ``size`` and
    ``strokeWidth``.  Mapped icons keep their color on the source array item.
    These values are later used to materialize color-correct SVG/PNG assets.
    """
    if not name:
        return {}
    props: dict[str, object] = {}
    opening = re.search(rf"<{re.escape(name)}\b([^>]*)/?>", content)
    attrs = opening.group(1) if opening else ""
    color = re.search(r"\bcolor\s*=\s*[\"']([^\"']+)[\"']", attrs)
    size = re.search(r"\bsize\s*=\s*\{?([0-9]+(?:\.[0-9]+)?)", attrs)
    stroke = re.search(r"\bstrokeWidth\s*=\s*\{?([0-9]+(?:\.[0-9]+)?)", attrs)
    if color:
        props["icon_color"] = color.group(1)
    if size:
        props["icon_size"] = float(size.group(1)) if "." in size.group(1) else int(size.group(1))
    if stroke:
        props["icon_stroke_width"] = float(stroke.group(1)) if "." in stroke.group(1) else int(stroke.group(1))
    if not color and str(name) and re.search(r"<Icon\b", content):
        maps = list(re.finditer(r"\b([A-Za-z_$][\w$]*)\.map\s*\(", body[:offset]))
        if maps:
            source = maps[-1].group(1)
            evidence = _array_named(component, source)
            items = evidence.get("items", []) if evidence else []
            if isinstance(items, list) and mapped_index < len(items) and isinstance(items[mapped_index], dict):
                item_color = items[mapped_index].get("color")
                if item_color:
                    props["icon_color"] = str(item_color)
    return props


def _select_evidence(component: Component, body: str) -> list[dict[str, object]]:
    """Capture Select/DropdownMenu choices as declarative, audit-friendly data."""
    widgets: list[dict[str, object]] = []
    for tag in ("Select", "DropdownMenu"):
        for index, (offset, opening, content) in enumerate(_tag_blocks(body, tag)):
            value = _opening_tag_prop(opening, 0, "value")
            item_options: list[str] = []
            source: str | None = None
            item_tag = "SelectItem" if tag == "Select" else "DropdownMenuItem"
            for _, item_opening, item_content in _tag_blocks(content, item_tag):
                item_options.append(_plain_jsx_text(item_content))
            map_match = re.search(r"\b([A-Za-z_$][\w$]*)\.map\s*\(", content)
            if map_match:
                source = map_match.group(1)
                evidence = _array_named(component, source)
                items = evidence.get("items", []) if evidence else []
                if isinstance(items, list):
                    for item in items:
                        if isinstance(item, dict):
                            label = next((item[key] for key in ("label", "name", "title", "value", "id") if item.get(key) not in (None, "")), None)
                            if label is not None:
                                item_options.append(str(label))
            # A mapped SelectItem is a JSX template, not an additional option.
            # When its local array was resolved, discard template-only labels
            # such as "…" or "… …" and preserve the concrete source values.
            if source and evidence and isinstance(items, list):
                item_options = [
                    str(next((item[key] for key in ("label", "name", "title", "value", "id") if item.get(key) not in (None, "")), ""))
                    for item in items if isinstance(item, dict)
                ]
            # Preserve order but avoid rendering the JSX template and resolved
            # local data twice for a literal array.
            options = list(dict.fromkeys(option for option in item_options if option))
            selected = _state_initial(component, value)
            widgets.append({
                "kind": "select" if tag == "Select" else "menu",
                "index": index,
                "value_expression": value,
                "selected": selected,
                "options": options,
                "option_source": source,
                "placeholder": _opening_tag_prop(content, 0, "placeholder"),
                "offset": offset,
            })
    return widgets


def _table_evidence(component: Component, body: str) -> dict[str, object] | None:
    table_tag = "Table" if re.search(r"<Table\b", body) else "table"
    blocks = _tag_blocks(body, table_tag)
    if not blocks:
        return None
    _, _, table_body = blocks[0]
    headers = [_plain_jsx_text(content) for _, _, content in _tag_blocks(table_body, "TableHead")]
    if not headers:
        headers = [_plain_jsx_text(content) for _, _, content in _tag_blocks(table_body, "th")]
    body_block = next((content for _, _, content in _tag_blocks(table_body, "TableBody")), table_body)
    cells = _tag_blocks(body_block, "TableCell") or _tag_blocks(body_block, "td")
    map_match = re.search(r"\b([A-Za-z_$][\w$]*)\.map\s*\(", body_block)
    row_source = map_match.group(1) if map_match else None
    evidence = _array_named(component, row_source)
    row_count = evidence.get("length", 0) if evidence else 0
    return {
        "headers": headers,
        "column_count": max(len(headers), len(cells), 1),
        "row_source": row_source,
        "row_count": int(row_count) if isinstance(row_count, int) else 0,
        "row_template": [_plain_jsx_text(content) for _, _, content in cells],
        "scroll_x": bool(re.search(r"\boverflow-x-(?:auto|scroll)\b", body)),
    }


def _scroll_evidence(body: str) -> dict[str, object] | None:
    # Figma Make commonly emits React inline styles instead of utility classes.
    match = re.search(r"\boverflow-(?P<axis>[xy])-(?P<mode>auto|scroll)\b", body)
    if match:
        return {"axis": match.group("axis"), "mode": match.group("mode"), "source": "tailwind"}
    inline = re.search(
        r"\boverflow(?P<axis>[XY])\s*:\s*(?:[\"'](?P<quoted>auto|scroll)[\"']|(?P<bare>auto|scroll)\b)",
        body,
    )
    if inline:
        return {
            "axis": "x" if inline.group("axis") == "X" else "y",
            "mode": inline.group("quoted") or inline.group("bare"),
            "source": "inline-style",
        }
    shorthand = re.search(r"\boverflow\s*:\s*(?:[\"'](?P<quoted>auto|scroll)[\"']|(?P<bare>auto|scroll)\b)", body)
    if shorthand:
        return {"axis": "y", "mode": shorthand.group("quoted") or shorthand.group("bare"), "source": "inline-style"}
    if re.search(r"<ScrollArea\b", body):
        return {"axis": "y", "mode": "auto", "source": "ScrollArea"}
    return None


def _float_value(value: object) -> float | None:
    match = re.search(r"-?\d+(?:\.\d+)?", str(value or ""))
    return float(match.group(0)) if match else None


def _svg_numeric(value: object, constants: dict[str, float] | None = None) -> float | None:
    """Resolve the small arithmetic expressions commonly used in JSX SVG."""
    expression = str(value or "").strip()
    if not expression:
        return None
    for name, number in (constants or {}).items():
        expression = re.sub(rf"\b{re.escape(name)}\b", str(number), expression)
    if not re.fullmatch(r"[\d\s.+\-*/()]+", expression):
        return _float_value(value)
    try:
        result = eval(expression, {"__builtins__": {}}, {})
    except (TypeError, ValueError, SyntaxError, ZeroDivisionError):
        return _float_value(value)
    return float(result) if isinstance(result, (int, float)) else None


def _local_numeric_constants(body: str) -> dict[str, float]:
    """Resolve simple local numeric declarations used by SVG geometry.

    Make output uses both upper/lower-case names and occasionally comma
    declarations (``const cx = 120, cy = 80, r = 40``).  Keep this strictly
    literal/arithmetic; never evaluate arbitrary source.
    """
    constants: dict[str, float] = {}
    declaration = re.compile(r"\b(?:const|let|var)\s+([^;\n]+)")
    for match in declaration.finditer(body):
        for name, raw in re.findall(r"\b([A-Za-z_$][\w$]*)\s*=\s*([^,]+?)(?=\s*,\s*[A-Za-z_$][\w$]*\s*=|\s*$)", match.group(1)):
            value = _svg_numeric(raw.strip(), constants)
            if value is not None:
                constants[name] = value
    for name, raw in re.findall(r"\b(CX_[A-Za-z][\w]*|CY|R)\s*=\s*(-?\d+(?:\.\d+)?)", body):
        constants.setdefault(name, float(raw))
    return constants


def _svg_color(value: object) -> str | None:
    """Return a stable RGB color while keeping the original stroke separately."""
    raw = str(value or "").strip()
    match = re.fullmatch(
        r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)(?:\s*,\s*[0-9.]+)?\s*\)",
        raw, re.I,
    )
    if match:
        return "#%02X%02X%02X" % tuple(int(match.group(index)) for index in (1, 2, 3))
    if re.fullmatch(r"#[0-9a-fA-F]{3,8}", raw):
        return raw.upper()
    return None


def _svg_alpha(value: object, default: float = 1.0) -> float:
    """Extract alpha from an SVG/CSS color without choosing a renderer."""
    raw = str(value or "").strip()
    match = re.fullmatch(
        r"rgba?\(\s*\d+\s*,\s*\d+\s*,\s*\d+(?:\s*,\s*([0-9.]+))?\s*\)",
        raw, re.I,
    )
    if match and match.group(1) is not None:
        try:
            return max(0.0, min(1.0, float(match.group(1))))
        except ValueError:
            return default
    try:
        return max(0.0, min(1.0, float(raw))) if raw else default
    except ValueError:
        return default


def _svg_color_branches(body: str, name: object) -> list[str]:
    """Read literal colors from a conditional color binding without executing it."""
    binding = str(name or "").strip()
    if not re.fullmatch(r"[A-Za-z_$][\w$]*", binding):
        return []
    declaration = re.search(
        rf"\b(?:const|let|var)\s+{re.escape(binding)}\s*=\s*([^;\n]+)", body
    )
    if not declaration:
        return []
    return re.findall(r"#[0-9a-fA-F]{3,8}|rgba?\([^)]*\)", declaration.group(1))


def _effect_id(rel: str, offset: int, kind: str, raw: str) -> str:
    digest = hashlib.sha1(f"{rel}:{offset}:{kind}:{raw}".encode("utf-8")).hexdigest()[:10]
    return f"effect:{rel}:{offset}:{kind}:{digest}"


def _effect_value(attrs: str, name: str) -> str | None:
    match = re.search(rf"\b{re.escape(name)}\s*=\s*([\"'])(.*?)\1", attrs, re.S)
    if match:
        return match.group(2).strip()
    # JSX permits dynamic SVG attributes such as id={`glow-${label}`} or
    # id={"glow-static"}. Preserve the template expression's literal text so
    # capability planning can still classify the source-backed glow.
    template = re.search(rf"\b{re.escape(name)}\s*=\s*\{{\s*`([\s\S]*?)`\s*\}}", attrs)
    if template:
        return template.group(1).strip()
    literal = re.search(rf"\b{re.escape(name)}\s*=\s*\{{\s*([\"'])(.*?)\1\s*\}}", attrs, re.S)
    return literal.group(2).strip() if literal else None


def _gradient_properties(raw: str) -> dict[str, object]:
    match = re.search(r"(?P<type>linear|radial)-gradient\s*\((?P<body>[^)]*)\)", raw, re.I | re.S)
    if not match:
        return {}
    body = match.group("body")
    parts = [part.strip() for part in body.split(",") if part.strip()]
    angle = _float_value(parts[0]) if parts and re.search(r"(?:deg|turn|rad)", parts[0]) else None
    stops = []
    for part in parts[1:] if angle is not None else parts:
        color = re.match(r"(#[0-9a-fA-F]{3,8}|rgba?\([^)]*\)|[A-Za-z]+)", part)
        if color:
            stops.append({"color": color.group(1), "position": part[color.end():].strip() or None})
    return {"gradient_type": match.group("type").lower(), "angle": angle, "stops": stops}


def _shadow_properties(raw: str) -> dict[str, object]:
    values = [_float_value(item) for item in re.findall(r"-?\d+(?:\.\d+)?(?:px|pt|em)?", raw)]
    colors = re.findall(r"#[0-9a-fA-F]{3,8}|rgba?\([^)]*\)|hsla?\([^)]*\)", raw)
    return {
        "offset_x": values[0] if len(values) > 0 else None,
        "offset_y": values[1] if len(values) > 1 else None,
        "blur_radius": values[2] if len(values) > 2 else None,
        "spread": values[3] if len(values) > 3 else None,
        "color": colors[0] if colors else None,
    }


def _record_visual_effect(
    model: ReactProjectModel,
    *,
    rel: str,
    offset: int,
    kind: str,
    raw: str,
    target_component: str | None = None,
    **kwargs: object,
) -> VisualEffectIR:
    effect_id = _effect_id(rel, offset, kind, raw)
    # A filter is a definition, not a new effect for every paint reference.
    # Keep one source-backed IR node per (file, target, kind, filter id) so a
    # glow used by several paths cannot create duplicate blockers.
    filter_id = kwargs.get("filter_id")
    if filter_id and kind in {"filter", "glow", "blur"}:
        for existing in model.visual_effects.values():
            if (existing.source_file == rel and existing.target_component == target_component
                    and existing.kind == kind and existing.filter_id == str(filter_id)):
                return existing
    existing = model.visual_effects.get(effect_id)
    if existing:
        return existing
    evidence_id = f"source:{rel}:{offset}"
    effect = VisualEffectIR(
        id=effect_id, kind=kind, source_file=rel, source_offset=offset,
        target_component=target_component, evidence_ids=[evidence_id],
        properties={"raw": re.sub(r"\s+", " ", raw).strip()[:800], **kwargs.pop("properties", {})},
        **kwargs,
    )
    model.visual_effects[effect_id] = effect
    model.source_evidence[evidence_id] = {
        "id": evidence_id, "kind": "visual-effect", "effect_id": effect_id,
        "source_file": rel, "offset": offset, "effect_type": kind,
    }
    return effect


def _resource_reference(root: Path, source_file: str, reference: str) -> Path | None:
    """Resolve a source-backed image URL without treating virtual URLs as files."""
    value = str(reference).strip().split("#", 1)[0].split("?", 1)[0]
    if not value or value.startswith(("data:", "http:", "https:", "blob:", "#", "figma:")):
        return None
    source = (root / source_file).resolve()
    candidates = [(source.parent / value).resolve(), (root / value.lstrip("/\\")).resolve()]
    if not value.startswith((".", "/", "\\")):
        candidates.extend((root / folder / value).resolve() for folder in ("public", "public/figma-assets", "figma-assets"))
    return next((item for item in candidates if item.is_file() and root in item.parents), None)


def _svg_image_references(text: str) -> list[str]:
    refs: list[str] = []
    for match in re.finditer(r"<(?:image|img)\b(?P<attrs>[^>]*)>", text, re.I):
        attrs = match.group("attrs")
        href = re.search(r"\b(?:href|xlink:href|src)\s*=\s*([\"'])(.*?)\1", attrs, re.I)
        if href and href.group(2).strip():
            refs.append(href.group(2).strip())
    return list(dict.fromkeys(refs))


def _css_filter_references(model: ReactProjectModel, rel: str, text: str) -> None:
    """Link external CSS ``url(#filter)`` uses to existing SVG definitions."""
    for match in re.finditer(r"\bfilter\s*:\s*url\(\s*#([^)'\"\s]+)\s*\)", text, re.I):
        filter_id = match.group(1)
        evidence_id = f"source:{rel}:{match.start()}"
        model.source_evidence[evidence_id] = {
            "id": evidence_id, "kind": "visual-effect-reference", "source_file": rel,
            "offset": match.start(), "effect_type": "filter", "filter_id": filter_id,
        }
        matches = [effect for effect in model.visual_effects.values()
                   if effect.filter_id and str(effect.filter_id).casefold() == filter_id.casefold()
                   and effect.kind in {"filter", "glow", "blur"}]
        if matches:
            for effect in matches:
                if evidence_id not in effect.evidence_ids:
                    effect.evidence_ids.append(evidence_id)
                refs = effect.properties.setdefault("references", [])
                reference = {"source_file": rel, "offset": match.start(), "property": "filter"}
                if reference not in refs:
                    refs.append(reference)
            continue
        # Keep unresolved CSS references explicit, but only once per id. The
        # capability pass can then report one blocker rather than one per use.
        _record_visual_effect(model, rel=rel, offset=match.start(), kind="filter",
                              raw=match.group(0), filter_id=filter_id,
                              properties={"css_reference": True, "references": [
                                  {"source_file": rel, "offset": match.start(), "property": "filter"}
                              ]})


def _visual_effect_evidence(component: Component, body: str, rel: str, offset: int, model: ReactProjectModel) -> None:
    """Collect CSS/SVG effect facts without choosing a target strategy."""
    filter_ids: set[str] = set()
    for match in re.finditer(r"<filter\b(?P<attrs>[^>]*)>(?P<content>[\s\S]*?)</filter>", body, re.I):
        attrs, content = match.group("attrs"), match.group("content")
        filter_id = _effect_value(attrs, "id") or f"inline-{match.start()}"
        primitives = []
        for primitive in re.finditer(r"<(?P<name>fe[A-Za-z]+)\b(?P<attrs>[^>]*)/?>", content, re.I):
            primitives.append({
                "name": primitive.group("name"),
                "attributes": {name: value for name, _quote, value in re.findall(r"([A-Za-z][\w-]*)\s*=\s*([\"'])(.*?)\2", primitive.group("attrs"))},
            })
        # The tuple form above cannot preserve the quote with a backreference
        # in all Python versions; retain the raw attribute string as well.
        for item, primitive in zip(primitives, re.finditer(r"<(?P<name>fe[A-Za-z]+)\b(?P<attrs>[^>]*)/?>", content, re.I)):
            item["raw_attributes"] = primitive.group("attrs").strip()
        filter_ids.add(filter_id)
        _record_visual_effect(
            model, rel=rel, offset=offset + match.start(), kind="filter", raw=match.group(0),
            target_component=component.id, filter_id=filter_id, primitives=primitives,
            properties={"svg_filter": True},
        )
        for primitive in primitives:
            if str(primitive.get("name", "")).casefold() != "fegaussianblur":
                continue
            radius = _float_value(re.search(r"\bstdDeviation\s*=\s*([\"'][^\"']+[\"'])", str(primitive.get("raw_attributes", "")), re.I).group(1) if re.search(r"\bstdDeviation\s*=", str(primitive.get("raw_attributes", "")), re.I) else None)
            _record_visual_effect(
                model, rel=rel, offset=offset + match.start(), kind="blur", raw=str(primitive),
                target_component=component.id, blur_radius=radius, filter_id=filter_id,
                properties={"svg_filter": True, "primitive": "feGaussianBlur"},
            )
    for match in re.finditer(r"\bfilter\s*=\s*([\"'])url\(#([^\"')]+)\)\1", body, re.I):
        filter_id = match.group(2)
        if filter_id not in filter_ids:
            _record_visual_effect(
                model, rel=rel, offset=offset + match.start(), kind="filter", raw=match.group(0),
                target_component=component.id, filter_id=filter_id,
                properties={"referenced": True},
            )
        if filter_id.casefold() != "text-glow" and (
                "glow" in filter_id.casefold()
                or re.search(r"<(?:path|circle|ellipse)\b", body[:match.start()], re.I)):
            _record_visual_effect(
                model, rel=rel, offset=offset + match.start(), kind="glow", raw=match.group(0),
                target_component=component.id, filter_id=filter_id,
                properties={"source": "svg-filter-reference"},
            )

    property_patterns = (
        ("filter", "filter"), ("box-shadow", "shadow"), ("boxShadow", "shadow"),
        ("text-shadow", "shadow"), ("textShadow", "shadow"),
        ("background", "gradient"), ("background-image", "gradient"),
        ("backgroundImage", "gradient"), ("mask", "mask"), ("mask-image", "mask"),
        ("maskImage", "mask"), ("clip-path", "clip"), ("clipPath", "clip"),
    )
    for property_name, kind in property_patterns:
        expression = re.compile(
            rf"\b{re.escape(property_name)}\s*:\s*(?:([\"'])(?P<quoted>.*?)(?:\1)|(?P<bare>[^;\n}}]+))",
            re.I | re.S,
        )
        for match in expression.finditer(body):
            raw = match.group(0)
            value = (match.group("quoted") or match.group("bare") or "").strip()
            if value in {"", "none", "transparent"}:
                continue
            props: dict[str, object] = {"property": property_name, "value": value}
            if kind == "gradient":
                props.update(_gradient_properties(value))
                if not props.get("gradient_type"):
                    continue
            elif kind == "shadow":
                props.update(_shadow_properties(value))
            elif kind == "filter":
                blur = re.search(r"\bblur\(\s*([^)]*)\)", value, re.I)
                if blur:
                    props["blur_radius"] = _float_value(blur.group(1))
                drop_shadow = re.search(r"\bdrop-shadow\(([^)]*)\)", value, re.I)
                if drop_shadow:
                    # Keep CSS drop-shadow explicit: LVGL has no exact filter
                    # equivalent, so the capability pass can report a bounded
                    # partial/fallback strategy instead of an opaque blocker.
                    props["drop_shadow"] = _shadow_properties(drop_shadow.group(1))
                    props["target_strategy"] = "partial-fallback"
                reference = re.fullmatch(r"url\(\s*#([^)'\"\s]+)\s*\)", value, re.I)
                if reference:
                    filter_id = reference.group(1)
                    linked = [effect for effect in model.visual_effects.values()
                              if effect.filter_id and str(effect.filter_id).casefold() == filter_id.casefold()
                              and effect.kind in {"filter", "glow", "blur"}]
                    if linked:
                        evidence_id = f"source:{rel}:{offset + match.start()}"
                        for effect in linked:
                            if evidence_id not in effect.evidence_ids:
                                effect.evidence_ids.append(evidence_id)
                            refs = effect.properties.setdefault("references", [])
                            item = {"source_file": rel, "offset": offset + match.start(), "property": property_name}
                            if item not in refs:
                                refs.append(item)
                        continue
            _record_visual_effect(
                model, rel=rel, offset=offset + match.start(), kind=kind, raw=raw,
                target_component=component.id, blur_radius=props.get("blur_radius") if kind == "filter" else None,
                color=props.get("color"), spread=props.get("spread"),
                offset_x=props.get("offset_x"), offset_y=props.get("offset_y"),
                gradient_type=props.get("gradient_type"), angle=props.get("angle"),
                stops=props.get("stops", []), mask_path=value if kind in {"mask", "clip"} else None,
                clip_path=value if kind == "clip" else None, properties=props,
            )


def _svg_arc_evidence(component: Component, body: str, rel: str, offset: int, model: ReactProjectModel) -> None:
    """Record every SVG arc paint and bind it to its logical gauge.

    Figma Make dashboards commonly paint one instrument several times.  The
    path's ``arc(CX_*, ...)`` call is the strongest ownership signal, so keep
    it together with the exact filter, stroke and dynamic angle expression.
    """
    constants = _local_numeric_constants(body)
    centers = {
        name: value
        for name, value in re.findall(
            r"\bCX_([A-Za-z][A-Za-z0-9_]*)\s*=\s*(-?\d+(?:\.\d+)?)", body
        )
    }
    filter_blurs: dict[str, float | None] = {}
    for match in re.finditer(
        r"<filter\b(?P<attrs>[^>]*)>(?P<content>[\s\S]*?)</filter>", body, re.I
    ):
        filter_id = _effect_value(match.group("attrs"), "id")
        if not filter_id:
            continue
        blur = re.search(
            r"<feGaussianBlur\b[^>]*\bstdDeviation\s*=\s*([\"'][^\"']+[\"'])",
            match.group("content"), re.I,
        )
        filter_blurs[filter_id] = _float_value(blur.group(1)) if blur else None

    def split_args(expression: str) -> list[str]:
        return [item.strip() for item in expression.split(",")]

    def gauge_for(cx_expression: str, fallback_index: int) -> tuple[str, str | None, float | None]:
        token = re.search(r"\bCX_([A-Za-z][A-Za-z0-9_]*)\b", cx_expression)
        if token:
            name = token.group(1)
            return f"gauge_{name}", name, _svg_numeric(centers.get(name), constants)
        cx = _svg_numeric(cx_expression, constants)
        if cx is not None and centers:
            name = min(centers, key=lambda item: abs(float(centers[item]) - cx))
            return f"gauge_{name}", name, _svg_numeric(centers[name], constants)
        return f"gauge_{fallback_index + 1}", None, cx

    coordinate_groups: dict[tuple[float, float], str] = {}

    def dynamic_binding(start: str | None, end: str | None) -> str | None:
        for expression in (end, start):
            value = str(expression or "").strip()
            if value and not _svg_numeric(value, constants) and re.search(
                r"(?:angle|value|pct|percent|progress|speed|rpm|temp|battery)", value, re.I
            ):
                return value
        return None

    def opacity_for(stroke: str, branches: list[str]) -> float | None:
        values = [stroke, *(branches[-1:] if branches else [])]
        for value in values:
            match = re.search(r"rgba\(\s*[^,]+\s*,\s*[^,]+\s*,\s*[^,]+\s*,\s*([0-9.]+)", value, re.I)
            if match:
                return float(match.group(1))
        return None

    paints: list[dict[str, object]] = []
    fallback_index = 0
    for svg_offset, _opening, content in _tag_blocks(body, "svg"):
        for tag in ("path", "circle", "ellipse"):
            for node_offset, node_opening in _opening_tags(content, tag):
                d_expression = _opening_tag_prop(node_opening, 0, "d") or ""
                filter_match = re.search(
                    r"\bfilter\s*=\s*([\"'])url\(#([^\"')]+)\)\1", node_opening, re.I
                )
                filter_id = filter_match.group(2) if filter_match else None
                arc_call = re.search(r"\barc\s*\(([^()]*)\)", d_expression, re.I)
                direct_arc = bool(re.search(r"\bA\s*[-+\d.]", d_expression, re.I))
                dashed = bool(re.search(r"stroke(?:Dash(?:array|offset)|-dash(?:array|offset))\s*=", node_opening, re.I))
                if tag != "path" and not dashed:
                    continue
                if tag == "path" and not arc_call and not dashed and not direct_arc:
                    continue
                if filter_id and filter_id.casefold() == "text-glow":
                    continue
                args = split_args(arc_call.group(1)) if arc_call else []
                cx_expression = args[0] if len(args) > 0 else (_opening_tag_prop(node_opening, 0, "cx") or "")
                cy_expression = args[1] if len(args) > 1 else (_opening_tag_prop(node_opening, 0, "cy") or "")
                radius_expression = args[2] if len(args) > 2 else (_opening_tag_prop(node_opening, 0, "r") or "")
                start = args[3] if len(args) > 3 else None
                end = args[4] if len(args) > 4 else None
                gauge_id, center_name, cx = gauge_for(cx_expression, fallback_index)
                cy = _svg_numeric(cy_expression, constants)
                if center_name is None and cx is not None and cy is not None:
                    key = (round(cx, 4), round(cy, 4))
                    gauge_id = coordinate_groups.setdefault(key, f"gauge_{len(coordinate_groups) + 1}")
                fallback_index += 1
                radius = _svg_numeric(radius_expression, constants)
                geometry = None
                if cx is not None and cy is not None and radius is not None and radius > 0:
                    geometry = {
                        "x": round(cx - radius, 2), "y": round(cy - radius, 2),
                        "width": round(radius * 2, 2), "height": round(radius * 2, 2),
                        "cx": round(cx, 2), "cy": round(cy, 2), "radius": round(radius, 2),
                    }
                stroke = _opening_tag_prop(node_opening, 0, "stroke") or ""
                stroke_width_expression = (
                    _opening_tag_prop(node_opening, 0, "strokeWidth")
                    or _opening_tag_prop(node_opening, 0, "stroke-width") or ""
                )
                branches = _svg_color_branches(body, stroke)
                representative = branches[-1] if branches else stroke
                source_opacity = _opening_tag_prop(node_opening, 0, "opacity")
                opacity = _svg_numeric(source_opacity, constants) if source_opacity else opacity_for(stroke, branches)
                if opacity is None:
                    opacity = 1.0
                if filter_id and filter_id.casefold() == "glow-wide":
                    role = "glow-wide"
                elif filter_id and filter_id.casefold() == "glow-soft":
                    role = "glow-soft"
                elif start and "redline" in start.casefold():
                    role = "red-zone"
                else:
                    role = "track"
                binding = dynamic_binding(start, end)
                paint = {
                    "gauge": center_name or gauge_id,
                    "gauge_id": gauge_id,
                    "role": role,
                    "filter_id": filter_id,
                    "blur": filter_blurs.get(filter_id) if filter_id else None,
                    "stroke": stroke,
                    "stroke_color": _svg_color(representative),
                    "stroke_branches": branches,
                    "stroke_binding": stroke if branches else None,
                    "stroke_width": _svg_numeric(stroke_width_expression, constants),
                    "stroke_width_expression": stroke_width_expression,
                    "opacity": round(float(opacity), 4),
                    "start": start,
                    "end": end,
                    "value_binding": binding,
                    "angle_binding": binding,
                    "layer_order": len([item for item in paints if item.get("gauge_id") == gauge_id]),
                    "source_offset": offset + svg_offset + node_offset,
                    "geometry": geometry,
                    "element": tag,
                }
                paints.append(paint)

    records: list[dict[str, object]] = []
    for gauge_id in dict.fromkeys(str(item["gauge_id"]) for item in paints):
        gauge_paints = [item for item in paints if item.get("gauge_id") == gauge_id]
        for layer_order, paint in enumerate(gauge_paints):
            paint["gauge_index"] = len(records)
            paint["layer_order"] = layer_order
        first = gauge_paints[0]
        geometry = first.get("geometry")
        records.append({
            "index": len(records), "gauge_id": gauge_id,
            "center_name": str(first.get("gauge") or ""),
            "cx": (geometry or {}).get("cx") if isinstance(geometry, dict) else None,
            "cy": (geometry or {}).get("cy") if isinstance(geometry, dict) else None,
            "radius": (geometry or {}).get("radius") if isinstance(geometry, dict) else None,
            "geometry": geometry,
            "source_offset": first.get("source_offset", offset),
            "paint_count": len(gauge_paints),
        })
    if not records:
        for index, match in enumerate(re.finditer(r"<Arc\b", body)):
            records.append({"index": index, "gauge_id": f"gauge_{index + 1}", "source_offset": offset + match.start()})
    if not records:
        return
    evidence_id = f"source:{rel}:{int(records[0].get('source_offset', offset))}:svg-arcs"
    model.source_evidence[evidence_id] = {
        "id": evidence_id, "kind": "svg-arcs", "source_file": rel,
        "offset": offset, "count": len(records), "records": records,
        "arc_paints": paints,
    }
    component.props["svg_arcs"] = records
    component.props["arcs"] = records
    component.props["arc_paints"] = paints
    component.props.setdefault("source_evidence_ids", []).append(evidence_id)


def _jsx_balanced_block(body: str, start: int, tag: str) -> tuple[int, int, str] | None:
    """Return one JSX element range while accounting for nested same-name tags."""
    opening_end = _jsx_opening_end(body, start)
    if opening_end is None:
        return None
    opening = body[start:opening_end + 1]
    if opening.rstrip().endswith("/>"):
        return start, opening_end + 1, ""
    depth = 1
    cursor = opening_end + 1
    open_pattern = re.compile(rf"<{re.escape(tag)}\b", re.I)
    close_pattern = re.compile(rf"</{re.escape(tag)}\s*>", re.I)
    while depth:
        next_open = open_pattern.search(body, cursor)
        next_close = close_pattern.search(body, cursor)
        if next_close is None:
            return None
        if next_open is not None and next_open.start() < next_close.start():
            nested_end = _jsx_opening_end(body, next_open.start())
            if nested_end is not None and not body[next_open.start():nested_end + 1].rstrip().endswith("/>"):
                depth += 1
            cursor = (nested_end + 1) if nested_end is not None else next_open.end()
            continue
        depth -= 1
        if depth == 0:
            return start, next_close.end(), body[opening_end + 1:next_close.start()]
        cursor = next_close.end()
    return None


def _jsx_style_value(opening: str, name: str) -> str | None:
    """Read a value from an inline JSX style object."""
    style = _opening_tag_prop(opening, 0, "style") or ""
    match = re.search(
        rf"\b{re.escape(name)}\s*:\s*(?:([\"'])(.*?)\1|([^,}}]+))",
        style, re.S,
    )
    if not match:
        return None
    return (match.group(2) or match.group(3) or "").strip()


def _css_box_values(raw: object, default: list[float]) -> list[float]:
    """Normalize CSS padding shorthand to top/right/bottom/left."""
    values = []
    for item in re.findall(r"-?\d+(?:\.\d+)?", str(raw or "")):
        try:
            values.append(float(item))
        except ValueError:
            pass
    if not values:
        return list(default)
    if len(values) == 1:
        return [values[0]] * 4
    if len(values) == 2:
        return [values[0], values[1], values[0], values[1]]
    if len(values) == 3:
        return [values[0], values[1], values[2], values[1]]
    return values[:4]


def _source_component_body(source: str, name: str) -> tuple[str, int] | None:
    """Find a local component definition for a capitalized JSX invocation."""
    for component_name, start, end in _component_spans(source):
        if component_name == name:
            return source[start:end], start
    return None


def _svg_primitive_evidence(svg: str, constants: dict[str, float]) -> list[dict[str, object]]:
    """Keep the small SVG primitive set used by status icons."""
    primitives: list[dict[str, object]] = []
    for tag in ("circle", "rect", "ellipse", "line", "path", "polyline", "polygon"):
        for node_offset, opening in _opening_tags(svg, tag):
            primitive: dict[str, object] = {"type": tag, "source_offset": node_offset}
            for name in ("cx", "cy", "r", "rx", "ry", "x", "y", "x1", "y1", "x2", "y2", "width", "height"):
                raw = _opening_tag_prop(opening, 0, name)
                if raw is not None:
                    primitive[name] = _svg_numeric(raw, constants)
            for name in ("d", "points", "fill", "stroke", "strokeLinecap", "strokeLinejoin", "strokeWidth", "stroke-width"):
                raw = _opening_tag_prop(opening, 0, name)
                if raw is not None:
                    primitive[name.replace("-", "_")] = raw
            stroke = primitive.get("stroke")
            fill = primitive.get("fill")
            if stroke:
                primitive["stroke_color"] = _svg_color(stroke)
                primitive["stroke_opa"] = round(_svg_alpha(stroke), 4)
            if fill:
                primitive["fill_color"] = _svg_color(fill)
                primitive["fill_opa"] = round(_svg_alpha(fill), 4)
            width = primitive.get("strokeWidth") or primitive.get("stroke_width")
            if width is not None:
                primitive["stroke_width"] = _svg_numeric(width, constants) or _float_value(width)
            primitives.append(primitive)
    primitives.sort(key=lambda item: int(item.get("source_offset", 0)))
    return primitives


def _status_vector_item(svg: str, source_offset: int, name: str | None = None) -> dict[str, object]:
    """Convert one inline SVG icon into renderer-neutral primitive evidence."""
    constants: dict[str, float] = {}
    opening = re.search(r"<svg\b[^>]*>", svg, re.I)
    attrs = opening.group(0) if opening else ""
    width = _svg_numeric(_opening_tag_prop(attrs, 0, "width"), constants) or 0
    height = _svg_numeric(_opening_tag_prop(attrs, 0, "height"), constants) or 0
    view_box = _opening_tag_prop(attrs, 0, "viewBox") or ""
    view_values = [float(value) for value in re.findall(r"-?\d+(?:\.\d+)?", view_box)]
    primitives = _svg_primitive_evidence(svg, constants)
    tags = {str(item.get("type")) for item in primitives}
    semantic = str(name or "vector").strip()
    if semantic == "vector":
        if height <= 12 and "circle" in tags and "path" in tags:
            semantic = "wifi"
        elif "rect" in tags and "path" in tags:
            semantic = "lock"
        elif "circle" in tags and "path" in tags:
            semantic = "user"
    item_type = "connectivity_indicator" if semantic.casefold() in {"wifi", "wlan", "connectivity"} else "vector_icon"
    return {
        "type": item_type,
        "name": semantic,
        "width": round(width, 2), "height": round(height, 2),
        "view_box": view_values if len(view_values) == 4 else [0, 0, width, height],
        "primitives": primitives,
        "source_offset": source_offset,
    }


def _status_component_svg(source: str, name: str, source_offset: int) -> dict[str, object] | None:
    """Extract the SVG returned by a local icon component."""
    definition = _source_component_body(source, name)
    if not definition:
        return None
    definition_body, definition_offset = definition
    svg_match = re.search(r"<svg\b", definition_body, re.I)
    if svg_match is None:
        return None
    block = _jsx_balanced_block(definition_body, svg_match.start(), "svg")
    if block is None:
        return None
    item = _status_vector_item(definition_body[block[0]:block[1]], source_offset + definition_offset + block[0], name)
    item["component"] = name
    return item


def _status_dynamic_text(content: str) -> tuple[str | None, object | None, str]:
    raw = content.strip()
    dynamic = re.fullmatch(r"\{\s*([\s\S]*?)\s*\}", raw)
    if dynamic:
        binding = dynamic.group(1).strip()
        return binding, None, ""
    text = _plain_jsx_text(raw)
    return None, _literal(text), text


def _status_bar_evidence(body: str, source: str, offset: int, canvas: dict[str, object]) -> list[dict[str, object]]:
    """Extract an edge status bar from JSX layout and local SVG primitives."""
    candidates: list[tuple[int, str, str]] = []
    for div_offset, opening in _opening_tags(body, "div"):
        if not re.search(r"\btop\s*:\s*0\b", opening) or not re.search(r"\bheight\s*:\s*32\b", opening):
            continue
        block = _jsx_balanced_block(body, div_offset, "div")
        if block:
            candidates.append((div_offset, opening, block[2]))
    if not candidates:
        return []
    top_offset, top_opening, top_content = candidates[0]
    width = int(float(canvas.get("width", 0) or 0))
    height = int(float(canvas.get("height", 0) or 0))
    background = _jsx_style_value(top_opening, "background") or _jsx_style_value(top_opening, "backgroundImage")
    layout = {
        "display": _jsx_style_value(top_opening, "display") or "flex",
        "direction": _jsx_style_value(top_opening, "flexDirection") or "row",
        "align": _jsx_style_value(top_opening, "alignItems") or "center",
        "justify": _jsx_style_value(top_opening, "justifyContent") or "space-between",
        "padding": _css_box_values(_jsx_style_value(top_opening, "padding"), [0, 14, 0, 14]),
        "box_sizing": _jsx_style_value(top_opening, "boxSizing") or "border-box",
    }
    if background:
        background_evidence: dict[str, object] = {"value": background, "kind": "gradient" if "gradient" in background.casefold() else "color"}
        parsed = _gradient_properties(background)
        if parsed:
            background_evidence.update(parsed)
    else:
        background_evidence = {"kind": "transparent"}

    div_blocks: list[tuple[int, str, str]] = []
    for div_offset, opening in _opening_tags(top_content, "div"):
        if not re.search(r"\bdisplay\s*:\s*[\"']?flex", opening):
            continue
        block = _jsx_balanced_block(top_content, div_offset, "div")
        if block:
            div_blocks.append((div_offset, opening, block[2]))
    start_block = next((item for item in div_blocks if not re.search(r"SignalBars|Wifi|wifi", item[2], re.I)), None)
    end_block = next((item for item in div_blocks if re.search(r"SignalBars|Wifi|wifi", item[2], re.I)), None)
    layout["slot_gaps"] = {
        "start": _float_value(_jsx_style_value(start_block[1], "gap")) if start_block else None,
        "end": _float_value(_jsx_style_value(end_block[1], "gap")) if end_block else None,
    }
    start_content = start_block[2] if start_block else ""
    end_content = end_block[2] if end_block else ""

    start_items: list[dict[str, object]] = []
    for svg_offset, opening in _opening_tags(start_content, "svg"):
        block = _jsx_balanced_block(start_content, svg_offset, "svg")
        if block:
            start_items.append(_status_vector_item(start_content[block[0]:block[1]], offset + top_offset + svg_offset, None))

    spans = _tag_blocks(start_content, "span")
    span_values: list[tuple[int, str, str | None, object | None, str]] = []
    for span_offset, opening, content in spans:
        binding, value, text = _status_dynamic_text(content)
        span_values.append((span_offset, opening, binding, value, text))
    for index, (span_offset, opening, binding, value, text) in enumerate(span_values):
        unit_candidate = next((entry for entry in span_values[index + 1:] if entry[0] >= span_offset and (entry[4].startswith(("°", "℃")) or entry[4].casefold() in {"c", "f", "°c", "°f"})), None)
        if unit_candidate is None:
            continue
        if not (binding or isinstance(value, (int, float)) or re.search(r"\b(?:temp|temperature|celsius)\b", text, re.I)):
            continue
        unit = unit_candidate[4]
        start_items.append({
            "type": "value_with_unit", "name": "temperature",
            "value": value if value is not None else text,
            "binding": binding, "unit": unit,
            "value_style": {"font_size": _jsx_style_value(opening, "fontSize"), "color": _jsx_style_value(opening, "color")},
            "unit_style": {"font_size": _jsx_style_value(unit_candidate[1], "fontSize"), "color": _jsx_style_value(unit_candidate[1], "color")},
            "source_offset": offset + top_offset + span_offset,
        })
        break

    center_items: list[dict[str, object]] = []
    for span_offset, opening, content in _tag_blocks(top_content, "span"):
        binding, value, text = _status_dynamic_text(content)
        if binding and re.search(r"\b(?:time|clock)[A-Za-z0-9_$]*\b", binding, re.I):
            format_name = "HH:mm"
            hour12 = not bool(re.search(r"\bhour12\s*:\s*false\b", source, re.I))
            interval_match = re.search(r"set(?:Time|TimeStr)\s*\([\s\S]{0,500}?setInterval\s*\([^,]+,\s*(\d+)\s*\)", source, re.I)
            center_items.append({
                "type": "clock", "binding": binding, "format": format_name,
                "hour12": hour12, "interval_ms": int(interval_match.group(1)) if interval_match else 1000,
                "source_offset": offset + top_offset + span_offset,
                "style": {"font_size": _jsx_style_value(opening, "fontSize"), "color": _jsx_style_value(opening, "color"), "letter_spacing": _jsx_style_value(opening, "letterSpacing")},
            })

    end_items: list[dict[str, object]] = []
    for tag_match in re.finditer(r"<([A-Z][A-Za-z0-9_]*)\b", end_content):
        name = tag_match.group(1)
        opening_end = _jsx_opening_end(end_content, tag_match.start())
        if opening_end is None:
            continue
        opening = end_content[tag_match.start():opening_end + 1]
        definition = _source_component_body(source, name)
        definition_body = definition[0] if definition else ""
        if re.search(r"\.map\s*\(|Array\.from\s*\(", definition_body) and re.search(r"\bheight\s*:", definition_body):
            count_match = re.search(r"\[\s*([^\]]+)\s*\]\s*\.map\s*\(", definition_body)
            array_from = re.search(r"Array\.from\s*\(\s*\{\s*length\s*:\s*(\d+)", definition_body)
            count = len([item for item in (count_match.group(1).split(",") if count_match else []) if item.strip()]) if count_match else int(array_from.group(1)) if array_from else 0
            level_raw = _opening_tag_prop(opening, 0, "level")
            level_binding, level_value, _ = _status_dynamic_text("{" + level_raw + "}") if level_raw else (None, None, "")
            if level_raw and re.fullmatch(r"\d+", level_raw.strip()):
                level_value = int(level_raw.strip())
                level_binding = None
            end_items.append({
                "type": "signal_bars", "name": "signal", "bar_count": count or 3,
                "level": level_value if level_value is not None else (level_binding or level_raw),
                "level_binding": level_binding,
                "width": _float_value(re.search(r"\bwidth\s*:\s*([\d.]+)", definition_body).group(1)) if re.search(r"\bwidth\s*:\s*([\d.]+)", definition_body) else 3,
                "gap": _float_value(re.search(r"\bgap\s*:\s*([\d.]+)", definition_body).group(1)) if re.search(r"\bgap\s*:\s*([\d.]+)", definition_body) else 2,
                "height_base": _float_value(re.search(r"\bheight\s*:\s*([\d.]+)\s*\+", definition_body).group(1)) if re.search(r"\bheight\s*:\s*([\d.]+)\s*\+", definition_body) else 3,
                "height_step": _float_value(re.search(r"\bheight\s*:\s*[\d.]+\s*\+\s*\w+\s*\*\s*([\d.]+)", definition_body).group(1)) if re.search(r"\bheight\s*:\s*[\d.]+\s*\+\s*\w+\s*\*\s*([\d.]+)", definition_body) else 3,
                "source_offset": offset + top_offset + tag_match.start(),
            })
            continue
        if definition_body and re.search(r"<svg\b", definition_body, re.I):
            icon = _status_component_svg(source, name, offset + top_offset + tag_match.start())
            if icon:
                icon["type"] = "connectivity_indicator" if icon.get("name", "").casefold() in {"wifi", "wlan"} or len(icon.get("primitives", [])) >= 3 else icon.get("type", "vector_icon")
                icon["name"] = "wifi" if icon["type"] == "connectivity_indicator" else icon.get("name")
                end_items.append(icon)

    start_items.sort(key=lambda item: int(item.get("source_offset", 0)))
    end_items.sort(key=lambda item: int(item.get("source_offset", 0)))
    center_items.sort(key=lambda item: int(item.get("source_offset", 0)))
    bar = {
        "id": "top_status", "edge": "top",
        "frame": {"x": 0, "y": 0, "width": width, "height": min(32, height)},
        "background": background_evidence,
        "layout": layout,
        "slots": {"start": start_items, "center": center_items, "end": end_items},
        "source_offset": offset + top_offset,
    }
    return [bar]


def _gauge_scene_evidence(component: Component, body: str, rel: str, offset: int, model: ReactProjectModel) -> None:
    """Extract one fixed-canvas instrument scene without relying on filenames.

    All values below come from source syntax: SVG dimensions, named center
    constants, the Ticks loop, dynamic angle expressions, and text nodes.  The
    scene record is deliberately renderer-neutral so adapters can choose one
    compound draw operation later.
    """
    if component.kind != "composite_gauge_scene":
        return
    source_text = body
    try:
        candidate = (model.source_dir / rel).resolve()
        if candidate.is_file():
            source_text = candidate.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        pass
    constants = _local_numeric_constants(source_text)
    svg_match = re.search(r"<svg\b(?P<attrs>[^>]*)>", body, re.I)
    attrs = svg_match.group("attrs") if svg_match else ""
    width_raw = _opening_tag_prop(body, svg_match.start(), "width") if svg_match else ""
    height_raw = _opening_tag_prop(body, svg_match.start(), "height") if svg_match else ""
    view_match = re.search(r"\bviewBox\s*=\s*[\"']\s*0\s+0\s+(\d+(?:\.\d+)?)\s+(\d+(?:\.\d+)?)", attrs, re.I)
    width = _svg_numeric(width_raw, constants) or (float(view_match.group(1)) if view_match else None)
    height = _svg_numeric(height_raw, constants) or (float(view_match.group(2)) if view_match else None)
    if width is None or height is None:
        return
    starts = re.search(r"\bSTART\s*=\s*(-?\d+(?:\.\d+)?)", source_text)
    sweeps = re.search(r"\bSWEEP\s*=\s*(-?\d+(?:\.\d+)?)", source_text)
    tw = re.search(r"\bTW\s*=\s*(-?\d+(?:\.\d+)?)", body)
    center_defs = [(name, float(value), match.start()) for match in re.finditer(
        r"\bCX_([A-Za-z][A-Za-z0-9_]*)\s*=\s*(-?\d+(?:\.\d+)?)", body
    ) for name, value in [(match.group(1), match.group(2))]]
    cy = _svg_numeric("CY", constants)
    radius = _svg_numeric("R", constants)
    value_max: dict[str, tuple[str, float, int]] = {}
    for match in re.finditer(r"\b([A-Za-z_$][\w$]*)Pct\s*=\s*Math\.min\(\s*([A-Za-z_$][\w$]*)\s*/\s*(\d+(?:\.\d+)?)", body):
        value_max[match.group(1) + "Pct"] = (match.group(2), float(match.group(3)), offset + match.start())
    angle_bindings: dict[str, tuple[str, int]] = {}
    for match in re.finditer(r"\b([A-Za-z_$][\w$]*)\s*=\s*START\s*\+\s*([A-Za-z_$][\w$]*)\s*\*\s*SWEEP", body):
        angle_bindings[match.group(1)] = (match.group(2), offset + match.start())
    redline = None
    red_match = re.search(r"\bredlineTick\s*=\s*Math\.round\(\s*\(\s*(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)\s*\)\s*\*\s*(\d+)\s*\)", body)
    if red_match:
        redline = {"value": float(red_match.group(1)), "max": float(red_match.group(2)),
                   "tick_total": int(red_match.group(3)), "tick": round(float(red_match.group(1)) / float(red_match.group(2)) * int(red_match.group(3))),
                   "source_offset": offset + red_match.start()}
    ticks_by_center: dict[str, dict[str, object]] = {}
    tick_loop_match = re.search(r"Array\.from\s*\(\s*\{\s*length\s*:\s*([^}]+)\}\s*,", source_text)
    for match in re.finditer(r"<Ticks\b", body):
        cx_expr = _opening_tag_prop(body, match.start(), "cx") or ""
        center_match = re.search(r"CX_([A-Za-z][A-Za-z0-9_]*)", cx_expr)
        center = center_match.group(1) if center_match else ""
        total = _svg_numeric(_opening_tag_prop(body, match.start(), "total") or "", constants)
        major = _svg_numeric(_opening_tag_prop(body, match.start(), "majorEvery") or "", constants)
        labels_raw = _opening_tag_prop(body, match.start(), "labels") or ""
        labels = re.findall(r"[\"']([^\"']*)[\"']", labels_raw)
        red_expr = _opening_tag_prop(body, match.start(), "redlineIdx")
        ticks_by_center[center] = {
            "total": int(total or 0), "major_every": int(major or 1), "labels": labels,
            "redline_binding": red_expr, "source_offset": offset + match.start(),
            "loop_source_offset": tick_loop_match.start() if tick_loop_match else None,
        }
    text_nodes: list[dict[str, object]] = []
    for node_offset, opening, content in _tag_blocks(body, "text"):
        x = _svg_numeric(_opening_tag_prop(opening, 0, "x") or "", constants)
        y = _svg_numeric(_opening_tag_prop(opening, 0, "y") or "", constants)
        raw = content.strip()
        dynamic = re.fullmatch(r"\{\s*([\s\S]*?)\s*\}", raw)
        text_nodes.append({"x": x, "y": y, "text": _plain_jsx_text(raw),
                           "binding": dynamic.group(1).strip() if dynamic else None,
                           "source_offset": offset + node_offset})
    records = {str(item.get("gauge_id")): dict(item) for item in component.props.get("svg_arcs", []) if isinstance(item, dict)}
    gauges: list[dict[str, object]] = []
    for index, (center_name, cx, center_offset) in enumerate(center_defs):
        gauge_id = f"gauge_{center_name}"
        record = records.get(gauge_id, {})
        geometry = record.get("geometry") or {"x": cx - (radius or 0), "y": (cy or 0) - (radius or 0),
                                               "width": (radius or 0) * 2, "height": (radius or 0) * 2,
                                               "cx": cx, "cy": cy, "radius": radius}
        paint_layers = [dict(item) for item in component.props.get("arc_paints", [])
                        if isinstance(item, dict) and str(item.get("gauge_id")) == gauge_id]
        # A filtered foreground path is SourceGraphic plus blur.  Preserve the
        # source layer and add the unfiltered paint as a derived foreground.
        soft = next((item for item in reversed(paint_layers) if item.get("role") == "glow-soft"), None)
        if soft:
            foreground = dict(soft)
            foreground.update({"role": "foreground", "filter_id": None, "blur": None,
                               "derived_from": soft.get("source_offset")})
            foreground["layer_order"] = len(paint_layers)
            paint_layers.append(foreground)
        binding = None
        angle_binding = None
        max_value = None
        for paint in paint_layers:
            angle = str(paint.get("value_binding") or "")
            if angle in angle_bindings:
                pct_name, _ = angle_bindings[angle]
                value_info = value_max.get(pct_name)
                if value_info:
                    binding, max_value = value_info[0], value_info[1]
                    angle_binding = angle
                    break
        center_text = [item for item in text_nodes if item.get("x") is not None and abs(float(item["x"]) - cx) < 2]
        readout = next((item for item in center_text if item.get("binding")), None)
        unit_node = next((item for item in center_text if item is not readout and item.get("y") is not None and cy is not None and float(item["y"]) > cy), None)
        tick = ticks_by_center.get(center_name, {})
        gauges.append({
            "gauge_id": gauge_id, "center": center_name, "cx": cx, "cy": cy, "radius": radius,
            "geometry": geometry, "start": starts.group(1) if starts else None,
            "sweep": sweeps.group(1) if sweeps else None, "track_width": float(tw.group(1)) if tw else None,
            "range": {"min": 0, "max": int(max_value or (max(tick.get("labels", [0]), key=lambda value: float(value) if str(value).replace('.', '', 1).isdigit() else 0) if tick.get("labels") else 100))},
            "value_binding": binding, "angle_binding": angle_binding,
            "ticks": tick, "redline": redline if tick.get("redline_binding") or center_name.casefold() in {"r", "right", "rpm"} else None,
            "paint_layers": paint_layers, "paints": paint_layers,
            "readout": {"value_binding": readout.get("binding") if readout else binding,
                         "text": readout.get("text") if readout else None,
                         "unit": unit_node.get("text") if unit_node else None,
                         "source_offset": readout.get("source_offset") if readout else offset + center_offset},
            "source_offset": offset + center_offset,
        })
    canvas = {"width": int(width), "height": int(height), "view_box": [0, 0, int(width), int(height)],
              "source_offset": offset + (svg_match.start() if svg_match else 0)}
    status_bars = _status_bar_evidence(body, source_text, offset, canvas)
    scene = {
        "kind": "composite-gauge-scene", "canvas": canvas,
        "constants": {"start": float(starts.group(1)) if starts else None, "sweep": float(sweeps.group(1)) if sweeps else None,
                      "cy": cy, "radius": radius, "track_width": float(tw.group(1)) if tw else None},
        "gauges": gauges, "tick_loop": {"source_offset": tick_loop_match.start() if tick_loop_match else (next(iter(ticks_by_center.values())).get("source_offset") if ticks_by_center else None),
                                           "expression": tick_loop_match.group(0) if tick_loop_match else "Ticks component uses Array.from tick loop"},
        "redline": redline, "text_layers": text_nodes, "status_bars": status_bars,
        "regions": {"top_status": bool(status_bars) or bool(re.search(r"top\s*:\s*0|height\s*:\s*32", body)),
                    "bottom_energy": bool(re.search(r"bottom\s*:\s*0|BATTERY|KM RANGE", body)),
                    "center_gear": bool(re.search(r"\bgear\b|>GEAR<", body, re.I)),
                    "battery_segments": 10 if re.search(r"(?:Array\.from\s*\(\s*\{\s*length\s*:\s*10|<BatterySegments\b)", body) else 0},
        "background": {"radial_gradient": bool(re.search(r"radial-gradient", body)), "vignette": bool(re.search(r"Vignette|rgba\(0,0,0,0\.55", body, re.I))},
        "source_offsets": {"canvas": canvas["source_offset"], "centers": [offset + item[2] for item in center_defs],
                           "ticks": [int(item.get("source_offset")) for item in ticks_by_center.values() if item.get("source_offset") is not None],
                           "status_bars": [int(item.get("source_offset")) for item in status_bars if item.get("source_offset") is not None]},
    }
    evidence_id = f"source:{rel}:{scene['canvas']['source_offset']}:gauge-scene"
    model.source_evidence[evidence_id] = {"id": evidence_id, "kind": "composite-gauge-scene", "source_file": rel,
                                          "offset": scene["canvas"]["source_offset"], "scene": scene}
    component.props["gauge_scene"] = scene
    component.props["gauges"] = gauges
    component.props.setdefault("source_evidence_ids", []).append(evidence_id)


def _opening_tags(body: str, tag: str) -> list[tuple[int, str]]:
    """Return exact JSX opening tags, including self-closing native elements."""
    result: list[tuple[int, str]] = []
    for match in re.finditer(rf"<{re.escape(tag)}\b", body):
        end = _jsx_opening_end(body, match.start())
        if end is not None:
            result.append((match.start(), body[match.start():end + 1]))
    return result


def _control_value(opening: str, component: Component, prop: str = "value") -> object | None:
    raw = _opening_tag_prop(opening, 0, prop)
    initial = _state_initial(component, raw)
    return initial if initial is not None else raw


def _opening_events(opening: str) -> list[str]:
    """Return only React event props actually present on this JSX node.

    The component-level event list is intentionally broader (it is useful for
    a screen audit).  Control evidence must not borrow a sibling's handler,
    otherwise a generated button could look interactive simply because a
    nearby switch has an ``onChange``.  Keeping the property names here makes
    the later target decision independently auditable.
    """
    return list(dict.fromkeys(re.findall(r"\b(on(?:Click|Change|ValueChange|Input|Drag(?:End)?|PointerUp|TouchEnd))\s*=", opening)))


def _generic_control_evidence(component: Component, body: str) -> list[dict[str, object]]:
    """Capture concrete JSX controls, never inferred from screenshots or CSS.

    Each record is deliberately small and JSON-shaped so generation can audit
    exactly which tag, literal values, and data source produced a target widget.
    """
    controls: list[dict[str, object]] = []

    # Text nested inside a semantic control is rendered by that control's
    # native label/button/tab/list item.  Emitting it again as a free-standing
    # target label duplicates captions (a frequent pattern in Figma Make's
    # `<Button><span>Save</span></Button>` output).  Only suppress descendants
    # of controls whose renderer already owns the caption; visible labels next
    # to a control remain independent evidence.
    consumed_text_ranges: list[tuple[int, int]] = []
    for owner_tag in ("Button", "button", "TabsTrigger", "li", "option"):
        for owner_offset, owner_opening, owner_content in _tag_blocks(body, owner_tag):
            consumed_text_ranges.append((owner_offset, owner_offset + len(owner_opening) + len(owner_content) + len(f"</{owner_tag}>")))

    def text_is_owned(offset: int) -> bool:
        return any(start < offset < end for start, end in consumed_text_ranges)

    def add_openings(kind: str, tags: tuple[str, ...], extra: dict[str, object] | None = None) -> None:
        for tag in tags:
            for offset, opening in _opening_tags(body, tag):
                item: dict[str, object] = {"kind": kind, "tag": tag, "offset": offset}
                value = _control_value(opening, component)
                if value is not None:
                    item["value"] = value
                checked = _control_value(opening, component, "checked")
                if checked is not None:
                    item["checked"] = checked
                disabled = _opening_tag_prop(opening, 0, "disabled")
                if disabled is not None:
                    item["disabled"] = disabled
                if kind == "slider":
                    for prop in ("min", "max", "step"):
                        raw = _opening_tag_prop(opening, 0, prop)
                        if raw is not None:
                            item[prop] = raw
                if kind == "input":
                    placeholder = _opening_tag_prop(opening, 0, "placeholder")
                    if placeholder:
                        item["placeholder"] = placeholder
                if extra:
                    item.update(extra)
                events = _opening_events(opening)
                if events:
                    item["events"] = events
                controls.append(item)

    # Block controls retain their rendered label; self-closing controls use a
    # value/placeholder instead.  Prefix-safe matching avoids SelectTrigger
    # accidentally becoming a Select instance.
    mapped_icon_counts: dict[str, int] = {}
    for tag in ("Button", "button"):
        for offset, opening, content in _tag_blocks(body, tag):
            # For ``options.map(option => <Button><Icon /></Button>)`` use the
            # matching array item, preserving the source order exactly.
            maps = list(re.finditer(r"\b([A-Za-z_$][\w$]*)\.map\s*\(", body[:offset]))
            map_source = maps[-1].group(1) if maps else None
            icon_index = mapped_icon_counts.get(map_source or "", 0)
            icon_name, icon_source = _icon_for_control(component, body, offset, content, icon_index)
            if map_source:
                mapped_icon_counts[map_source] = icon_index + 1
            role = _opening_tag_prop(opening, 0, "role")
            pressed = _opening_tag_prop(opening, 0, "aria-pressed")
            visible_source = re.sub(r"\{\/\*[\s\S]*?\*\/\}", "", content)
            visible_text = _plain_jsx_text(visible_source)
            boolean_state = any(isinstance(state, dict) and state.get("name") == pressed
                                and isinstance(state.get("initial"), bool)
                                for state in component.props.get("state", []))
            boolean_expression = bool(re.fullmatch(r"[A-Za-z_$][\w$]*|true|false", str(pressed or "").strip()))
            pure_pressed = pressed is not None and boolean_expression and (visible_text in {"", "…"} or boolean_state)
            item = {"kind": "switch" if role == "switch" or pure_pressed else "button", "tag": tag, "offset": offset,
                    "text": _plain_jsx_text(content),
                    "disabled": _opening_tag_prop(opening, 0, "disabled"),
                    "events": _opening_events(opening),
                    "className": _opening_tag_prop(opening, 0, "className") or ""}
            aria_label = _opening_tag_prop(opening, 0, "aria-label")
            if aria_label is not None:
                item["aria_label"] = aria_label
            if pressed is not None:
                item["checked"] = _literal(pressed)
            if icon_name:
                item["icon"] = icon_name
                item["icon_source"] = icon_source
                item.update(_icon_props(icon_name, content, component, body, offset, icon_index))
            controls.append(item)
    # Decorative icon tiles are structural UI, not text.  A frequent Figma
    # Make pattern is a colored header tile (`<div className="bg-[#...] p-2">`
    # containing a Lucide icon).  Preserve it so it does not disappear when
    # the surrounding header is converted to a target container.
    for offset, opening, content in _tag_blocks(body, "div"):
        classes = _opening_tag_prop(opening, 0, "className") or ""
        if "bg-[#" not in classes or "p-" not in classes or "<button" in content:
            continue
        icon_name, icon_source = _icon_for_control(component, body, offset, content)
        if not icon_name:
            continue
        item = {
            "kind": "icon_tile", "tag": "div", "offset": offset,
            "text": "", "className": classes, "icon": icon_name,
            "icon_source": icon_source or "jsx",
        }
        bg_match = re.search(r"bg-\[#([0-9A-Fa-f]{6})\]", classes)
        if bg_match:
            item["icon_tile_background"] = "#" + bg_match.group(1)
        item["icon_color"] = "#ffffff"
        item.update(_icon_props(icon_name, content, component, body, offset))
        controls.append(item)
    for tag in ("label", "Label", "p", "span", "h1", "h2", "h3", "h4", "h5", "h6", "Text"):
        for offset, _, content in _tag_blocks(body, tag):
            if text_is_owned(offset):
                continue
            text = _plain_jsx_text(content)
            if text != "…":
                controls.append({"kind": "label", "tag": tag, "offset": offset, "text": text})
    # Text-only layout nodes (for example the HomePage status bar's ``Ready``
    # div) are real visual elements, but containers that wrap buttons must not
    # be flattened into duplicate labels.
    for offset, opening, content in _tag_blocks(body, "div"):
        if "<button" in content or re.search(r"<(?:Button|Switch|Checkbox|Input|img|Image)\b", content):
            continue
        classes = _opening_tag_prop(opening, 0, "className") or ""
        if "text-" not in classes and "text" not in classes:
            continue
        text = _plain_jsx_text(content)
        if text not in {"…", ""} and not re.search(r"<div\b[\s\S]*<div\b", content):
            controls.append({"kind": "label", "tag": "div", "offset": offset, "text": text})
    for tag in ("img", "Image"):
        for offset, opening in _opening_tags(body, tag):
            src = _opening_tag_prop(opening, 0, "src")
            alt = _opening_tag_prop(opening, 0, "alt")
            if src is not None:
                controls.append({"kind": "image", "tag": tag, "offset": offset, "src": src, "alt": alt or "", "events": _opening_events(opening)})
    add_openings("switch", ("Switch", "switch"))
    add_openings("checkbox", ("Checkbox",))
    for offset, opening in _opening_tags(body, "input"):
        input_type = _opening_tag_prop(opening, 0, "type")
        kind = ("checkbox" if input_type == "checkbox" else "radio" if input_type == "radio"
                else "slider" if input_type == "range" else "input")
        item: dict[str, object] = {"kind": kind, "tag": "input", "offset": offset}
        value = _control_value(opening, component)
        if value is not None: item["value"] = value
        checked = _control_value(opening, component, "checked")
        if checked is not None: item["checked"] = checked
        placeholder = _opening_tag_prop(opening, 0, "placeholder")
        if placeholder: item["placeholder"] = placeholder
        aria_label = _opening_tag_prop(opening, 0, "aria-label")
        if aria_label is not None: item["aria_label"] = aria_label
        if kind == "slider":
            for prop in ("min", "max", "step"):
                raw = _opening_tag_prop(opening, 0, prop)
                if raw is not None: item[prop] = raw
        events = _opening_events(opening)
        if events: item["events"] = events
        controls.append(item)
    add_openings("slider", ("Slider",))
    add_openings("input", ("Input", "Textarea", "TextArea", "textarea"))

    # Native HTML selects do not use the shadcn/Radix ``SelectItem`` pattern
    # handled above.  Preserve their literal option evidence separately so a
    # real `<select>` still becomes a native dropdown rather than being lost.
    for offset, opening, content in _tag_blocks(body, "select"):
        options = [_plain_jsx_text(option_body) for _, _, option_body in _tag_blocks(content, "option")]
        controls.append({
            "kind": "select", "tag": "select", "offset": offset,
            "value": _control_value(opening, component),
            "options": [option for option in options if option],
            "events": _opening_events(opening),
        })

    for offset, opening, content in _tag_blocks(body, "RadioGroup"):
        options = []
        for _, item_opening in _opening_tags(content, "RadioGroupItem"):
            value = _opening_tag_prop(item_opening, 0, "value")
            options.append(value or "Option")
        controls.append({"kind": "radio_group", "tag": "RadioGroup", "offset": offset,
                         "value": _control_value(opening, component), "options": options,
                         "events": _opening_events(opening)})
    for tag in ("ul", "ol", "List"):
        for offset, _, content in _tag_blocks(body, tag):
            items = [_plain_jsx_text(item_content) for _, _, item_content in _tag_blocks(content, "li")]
            source_match = re.search(r"\b([A-Za-z_$][\w$]*)\.map\s*\(", content)
            source = source_match.group(1) if source_match else None
            evidence = _array_named(component, source)
            if evidence and isinstance(evidence.get("items"), list):
                items = [str(next((entry[key] for key in ("label", "name", "title", "value", "id") if entry.get(key) not in (None, "")), "…"))
                         for entry in evidence["items"] if isinstance(entry, dict)]
            controls.append({"kind": "list", "tag": tag, "offset": offset, "items": items, "item_source": source,
                             "events": _opening_events(content[:_jsx_opening_end(content, 0) + 1]) if content.startswith("<") and _jsx_opening_end(content, 0) is not None else []})
    for offset, opening, content in _tag_blocks(body, "Tabs"):
        labels = [_plain_jsx_text(item_content) for _, _, item_content in _tag_blocks(content, "TabsTrigger")]
        values = [_opening_tag_prop(item_opening, 0, "value") for _, item_opening, _ in _tag_blocks(content, "TabsTrigger")]
        controls.append({"kind": "tabs", "tag": "Tabs", "offset": offset,
                         "value": _control_value(opening, component),
                         "tabs": [label for label in labels if label],
                         "tab_values": [value for value in values if value],
                         "events": _opening_events(opening)})
    # Preserve DOM/source order.  The extraction passes are grouped by
    # semantic tag, but the generated snapshot must follow the React layout
    # order (status bar before the button grid, etc.).
    controls.sort(key=lambda item: int(item.get("offset", 0)) if isinstance(item, dict) else 0)
    return controls


def _visibility_evidence(component: Component, body: str, offset: int) -> None:
    """Record source-backed JSX conditional visibility without evaluating it."""
    records: list[dict[str, object]] = []
    pattern = re.compile(r"\{\s*(?P<expr>[^{}\n]+?)\s*&&\s*(?:\(\s*)?<(?P<tag>[A-Za-z][\w.]*)\b")
    for match in pattern.finditer(body):
        records.append({
            "expression": _compact(match.group("expr")),
            "target": match.group("tag"),
            "source_offset": offset + match.start(),
        })
    if records:
        component.props["visibility"] = records


def _events(body: str, component: Component, rel: str, offset: int, model: ReactProjectModel) -> None:
    for match in re.finditer(r"\bon(?P<name>[A-Z][A-Za-z]+)\s*=\s*\{", body):
        end = _balanced_end(body, match.end() - 1)
        if not end: continue
        raw = match.group("name")
        kind = {"Click":"click", "Change":"change", "ValueChange":"change", "Input":"input", "DragEnd":"drag", "Drag":"drag", "PointerUp":"drag", "TouchEnd":"swipe"}.get(raw, "click")
        tags = list(re.finditer(r"<([A-Za-z][\w.]*)\b[^>]*$", body[:match.start()]))
        event_id = f"{rel}:{offset + match.start()}"
        model.events[event_id] = Event(event_id, kind, _compact(body[match.end():end - 1]), tags[-1].group(1) if tags else None, {"react_event":raw})
        component.event_ids.append(event_id)


def _component_evidence(component: Component, body: str, rel: str, offset: int, model: ReactProjectModel) -> None:
    _visual_effect_evidence(component, body, rel, offset, model)
    _svg_arc_evidence(component, body, rel, offset, model)
    _gauge_scene_evidence(component, body, rel, offset, model)
    _visibility_evidence(component, body, offset)
    states: list[dict[str, object]] = []
    for match in STATE_DECL_RE.finditer(body):
        initial = match.group("initial") or match.group("reducer_initial") or ""
        states.append({"name":match.group("state"), "setter":match.group("setter"), "hook":"useReducer" if match.group("reducer") else "useState", "initial":_literal(initial), "reducer":_compact(match.group("reducer") or "") or None})
    if states: component.props["state"] = states
    timers = re.findall(r"setInterval\s*\([^,]+,\s*(\d+)\s*\)", body)
    if timers:
        component.props["timers"] = [{"interval_ms": int(value), "source": "setInterval"} for value in timers]
        component.props["dynamic_bindings"] = list(dict.fromkeys(re.findall(r"\b(rpm|temp|pressure|battery|volt|load|freq|uptime|time)\b", body)))
    arrays: list[dict[str, object]] = []
    for match in ARRAY_DECL_RE.finditer(body):
        end = _balanced_end(body, match.end() - 1, "[", "]")
        if end: arrays.append(_array_evidence(match.group("name"), body[match.end() - 1:end]))
    if arrays: component.props["arrays"] = arrays
    for match in ARRAY_FROM_RE.finditer(body):
        end = _balanced_end(body, body.find("(", match.start()), "(", ")")
        if end:
            arrays.append(_array_from_evidence(match.group("name"), int(match.group("length")), body[match.start():end]))
    if arrays:
        component.props["arrays"] = arrays
    _events(body, component, rel, offset, model)
    motion = bool(re.search(r"<(?:motion\.|AnimatePresence\b)|\bvariants\s*=|\banimate\s*=", body))
    css = bool(re.search(r"(?:transition-[\w\[\],-]+|animate-[\w-]+|duration-\d+)", body))
    if motion or css: component.props["animations"] = {"motion":motion, "css_transition":css, "spring":bool(re.search(r"type\s*:\s*[\"']spring", body))}
    if component.kind == "carousel":
        map_source = next(iter(re.findall(r"\b(\w+)\.map\s*\(", body)), None)
        indexed_sources = re.findall(r"\b(\w+)\s*\[\s*\w+\s*\]", body)
        # A stack commonly maps an index queue then looks up presentation data
        # in a separate imported array: `indices.map(index => items[index])`.
        data_source = next((name for name in indexed_sources if name != map_source), map_source)
        component.props["carousel"] = {"gesture":"drag" if any(model.events[event_id].kind == "drag" for event_id in component.event_ids) else "swipe", "rotates_indices":bool(re.search(r"set\w+\s*\(\s*\(?\w*\)?\s*=>\s*\[", body)), "item_source":data_source, "item_sources":list(dict.fromkeys(indexed_sources)), "index_source":map_source}
    if component.kind == "chart":
        types = re.findall(r"<(LineChart|AreaChart|BarChart|PieChart|RadarChart|ScatterChart)\b", body)
        series = []
        for match in re.finditer(r"<(Line|Area|Bar|Pie|Radar|Scatter)\b", body):
            key = _opening_tag_prop(body, match.start(), "dataKey")
            if key: series.append({"type":match.group(1).lower(), "data_key":key})
        component.props["chart"] = {"types":list(dict.fromkeys(types)), "series":series, "data":next((value for value in (_jsx_prop(body, tag, "data") for tag in types) if value), None), "x_key":_jsx_prop(body, "XAxis", "dataKey")}
    # These evidences are independent: a report can contain both a table and
    # an export dropdown.  The generator therefore adapts each present intent
    # instead of forcing a lossy single component kind.
    selects = _select_evidence(component, body)
    if selects:
        component.props["selects"] = selects
    table = _table_evidence(component, body)
    if table:
        component.props["table"] = table
    scroll = _scroll_evidence(body)
    if scroll:
        component.props["scroll"] = scroll
    child_bindings: dict[str, list[dict[str, object]]] = {}
    for opening_match in re.finditer(r"<([A-Z][A-Za-z0-9_]*)\b", body):
        tag = opening_match.group(1)
        data_value = _opening_tag_prop(body, opening_match.start(), "data")
        if data_value:
            child_bindings.setdefault(tag, []).append({"data": data_value, "offset": opening_match.start()})
    if child_bindings:
        component.props["child_bindings"] = child_bindings
    controls = _generic_control_evidence(component, body) or []
    meters = []
    for match in re.finditer(r"<([A-Z][A-Za-z0-9_]*)\b([^>]*)>", body):
        attrs = match.group(2)
        if match.group(1) == "Gauge" or "role=\"meter\"" in attrs:
            def attr(name, default=None):
                # Reuse the JSX-aware reader so literal strings and expression
                # props such as value={values.rpm} are both retained.
                value = _opening_tag_prop(body, match.start(), name)
                return value if value is not None else default
            meters.append({"label": attr("label", ""), "unit": attr("unit", ""),
                           "min": attr("min", "0"), "max": attr("max", "100"),
                           "value": attr("value", "0"), "warn": attr("warn"), "danger": attr("danger"),
                           # Used after SourceInstanceIR is built to identify
                           # this legacy regex record's exact JSX opening.
                           "source_offset": offset + match.start()})
            meters[-1]["colors"] = {"normal": "#22c55e", "warn": "#f59e0b", "danger": "#ef4444", "track": "#263241"}
    if meters:
        component.props["gauges"] = meters
        controls.extend({"kind": "gauge", "text": item["label"], "unit": item["unit"],
                         "min": item["min"], "max": item["max"], "value": item["value"],
                         "warn": item.get("warn"), "danger": item.get("danger"),
                         "colors": item.get("colors"),
                         "className": "meter-gauge"} for item in meters)
    # Some Figma Make screens draw multiple instruments in one SVG instead of
    # using a Gauge component. Preserve explicit source geometry so later
    # adapters can split that SVG into separate semantic arc widgets.
    canvas_match = re.search(
        r"CX_L\s*=\s*(\d+)\s*,\s*CX_R\s*=\s*(\d+)\s*,\s*CY\s*=\s*(\d+)\s*,\s*R\s*=\s*(\d+)",
        body,
    )
    if canvas_match and re.search(r"<svg\b[\s\S]*?<path\b[\s\S]*?A", body):
        cx_left, cx_right, cy, radius = (int(value) for value in canvas_match.groups())
        component.props["gauge_layout"] = [
            (cx_left - radius, cy - radius, radius * 2, radius * 2),
            (cx_right - radius, cy - radius, radius * 2, radius * 2),
        ]
        if not any(control.get("kind") == "gauge" for control in controls):
            controls.extend({"kind": "gauge", "text": "", "className": "raw-svg-gauge", "box": box}
                            for box in component.props["gauge_layout"])
        else:
            gauge_controls = [control for control in controls if control.get("kind") == "gauge"]
            for control, box in zip(gauge_controls, component.props["gauge_layout"]):
                control["box"] = box
    if component.props.get("svg_arcs"):
        arc_records = [arc for arc in component.props["svg_arcs"] if isinstance(arc, dict)]
        paints = [paint for paint in component.props.get("arc_paints", [])
                  if isinstance(paint, dict)]
        # Keep reusable <Gauge /> call-site instances separate from unrelated
        # path/circle arc groups in the same SVG. Positional assignment used
        # to attach side arcs to radial calls and lose those groups.
        gauges = [item for item in component.props.get("gauges", [])
                  if isinstance(item, dict)]
        by_id = {str(item.get("gauge_id")): item for item in gauges
                 if item.get("gauge_id") is not None}
        for index, record in enumerate(arc_records):
            gauge_id = str(record.get("gauge_id") or f"gauge_arc_{index + 1}")
            gauge = by_id.get(gauge_id)
            if gauge is None:
                gauge = {"label": "", "unit": "", "min": "0", "max": "100",
                         "value": "0", "gauge_id": gauge_id}
                gauges.append(gauge)
                by_id[gauge_id] = gauge
            gauge["gauge_id"] = gauge_id
            gauge["arc"] = record
            gauge["geometry"] = record.get("geometry")
            group_paints = [paint for paint in paints if str(paint.get("gauge_id")) == gauge_id]
            foreground = next((paint for paint in reversed(group_paints)
                               if paint.get("role") == "foreground"), None)
            track = next((paint for paint in group_paints
                          if paint.get("role") in {"track", "warning-track"}), None)
            colors = dict(gauge.get("colors") if isinstance(gauge.get("colors"), dict) else {})
            if foreground and foreground.get("stroke_color"):
                colors["normal"] = foreground["stroke_color"]
            if track and track.get("stroke_color"):
                colors["track"] = track["stroke_color"]
            gauge["colors"] = colors
            gauge["arc_paints"] = group_paints
        component.props["gauges"] = gauges
        gauge_cursor = 0
        for control in controls:
            if not isinstance(control, dict) or control.get("kind") != "gauge":
                continue
            if gauge_cursor >= len(component.props.get("gauges", [])):
                continue
            gauge = component.props["gauges"][gauge_cursor]
            gauge_cursor += 1
            if isinstance(gauge, dict):
                control.update({
                    "gauge_id": gauge.get("gauge_id"),
                    "geometry": gauge.get("geometry"),
                    "arc_paints": gauge.get("arc_paints", []),
                    "colors": gauge.get("colors", {}),
                    "min": gauge.get("min", "0"), "max": gauge.get("max", "100"),
                    "value": gauge.get("value", "0"),
                })
    tabs = []
    for match in re.finditer(r"<button\b([^>]*)>([\s\S]*?)</button>", body):
        attrs, content = match.group(1), match.group(2)
        page = re.search(r"data-page\s*=\s*[\"']([^\"']+)", attrs)
        if page:
            tabs.append({"page": page.group(1), "text": _plain_jsx_text(content), "events": _opening_events(attrs)})
    if tabs:
        component.props["tabs"] = tabs
    if controls:
        component.props["controls"] = controls


def _navigation_evidence(component: Component, body: str, model: ReactProjectModel) -> None:
    """Extract navigation intent without treating every click as navigation."""
    state_defs = {
        match.group("setter"): (match.group("state"), match.group("initial"))
        for match in re.finditer(
            r"const\s*\[\s*(?P<state>[A-Za-z_$][\w$]*)\s*,\s*(?P<setter>set[A-Za-z_$][\w$]*)\s*\]"
            r"\s*=\s*useState(?:<[^;>]+>)?\s*\(\s*['\"](?P<initial>[^'\"]+)['\"]",
            body,
        )
    }
    route_components: dict[tuple[str, str], str] = {}
    for setter, (state, _initial) in state_defs.items():
        for match in re.finditer(
            rf"\b{re.escape(state)}\s*===\s*['\"](?P<route>[^'\"]+)['\"]\s*"
            rf"(?:\?\s*|&&\s*)<(?P<component>[A-Z][\w$]*)\b",
            body,
        ):
            route_components[(setter, match.group("route"))] = match.group("component")
        ternary = re.search(
            rf"\b{re.escape(state)}\s*===\s*['\"](?P<route>[^'\"]+)['\"]\s*\?\s*"
            rf"<(?P<first>[A-Z][\w$]*)\b[\s\S]*?:\s*<(?P<second>[A-Z][\w$]*)\b",
            body,
        )
        if ternary:
            values = list(dict.fromkeys(re.findall(rf"\b{re.escape(setter)}\s*\(\s*['\"]([^'\"]+)['\"]", body)))
            alternatives = [value for value in values if value != ternary.group("route")]
            if len(alternatives) == 1:
                route_components[(setter, alternatives[0])] = ternary.group("second")

    candidates: list[tuple[int, str, str, str, str]] = []
    for tag in ("button", "Button", "a", "Link"):
        for offset, opening, content in _tag_blocks(body, tag):
            handler = _opening_tag_prop(opening, 0, "onClick") or ""
            setter_match = next((
                (setter, match.group(1))
                for setter in state_defs
                if (match := re.search(rf"\b{re.escape(setter)}\s*\(\s*['\"]([^'\"]+)['\"]", handler))
            ), None)
            route_call = re.search(
                r"\b(?:(?:router|history)\.(?:push|replace)|navigate)\s*\(\s*['\"]([^'\"]+)['\"]",
                handler,
            )
            delegated_route = re.search(r"\bonNavigate\s*\(\s*['\"]([^'\"]+)['\"]", handler)
            href = (_opening_tag_prop(opening, 0, "href") or
                    _opening_tag_prop(opening, 0, "to") or "")
            if setter_match:
                action, target, setter = "state", setter_match[1], setter_match[0]
            elif route_call:
                action, target, setter = "route", route_call.group(1), ""
            elif delegated_route:
                action, target, setter = "state", delegated_route.group(1), ""
            elif isinstance(href, str) and href and not href.startswith(("#", "javascript:")):
                action, target, setter = "link", href, ""
            else:
                continue
            label = (_opening_tag_prop(opening, 0, "aria-label") or
                     _opening_tag_prop(opening, 0, "data-page") or
                     _plain_jsx_text(content))
            target_component = route_components.get((setter, target), "")
            candidates.append((offset, str(label or target), target, action, target_component))

    for index, (offset, label, target, action, target_component) in enumerate(candidates, 1):
        confidence = 0.98 if target_component else 0.90 if action == "state" else 0.86
        status = "confirmed" if confidence >= 0.9 else "verify"
        model.navigation.append(NavigationEdge(
            id=f"nav:{component.id}:{index}", source_component=component.id,
            trigger=label, target_route=target, target_component=target_component or None,
            action=action, confidence=confidence, status=status,
            evidence={"source_file": component.source_file, "offset": component.props["span"][0] + offset},
        ))


def scan(source_dir: Path) -> ReactProjectModel:
    root = source_dir.resolve()
    model = ReactProjectModel(root)
    def project_files(suffixes: set[str], *, source_files: bool = False) -> list[Path]:
        found: list[Path] = []
        for current, dirs, names in os.walk(root):
            dirs[:] = [d for d in dirs if d not in (NON_SOURCE_DIRS if source_files else NON_BUSINESS_DIRS)
                        and not d.startswith("node_modules.")]
            base = Path(current)
            found.extend(base / name for name in names if Path(name).suffix.lower() in suffixes)
        return found

    files = sorted(project_files(EXTS, source_files=True), key=str)
    style_files = sorted(project_files({".css"}, source_files=True), key=str)
    by_path = {p.resolve(): _relative(p, root) for p in files}
    texts = {p.resolve():p.read_text(encoding="utf-8", errors="replace") for p in files}
    style_texts = {_relative(p, root): p.read_text(encoding="utf-8", errors="replace") for p in style_files}
    for asset in project_files(ASSET_EXTS):
        rid = _relative(asset, root); model.resources[rid] = Resource(rid, asset, _kind(asset), rid)

    imports: dict[str, dict[str, tuple[str, str]]] = {}
    arrays_by_module: dict[str, dict[str, dict[str, object]]] = {}
    asset_aliases: dict[str, dict[str, str]] = {}
    for path, text in texts.items():
        rel = by_path[path]; module = SourceModule(rel, rel); model.modules[rel] = module; imports[rel] = {}; arrays_by_module[rel] = {}; asset_aliases[rel] = {}
        if Path(rel).name.lower().startswith(("main.", "index.")): model.entry_modules.append(rel)
        for match in IMPORT_RE.finditer(text):
            specifier = match.group("source")
            local = _local_module(root, path, specifier)
            if local:
                target = by_path[local]; module.imports.append(target)
                for binding in _bindings(match.group("bindings")): imports[rel][binding] = (target, binding)
            else:
                module.external_imports.append(specifier)
                if specifier.startswith("."):
                    asset_path = (path.parent / specifier).resolve()
                    if asset_path.is_file() and root in asset_path.parents:
                        asset_id = _relative(asset_path, root)
                        resource = model.resources.setdefault(asset_id, Resource(asset_id, asset_path, _kind(asset_path), rel))
                        if rel not in resource.references: resource.references.append(rel)
                        for binding in _bindings(match.group("bindings")): asset_aliases[rel][binding] = asset_id
        module.exports.extend(re.findall(r"\bexport\s+(?:default\s+)?(?:function|const|class)\s+(\w+)", text))
        for match in ARRAY_DECL_RE.finditer(text):
            end = _balanced_end(text, match.end() - 1, "[", "]")
            if end: arrays_by_module[rel][match.group("name")] = _array_evidence(match.group("name"), text[match.end() - 1:end])
        for asset_match in re.finditer(r"figma:asset/([^'\"`]+)", text):
            name = Path(asset_match.group(1)).name; candidate = next((p for p in project_files(ASSET_EXTS) if p.name == name), None); rid = f"figma:{name}"
            resource = model.resources.setdefault(rid, Resource(rid, candidate or Path(name), _kind(Path(name)), rel))
            if rel not in resource.references: resource.references.append(rel)
        for match in IMPORT_RE.finditer(text):
            specifier = match.group("source")
            if specifier.startswith("figma:asset/"):
                rid = f"figma:{Path(specifier).name}"
                for binding in _bindings(match.group("bindings")): asset_aliases[rel][binding] = rid

    exports: dict[tuple[str, str], str] = {}
    for path, text in texts.items():
        rel = by_path[path]
        for name, start, end in _component_spans(text):
            body = text[start:end]; component = Component(f"{rel}:{name}", _component_kind(body), rel, {"name":name, "span":[start, end]})
            model.components[component.id] = component; exports[(rel, name)] = component.id
            _component_evidence(component, body, rel, start, model)
            _navigation_evidence(component, body, model)

    _source_instances(model, texts, exports, imports, root)

    for component in model.components.values():
        body = texts[(root / component.source_file).resolve()][component.props["span"][0]:component.props["span"][1]]; bindings = imports[component.source_file]
        children = []
        for tag in re.findall(r"<([A-Z][\w$]*)\b", body):
            # JSX declarations in the same module do not have an import
            # binding. Resolve them against the local export table before
            # treating the graph edge as absent.
            target = bindings.get(tag) or ((component.source_file, tag) if (component.source_file, tag) in exports else None)
            if target and target in exports: children.append(exports[target])
        component.children = list(dict.fromkeys(children)); model.component_graph[component.id] = component.children
        # A reusable gauge child may be invoked multiple times by a parent.
        # Keep each call's semantic props (and the child's source paint
        # layers) as source-backed instances for GaugeAdapter.adapt_many.
        gauge_instances: list[dict[str, object]] = []
        for opening_match in re.finditer(r"<([A-Z][\w$]*)\b", body):
            tag = opening_match.group(1)
            target = bindings.get(tag) or ((component.source_file, tag) if (component.source_file, tag) in exports else None)
            child_id = exports.get(target) if target else None
            child = model.components.get(child_id) if child_id else None
            if not child or child.kind != "gauge":
                continue
            child.props["_instantiated_as_template"] = True
            child_gauge = next((item for item in child.props.get("gauges", []) if isinstance(item, dict)), {})
            call_offset = component.props["span"][0] + opening_match.start()
            source_instance = next((item for item in model.instances.values()
                                    if item.owner_component == component.id
                                    and item.tag == tag
                                    and item.span and int(item.span[0]) == call_offset), None)
            instance: dict[str, object] = {"source_component": child.id, "source_offset": call_offset,
                                           "instance": True}
            if source_instance is not None:
                instance["source_instance_id"] = source_instance.instance_id
                instance["semantic_id"] = source_instance.semantic_id
            for prop in ("label", "unit", "min", "max", "value", "display", "displayId",
                         "color", "dec", "warn", "danger"):
                value = _opening_tag_prop(body, opening_match.start(), prop)
                if value is not None:
                    instance[prop] = value
                elif isinstance(child_gauge, dict) and prop in child_gauge:
                    instance[prop] = child_gauge[prop]
            for key in ("geometry", "arc_paints", "colors"):
                if isinstance(child_gauge, dict) and key in child_gauge:
                    instance[key] = child_gauge[key]
            explicit_id = next((instance.get(key) for key in ("gaugeId", "gauge_id", "data-openhmi-id", "id")
                                if instance.get(key)), None)
            if not explicit_id and isinstance(source_instance, SourceInstanceIR):
                explicit_id = source_instance.semantic_id or source_instance.instance_id
            instance["gauge_id"] = str(explicit_id or f"{child.id}#instance-{len(gauge_instances) + 1}")
            if isinstance(instance.get("arc_paints"), list):
                instance["arc_paints"] = [
                    {**paint, "gauge_id": instance["gauge_id"]}
                    for paint in instance["arc_paints"] if isinstance(paint, dict)
                ]
            gauge_instances.append(instance)
        if gauge_instances:
            # Preserve arc groups already extracted from the parent's SVG;
            # reusable Gauge call sites are additional logical owners.
            existing = [item for item in component.props.get("gauges", [])
                        if isinstance(item, dict)]
            instance_offsets = {int(item.get("source_offset")) for item in gauge_instances
                                if item.get("source_offset") is not None}
            # The legacy <Gauge> regex runs before SourceInstanceIR exists and
            # therefore creates duplicate, id-less records.  Once the exact
            # call-site offsets are known, discard only those records; side
            # arc groups carry stable gauge_id values and remain intact.
            existing = [item for item in existing
                        if not (item.get("gauge_id") is None
                                and item.get("source_offset") in instance_offsets)]
            seen = {str(item.get("gauge_id")) for item in existing
                    if item.get("gauge_id") is not None}
            existing.extend(item for item in gauge_instances
                            if str(item.get("gauge_id")) not in seen)
            component.props["gauges"] = existing
        # Conditions live in the parent JSX, but the rendered unit belongs to
        # the referenced child. Propagate an auditable binding without
        # evaluating the expression or inventing a callback.
        for visibility in component.props.get("visibility", []):
            if not isinstance(visibility, dict):
                continue
            target_name = str(visibility.get("target"))
            target = bindings.get(target_name) or ((component.source_file, target_name)
                                                    if (component.source_file, target_name) in exports else None)
            child_id = exports.get(target) if target else None
            child = model.components.get(child_id) if child_id else None
            if child is not None:
                child.props.setdefault("visibility_bindings", []).append({
                    **visibility, "source_component": component.id,
                })
        carousel = component.props.get("carousel")
        if isinstance(carousel, dict) and carousel.get("item_source"):
            # Prefer an imported indexed array over a local rotation queue.
            names = [str(name) for name in carousel.get("item_sources", [])]
            name = next((candidate for candidate in names if candidate in bindings), str(carousel["item_source"]))
            carousel["item_source"] = name
            target = bindings.get(name); data_module = target[0] if target else component.source_file; evidence = arrays_by_module.get(data_module, {}).get(name)
            if evidence:
                component.props["data"] = evidence
                for item in evidence["items"]:
                    if isinstance(item, dict) and isinstance(item.get("image"), str):
                        image = str(item["image"])
                        if image in asset_aliases.get(data_module, {}): component.resource_ids.append(asset_aliases[data_module][image])
                        image_name = Path(image).name
                        component.resource_ids.extend(rid for rid, resource in model.resources.items() if resource.path.name == image_name or rid.endswith(image_name))
        component.resource_ids = list(dict.fromkeys(component.resource_ids))
        for control in component.props.get("controls", []):
            if not isinstance(control, dict) or control.get("kind") != "image":
                continue
            src = control.get("src")
            if isinstance(src, str) and src in asset_aliases.get(component.source_file, {}):
                control["resource_id"] = asset_aliases[component.source_file][src]
            elif isinstance(src, str) and src in model.resources:
                control["resource_id"] = src

        # Materialize inline SVG/HTML image hrefs as ordinary resource edges.
        # This covers public/, public/figma-assets/ and source-relative assets;
        # unresolved/remote URLs remain auditable but are never copied blindly.
        for reference in _svg_image_references(body):
            asset = _resource_reference(root, component.source_file, reference)
            if asset is None:
                continue
            resource_id = _relative(asset, root)
            resource = model.resources.setdefault(resource_id, Resource(resource_id, asset, _kind(asset), component.source_file))
            if component.source_file not in resource.references:
                resource.references.append(component.source_file)
            component.resource_ids.append(resource_id)
        component.resource_ids = list(dict.fromkeys(component.resource_ids))

    # SVG files can themselves contain image hrefs. Preserve those nested
    # dependencies in the generic resource graph for exporter materialization.
    for resource in list(model.resources.values()):
        if resource.kind != "svg" or not resource.path.is_file():
            continue
        try:
            svg_text = resource.path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for reference in _svg_image_references(svg_text):
            asset = _resource_reference(root, resource.id, reference)
            if asset is None:
                continue
            resource_id = _relative(asset, root)
            nested = model.resources.setdefault(resource_id, Resource(resource_id, asset, _kind(asset), resource.id))
            if resource.id not in nested.references:
                nested.references.append(resource.id)

    # Carry nested SVG dependencies to the owning component so the exporter
    # copies the complete chain (the generated LVGL unit only names the root
    # SVG resource).
    changed = True
    while changed:
        changed = False
        for component in model.components.values():
            for resource_id in list(component.resource_ids):
                for nested_id, nested in model.resources.items():
                    if resource_id in nested.references and nested_id not in component.resource_ids:
                        component.resource_ids.append(nested_id)
                        changed = True

    for rel, css_text in style_texts.items():
        _css_filter_references(model, rel, css_text)

    # Same-module state tabs often use a route slug (``dashboard``) while the
    # rendered declaration is ``Dashboard``. Resolve that structural alias
    # after the component graph exists, without consulting project names or a
    # renderer-specific table.
    def route_key(value: object) -> str:
        return re.sub(r"[^a-z0-9]", "", str(value).casefold())

    route_component_names: dict[str, str] = {}
    for app in (component for component in model.components.values()
                if component.id.rsplit(":", 1)[-1] == "App"):
        app_text = texts.get((root / app.source_file).resolve(), "")
        span = app.props.get("span", [0, len(app_text)])
        body = app_text[int(span[0]):int(span[1])]
        for match in re.finditer(
            r"case\s*['\"](?P<route>[^'\"]+)['\"]\s*:[\s\S]{0,500}?return\s*(?:\(\s*)?<(?P<component>[A-Z][\w$]*)\b",
            body,
        ):
            route_component_names[route_key(match.group("route"))] = match.group("component")

    for edge in model.navigation:
        if edge.target_component:
            continue
        source_children = model.component_graph.get(edge.source_component, [])
        candidates = [model.components[item] for item in source_children if item in model.components]
        route_name = route_component_names.get(route_key(edge.target_route))
        if route_name:
            candidates += [component for component in model.components.values()
                           if component not in candidates and
                           component.id.rsplit(":", 1)[-1] == route_name]
        candidates += [component for component in model.components.values()
                       if component not in candidates and
                       route_key(component.id.rsplit(":", 1)[-1]) == route_key(edge.target_route)]
        match = next((component for component in candidates
                      if (route_name and component.id.rsplit(":", 1)[-1] == route_name) or
                      route_key(component.id.rsplit(":", 1)[-1]) == route_key(edge.target_route)), None)
        if match is not None:
            edge.target_component = match.id
            edge.confidence = max(edge.confidence, .96)
            edge.status = "confirmed"
            edge.evidence["resolved_by"] = "same-module route/component structural match"

    # Hooks are often declared outside the component span that renders them.
    # Promote their stable signals to the owning app component so generators
    # can create one shared timer and refresh all bound gauges/labels.
    # Include stylesheets for SVG track/fill colors; they are part of the
    # visual source even though they are not component modules.
    css_files = project_files({".css"}, source_files=True)
    css_text = "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in css_files)
    full_source = "\n".join(texts.values()) + "\n" + css_text
    for css_path in css_files:
        _visual_effect_evidence(
            Component(f"style:{_relative(css_path, root)}", "style", _relative(css_path, root)),
            css_path.read_text(encoding="utf-8", errors="replace"),
            _relative(css_path, root), 0, model,
        )
    sensor_fields = list(dict.fromkeys(re.findall(r"\b(rpm|temp|pressure|battery|volt|load|freq|uptime|time)\b", full_source)))
    timer_values = [int(v) for v in re.findall(r"setInterval\s*\([^,]+,\s*(\d+)\s*\)", full_source)]
    if sensor_fields or timer_values:
        owner = next((c for c in model.components.values() if c.id.rsplit(":", 1)[-1] in {"App", "Dashboard"}), None)
        if owner:
            owner.props["dynamic_bindings"] = sensor_fields
            owner.props["timers"] = [{"interval_ms": v, "source": "setInterval"} for v in sorted(set(timer_values or [1000]))]

    # Promote visual color tokens into gauge semantics.  This deliberately
    # follows source evidence (arcColor return branches and gauge-track CSS),
    # so another Make project with different colors is handled identically.
    arc_fn = re.search(r"(?:function|const)\s+arcColor[\s\S]{0,1200}?\n\}", full_source)
    arc_colors = re.findall(r"return\s+[\"'](#[0-9a-fA-F]{3,8})[\"']", arc_fn.group(0) if arc_fn else "")
    track_match = re.search(r"\.gauge-track\s*\{[\s\S]*?stroke\s*:\s*(#[0-9a-fA-F]{3,8})", full_source)
    palette = {"danger": arc_colors[0] if len(arc_colors) > 0 else "#ef4444",
               "warn": arc_colors[1] if len(arc_colors) > 1 else "#f59e0b",
               "normal": arc_colors[2] if len(arc_colors) > 2 else "#22c55e",
               "track": track_match.group(1) if track_match else "#263241"}
    for component in model.components.values():
        for gauge in component.props.get("gauges", []) if isinstance(component.props.get("gauges"), list) else []:
            colors = gauge.setdefault("colors", {})
            colors.update(palette)

    # Resolve the components actually rendered by entry modules.  Later stages
    # use this as the starting point for reachability, which prevents a shadcn
    # implementation file from being treated as an independent screen merely
    # because it defines a Button or Input function.
    for entry in model.entry_modules:
        text = texts[(root / entry).resolve()]
        # React roots are often wrapped in StrictMode, providers, fragments,
        # or a suspense boundary. Select the first JSX descendant that maps to
        # a local component rather than requiring the root tag immediately
        # after render(.
        for render_match in re.finditer(r"\.render\s*\(", text):
            tail = text[render_match.end():]
            for tag in re.findall(r"<([A-Z][\w$]*)\b", tail):
                target = imports[entry].get(tag)
                component_id = exports.get(target) if target else exports.get((entry, tag))
                if component_id:
                    model.entry_components.append(component_id)
                    break
    model.entry_components = list(dict.fromkeys(model.entry_components))
    if not model.components: model.add_warning("未发现可识别的 React 组件定义")
    return model
