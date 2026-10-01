"""Create the minimum complete project contract expected by UIBuilder 2.2."""
from __future__ import annotations

import json
import base64
from io import BytesIO
import os
import re
import subprocess
import hashlib
import math
import xml.etree.ElementTree as ET
from dataclasses import asdict, replace
from shutil import copy2, copytree
from pathlib import Path
from xml.sax.saxutils import escape

from core.model import ReactProjectModel
from core.browser_layout import browser_background_paint, browser_visual_effects, capture_layout
from core.dynamic_validation import discover_canvas
from core.capability import plan_capabilities
from .screen_ir import build_screen_ir
from typing import Any
from .parser_registry import detect_parser


_ICON_COLORS = {
    "Settings": "#279DFB", "Wrench": "#0CCA70", "Scissors": "#F57636",
    "MoreHorizontal": "#A45BF7", "Home": "#279DFB", "Languages": "#279DFB",
    "Wifi": "#A45BF7", "WifiOff": "#A45BF7", "Check": "#0CCA70",
    "Clock": "#279DFB", "HelpCircle": "#279DFB", "ArrowLeft": "#279DFB",
    "FileText": "#279DFB", "Droplet": "#279DFB", "Droplets": "#279DFB",
    "Sparkles": "#F7A905", "RefreshCw": "#F57636", "ChevronRight": "#279DFB",
}


def _icon_resource_filename(name: str, color: object | None = None) -> str:
    """Stable asset name; color is part of identity because Lucide is inline SVG."""
    base = re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_") or "icon"
    value = str(color or _ICON_COLORS.get(name, "#279DFB")).strip()
    match = re.fullmatch(r"#([0-9A-Fa-f]{6})", value)
    return f"uagent_icon_{base}_{match.group(1).lower()}" if match else f"uagent_icon_{base}"


def _icon_tile_filename(name: str, color: object) -> str:
    base = re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_") or "icon"
    match = re.fullmatch(r"#([0-9A-Fa-f]{6})", str(color).strip())
    return f"uagent_tile_{base}_{match.group(1).lower() if match else '279dfb'}"


def _lucide_svg(name: str, model: ReactProjectModel, color: str | None = None,
                stroke_width: object = 2, size: object = 40) -> str | None:
    if os.environ.get("UAGENT_SKIP_LUCIDE_SOURCE") == "1":
        return None
    aliases = {"Home": "house", "HelpCircle": "circle-help", "MoreHorizontal": "ellipsis",
               "RefreshCw": "refresh-cw", "ArrowLeft": "arrow-left"}
    kebab = aliases.get(name, re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", name).lower())
    candidates = [
        model.source_dir / "node_modules" / "lucide-react" / "dist" / "esm" / "icons" / f"{kebab}.js",
        Path(r"D:\aiassit\_aux\work\print_demo\react_workspace\node_modules\lucide-react\dist\esm\icons") / f"{kebab}.js",
    ]
    source = next((path for path in candidates if path.is_file()), None)
    if source is None:
        return None
    text = source.read_text(encoding="utf-8", errors="replace")
    elements: list[str] = []
    for tag, attrs in re.findall(r'\[\s*["\'](path|circle|rect|line|polyline|polygon|ellipse)["\']\s*,\s*\{([^}]*)\}\s*\]', text):
        values = dict(re.findall(r'([A-Za-z][\w-]*)\s*:\s*["\']([^"\']*)["\']', attrs))
        rendered = " ".join(f'{key}="{escape(value)}"' for key, value in values.items() if key != "key")
        elements.append(f'<{tag} {rendered}/>' if tag not in {"path", "circle", "rect", "line", "polyline", "polygon", "ellipse"} else f'<{tag} {rendered}/>')
    if not elements:
        return None
    stroke = escape(str(color or _ICON_COLORS.get(name, "#279DFB")))
    width = escape(str(stroke_width or 2))
    pixels = max(12, min(96, int(float(size or 40))))
    return f'<svg xmlns="http://www.w3.org/2000/svg" width="{pixels}" height="{pixels}" viewBox="0 0 24 24" fill="none" stroke="' + stroke + '" stroke-width="' + width + '" stroke-linecap="round" stroke-linejoin="round" role="img" aria-label="' + escape(name) + '">' + "".join(elements) + "</svg>\n"


def _icon_svg(name: str, model: ReactProjectModel | None = None,
              color: str | None = None, stroke_width: object = 2, size: object = 40) -> str:
    """Create a standalone, importable SVG asset for a parsed React icon.

    Lucide icons are inline React SVG components, so there is no source file to
    copy.  This compact asset preserves the semantic name and gives UIBuilder
    an actual vector resource; native LVGL symbol rendering remains available
    for controls that do not use image storage.
    """
    if model is not None:
        real = _lucide_svg(name, model, color=color, stroke_width=stroke_width, size=size)
        if real:
            return real
    color = color or _ICON_COLORS.get(name, "#279DFB")
    safe = escape(name)
    if name == "MoreHorizontal":
        shape = '<circle cx="32" cy="32" r="4"/><circle cx="48" cy="32" r="4"/><circle cx="64" cy="32" r="4"/>'
    elif name in {"Check", "ChevronRight", "ArrowLeft"}:
        points = {"Check": "24,32 30,38 42,24", "ChevronRight": "28,20 44,32 28,44", "ArrowLeft": "44,20 28,32 44,44"}[name]
        shape = f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="5" stroke-linecap="round" stroke-linejoin="round"/>'
    else:
        shape = f'<rect x="18" y="18" width="60" height="60" rx="14" fill="none" stroke="{color}" stroke-width="4"/><circle cx="48" cy="48" r="10" fill="none" stroke="{color}" stroke-width="4"/>'
    return f'<svg xmlns="http://www.w3.org/2000/svg" width="96" height="96" viewBox="0 0 96 96" role="img" aria-label="{safe}"><title>{safe}</title>{shape}</svg>\n'


def _lucide_tile_svg(name: str, color: str, model: ReactProjectModel, dimension: int = 40) -> str:
    icon_size = max(16, dimension - 16)
    icon = _lucide_svg(name, model, color="#ffffff", stroke_width=2, size=icon_size)
    inner = icon.split(">", 1)[1].rsplit("</svg>", 1)[0] if icon else ""
    inset = (dimension - icon_size) // 2
    radius = max(4, dimension // 5)
    if icon:
        return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{dimension}" height="{dimension}" viewBox="0 0 {dimension} {dimension}" role="img" aria-label="{escape(name)}">'
                f'<rect width="{dimension}" height="{dimension}" rx="{radius}" fill="{escape(color)}"/>'
                f'<g transform="translate({inset} {inset})" fill="none" stroke="#ffffff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">{inner}</g></svg>\n')
    return f'<svg xmlns="http://www.w3.org/2000/svg" width="{dimension}" height="{dimension}"><rect width="{dimension}" height="{dimension}" rx="6" fill="{escape(color)}"/></svg>\n'


def _write_icon_resources(project_root: Path, model: ReactProjectModel) -> list[Path]:
    specs: dict[tuple[str, str], tuple[object, object]] = {}
    for component in model.components.values():
        for control in component.props.get("controls", []):
            if isinstance(control, dict) and control.get("icon"):
                name = str(control["icon"])
                color = str(control.get("icon_color") or _ICON_COLORS.get(name, "#279DFB"))
                candidate = (control.get("icon_stroke_width", 2), control.get("icon_size", 40))
                # Component order follows application source before bundled UI
                # primitives.  Keep the first concrete usage so an unused
                # library component cannot overwrite Navigation's white 20px
                # ArrowLeft with a default-colored 40px variant.
                specs.setdefault((name, color), candidate)
        for array in component.props.get("arrays", []):
            if isinstance(array, dict):
                for item in array.get("items", []):
                    if isinstance(item, dict) and item.get("icon"):
                        name = str(item["icon"])
                        color = str(item.get("color") or _ICON_COLORS.get(name, "#279DFB"))
                        specs.setdefault((name, color), (2, 40))
    # Browser-materialized controls can introduce icon colors that are not
    # present in the React control metadata (for example the gray maintenance
    # wrench on InkStatus). Keep these generated references resource-complete.
    specs.setdefault(("Wrench", "#9CA3AF"), (2, 14))
    specs.setdefault(("Calendar", "#252C36"), (2, 14))
    specs.setdefault(("Clock", "#252C36"), (2, 14))
    specs.setdefault(("ChevronDown", "#717182"), (2, 14))
    # Dynamic array rows may retain the template's trailing icon color when
    # the repeated item has the same icon name. Keep that variant available.
    specs.setdefault(("Droplets", "#9CA3AF"), (2, 16))
    target = project_root / "resources" / "image"
    written: list[Path] = []
    rasterized: set[Path] = set()

    def rasterize(path: Path) -> None:
        if os.environ.get("UAGENT_SKIP_RASTER") == "1":
            return
        png_path = path.with_suffix(".png")
        if png_path in rasterized or png_path.is_file():
            return
        rasterized.add(png_path)
        try:
            subprocess.run(["magick", str(path), "-background", "none", str(png_path)],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        except (OSError, subprocess.SubprocessError):
            # Keep snapshot references valid on machines without ImageMagick.
            # These compact raster glyphs are only a fallback for generated
            # navigation assets; the SVG remains the editable source.
            try:
                from PIL import Image, ImageDraw
                size = 36 if "_nav" in path.stem else 40
                image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
                draw = ImageDraw.Draw(image)
                color = (226, 232, 240, 255)
                if "LayoutDashboard" in path.stem:
                    pad, gap = 8, 3
                    cell = (size - pad * 2 - gap) // 2
                    for row in range(2):
                        for col in range(2):
                            x = pad + col * (cell + gap); y = pad + row * (cell + gap)
                            draw.rounded_rectangle((x, y, x + cell, y + cell), radius=2, outline=color, width=2)
                elif "Settings" in path.stem:
                    c = size // 2
                    draw.ellipse((c - 7, c - 7, c + 7, c + 7), outline=color, width=2)
                    draw.ellipse((c - 2, c - 2, c + 2, c + 2), fill=color)
                    for dx, dy in ((0, -11), (0, 11), (-11, 0), (11, 0)):
                        draw.line((c + dx // 2, c + dy // 2, c + dx, c + dy), fill=color, width=2)
                else:
                    draw.rounded_rectangle((8, 8, size - 8, size - 8), radius=4, outline=color, width=2)
                image.save(png_path)
            except (OSError, ImportError):
                pass

    for (name, color), (stroke_width, size) in sorted(specs.items()):
        path = target / f"{_icon_resource_filename(name, color)}.svg"
        path.write_text(_icon_svg(name, model, color=color, stroke_width=stroke_width, size=size), encoding="utf-8", newline="\n")
        written.append(path)
        # UIBuilder's design canvas accepts raster image storage more reliably
        # than SVG.  Keep the SVG source and create a PNG companion when the
        # bundled/system ImageMagick executable is available.
        rasterize(path)
    for component in model.components.values():
        for control in component.props.get("controls", []):
            if not isinstance(control, dict) or control.get("kind") != "icon_tile" or not control.get("icon_tile_background"):
                continue
            name, color = str(control["icon"]), str(control["icon_tile_background"])
            path = target / f"{_icon_tile_filename(name, color)}_header.svg"
            path.write_text(_lucide_tile_svg(name, color, model, dimension=32), encoding="utf-8", newline="\n")
            written.append(path)
            rasterize(path)
        for array in component.props.get("arrays", []):
            if not isinstance(array, dict):
                continue
            for item in array.get("items", []):
                if not isinstance(item, dict) or not item.get("icon") or not item.get("color"):
                    continue
                name, color = str(item["icon"]), str(item["color"])
                path = target / f"{_icon_tile_filename(name, color)}.svg"
                path.write_text(_lucide_tile_svg(name, color, model), encoding="utf-8", newline="\n")
                written.append(path)
                rasterize(path)
    # Browser-only responsive dashboards can introduce an icon-only sidebar
    # after the static component scan.  Restrict these synthetic resources to
    # that parser profile; emitting them for every project polluted unrelated
    # exports and their resource counts.
    inferred_nav = []
    parser_id = detect_parser(model.source_dir).parser_id
    if parser_id == "responsive-dashboard":
        inferred_nav = [("Zap", "#2F6B5A"), ("LayoutDashboard", "#2F6B5A"),
                        ("BarChart3", "#2F6B5A"), ("Settings", "#2F6B5A"),
                        ("FileText", "#2F6B5A"), ("User", "#2F6B5A")]
    # Synthetic settings-page controls are injected from DOM semantics during
    # layout generation; ensure their raster resources are emitted as well.
    if parser_id == "print-device":
        inferred_nav += [("Settings", "#F7A905"), ("Languages", "#279DFB"),
                         ("Clock", "#0CCA70"), ("Wifi", "#A45BF7"),
                         ("Wrench", "#0CCA70"), ("Scissors", "#F57636"),
                         ("MoreHorizontal", "#A45BF7")]
    has_meter_tabs = any(
        {str(control.get("text", "")).strip().upper() for control in component.props.get("controls", []) if isinstance(control, dict)}
        >= {"DASH", "SET"}
        for component in model.components.values()
    )
    if parser_id == "meter" or has_meter_tabs:
        inferred_nav += [("LayoutDashboard", "#101218"), ("Settings", "#101218")]
    for name, color in inferred_nav:
        suffix = "_nav" if has_meter_tabs or parser_id == "meter" or name in {"Zap", "LayoutDashboard", "BarChart3", "FileText", "User"} else "_header"
        path = target / f"{_icon_tile_filename(name, color)}{suffix}.svg"
        if not path.exists():
            path.write_text(_lucide_tile_svg(name, color, model, dimension=36), encoding="utf-8", newline="\n")
            written.append(path)
        rasterize(path)
        png = path.with_suffix(".png")
        if not png.exists():
            # Use the regular icon raster as a safe fallback instead of
            # leaving a broken image reference in the AiBuilder snapshot.
            fallback = target / f"{_icon_resource_filename(name, '#279DFB')}.png"
            if fallback.exists():
                copy2(fallback, png)
    for component in model.components.values():
        if component.props.get("name") == "Navigation":
            for index, control in enumerate(component.props.get("controls", [])):
                if not isinstance(control, dict) or not control.get("icon"):
                    continue
                name = str(control["icon"])
                color = "#17D4D8" if index == 0 else "#252C36"
                path = target / f"{_icon_tile_filename(name, color)}_nav.svg"
                path.write_text(_lucide_tile_svg(name, color, model, dimension=36), encoding="utf-8", newline="\n")
                written.append(path)
                rasterize(path)
    # UIBuilder resolves imported image storage from resources/image.  Keep
    # the same files in its asset index too; older builds otherwise show an
    # empty Resources panel even though the snapshot references the image.
    for mirror in (project_root / "ui_builder" / "assets" / "image",
                   project_root / "ui_builder" / "custom" / "assets" / "image"):
        mirror.mkdir(parents=True, exist_ok=True)
        for path in written:
            copy2(path, mirror / path.name)
            png = path.with_suffix(".png")
            if png.is_file():
                copy2(png, mirror / png.name)
    return written


def _snapshot_style(control: dict, kind: str, text_value: str = "") -> str:
    def css_color(value: str, fallback: str) -> str:
        value = str(value or "").strip()
        match = re.fullmatch(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)(?:\s*,\s*([0-9.]+))?\s*\)", value)
        if match and (match.group(4) is None or float(match.group(4)) > 0):
            return "#%02X%02X%02X" % tuple(int(match.group(i)) for i in (1, 2, 3))
        return value if value.startswith("#") else fallback
    classes = str(control.get("className", ""))
    computed = control.get("style") if isinstance(control.get("style"), dict) else {}
    raw_bg = str(computed.get("backgroundColor") or "")
    bg = css_color(raw_bg, "")
    transparent_bg = raw_bg.casefold() in {"transparent", "rgba(0, 0, 0, 0)", "rgba(0,0,0,0)"}
    rgba_bg = re.fullmatch(r"rgba?\(\s*\d+\s*,\s*\d+\s*,\s*\d+(?:\s*,\s*([0-9.]+))?\s*\)", raw_bg, re.I)
    if rgba_bg and rgba_bg.group(1) is not None:
        try:
            transparent_bg = float(rgba_bg.group(1)) <= 0
        except ValueError:
            pass
    # Preserve computed alpha instead of collapsing every painted color to
    # fully opaque.  This matters for translucent section fills and bars.
    bg_alpha = 0.0 if transparent_bg else 1.0
    if rgba_bg and rgba_bg.group(1) is not None:
        try:
            bg_alpha = max(0.0, min(1.0, float(rgba_bg.group(1))))
        except ValueError:
            bg_alpha = 0.0
    try:
        bg_alpha *= max(0.0, min(1.0, float(str(computed.get("opacity", "1")))))
    except (TypeError, ValueError):
        pass
    if not bg or transparent_bg:
        bg = "#ffffff" if "bg-white" in classes else "#101218"
    # Resolve the shadcn/Tailwind semantic tokens used heavily by Figma Make
    # dashboards. Leaving these unresolved made transparent labels inherit a
    # black fallback and turned dropdowns/cards into dark bars.
    token_colors = {
        "background": "#FFFFFF", "card": "#FFFFFF", "popover": "#FFFFFF",
        "foreground": "#252C36", "card-foreground": "#252C36",
        "muted": "#ECECF0", "muted-foreground": "#717182",
        "border": "#E5E7EB", "input": "#E5E7EB", "primary": "#030213",
        "primary-foreground": "#FFFFFF", "sidebar": "#2C5F4E",
        "sidebar-foreground": "#FFFFFF", "sidebar-primary": "#7BDC93",
        "sidebar-accent": "#3A6B5A", "sidebar-accent-foreground": "#FFFFFF",
    }
    for token, value in token_colors.items():
        if re.search(rf"(?:^|\s)bg-{re.escape(token)}(?:\s|$)", classes):
            bg = value
            break
    if "from-sidebar-primary" in classes:
        bg = token_colors["sidebar-primary"]
    elif "from-sidebar" in classes:
        bg = token_colors["sidebar"]
    elif "from-primary" in classes:
        bg = token_colors["primary"]
    bg_opa = int(round(bg_alpha * 255)) if raw_bg else (0 if kind == "label" and "bg-" not in classes else 255)
    if transparent_bg:
        bg_opa = 0
    bg_match = re.search(r"bg-\[#([0-9A-Fa-f]{6})\]", classes)
    if bg_match:
        bg = "#" + bg_match.group(1)
    # Bottom navigation actions are transparent controls on the dark canvas,
    # even when the source button carries a generic ``bg-white`` class.
    if kind == "button" and (control.get("nav_icon") or str(text_value).strip().upper() in {"DASH", "SET"}):
        bg = "#101218"
        bg_opa = 0
    if kind == "button" and not transparent_bg and "bg-white" not in classes and "bg-[" not in classes \
            and "bg-gradient" not in classes and "from-sidebar" not in classes and not computed:
        bg = "#ffffff"
    text_match = re.search(r"text-\[#([0-9A-Fa-f]{6})\]", classes)
    text = css_color(str(computed.get("color") or ""), "")
    if not text or text in {"transparent", "rgba(0, 0, 0, 0)"}:
        text = "#252C36" if text_match is None and kind in {"button", "label", "select", "input"} else ("#" + text_match.group(1) if text_match else "#ffffff")
    for token, value in token_colors.items():
        if re.search(rf"(?:^|\s)text-{re.escape(token)}(?:\s|$)", classes):
            text = value
            break
    if control.get("gauge_color"):
        text = css_color(str(control["gauge_color"]), text)
    if "tab-btn" in classes:
        active = "active" in classes
        bg = "#111820" if active else "#0F1318"
        text = "#F59E0B" if active else "#4A5568"
    border = css_color(str(computed.get("borderColor") or ""), "") or ("#e5e7eb" if "border-gray-200" in classes else "#000000")
    # Browser computed styles may expose only per-side values for a partially
    # bordered section. Select the first visible side as LVGL's uniform border
    # representation instead of silently losing the section outline.
    side_colors = [computed.get(key) for key in
                   ("borderTopColor", "borderRightColor", "borderBottomColor", "borderLeftColor")]
    side_widths = [computed.get(key) for key in
                   ("borderTopWidth", "borderRightWidth", "borderBottomWidth", "borderLeftWidth")]
    side_width_values = []
    for width_value in side_widths:
        match = re.search(r"([0-9]+(?:\.[0-9]+)?)", str(width_value or ""))
        side_width_values.append(float(match.group(1)) if match else 0.0)
    visible_side = next((index for index, value in enumerate(side_width_values) if value > 0), None)
    if visible_side is not None:
        side_border = css_color(str(side_colors[visible_side] or ""), "")
        if side_border:
            border = side_border
    for token, value in token_colors.items():
        if re.search(rf"(?:^|\s)border-{re.escape(token)}(?:\s|$)", classes):
            border = value
            break
    has_cjk = any("\u2e80" <= char <= "\u9fff" for char in text_value)
    font_path = "DroidSansFallback.ttf" if has_cjk else "montserratMedium.ttf"
    measured_font = re.search(r"([0-9]+(?:\.[0-9]+)?)px", str(computed.get("fontSize", "")))
    font_size = int(round(float(measured_font.group(1)))) if measured_font else (12 if "text-xs" in classes else 14 if "text-sm" in classes else 16)
    measured_radius = re.search(r"([0-9]+(?:\.[0-9]+)?)px", str(computed.get("borderRadius", "")))
    radius = int(round(float(measured_radius.group(1)))) if measured_radius else (999 if "rounded-full" in classes else 8 if "rounded-lg" in classes else 4 if "rounded" in classes else 0)
    measured_border = re.search(r"([0-9]+(?:\.[0-9]+)?)px", str(computed.get("borderWidth", "")))
    border_width = int(round(float(measured_border.group(1)))) if measured_border else (2 if "border-2" in classes else 1 if re.search(r"(?:^|\s)border(?:\s|$)", classes) else 0)
    if border_width <= 0 and visible_side is not None:
        border_width = max(1, int(round(side_width_values[visible_side])))
    border_alpha_raw = str((side_colors[visible_side] if visible_side is not None else computed.get("borderColor")) or "")
    border_alpha = 1.0
    border_rgba = re.fullmatch(r"rgba?\(\s*\d+\s*,\s*\d+\s*,\s*\d+(?:\s*,\s*([0-9.]+))?\s*\)", border_alpha_raw, re.I)
    if border_rgba and border_rgba.group(1) is not None:
        try:
            border_alpha = max(0.0, min(1.0, float(border_rgba.group(1))))
        except ValueError:
            border_alpha = 0.0
    border_opa = int(round(border_alpha * 255)) if border_width > 0 else 0
    icon = str(control.get("icon") or "")
    bg_image = ""
    use_background_icon = bool(icon and (control.get("kind") == "icon_tile" or control.get("nav_icon")))
    if use_background_icon:
        icon_file = (_icon_tile_filename(icon, control["icon_tile_background"]) + ("_nav" if control.get("nav_icon") else "_header")
                     if control.get("icon_tile_background") else
                     _icon_resource_filename(icon, control.get("icon_color")))
        bg_image = (f'<bg-img-src>{icon_file}.png</bg-img-src><bg-img-opa>255</bg-img-opa>'
                    '<bg-img-recolor>#000000</bg-img-recolor><bg-img-recolor-opa>0</bg-img-recolor-opa>')
    text_align = 2 if "text-center" in classes or kind == "button" else 0
    font = (f'<text-font-type>0</text-font-type><text-font-path>{font_path}</text-font-path>'
            f'<text-font-size>{font_size}</text-font-size><text-align>{text_align}</text-align>') if kind in {"label", "button"} else ''
    if kind == "select":
        # LVGL dropdowns are multi-part widgets.  Styling only Main leaves
        # List/Selected to the active UIBuilder theme (bright blue in 2.2),
        # which is why the generated Energy filters did not resemble the DOM.
        select_bg = bg if "bg-" in classes else "#FFFFFF"
        select_border = border if "border-" in classes else "#E5E7EB"
        main = (
            f'<bg-color>{select_bg}</bg-color><bg-opa>255</bg-opa><text-color>{text}</text-color>'
            f'<border-color>{select_border}</border-color><border-opa>255</border-opa>'
            f'<border-width>{max(1, border_width)}</border-width><radius>{max(8, radius)}</radius>'
            '<pad-top>8</pad-top><pad-bottom>8</pad-bottom><pad-left>10</pad-left><pad-right>28</pad-right>'
        )
        focused = (
            '<bg-color>#101218</bg-color><bg-opa>255</bg-opa><text-color>#22C55E</text-color>'
            '<border-color>#7BDC93</border-color><border-opa>255</border-opa><border-width>2</border-width>'
            '<radius>8</radius><pad-top>8</pad-top><pad-bottom>8</pad-bottom><pad-left>10</pad-left><pad-right>28</pad-right>'
        )
        return (
            '<Style><Part name="Main" value="0">'
            f'<State name="Default" value="0">{main}</State>'
            f'<State name="Checked" value="1">{focused}</State>'
            f'<State name="Focused" value="2">{focused}</State>'
            f'<State name="Pressed" value="32">{focused}</State>'
            '</Part>'
            '<Part name="Selected" value="262144"><State name="Checked" value="1">'
            '<bg-color>#D9F8E2</bg-color><bg-opa>255</bg-opa><text-color>#252C36</text-color>'
            '<border-color>#000000</border-color><border-opa>0</border-opa><border-width>0</border-width><radius>6</radius>'
            '</State></Part>'
            '<Part name="List" value="589824"><State name="Default" value="0">'
            '<bg-color>#FFFFFF</bg-color><bg-opa>255</bg-opa><text-color>#252C36</text-color>'
            '<border-color>#E5E7EB</border-color><border-opa>255</border-opa><border-width>1</border-width><radius>8</radius>'
            '</State></Part>'
            '<Part name="Scrollbar" value="65536"><State name="Default" value="0">'
            '<bg-color>#A8B0BB</bg-color><bg-opa>102</bg-opa><radius>4</radius>'
            '</State></Part></Style>'
        )
    # A range input can be an interaction-only anchor in the source DOM
    # (for example, a custom visual bar drives it while CSS sets opacity: 0).
    # Preserve its geometry and value in the snapshot, but explicitly clear
    # every LVGL slider part so the simulator cannot paint a theme-default
    # track or knob over the custom visual.
    if kind == "slider":
        try:
            invisible = float(str(computed.get("opacity", "1")).strip()) <= 0.001
        except (TypeError, ValueError):
            invisible = False
        if invisible:
            return (
                '<Style>'
                '<Part name="Main" value="0"><State name="Default" value="0">'
                '<bg-color>#000000</bg-color><bg-opa>0</bg-opa>'
                '<border-color>#000000</border-color><border-opa>0</border-opa>'
                '<border-width>0</border-width><radius>0</radius>'
                '</State></Part>'
                '<Part name="Indicator" value="131072"><State name="Default" value="0">'
                '<bg-color>#000000</bg-color><bg-opa>0</bg-opa>'
                '<border-color>#000000</border-color><border-opa>0</border-opa>'
                '<border-width>0</border-width><radius>0</radius>'
                '</State></Part>'
                '<Part name="Knob" value="196608"><State name="Default" value="0">'
                '<bg-color>#000000</bg-color><bg-opa>0</bg-opa>'
                '<border-color>#000000</border-color><border-opa>0</border-opa>'
                '<border-width>0</border-width><radius>0</radius>'
                '</State></Part>'
                '</Style>'
            )
    return (f'<Style><Part name="Main" value="0"><State name="Default" value="0">'
            f'<bg-color>{bg}</bg-color><bg-opa>{bg_opa}</bg-opa>{bg_image}<text-color>{text}</text-color>{font}'
            f'<border-color>{border}</border-color><border-opa>{border_opa}</border-opa>'
            f'<border-width>{border_width}</border-width><radius>{radius}</radius>'
            '<pad-top>0</pad-top><pad-bottom>0</pad-bottom><pad-left>0</pad-left><pad-right>0</pad-right>'
            '</State></Part></Style>')


class _IdAllocator:
    """Allocate unique ids across an entire snapshot, including all screens."""

    def __init__(self) -> None:
        self._next = 1000000000000

    def take(self) -> int:
        self._next += 1
        return self._next


def _project_name(project_root: Path, model: ReactProjectModel) -> str:
    # The workspace folder is intentionally named ``aibuilder``; using that as
    # every project's identity makes exported files indistinguishable.  Keep
    # the React/Figma Make source folder name as the default project identity.
    raw = model.source_dir.name if project_root.name.lower() == "aibuilder" else project_root.name
    value = re.sub(r"[^A-Za-z0-9_-]+", "_", raw).strip("_")
    return value or "uagent_project"


def detect_canvas(model: ReactProjectModel) -> tuple[int, int]:
    if model.screens:
        return model.screens[0].width, model.screens[0].height
    patterns = (
        re.compile(r"w-\[(\d+)px\][\s\S]{0,160}?h-\[(\d+)px\]"),
        re.compile(r"(?:width|w)\s*[:=]\s*[\"']?(\d+)[^\n]{0,240}?(?:height|h)\s*[:=]\s*[\"']?(\d+)", re.I),
        re.compile(r"(?:width|w)\s*[:=]\s*[\"']?(\d+)[\s\S]{0,240}?(?:height|h)\s*[:=]\s*[\"']?(\d+)", re.I),
    )
    candidates: list[tuple[int, int]] = []
    # The scanner already resolved source modules and excludes node_modules;
    # never recurse the installed dependency tree again during generation.
    sources = [model.source_dir / module.path for module in model.modules.values()
               if not any(part.lower() in {"simulator", "docs", "reference", "assets", "freetype"}
                          for part in Path(module.path).parts)]
    for source in sorted(sources):
        if source.suffix.lower() not in {".tsx", ".jsx", ".ts", ".js"} or not source.is_file():
            continue
        try:
            text = source.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        for pattern in patterns:
            for match in pattern.finditer(text):
                width, height = int(match.group(1)), int(match.group(2))
                if 120 <= width <= 4096 and 120 <= height <= 4096:
                    candidates.append((width, height))
    return max(candidates, key=lambda size: size[0] * size[1]) if candidates else (1024, 600)


def _uibuilder_root() -> Path | None:
    for candidate in (Path(r"D:\UIBuilder"), Path(r"C:\UIBuilder")):
        if (candidate / "tool" / "simulator").is_dir():
            return candidate
    return None


def _copy_project_fonts(project_root: Path) -> list[Path]:
    """Copy fonts independently of the optional simulator runtime bundle."""
    install = _uibuilder_root()
    if install is None:
        return []
    target = project_root / "resources" / "font"
    target.mkdir(parents=True, exist_ok=True)
    candidates = {
        "montserratMedium.ttf": (
            install / "app_template" / "smart_home" / "resources" / "font" / "montserratMedium.ttf",
            install / "app_template" / "order_coffee" / "resources" / "font" / "montserratMedium.ttf",
        ),
        "DroidSansFallback.ttf": (
            install / "app_template" / "dashboard" / "resources" / "font" / "DroidSansFallback.ttf",
            install / "app_template" / "bread_machine" / "resources" / "font" / "DroidSansFallback.ttf",
        ),
    }
    copied: list[Path] = []
    for filename, sources in candidates.items():
        source = next((candidate for candidate in sources if candidate.is_file()), None)
        if source is not None:
            destination = target / filename
            copy2(source, destination)
            copied.append(destination)
    return copied


def _normalize_simulator_png(path: Path) -> None:
    """Use a conservative RGBA PNG encoding for UIBuilder's LodePNG build."""
    if path.suffix.casefold() != ".png":
        return
    try:
        from PIL import Image
        with Image.open(path) as source:
            image = source.convert("RGBA")
            image.save(path, format="PNG", compress_level=0)
    except (ImportError, OSError, ValueError):
        # Generation remains valid without Pillow; validation will surface a
        # decoder failure rather than silently dropping the image.
        return


def _size_image_controls(geometry: dict[str, Any], asset_dir: Path | None) -> None:
    """Rasterize image controls to their measured browser boxes for LVGL."""
    if asset_dir is None:
        return
    controls = geometry.get("materialized_controls", [])
    boxes = geometry.get("controls", [])
    for control, box in zip(controls, boxes):
        if str(control.get("kind", "")).casefold() != "image" or len(box) != 4:
            continue
        source = asset_dir / str(control.get("src", ""))
        width, height = max(1, int(box[2])), max(1, int(box[3]))
        if not source.is_file() or source.suffix.casefold() != ".png":
            continue
        target = source.with_name(f"{source.stem}_{width}x{height}.png")
        try:
            from PIL import Image
            with Image.open(source) as image:
                if image.size != (width, height) or not target.is_file():
                    image.convert("RGBA").resize((width, height), Image.Resampling.LANCZOS).save(
                        target, format="PNG", compress_level=0)
            control["src"] = target.name
        except (ImportError, OSError, ValueError):
            continue
def _copy_uibuilder_runtime(project_root: Path) -> bool:
    """Materialize simulator prerequisites used by UIBuilder 2.2 projects."""
    install = _uibuilder_root()
    if install is None:
        return False
    tool = install / "tool" / "simulator"
    simulator = project_root / "simulator"
    package = tool / "lvgl" / "9.1.0"
    for source, destination in (
        (package / "lvgl", simulator / "lvgl"),
        (tool / "freetype", simulator / "freetype"),
        (tool / "ffmpeg", simulator / "ffmpeg"),
    ):
        if source.is_dir():
            copytree(source, destination, dirs_exist_ok=True)
    if (package / "lv_conf.h").is_file():
        copy2(package / "lv_conf.h", simulator / "lv_conf.h")
        # AiBuilder's exported simulator compiles ui_util.c and its dynamic
        # fonts, so the project configuration must expose LVGL 9 FreeType.
        # The standalone UAgent snapshot harness uses a separate config with
        # FreeType disabled; see tools/cross_validate.py.
        conf = simulator / "lv_conf.h"
        raw = conf.read_text(encoding="utf-8", errors="ignore")
        raw = re.sub(r"^(\s*#\s*define\s+LV_USE_FFMPEG)\s+1\b", r"\1 0", raw, flags=re.M)
        raw = re.sub(r"^(\s*#\s*define\s+LV_USE_FREETYPE)\s+0\b", r"\1 1", raw, flags=re.M)
        # PNG is the interchange format used by materialized browser SVG
        # images.  Keep LVGL's bundled lodepng decoder enabled in every
        # generated runtime; the CMake LVGL source glob includes src/libs/lodepng.
        raw = re.sub(r"^(\s*#\s*define\s+LV_USE_LODEPNG)\s+0\b", r"\1 1", raw, flags=re.M)
        conf.write_text(raw, encoding="utf-8", newline="\n")
    runtime = simulator / "lib"
    runtime.mkdir(parents=True, exist_ok=True)
    for source in (install / "bin" / "SDL2.dll", install / "bin" / "libwinpthread-1.dll", install / "bin" / "icon.bmp"):
        if source.is_file():
            copy2(source, runtime / source.name)
    _copy_project_fonts(project_root)
    return (simulator / "lvgl").is_dir() and (simulator / "ffmpeg").is_dir() and (simulator / "lib").is_dir()


def _sync_existing_uibuilder_geometry(project_root: Path, snapshot: Path,
                                      model: ReactProjectModel) -> None:
    """Keep an existing UIBuilder C export aligned with the new snapshot.

    Fresh projects have no exported screen C yet; UIBuilder creates it when
    opened. For a rebuilt simulator, synchronize only when every snapshot
    widget already has a matching C object, so structure changes are never
    silently hidden by a partial patch.
    """
    root = ET.parse(snapshot).getroot()
    for screen in root.findall("Screen"):
        screen_c = project_root / "ui_builder" / f"{screen.get('name', '')}.c"
        if not screen_c.is_file():
            continue
        original = screen_c.read_text(encoding="utf-8")
        # Recompute generated visibility on every run so an object can become
        # source-backed again without retaining a stale hidden flag.
        updated = re.sub(r"\n\s*/\* UAgent snapshot-removed widget \*/\s*\n\s*lv_obj_add_flag\(scr->\w+,\s*LV_OBJ_FLAG_HIDDEN\);", "", original)
        widgets = screen.findall(".//Widget")
        if any(not widget.get("name") or
               not re.search(rf"\bscr->{re.escape(widget.get('name', ''))}\s*=\s*lv_\w+_create\(", original)
               for widget in widgets):
            model.add_warning(f"Existing UIBuilder C has a different widget tree: {screen_c}; reopen the snapshot in UIBuilder")
            continue
        complete = True
        for widget in widgets:
            name = re.escape(widget.get("name", ""))
            normal = widget.find("Normal")
            if normal is None:
                continue
            attribute = widget.find("Attribute")
            if widget.get("type") == "1" and attribute is not None:
                label_text = attribute.findtext("text", "")
                pattern = rf"(lv_label_set_text\(scr->{name},\s*)\"(?:\\.|[^\"\\])*\"(\s*\);)"
                updated, count = re.subn(pattern, lambda match: f"{match.group(1)}{json.dumps(label_text, ensure_ascii=False)}{match.group(2)}", updated)
                if count != 1:
                    complete = False
                    break
            if widget.get("type") == "5" and attribute is not None:
                image_source = attribute.findtext("src", "")
                if image_source and re.fullmatch(r"[\w.-]+", image_source):
                    pattern = rf"(lv_img_set_src\(scr->{name},\s*LVGL_IMAGE_PATH\()[^()]+(\)\s*\);)"
                    updated, count = re.subn(pattern, lambda match: f"{match.group(1)}{image_source}{match.group(2)}", updated)
                    if count != 1:
                        complete = False
                        break
            for function, field in (("pos", "postion"), ("size", "size")):
                if function == "size" and widget.get("type") == "5":
                    # UIBuilder image size comes from lv_img_set_src, not an
                    # explicit lv_obj_set_size call.
                    continue
                value = normal.findtext(field, "")
                if not re.fullmatch(r"-?\d+,-?\d+", value):
                    continue
                first, second = value.split(",")
                pattern = rf"(lv_obj_set_{function}\(scr->{name},\s*)-?\d+\s*,\s*-?\d+(\s*\);)"
                updated, count = re.subn(pattern, lambda match: f"{match.group(1)}{first}, {second}{match.group(2)}", updated)
                if count != 1:
                    complete = False
                    break
            if not complete:
                break
        if complete:
            snapshot_names = {widget.get("name", "") for widget in widgets}
            root_names = {"obj", str(screen.get("name") or "")}
            created_names = set(re.findall(r"\bscr->(\w+)\s*=\s*lv_\w+_create\(", updated))
            for extra in sorted(created_names - snapshot_names - root_names):
                pattern = rf"(\bscr->{re.escape(extra)}\s*=\s*lv_\w+_create\([^;]+;)[ \t]*"
                replacement = (rf"\1\n    /* UAgent snapshot-removed widget */\n"
                               rf"    lv_obj_add_flag(scr->{extra}, LV_OBJ_FLAG_HIDDEN);")
                updated, count = re.subn(pattern, replacement, updated, count=1)
                if count != 1:
                    complete = False
                    break
        if complete and updated != original:
            screen_c.write_text(updated, encoding="utf-8", newline="\n")
        elif not complete:
            model.add_warning(f"Could not synchronize existing UIBuilder C geometry: {screen_c}; reopen the snapshot in UIBuilder")


def _ensure_simulator_frame_capture(project_root: Path, model: ReactProjectModel) -> None:
    """Add deterministic frame export to an existing UIBuilder SDL entry."""
    entry = project_root / "simulator" / "main.c"
    if not entry.is_file():
        return
    raw = entry.read_text(encoding="utf-8")
    if "uagent_capture_frame" in raw:
        return
    init = re.search(r"(?m)^([ \t]*)hal_init\((\d+)\s*,\s*(\d+)\);", raw)
    loop = re.search(r"(?m)^([ \t]*)lv_timer_handler\(\);", raw)
    if "#undef main" not in raw or init is None or loop is None:
        model.add_warning(f"Cannot attach deterministic SDL frame capture to {entry}")
        return
    helper = r'''
static void uagent_capture_frame(lv_display_t * display)
{
    const char * path = getenv("UAGENT_SIMULATOR_CAPTURE");
    if(!path || !*path || !display) return;
    lv_draw_buf_t * buf = lv_display_get_buf_active(display);
    if(!buf || !buf->data) return;
    FILE * out = fopen(path, "wb");
    if(!out) return;
    uint32_t row = (uint32_t)buf->header.w * 4U;
    uint32_t bytes = row * (uint32_t)buf->header.h;
    uint8_t header[54] = {0};
    header[0] = 'B'; header[1] = 'M';
    header[2] = (uint8_t)(54U + bytes); header[3] = (uint8_t)((54U + bytes) >> 8);
    header[4] = (uint8_t)((54U + bytes) >> 16); header[5] = (uint8_t)((54U + bytes) >> 24);
    header[10] = 54; header[14] = 40;
    header[18] = (uint8_t)buf->header.w; header[19] = (uint8_t)(buf->header.w >> 8);
    header[22] = (uint8_t)(-(int32_t)buf->header.h);
    header[23] = (uint8_t)((-(int32_t)buf->header.h) >> 8);
    header[24] = (uint8_t)((-(int32_t)buf->header.h) >> 16);
    header[25] = (uint8_t)((-(int32_t)buf->header.h) >> 24);
    header[26] = 1; header[28] = 32;
    fwrite(header, 1, sizeof(header), out);
    for(uint32_t y = 0; y < buf->header.h; y++)
        fwrite(buf->data + y * buf->header.stride, 1, row, out);
    fclose(out);
}
'''
    raw = raw.replace("#undef main", "#undef main\n#include <stdio.h>\n" + helper, 1)
    raw = re.sub(r"(?m)^([ \t]*)hal_init\((\d+)\s*,\s*(\d+)\);",
                 r"\1lv_display_t * uagent_capture_display = hal_init(\2, \3);", raw, count=1)
    raw = re.sub(r"(?m)^([ \t]*)lv_timer_handler\(\);",
                 r"\1lv_timer_handler();\n\1static unsigned uagent_frame_count;\n\1if(++uagent_frame_count == 10) uagent_capture_frame(uagent_capture_display);",
                 raw, count=1)
    entry.write_text(raw, encoding="utf-8", newline="\n")


def _expanded_controls(component) -> list[dict]:
    """Materialize literal array-driven controls without losing provenance."""
    expanded: list[dict] = []
    arrays = component.props.get("arrays", [])
    for control in component.props.get("controls", []):
        if not isinstance(control, dict):
            continue
        source_marker = str(control.get("icon_source", ""))
        if str(control.get("kind", "")).lower() == "button" and source_marker.startswith("array:"):
            source = source_marker.split(":", 1)[1].split(".", 1)[0]
            evidence = next((item for item in arrays if isinstance(item, dict) and item.get("name") == source), None)
            concrete = [item for item in (evidence.get("items", []) if isinstance(evidence, dict) else []) if isinstance(item, dict)]
            if concrete:
                for item in concrete:
                    clone = dict(control)
                    original_icon = clone.get("icon")
                    clone["text"] = next((item[key] for key in ("title", "label", "name", "value", "id")
                                          if item.get(key) not in (None, "")), control.get("text", ""))
                    if item.get("value") not in (None, "") and item.get("value") != clone["text"]:
                        clone["subtext"] = str(item["value"])
                    if item.get("icon"):
                        clone["icon"] = str(item["icon"])
                    if original_icon and item.get("icon") and original_icon != item.get("icon"):
                        clone["trailing_icon"] = str(original_icon)
                    if item.get("color"):
                        clone["icon_color"] = str(item["color"])
                        clone["icon_tile_background"] = str(item["color"])
                    expanded.append(clone)
                continue
        expanded.append(dict(control))
    # Some JSX maps use a dynamic button body, so the scanner cannot attach an
    # ``array:...`` marker to the control. If a component has exactly one
    # button-like template plus one repeated data array, materialize that
    # template into one LVGL row per item.
    if len(expanded) == len(component.props.get("controls", [])) and component.props.get("arrays"):
        repeated = next((a for a in component.props.get("arrays", [])
                         if isinstance(a, dict) and len(a.get("items", [])) > 1), None)
        template_indexes = [
            i for i, control in enumerate(expanded)
            if isinstance(control, dict)
            and control.get("kind") == "button"
            and (not str(control.get("text", "")).strip() or control.get("icon"))
        ]
        if repeated and len(template_indexes) == 1:
            i = template_indexes[0]
            template = expanded[i]
            rows: list[dict] = expanded[:i]
            for item in repeated.get("items", []):
                if not isinstance(item, dict):
                    continue
                clone = dict(template)
                original_icon = clone.get("icon")
                clone["text"] = next((item[k] for k in ("title", "name", "label", "value")
                                      if item.get(k) not in (None, "")), template.get("text", ""))
                if item.get("value") not in (None, "") and item.get("value") != clone["text"]:
                    clone["subtext"] = str(item["value"])
                if item.get("icon"):
                    clone["icon"] = str(item["icon"])
                if original_icon and item.get("icon") and original_icon != item.get("icon"):
                    clone["trailing_icon"] = str(original_icon)
                if item.get("color"):
                    clone["icon_color"] = str(item["color"])
                    clone["icon_tile_background"] = str(item["color"])
                rows.append(clone)
            # Drop unresolved JSX placeholders after expanding the repeated
            # template; they otherwise create overlapping blank widgets.
            tail = [
                item for item in expanded[i + 1:]
                if "{" not in str(item.get("text", ""))
            ]
            rows.extend(tail)
            expanded = rows
    if str(component.props.get("name", "")) == "LanguageSettings":
        languages = next((a for a in component.props.get("arrays", [])
                          if isinstance(a, dict) and a.get("name") == "languages"), None)
        first = next((item for item in (languages or {}).get("items", []) if isinstance(item, dict)), None)
        language_items = [item for item in (languages or {}).get("items", []) if isinstance(item, dict)]
        language_buttons = [control for control in expanded if control.get("kind") == "button"]
        for row_index, (control, item) in enumerate(zip(language_buttons, language_items)):
            native = str(item.get("nativeName") or item.get("name") or "")
            translated = str(item.get("name") or "")
            control["text"] = native
            control["subtext"] = translated if translated and translated != native else ""
            control["icon"] = ""
            control.pop("icon_tile_background", None)
            control["radio_state"] = "selected" if row_index == 0 else "normal"
            control["className"] = "border rounded-lg flex items-center bg-white"
        if first:
            expanded.append({"kind": "label", "text": f"\u5f53\u524d\u8bed\u8a00: {first.get('nativeName') or first.get('name', '')}",
                             "className": "text-[#279DFB] text-xs text-center"})
    if str(component.props.get("name", "")) == "InkStatus":
        ink = next((a for a in arrays if isinstance(a, dict) and a.get("name") == "inkLevels"), None)
        for item in (ink or {}).get("items", []):
            if isinstance(item, dict):
                expanded.append({"kind": "progress", "text": f"{item.get('percent', 0)}%",
                                 "className": f"bg-[{item.get('rgb', '#279DFB')}]",
                                 "icon_color": item.get("rgb"), "value": item.get("percent", 0)})
    return expanded


def _control_boxes(controls: list[dict], width: int, height: int, role: str) -> list[tuple[int, int, int, int]]:
    """First-stage layout slots; exact browser geometry replaces this in stage two."""
    if role == "navigation":
        count = max(1, len(controls))
        # CSS justify-around: equal space around each item, not the tighter
        # (count+1) distribution that pulled the first/last buttons inward.
        return [(4, max(2, round((index + 0.5) * height / count - 18)), 36, 36)
                for index in range(count)]
    leading_labels = 0
    for control in controls:
        if control.get("kind") != "label":
            break
        leading_labels += 1
    icon_buttons = [control for control in controls if control.get("kind") == "button" and control.get("icon")]
    progress = [c for c in controls if c.get("kind") == "progress"]
    card_grid = bool(icon_buttons) and all("flex-col" in str(control.get("className", "")) for control in icon_buttons)
    if (len(controls) >= 3 and controls[0].get("kind") == "icon_tile" and controls[1].get("kind") == "label"
            and card_grid):
        cards = controls[2:]
        cols, gap = 2, 8
        card_width = max(40, (width - 24 - gap) // cols)
        rows = max(1, (len(cards) + cols - 1) // cols)
        card_height = max(44, (height - 64 - gap * (rows - 1)) // rows)
        boxes = [(12, 12, 32, 32), (52, 16, max(40, width - 64), 28)]
        boxes.extend((12 + (index % cols) * (card_width + gap),
                      52 + (index // cols) * (card_height + gap), card_width, card_height)
                     for index in range(len(cards)))
        return boxes
    # Detail/settings pages commonly render an array as a vertical list of
    # full-width rows (icon, title/value, chevron).  Treat this as a list, not
    # as a three-column dashboard merely because there are three items.
    if (leading_labels >= 2 and icon_buttons and not card_grid and len(icon_buttons) >= 2):
        boxes = [(12, 12, 32, 32), (52, 18, max(40, width - 64), 24)]
        boxes.extend((12, 52 + index * 48, max(40, width - 24), 44)
                     for index in range(len(icon_buttons)))
        return boxes
    if card_grid and leading_labels:
        boxes: list[tuple[int, int, int, int]] = []
        for index in range(leading_labels):
            label_width = max(40, (width - 24) // leading_labels)
            boxes.append((12 + index * label_width, 20, label_width, 20))
        button_count = max(1, len(controls) - leading_labels)
        gap = 12
        button_width = max(40, (width - 24 - gap * (button_count - 1)) // button_count)
        boxes.extend((12 + index * (button_width + gap), 84, button_width, max(44, height - 96))
                     for index in range(button_count))
        return boxes
    # Paper/status detail screens are label pairs (field name/value), often
    # followed by a note. Preserve the two-column rows from the source.
    if leading_labels >= 2 and len(controls) >= 8 and not icon_buttons and not progress:
        rest = controls[leading_labels:]
        boxes = [(12, 12, 32, 32), (52, 18, max(40, width - 64), 24)]
        row_width = max(40, (width - 24 - 12) // 2)
        for index, _control in enumerate(rest):
            col, row = index % 2, index // 2
            boxes.append((12 + col * (row_width + 12), 56 + row * 32, row_width, 24))
        return boxes
    if progress:
        boxes = [(12, 12, 32, 32), (52, 18, max(40, width - 64), 24)]
        bar_w = max(18, (width - 24 - 3 * 8) // 4)
        bar_index = 0
        for control in controls[2:]:
            if control.get("kind") == "progress":
                boxes.append((12 + bar_index * (bar_w + 8), 56, bar_w, max(40, height - 76)))
                bar_index += 1
            else:
                boxes.append((12, 56 + (bar_index + 1) * 32, max(40, width - 24), 24))
        return boxes
    cols = max(1, min(3, width // 100))
    cell_width = max(48, (width - 24 - (cols - 1) * 8) // cols)
    return [(12 + (index % cols) * (cell_width + 8), 12 + (index // cols) * 52, cell_width, 44)
            for index in range(len(controls))]


def _image_widget(name: str, source: str, x: int, y: int, width: int, height: int,
                  index: int, parent_id: int, ids: _IdAllocator, text: str = "") -> str:
    widget_id = ids.take()
    return (
        f'<Widget parent-id="{parent_id}" name="{escape(name)}" id="{widget_id}" is-hidden="0" type="5" index="{index}">'
        f'<Normal><name>{escape(name)}</name><postion>{x},{y}</postion><size>{width},{height}</size><scrollbar-mode>0</scrollbar-mode><flag /></Normal>'
        '<Animate><use-anim>0</use-anim></Animate><ImgStorage><use-img-storage>0</use-img-storage></ImgStorage>'
        '<FontStorage><use-font-storage>0</use-font-storage></FontStorage>'
        f'<Attribute><src>{escape(source)}</src>'
        f'{f"<text>{escape(text)}</text>" if text else ""}'
        '<center-x>50</center-x><center-y>50</center-y><offset-x>0</offset-x><offset-y>0</offset-y><rotate>0</rotate></Attribute>'
        '<Style /><Event /></Widget>'
    )


def _write_screenshot_crop(screenshot: str | None, asset_dir: Path | None,
                           name: str, box: tuple[int, int, int, int],
                           canvas_size: tuple[int, int] | None = None) -> str | None:
    """Write a local crop for a DOM surface with no native LVGL mapping."""
    if not screenshot or asset_dir is None:
        return None
    try:
        from PIL import Image
        image = Image.open(BytesIO(base64.b64decode(screenshot))).convert("RGBA")
        x, y, width, height = box
        if canvas_size:
            canvas_width, canvas_height = canvas_size
            if (width * height) >= (canvas_width * canvas_height * .85):
                return None
        crop = image.crop((max(0, x), max(0, y), max(0, x + width), max(0, y + height)))
        if crop.width < 2 or crop.height < 2:
            return None
        filename = re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_") + ".png"
        target = asset_dir / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        crop.save(target)
        return filename
    except (OSError, ValueError, TypeError):
        return None


def _materialize_browser_image(model: ReactProjectModel, reference: str,
                                asset_dir: Path | None) -> str | None:
    """Copy/rasterize one DOM SVG/HTML image reference for the LVGL snapshot.

    Browser evidence may expose an absolute ``/figma-assets/foo.svg`` URL while
    the scanner stores it as ``public/figma-assets/foo.svg``.  Resolve only
    against the scanned resource graph; remote URLs and screenshot QA assets
    are deliberately ignored.
    """
    if asset_dir is None or not reference or reference.startswith(("data:", "http:", "https:")):
        return None
    clean = reference.split("?", 1)[0].split("#", 1)[0]
    if not clean or Path(clean).name.lower() in {"reference-end.png", "reference-end.jpg", "reference-end.jpeg"}:
        return None
    candidates = []
    normalized = clean.lstrip("/").replace("\\", "/")
    for resource_id, resource in model.resources.items():
        rid = str(resource_id).replace("\\", "/")
        if rid == normalized or rid.endswith("/" + normalized) or Path(rid).name == Path(normalized).name:
            candidates.append(resource)
    if not candidates:
        return None
    source = next((item.path for item in candidates if item.path.is_file()), None)
    if source is None:
        return None
    token = hashlib.sha1(str(source).encode("utf-8")).hexdigest()[:10]
    # Keep the filename a valid C token because custom.c and the standalone
    # snapshot harness pass image names through LVGL_IMAGE_PATH(name).
    stem = re.sub(r"[^A-Za-z0-9_]+", "_", source.stem).strip("_") or "image"
    target = asset_dir / f"uagent_dom_image_{stem}_{token}{source.suffix.lower()}"
    asset_dir.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        copy2(source, target)
    if source.suffix.lower() == ".svg":
        # Some SVG assets rely on the browser's currentColor cascade, which
        # standalone converters do not have. Give it a deterministic visible
        # color while preserving the original source beside the PNG.
        try:
            svg_text = target.read_text(encoding="utf-8")
            if "currentColor" in svg_text:
                target.write_text(svg_text.replace("currentColor", "#FFFFFF"), encoding="utf-8", newline="\n")
        except (OSError, UnicodeError):
            pass
        png = target.with_suffix(".png")
        if not png.exists():
            try:
                subprocess.run(["magick", "-background", "none", str(target), "-alpha", "on",
                                "-define", "png:color-type=6", str(png)],
                               check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=8)
            except (OSError, subprocess.SubprocessError):
                # cairosvg is optional; when unavailable retain the SVG source
                # so a simulator configured for LVGL SVG decoding still works.
                try:
                    import cairosvg  # type: ignore
                    cairosvg.svg2png(url=str(target), write_to=str(png))
                except (ImportError, OSError, ValueError):
                    return target.name
        return png.name
    return target.name
def _effect_fallback_controls(model: ReactProjectModel,
                              screen_layout: dict[str, Any] | None,
                              asset_dir: Path | None,
                              state_index: int,
                              canvas_size: tuple[int, int]) -> list[dict[str, Any]]:
    """Materialize only measured local effect crops from the RenderPlan."""
    if not isinstance(screen_layout, dict) or not isinstance(screen_layout.get("data"), dict):
        return []
    screenshot = screen_layout.get("screenshot_png_base64")
    if not screenshot or asset_dir is None:
        return []
    browser_effects = list(screen_layout.get("visual_effects", []))
    browser_effects.extend(browser_visual_effects(screen_layout.get("data")))
    unique: dict[str, dict[str, Any]] = {}
    for item in browser_effects:
        if isinstance(item, dict):
            unique[str(item.get("effect_id") or item.get("source_id") or len(unique))] = item
    available = list(unique.values())
    used: set[str] = set()
    controls: list[dict[str, Any]] = []
    for item in model.render_plan.items:
        if item.operation != "local-screenshot":
            continue
        effect_kind = str(item.properties.get("effect_kind", "")).casefold()
        candidates = [candidate for candidate in available
                      if str(candidate.get("effect_id")) == item.id
                      or str(candidate.get("source_id")) == item.source_id]
        if not candidates:
            candidates = [candidate for candidate in available
                          if str(candidate.get("kind", "")).casefold() == effect_kind
                          and str(candidate.get("effect_id")) not in used]
        if not candidates and effect_kind == "filter":
            candidates = [candidate for candidate in available
                          if str(candidate.get("kind", "")).casefold() in {"filter", "blur"}
                          and str(candidate.get("effect_id")) not in used]
        candidate = candidates[0] if candidates else None
        if candidate is None:
            continue
        bounds = candidate.get("bounds")
        if not isinstance(bounds, list) or len(bounds) != 4:
            continue
        try:
            crop_box = tuple(max(0, int(round(float(value)))) for value in bounds)
        except (TypeError, ValueError):
            continue
        if crop_box[2] * crop_box[3] >= canvas_size[0] * canvas_size[1] * .85:
            continue
        filename = _write_screenshot_crop(
            screenshot, asset_dir,
            f"uagent_effect_state_{state_index + 1}_{re.sub(r'[^A-Za-z0-9_-]+', '_', item.id)}",
            crop_box,
        )
        if not filename:
            continue
        used.add(str(candidate.get("effect_id")))
        controls.append({
            "kind": "image", "text": "", "src": filename,
            "dom_index": candidate.get("node_index"), "effect_id": item.id,
            "visual_effect": True, "style": {},
        })
    return controls


def _status_panel_widget(name: str, width: int, parent_id: int, ids: _IdAllocator,
                         box: tuple[int, int, int, int] | None = None) -> str:
    """Materialize the status/header panel used above an icon-card grid."""
    widget_id = ids.take()
    x, y, panel_width, panel_height = box or (12, 12, max(1, width - 24), 60)
    return (
        f'<Widget parent-id="{parent_id}" name="{escape(name)}" id="{widget_id}" is-hidden="0" type="28" index="0">'
        f'<Normal><name>{escape(name)}</name><postion>{x},{y}</postion><size>{panel_width},{panel_height}</size><scrollbar-mode>0</scrollbar-mode><flag /></Normal>'
        '<Animate><use-anim>0</use-anim></Animate><ImgStorage><use-img-storage>0</use-img-storage></ImgStorage>'
        '<FontStorage><use-font-storage>0</use-font-storage></FontStorage><Attribute />'
        '<Style><Part name="Main" value="0"><State name="Default" value="0">'
        '<bg-color>#17D4D8</bg-color><bg-opa>26</bg-opa><border-color>#17D4D8</border-color>'
        '<border-opa>102</border-opa><border-width>2</border-width><radius>8</radius>'
        '<pad-top>0</pad-top><pad-bottom>0</pad-bottom><pad-left>0</pad-left><pad-right>0</pad-right>'
        '</State></Part></Style><Event /></Widget>'
    )


def _control_widget(control: dict, name: str, box: tuple[int, int, int, int], index: int,
                    parent_id: int, ids: _IdAllocator, role: str) -> str:
    x, y, width, height = box
    kind = str(control.get("kind", "label")).lower()
    icon = str(control.get("icon") or "")
    # Settings pages use Lucide icons rendered as anonymous SVG nodes in the
    # browser. Recover their semantic identity from the adjacent button text
    # when the DOM snapshot cannot provide an SVG name.
    semantic_icons = {
        "设置": ("Settings", "#F7A905"), "基本设置": ("Settings", "#279DFB"),
        "维护": ("Wrench", "#0CCA70"), "切刀设置": ("Scissors", "#F57636"),
        "其他": ("MoreHorizontal", "#A45BF7"), "语言": ("Languages", "#279DFB"),
        "时间": ("Clock", "#0CCA70"), "网络设置": ("Wifi", "#A45BF7"),
    }
    inferred = semantic_icons.get(str(control.get("text") or control.get("value") or "").strip())
    if inferred and not icon:
        icon, inferred_color = inferred
        control = {**control, "icon": icon, "icon_tile_background": inferred_color,
                   "icon_color": "#FFFFFF"}
    type_id = {"label": "1", "button": "2", "icon_tile": "2", "container": "28", "panel": "28", "image": "5", "switch": "9", "checkbox": "2",
               "radio": "2", "slider": "8", "progress": "6", "input": "10", "select": "7", "list": "22",
               "tabs": "28", "gauge": "12"}.get(kind, "1")
    # UIBuilder button widgets do not reliably paint nested children. React
    # rows/cards are composite surfaces, so represent them as containers and
    # materialize their visual children explicitly.
    if kind == "button" and (icon or control.get("subtext") is not None
                             or control.get("radio_state") or "items-center" in str(control.get("className", ""))):
        type_id = "28"
    widget_id = ids.take()
    raw_text = str(control.get("text") or control.get("value") or "")
    # Figma Make often uses an ellipsis placeholder for icon-only navigation
    # buttons.  It is not a visible label and must not become a child LVGL
    # label (which otherwise appears as stray dots beside the sidebar icon).
    if role == "navigation" and raw_text.strip() in {"...", "…"}:
        raw_text = ""
    visible_text = "" if icon else raw_text
    class_name = str(control.get("className", ""))
    styled = {**control, "className": class_name}
    if control.get("radio_state") == "selected":
        styled["className"] = "bg-[#F4FAFF] border-[#279DFB] " + class_name
    if role == "navigation":
        class_name = "bg-[#17D4D8] rounded" if index == 0 else "bg-[#252C36] rounded"
        styled["className"] = class_name
        styled["nav_icon"] = True
        styled["icon_tile_background"] = "#17D4D8" if index == 0 else "#252C36"
    use_background_icon = bool(icon and (kind == "icon_tile" or styled.get("nav_icon")))
    normal = (f'<Normal><name>{escape(name)}</name><postion>{x},{y}</postion><size>{width},{height}</size>'
              '<scrollbar-mode>0</scrollbar-mode><flag /></Normal>')
    prefix = (f'<Widget parent-id="{parent_id}" name="{escape(name)}" id="{widget_id}" is-hidden="0" type="{type_id}" index="{index}">'
              + normal + '<Animate><use-anim>0</use-anim></Animate><ImgStorage><use-img-storage>0</use-img-storage></ImgStorage>'
              '<FontStorage><use-font-storage>0</use-font-storage></FontStorage>')
    if kind == "image":
        source = str(control.get("src") or "")
        return _image_widget(name, source, x, y, width, height, index, parent_id, ids,
                             text=str(control.get("text") or ""))
    if kind == "progress":
        value = max(0, min(100, int(control.get("value", 0) or 0)))
        indicator = str(control.get("icon_color") or "#279DFB")
        return (prefix + '<Attribute><mode>0</mode><min-value>0</min-value><max-value>100</max-value>'
                f'<start-value>0</start-value><value>{value}</value><is-animate>0</is-animate></Attribute>'
                '<Style><Part name="Main" value="0"><State name="Default" value="0">'
                '<bg-color>#E5E7EB</bg-color><bg-opa>255</bg-opa><radius>0</radius>'
                '</State></Part><Part name="Indicator" value="131072"><State name="Default" value="0">'
                f'<bg-color>{escape(indicator)}</bg-color><bg-opa>255</bg-opa><radius>0</radius>'
                '</State></Part></Style><Event /></Widget>')
    if kind == "gauge":
        def integer(key: str, default: int) -> int:
            m = re.search(r"-?\d+(?:\.\d+)?", str(control.get(key, default)))
            return int(float(m.group(0))) if m else default
        minimum = integer("min", 0); maximum = max(integer("max", 100), minimum + 1)
        value = max(minimum, min(maximum, integer("value", 0)))
        colors = control.get("colors") if isinstance(control.get("colors"), dict) else {}
        track = str(colors.get("track", "#1E2530")); normal = str(colors.get("normal", "#22C55E"))
        layers = [layer for layer in control.get("arc_paints", []) if isinstance(layer, dict)]
        if layers:
            def color_for(layer: dict[str, Any]) -> str:
                return str(layer.get("stroke_color") or layer.get("stroke") or normal)

            def opacity_for(layer: dict[str, Any]) -> int:
                try:
                    opacity = float(layer.get("opacity", 1))
                    if layer.get("role") == "glow-soft" and opacity >= 1:
                        opacity = 0.45
                    return max(0, min(255, round(opacity * 255)))
                except (TypeError, ValueError):
                    return 255

            def width_for(layer: dict[str, Any]) -> int:
                try:
                    width = max(1, round(float(layer.get("stroke_width", 6))))
                except (TypeError, ValueError):
                    width = 6
                # UIBuilder has no SVG blur primitive; a wider translucent Arc
                # preserves the source glow hierarchy without raster fallback.
                if layer.get("role") == "glow-soft":
                    width += 4
                return width

            layer_widgets: list[str] = []
            for layer_index, layer in enumerate(layers):
                layer_role = str(layer.get("role", "foreground"))
                layer_name = f"{name}_{layer_role}_{layer_index + 1}"
                layer_id = ids.take()
                layer_color = color_for(layer)
                layer_opa = opacity_for(layer)
                layer_width = width_for(layer)
                layer_value = value
                main_opa = opacity_for(layer) if layer_role in {"track", "red-zone"} else 0
                main_color = color_for(layer) if layer_role in {"track", "red-zone"} else "#000000"
                layer_widgets.append(
                    f'<Widget parent-id="{parent_id}" name="{escape(layer_name)}" id="{layer_id}" is-hidden="0" type="12" index="{index + layer_index}">'
                    f'<Normal><name>{escape(layer_name)}</name><postion>{x},{y}</postion><size>{width},{height}</size><scrollbar-mode>0</scrollbar-mode><flag /></Normal>'
                    '<Animate><use-anim>0</use-anim></Animate><ImgStorage><use-img-storage>0</use-img-storage></ImgStorage><FontStorage><use-font-storage>0</use-font-storage></FontStorage>'
                    f'<Attribute><mode>0</mode><min-value>{minimum}</min-value><max-value>{maximum}</max-value><value>{layer_value}</value><angle-start>135</angle-start><angle-end>405</angle-end><bg-angle-start>135</bg-angle-start><bg-angle-end>405</bg-angle-end><rotate>0</rotate></Attribute>'
                    f'<Style><Part name="Main" value="0"><State name="Default" value="0"><bg-color>{escape(main_color)}</bg-color><bg-opa>{main_opa}</bg-opa><arc-width>{layer_width}</arc-width></State></Part>'
                    f'<Part name="Indicator" value="131072"><State name="Default" value="0"><arc-color>{escape(layer_color)}</arc-color><arc-opa>{layer_opa}</arc-opa><arc-width>{layer_width}</arc-width></State></Part>'
                    '<Part name="Knob" value="196608"><State name="Default" value="0"><bg-opa>0</bg-opa></State></Part></Style><Event /></Widget>'
                )
            return "".join(layer_widgets)
        return (prefix + f'<Attribute><mode>0</mode><min-value>{minimum}</min-value><max-value>{maximum}</max-value><value>{value}</value>'
                '<angle-start>135</angle-start><angle-end>405</angle-end><bg-angle-start>135</bg-angle-start><bg-angle-end>405</bg-angle-end><rotate>0</rotate></Attribute>'
                f'<Style><Part name="Main" value="0"><State name="Default" value="0"><arc-color>{escape(track)}</arc-color><bg-color>{escape(track)}</bg-color><text-color>{escape(track)}</text-color><arc-width>6</arc-width><bg-img-opa>0</bg-img-opa></State></Part>'
                f'<Part name="Indicator" value="131072"><State name="Default" value="0"><arc-color>{escape(normal)}</arc-color><text-color>{escape(normal)}</text-color><arc-width>6</arc-width></State></Part>'
                '<Part name="Knob" value="196608"><State name="Default" value="0"><bg-opa>0</bg-opa></State></Part></Style><Event /></Widget>')
    horizontal = "items-center" in class_name and "flex-col" not in class_name
    if horizontal:
        visible_text = ""
    option_xml = ""
    if kind == "select":
        # UIBuilder type 7 expects one <option> element per item.  A pipe
        # separated string (and type 18) silently fell back to the theme and
        # did not create a usable dropdown.
        select_items = [str(item).strip() for item in (control.get("items") or []) if str(item).strip()]
        current = str(control.get("value") or control.get("text") or "").strip()
        if current and current not in select_items:
            select_items.insert(0, current)
        if not select_items:
            select_items = [current or "Select"]
        option_xml = "<options>" + "".join(f"<option>{escape(item)}</option>" for item in select_items) + "</options><is-open>0</is-open>"
        xml = prefix + f'<Attribute>{option_xml}</Attribute>{_snapshot_style(styled, kind, current)}<Event />'
    else:
        xml = prefix + f'<Attribute><text>{escape(visible_text)}</text></Attribute>{_snapshot_style(styled, kind, visible_text)}<Event />'
    children: list[str] = []
    if horizontal:
        icon_size = min(30 if control.get("icon_tile_background") else 20,
                        max(12, width - 8), max(12, height - 8))
        title_x = 10
        if icon:
            icon_x = 12
            icon_y = max(2, (height - icon_size) // 2)
            icon_file = (_icon_tile_filename(icon, control["icon_tile_background"])
                         if control.get("icon_tile_background") else
                         _icon_resource_filename(icon, control.get("icon_color")))
            children.append(_image_widget(f"{name}_icon", f"{icon_file}.png",
                                          icon_x, icon_y, icon_size, icon_size, 0, widget_id, ids))
            title_x = icon_x + icon_size + 12
        trailing_space = 28 if control.get("trailing_icon") or control.get("radio_state") else 6
        title_width = max(1, width - title_x - trailing_space)
        subtext = str(control.get("subtext") or "")
        if raw_text and "{" not in raw_text:
            title_id = ids.take()
            title_y = 5 if subtext else max(2, (height - 20) // 2)
            children.append(
                f'<Widget parent-id="{widget_id}" name="{escape(name)}_label" id="{title_id}" is-hidden="0" type="1" index="1">'
                f'<Normal><name>{escape(name)}_label</name><postion>{title_x},{title_y}</postion><size>{title_width},20</size><scrollbar-mode>0</scrollbar-mode><flag /></Normal>'
                '<Animate><use-anim>0</use-anim></Animate><ImgStorage><use-img-storage>0</use-img-storage></ImgStorage><FontStorage><use-font-storage>0</use-font-storage></FontStorage>'
                f'<Attribute><text>{escape(raw_text)}</text></Attribute>{_snapshot_style({"className": "text-[#252C36] text-xs"}, "label", raw_text)}<Event /></Widget>'
            )
            if control.get("radio_state") == "selected":
                check_x = min(width - trailing_space - 14,
                              title_x + (len(raw_text) * (16 if any("\u2e80" <= ch <= "\u9fff" for ch in raw_text) else 8)) + 4)
                check_file = _icon_resource_filename("Check", "#279DFB")
                children.append(_image_widget(f"{name}_selected_check", f"{check_file}.png",
                                              max(title_x, check_x), title_y + 3, 14, 14, 2, widget_id, ids))
        if subtext:
            subtitle_id = ids.take()
            children.append(
                f'<Widget parent-id="{widget_id}" name="{escape(name)}_subtitle" id="{subtitle_id}" is-hidden="0" type="1" index="2">'
                f'<Normal><name>{escape(name)}_subtitle</name><postion>{title_x},25</postion><size>{title_width},16</size><scrollbar-mode>0</scrollbar-mode><flag /></Normal>'
                '<Animate><use-anim>0</use-anim></Animate><ImgStorage><use-img-storage>0</use-img-storage></ImgStorage><FontStorage><use-font-storage>0</use-font-storage></FontStorage>'
                f'<Attribute><text>{escape(subtext)}</text></Attribute>{_snapshot_style({"className": "text-[#6B7280] text-xs"}, "label", subtext)}<Event /></Widget>'
            )
        if control.get("trailing_icon"):
            trailing = str(control["trailing_icon"])
            trailing_file = _icon_resource_filename(trailing, control.get("trailing_icon_color", "#9CA3AF"))
            children.append(_image_widget(f"{name}_trailing_icon", f"{trailing_file}.png",
                                          max(0, width - 28), max(2, (height - 16) // 2),
                                          16, 16, 3, widget_id, ids))
        if control.get("radio_state"):
            radio_id = ids.take()
            selected = control.get("radio_state") == "selected"
            radio_bg = "#279DFB" if selected else "#FFFFFF"
            radio_border = "#279DFB" if selected else "#D1D5DB"
            children.append(
                f'<Widget parent-id="{widget_id}" name="{escape(name)}_radio" id="{radio_id}" is-hidden="0" type="28" index="3">'
                f'<Normal><name>{escape(name)}_radio</name><postion>{max(0, width - 22)},{max(2, (height - 12) // 2)}</postion><size>12,12</size><scrollbar-mode>0</scrollbar-mode><flag /></Normal>'
                '<Animate><use-anim>0</use-anim></Animate><ImgStorage><use-img-storage>0</use-img-storage></ImgStorage><FontStorage><use-font-storage>0</use-font-storage></FontStorage><Attribute />'
                f'<Style><Part name="Main" value="0"><State name="Default" value="0"><bg-color>{radio_bg}</bg-color><bg-opa>255</bg-opa><border-color>{radio_border}</border-color><border-opa>255</border-opa><border-width>2</border-width><radius>6</radius></State></Part></Style><Event /></Widget>'
            )
            if selected:
                dot_id = ids.take()
                children.append(
                    f'<Widget parent-id="{widget_id}" name="{escape(name)}_radio_dot" id="{dot_id}" is-hidden="0" type="28" index="4">'
                    f'<Normal><name>{escape(name)}_radio_dot</name><postion>{max(0, width - 18)},{max(4, (height - 4) // 2)}</postion><size>4,4</size><scrollbar-mode>0</scrollbar-mode><flag /></Normal>'
                    '<Animate><use-anim>0</use-anim></Animate><ImgStorage><use-img-storage>0</use-img-storage></ImgStorage><FontStorage><use-font-storage>0</use-font-storage></FontStorage><Attribute />'
                    '<Style><Part name="Main" value="0"><State name="Default" value="0"><bg-color>#FFFFFF</bg-color><bg-opa>255</bg-opa><border-width>0</border-width><radius>2</radius></State></Part></Style><Event /></Widget>'
                )
    elif icon:
        icon_size = min(40 if "flex-col" in class_name else 20, max(12, width - 8), max(12, height - 8))
        if horizontal:
            icon_x = 12
            icon_y = max(2, (height - icon_size) // 2)
        else:
            icon_x = max(0, (width - icon_size) // 2)
            icon_y = max(4, (height - icon_size) // 2) if not raw_text else max(4, height // 5)
        if not use_background_icon:
            icon_file = (_icon_tile_filename(icon, control["icon_tile_background"])
                         if control.get("icon_tile_background") and "flex-col" in class_name
                         else _icon_resource_filename(icon, control.get("icon_color")))
            children.append(_image_widget(f"{name}_icon", f"{icon_file}.png",
                                          icon_x, icon_y,
                                          icon_size, icon_size, 0, widget_id, ids))
        if raw_text and "{" not in raw_text:
            label_id = ids.take()
            label_name = f"{name}_label"
            label_y = max(2, (height - 20) // 2) if horizontal else max(icon_y + icon_size + 2, height - 26)
            label_x = icon_x + icon_size + 12 if horizontal else 4
            label_width = max(1, width - label_x - (28 if horizontal and control.get("trailing_icon") else 4))
            children.append(
                f'<Widget parent-id="{widget_id}" name="{escape(label_name)}" id="{label_id}" is-hidden="0" type="1" index="1">'
                f'<Normal><name>{escape(label_name)}</name><postion>{label_x},{label_y}</postion><size>{label_width},20</size><scrollbar-mode>0</scrollbar-mode><flag /></Normal>'
                '<Animate><use-anim>0</use-anim></Animate><ImgStorage><use-img-storage>0</use-img-storage></ImgStorage><FontStorage><use-font-storage>0</use-font-storage></FontStorage>'
                f'<Attribute><text>{escape(raw_text)}</text></Attribute>{_snapshot_style({"className": "text-[#252C36] text-xs text-center"}, "label", raw_text)}<Event /></Widget>'
            )
    if children:
        xml += '<Children>' + ''.join(children) + '</Children>'
    return xml + '</Widget>'


def _container_widget(model: ReactProjectModel, component_id: str, role: str,
                      x: int, y: int, width: int, height: int, index: int,
                      ids: _IdAllocator, screen_key: str,
                      browser_geometry: dict[str, Any] | None = None) -> str:
    component = model.components[component_id]
    component_name = component.props.get("name", component_id.rsplit(":", 1)[-1])
    safe_name = re.sub(r"[^A-Za-z0-9_-]+", "_", f"{screen_key}_{component_name}")
    widget_id = ids.take()
    source_semantic_page = component_name in {"SettingsPage", "BasicSettings", "MaintenancePage"}
    controls = (_expanded_controls(component) if source_semantic_page else
                list(browser_geometry.get("materialized_controls", []))
                if browser_geometry and browser_geometry.get("materialized_controls")
                else (_expanded_controls(component)
                      if not browser_geometry or not browser_geometry.get("fallback_controls")
                      else list(browser_geometry["fallback_controls"])))
    # A composite SVG gauge is painted by custom.c as one scene.  Keep this
    # snapshot node as a transparent anchor only; browser/SVG fallback labels
    # would otherwise duplicate the scene and reintroduce generic Arc widgets.
    scene_mode = isinstance(component.props.get("gauge_scene"), dict)
    if scene_mode:
        controls = []
    if source_semantic_page:
        boxes = []
        cursor = 0
        if controls and controls[0].get("kind") == "icon_tile":
            boxes.append((9, 9, 32, 32)); cursor = 1
        if cursor < len(controls) and controls[cursor].get("kind") == "label":
            boxes.append((47, 14, max(40, width - 56), 22)); cursor += 1
        buttons = [i for i in range(cursor, len(controls)) if controls[i].get("kind") == "button"]
        grid = bool(buttons) and all("flex-col" in str(controls[i].get("className", "")) for i in buttons)
        if grid:
            gw, gh, gap = max(40, (width - 24) // 2), 78, 8
            boxes.extend((9 + (n % 2) * (gw + gap), 50 + (n // 2) * (gh + gap), gw, gh) for n in range(len(buttons)))
        else:
            boxes.extend((9, 50 + n * 56, width - 18, 50) for n in range(len(buttons)))
        while len(boxes) < len(controls):
            boxes.append((16, min(height - 18, 50 + len(buttons) * 56 + 4), width - 32, 16))
    else:
        boxes = list(browser_geometry.get("controls", [])) if browser_geometry else _control_boxes(controls, width, height, role)
    # Dynamic Lucide SVGs have no stable DOM name; add semantic icon tiles for
    # printer settings pages while retaining all collected text/card controls.
    icon_specs = {
        "SettingsPage": [("Settings", "#F7A905", (25, 62, 30, 30)), ("Wrench", "#0CCA70", (163, 62, 30, 30)), ("Scissors", "#F57636", (25, 146, 30, 30)), ("MoreHorizontal", "#A45BF7", (163, 146, 30, 30))],
        "BasicSettings": [("Languages", "#279DFB", (18, 58, 30, 30)), ("Clock", "#0CCA70", (18, 114, 30, 30)), ("Wifi", "#A45BF7", (18, 170, 30, 30))],
    }
    if False and component_name in icon_specs:
        for icon_name, color, icon_box in icon_specs[component_name]:
            controls.append({"kind": "icon_tile", "text": "", "icon": icon_name,
                             "icon_tile_background": color, "icon_color": "#FFFFFF"})
            boxes.append(icon_box)
    if False and component_name == "SettingsPage" and not any(str(c.get("text", "")).strip() == "基本设置" for c in controls):
        for text, box in [("基本设置", (18, 93, 70, 16)), ("维护", (156, 93, 50, 16)), ("切刀设置", (12, 177, 82, 16)), ("其他", (164, 177, 48, 16)), ("待定", (164, 194, 48, 14))]:
            controls.append({"kind": "label", "text": text, "className": "text-[#252C36] text-xs text-center"}); boxes.append(box)
    if False and component_name == "BasicSettings" and not any(str(c.get("text", "")).strip() == "语言" for c in controls):
        for text, box in [("语言", (58, 63, 70, 16)), ("简体中文", (58, 80, 100, 14)), ("时间", (58, 119, 70, 16)), ("2025-10-20 14:30", (58, 136, 130, 14)), ("网络设置", (58, 175, 80, 16)), ("已连接", (58, 192, 70, 14))]:
            controls.append({"kind": "label", "text": text, "className": "text-[#252C36] text-xs"}); boxes.append(box)
    if False and component_name == "MaintenancePage" and not any(str(c.get("text", "")).strip() == "Nozzle Check" for c in controls):
        for icon, color, box, title, sub in [("Droplets", "#09C2E0", (18, 58, 32, 32), "Nozzle Check", "检查喷嘴状态"), ("Sparkles", "#0CCA70", (18, 117, 32, 32), "Printhead Clean", "清洁打印头")]:
            controls.append({"kind": "icon_tile", "text": "", "icon": icon, "icon_tile_background": color, "icon_color": "#FFFFFF"}); boxes.append(box)
            controls.append({"kind": "label", "text": title, "className": "text-[#252C36] text-xs"}); boxes.append((58, box[1] + 3, 130, 16))
            controls.append({"kind": "label", "text": sub, "className": "text-[#6B7280] text-xs"}); boxes.append((58, box[1] + 19, 150, 14))
    if len(boxes) != len(controls):
        boxes = _control_boxes(controls, width, height, role)
    if len(boxes) != len(controls):
        # Some source components mix free-form metric labels and semantic
        # gauges.  Specialized layout heuristics may intentionally return a
        # short header/card prefix; never let that desynchronise controls and
        # geometry in the snapshot.
        cols = max(1, min(3, width // 100))
        cell_width = max(48, (width - 24 - (cols - 1) * 8) // cols)
        boxes = [(12 + (index % cols) * (cell_width + 8),
                  12 + (index // cols) * 52, cell_width, 44)
                 for index in range(len(controls))]
    if len(boxes) < len(controls):
        # Specialized semantic layouts may intentionally reserve only their
        # leading rows. Keep every source control renderable with a bounded
        # deterministic slot instead of indexing past the geometry list.
        existing = len(boxes)
        cols = max(1, min(3, width // 100))
        cell_width = max(32, (width - 24 - (cols - 1) * 8) // cols)
        boxes.extend((12 + (index % cols) * (cell_width + 8),
                      min(max(12, height - 28), 12 + (index // cols) * 28),
                      cell_width, 24)
                     for index in range(existing, len(controls)))
    elif len(boxes) > len(controls):
        boxes = boxes[:len(controls)]
    # Source-backed SVG gauges retain their measured canvas boxes even when
    # surrounding text leaves require generic fallback slots.
    for control_index, control in enumerate(controls):
        source_box = control.get("box") if isinstance(control, dict) else None
        if (control_index < len(boxes) and str(control.get("kind", "")).lower() == "gauge"
                and isinstance(source_box, (list, tuple)) and len(source_box) == 4):
            boxes[control_index] = tuple(int(value) for value in source_box)
    leading_labels = 0
    for control in controls:
        if control.get("kind") != "label":
            break
        leading_labels += 1
    icon_buttons = [control for control in controls if control.get("kind") == "button" and control.get("icon")]
    has_status_card_header = role == "page" and leading_labels >= 2 and bool(icon_buttons) and all(
        "flex-col" in str(control.get("className", "")) for control in icon_buttons
    )
    prefix_children = [_status_panel_widget(f"{safe_name}_status_panel", width, widget_id, ids,
                                            browser_geometry.get("status") if browser_geometry else None)] if has_status_card_header else []
    # Background paint is a page/container concern.  Emit it before every
    # semantic child so gauge custom layers and controls remain on top.
    background_paint = browser_geometry.get("background_paint") if browser_geometry else None
    gradient_type = str((background_paint or {}).get("gradient_type", "")).casefold()
    stops = (background_paint or {}).get("stops", []) if isinstance(background_paint, dict) else []
    if gradient_type == "radial" and isinstance(stops, list):
        colors = [str(item.get("color", "")) for item in stops if isinstance(item, dict) and item.get("color")]
        if colors:
            # LVGL 9 has no radial background primitive in the UIBuilder
            # snapshot dialect. A bounded stack of circles preserves the
            # source gradient without flattening the page to a screenshot.
            def rgb(value: str) -> tuple[int, int, int]:
                raw = value.strip().lstrip("#")
                if len(raw) == 3:
                    raw = "".join(char * 2 for char in raw)
                if re.fullmatch(r"[0-9a-fA-F]{6}", raw):
                    return tuple(int(raw[index:index + 2], 16) for index in (0, 2, 4))
                return (5, 6, 8)

            def hex_color(value: tuple[int, int, int]) -> str:
                return "#%02X%02X%02X" % tuple(max(0, min(255, int(channel))) for channel in value)

            # Preserve the source's outer dark stop and interpolate toward
            # its luminous inner stop.  Alpha is deliberately monotonic from
            # the opaque full-canvas base into translucent inner overlays.
            ordered_colors = [rgb(color) for color in reversed(colors)]
            source_opacity = []
            for item in reversed(stops):
                try:
                    source_opacity.append(max(0.0, min(1.0, float(item.get("opacity", 1.0)))) if isinstance(item, dict) else 1.0)
                except (TypeError, ValueError):
                    source_opacity.append(1.0)
            element_opacity = 1.0
            try:
                element_opacity = max(0.0, min(1.0, float((background_paint or {}).get("opacity", 1.0))))
            except (TypeError, ValueError):
                pass
            cumulative_overlay = 0.0
            layer_count = min(6, max(3, len(colors) * 2))
            layers: list[str] = []
            for layer_index in range(layer_count):
                # Keep common source stop positions visible in the bounded
                # approximation (notably the midpoint ambient blue stop).
                fractions = [0.0, 0.24, 0.48, 0.72, 0.86, 1.0]
                fraction = fractions[layer_index] if layer_count == 6 else layer_index / max(1, layer_count - 1)
                if len(ordered_colors) == 3:
                    scaled = fraction / 0.48 if fraction <= 0.48 else 1.0 + (fraction - 0.48) / 0.52
                else:
                    scaled = fraction * (len(ordered_colors) - 1)
                segment = min(len(ordered_colors) - 2, int(scaled))
                local = scaled - segment
                start, end = ordered_colors[segment], ordered_colors[segment + 1]
                color = hex_color(tuple(start[channel] * (1.0 - local) + end[channel] * local for channel in range(3)))
                if layer_index == 0:
                    # Keep the source's opaque dark canvas as the stable base.
                    opa = 255
                else:
                    start_opa = source_opacity[segment] if source_opacity else 0.0
                    end_opa = source_opacity[min(segment + 1, len(source_opacity) - 1)] if source_opacity else 0.0
                    target = max(0.0, min(1.0, (start_opa * (1.0 - local) + end_opa * local) * element_opacity))
                    # Convert target cumulative coverage into an incremental
                    # alpha for this overlay; direct target alpha compounds
                    # and makes the blue layer much too bright.
                    increment = (target - cumulative_overlay) / max(1e-6, 1.0 - cumulative_overlay)
                    opa = int(round(max(0.0, min(1.0, increment)) * 255))
                    cumulative_overlay = cumulative_overlay + (1.0 - cumulative_overlay) * (opa / 255.0)
                inset = int(round(min(width, height) * 0.22 * fraction))
                layer_w, layer_h = max(1, width - inset * 2), max(1, height - inset * 2)
                layer_id = ids.take()
                layers.append(
                    f'<Widget parent-id="{widget_id}" name="{escape(safe_name)}_gradient_{layer_index + 1}" id="{layer_id}" is-hidden="0" type="28" index="{layer_index}">'
                    f'<Normal><name>{escape(safe_name)}_gradient_{layer_index + 1}</name><postion>{inset},{inset}</postion><size>{layer_w},{layer_h}</size><scrollbar-mode>0</scrollbar-mode><flag /></Normal>'
                    '<Animate><use-anim>0</use-anim></Animate><ImgStorage><use-img-storage>0</use-img-storage></ImgStorage><FontStorage><use-font-storage>0</use-font-storage></FontStorage><Attribute />'
                    f'<Style><Part name="Main" value="0"><State name="Default" value="0"><bg-color>{escape(color)}</bg-color><bg-opa>{opa}</bg-opa><radius>999</radius><border-width>0</border-width></State></Part></Style><Event /></Widget>'
                )
            prefix_children = layers + prefix_children
    decorations = list(browser_geometry.get("decorations", [])) if browser_geometry else []
    for decoration_index, decoration in enumerate(decorations):
        prefix_children.append(_control_widget(decoration, f"{safe_name}_decoration_{decoration_index + 1}",
                                               tuple(decoration["box"]), len(prefix_children), widget_id, ids, role))
    control_styles = list(browser_geometry.get("control_styles", [])) if browser_geometry else []
    sync_groups = {
        str(item.properties.get("gauge_id")): item.properties
        for item in model.render_plan.items
        if item.operation == "arc-glow-layers" and item.source_id == component_id
    }
    gauge_cursor = 0
    rendered_controls: list[str] = []
    for number, control in enumerate(controls):
        rendered = dict(control)
        if rendered.get("kind") == "gauge":
            gauge_id = str(rendered.get("gauge_id") or f"gauge_{gauge_cursor + 1}")
            group = sync_groups.get(gauge_id)
            if group:
                rendered["gauge_id"] = gauge_id
                rendered["geometry"] = group.get("geometry") or rendered.get("geometry")
                rendered["arc_paints"] = group.get("ordered_layers") or group.get("layers") or rendered.get("arc_paints", [])
            gauge_cursor += 1
        if number < len(control_styles):
            override = dict(control_styles[number])
            if override.get("className"):
                # Computed browser colors must win over conditional class
                # branches retained in the static JSX scanner.
                override["className"] = (str(override["className"]) + " " + str(rendered.get("className", ""))).strip()
            rendered.update(override)
        rendered_controls.append(_control_widget(rendered, f"{safe_name}_{number + 1}", boxes[number],
                                                 number + len(prefix_children), widget_id, ids, role))
    children = ''.join(prefix_children) + ''.join(rendered_controls)
    background = (str(browser_geometry.get("background")) if role == "page" and browser_geometry and browser_geometry.get("background")
                  else "#252C36" if role == "navigation" else "#101218")
    background_opa = 0 if scene_mode or role in {"shared"} or (role == "page" and browser_geometry and browser_geometry.get("has_embedded_sidebar")) else 255
    # Browser captures can contain list items below the 320x240 viewport (for
    # example the language selector).  Keep those absolute coordinates and
    # make the LVGL container scrollable instead of clipping/overlapping them.
    # LVGL scrollbar mode 3 is AUTO; flag 16_0 is the UIBuilder encoding for
    # LV_OBJ_FLAG_SCROLLABLE, as used by the bundled smart_home template.
    content_bottom = max((int(box[1]) + int(box[3]) for box in boxes), default=0)
    # Browser geometry already separates fixed header/tab-bar regions. Making
    # the whole page scrollable here moves the tab bar below the canvas and
    # creates the white/duplicate bottom area seen in earlier exports.
    needs_scroll = role == "page" and content_bottom > height and not browser_geometry
    scrollbar_mode = "3" if needs_scroll else "0"
    flag = "16_0" if needs_scroll else ""
    native_gradient = ""
    if gradient_type == "linear" and isinstance(stops, list):
        colors = [str(item.get("color", "")) for item in stops if isinstance(item, dict) and item.get("color")]
        if len(colors) >= 2:
            direction = str((background_paint or {}).get("direction") or "")
            # UIBuilder/LVGL encode horizontal/vertical direction as 0/1;
            # preserve the source direction in an audit comment attribute.
            grad_dir = "1" if "bottom" in direction.casefold() or "180" in direction else "0"
            native_gradient = (f'<bg-grad-color>{escape(colors[-1])}</bg-grad-color><bg-grad-dir>{grad_dir}</bg-grad-dir>'
                               f'<uagent-gradient-type>linear</uagent-gradient-type><uagent-gradient-direction>{escape(direction)}</uagent-gradient-direction>')
    return (
        f'<Widget name="{escape(safe_name)}" id="{widget_id}" is-hidden="0" type="28" index="{index}">'
        f'<Normal><name>{escape(safe_name)}</name><postion>{x},{y}</postion><size>{width},{height}</size><scrollbar-mode>{scrollbar_mode}</scrollbar-mode><flag>{flag}</flag></Normal>'
        '<Animate><use-anim>0</use-anim></Animate><ImgStorage><use-img-storage>0</use-img-storage></ImgStorage><FontStorage><use-font-storage>0</use-font-storage></FontStorage><Attribute />'
        f'<Style><Part name="Main" value="0"><State name="Default" value="0"><bg-color>{background}</bg-color><bg-opa>{background_opa}</bg-opa>{native_gradient}<border-width>0</border-width><radius>0</radius>'
        '<pad-top>0</pad-top><pad-bottom>0</pad-bottom><pad-left>0</pad-left><pad-right>0</pad-right>'
        '</State></Part></Style><Event />'
        + (f'<Children>{children}</Children>' if children else '') + '</Widget>'
    )


def _browser_geometry(layout: dict[str, Any] | None, width: int, height: int,
                      component_name: str = "HomePage",
                      component_controls: list[dict[str, Any]] | None = None,
                      font_dir: Path | None = None) -> dict[str, dict[str, Any]]:
    """Convert viewport rectangles to canvas-relative rectangles.

    Chrome reports absolute viewport coordinates.  The Figma Make canvas is a
    centered 320x240 root, so every rectangle must first subtract the measured
    root origin.  This is deliberately DOM-semantic (nav/page/status/cards)
    instead of relying on hard-coded viewport offsets.
    """
    if not layout or not isinstance(layout.get("data"), dict):
        return {}
    data = layout["data"]
    nodes = [node for node in data.get("nodes", []) if isinstance(node, dict) and isinstance(node.get("rect"), dict)]
    root = data.get("root") or {}
    root_x, root_y = float(root.get("x", 0)), float(root.get("y", 0))
    root_w, root_h = float(root.get("width", width)), float(root.get("height", height))
    # Prefer a real application/frame root. The old "first relative node"
    # rule selected Energy's 68x84 sidebar logo and discarded the dashboard.
    # A candidate must cover a meaningful part of the viewport; full-screen
    # responsive roots (h-screen/min-h-screen) outrank small relative blocks.
    # A Figma Make frame can use inline React styles rather than Tailwind.
    # Use the declared canvas dimensions as first-class evidence too.
    fixed_hint = any(
        ("w-[320px]" in str(n.get("className", "")) and "h-[240px]" in str(n.get("className", "")))
        or (abs(float(n["rect"].get("width", 0)) - width) <= 2 and abs(float(n["rect"].get("height", 0)) - height) <= 2)
        for n in nodes
    )
    min_frame_area = root_w * root_h * (0.05 if fixed_hint else 0.5)
    candidates = [n for n in nodes if n.get("tag") == "div"
                  and (float(n["rect"].get("width", 0)) < root_w - 1 if fixed_hint else float(n["rect"].get("width", 0)) <= root_w + 1)
                  and (float(n["rect"].get("height", 0)) < root_h - 1 if fixed_hint else float(n["rect"].get("height", 0)) <= root_h + 1)
                  and float(n["rect"].get("width", 0)) * float(n["rect"].get("height", 0))
                      >= min_frame_area]
    frame_node = None
    if candidates:
        def root_score(node: dict[str, Any]) -> tuple[int, int, float, int]:
            classes = str(node.get("className", ""))
            full_screen = int("h-screen" in classes or "min-h-screen" in classes)
            exact_canvas = int(abs(float(node["rect"].get("width", 0)) - width) <= 2 and abs(float(node["rect"].get("height", 0)) - height) <= 2)
            area = float(node["rect"].get("width", 0)) * float(node["rect"].get("height", 0))
            # Earlier DOM nodes are generally outer ancestors.
            return exact_canvas, full_screen, area, -int(node.get("index", 0))
        frame_node = max(candidates, key=root_score)
        frame = frame_node["rect"]
        root_x, root_y, root_w, root_h = (float(frame.get(k, v)) for k, v in
                                           (("x", root_x), ("y", root_y), ("width", root_w), ("height", root_h)))

    scale_x = width / root_w if root_w else 1.0
    scale_y = height / root_h if root_h else 1.0
    def rel(rect: dict[str, Any]) -> tuple[int, int, int, int]:
        x = (float(rect.get("x", 0)) - root_x) * scale_x
        y = (float(rect.get("y", 0)) - root_y) * scale_y
        w = float(rect.get("width", 0)) * scale_x
        h = float(rect.get("height", 0)) * scale_y
        return tuple(int(round(v)) for v in (x, y, w, h))

    nav_nodes = [n for n in nodes if "flex-col" in str(n.get("className", ""))
                 and float(n["rect"].get("width", 0)) <= max(140, root_w * 0.18)
                 and float(n["rect"].get("height", 0)) >= root_h * 0.7]
    # Prefer the real semantic main/page wrapper.  Energy's main is
    # ``flex-1 overflow-auto`` and intentionally has no ``bg-white`` class;
    # requiring the latter made all controls relative to the full root and
    # allowed sidebar nodes to leak into the page geometry.
    page_nodes = [n for n in nodes if n.get("tag") == "main"]
    page_nodes += [n for n in nodes
                   if n.get("tag") != "main"
                   and "flex-1" in str(n.get("className", ""))
                   and "overflow-auto" in str(n.get("className", ""))]
    # Monolithic dashboards often have no shared navigation or Tailwind
    # ``flex-1 bg-white`` page wrapper. Use the captured root as the page and
    # let the DOM fallback controls populate it instead of returning empty.
    nav = max(nav_nodes, key=lambda n: float(n["rect"].get("width", 0)) * float(n["rect"].get("height", 0))) if nav_nodes else None
    if page_nodes:
        page = page_nodes[0]
    elif fixed_hint and frame_node:
        fr = frame_node["rect"]
        inner = [n for n in nodes if n is not frame_node
                 and fr["x"] + 40 < n["rect"]["x"] < fr["x"] + fr["width"]
                 and fr["y"] <= n["rect"]["y"] <= fr["y"] + 2
                 and n["rect"]["width"] >= fr["width"] * 0.6
                 and n["rect"]["width"] < fr["width"]
                 and n["rect"]["height"] >= fr["height"] - 4]
        page = max(inner, key=lambda n: n["rect"]["width"] * n["rect"]["height"]) if inner else frame_node
    else:
        page = {"rect": {"x": root_x, "y": root_y, "width": root_w, "height": root_h}}
    px, py, _, _ = rel(page["rect"])
    nav_x, nav_y, nav_w, nav_h = rel(nav["rect"]) if nav else (0, 0, 0, 0)
    page_w, page_h = rel(page["rect"])[2:]
    nav_buttons = [n for n in nodes if nav and n.get("tag") == "button"
                   and nav["rect"]["x"] <= n["rect"]["x"] < nav["rect"]["x"] + nav["rect"]["width"]]
    nav_buttons.sort(key=lambda n: float(n["rect"].get("y", 0)))
    nav_geometry = {"box": (nav_x, nav_y, nav_w, nav_h),
                    "controls": [(int(round(float(n["rect"]["x"]) - nav["rect"]["x"])),
                                  int(round(float(n["rect"]["y"]) - nav["rect"]["y"])),
                                  int(round(float(n["rect"]["width"]))), int(round(float(n["rect"]["height"]))))
                                 for n in nav_buttons]}

    if component_name == "InkStatus":
        inside = [n for n in nodes if n is not page
                  and page["rect"]["x"] <= n["rect"]["x"]
                  and page["rect"]["y"] <= n["rect"]["y"]
                  and n["rect"]["x"] + n["rect"]["width"] <= page["rect"]["x"] + page["rect"]["width"] + 1]

        def ink_box(node: dict[str, Any], safe_width: int = 0) -> tuple[int, int, int, int]:
            x = int(round(float(node["rect"]["x"]) - page["rect"]["x"]))
            y = int(round(float(node["rect"]["y"]) - page["rect"]["y"]))
            w = max(int(round(float(node["rect"]["width"]))), safe_width)
            return x, y, min(w, page_w - x), max(1, int(round(float(node["rect"]["height"]))))

        header_tile = next((n for n in inside if n.get("tag") == "div" and "bg-[#279DFB]" in str(n.get("className", ""))), None)
        header_text = next((n for n in inside if n.get("tag") == "h2" and str(n.get("text", "")).strip()), None)
        bars = [n for n in inside if n.get("tag") == "div" and "h-[120px]" in str(n.get("className", ""))
                and "w-[20px]" in str(n.get("className", ""))]
        percents = [n for n in inside if re.fullmatch(r"\d+%", str(n.get("text", "")).strip())]
        channel_labels = [n for n in inside if str(n.get("text", "")).strip() in {"C", "M", "Y", "K"}]
        status_label = next((n for n in inside if str(n.get("text", "")).strip() == "\u58a8\u6c34\u5145\u8db3"), None)
        svg_nodes = [n for n in inside if n.get("tag") == "svg"]
        materialized: list[dict[str, Any]] = []
        boxes: list[tuple[int, int, int, int]] = []
        if header_tile:
            materialized.append({"kind": "icon_tile", "text": "", "className": "bg-[#279DFB] rounded",
                                 "icon": "Droplet", "icon_tile_background": "#279DFB", "icon_color": "#ffffff"})
            boxes.append(ink_box(header_tile))
        if header_text:
            materialized.append({"kind": "label", "text": str(header_text.get("text", "")),
                                 "className": "text-[#252C36]"})
            boxes.append(ink_box(header_text, 80))
        palette = ["#09C2E0", "#ED4B4E", "#F7A905", "#252C36", "#9CA3AF"]
        for index, bar in enumerate(bars[:5]):
            percent = int(str(percents[index].get("text", "0%")).rstrip("%")) if index < len(percents) else 0
            materialized.append({"kind": "progress", "text": "", "value": percent,
                                 "icon_color": palette[index], "className": ""})
            boxes.append(ink_box(bar))
        for node in percents[:5]:
            materialized.append({"kind": "label", "text": str(node.get("text", "")),
                                 "className": "text-[#252C36] text-center"})
            boxes.append(ink_box(node, 28))
        for node in channel_labels[:4]:
            materialized.append({"kind": "label", "text": str(node.get("text", "")),
                                 "className": "text-xs text-center"})
            boxes.append(ink_box(node, 16))
        if len(svg_nodes) > 1:
            wrench = svg_nodes[-1]
            materialized.append({"kind": "icon_tile", "text": "", "className": "",
                                 "icon": "Wrench", "icon_color": "#9CA3AF"})
            boxes.append(ink_box(wrench))
        if status_label:
            materialized.append({"kind": "label", "text": "\u58a8\u6c34\u5145\u8db3",
                                 "className": "text-[#0CCA70] text-xs text-center"})
            boxes.append(ink_box(status_label, 72))
        return {"Navigation": nav_geometry,
                component_name: {"box": (px, py, page_w, page_h), "controls": boxes,
                                 "materialized_controls": materialized}}

    if False and component_name in {"SettingsPage", "BasicSettings"}:
        # These pages use dynamically selected Lucide components; the DOM
        # exposes anonymous SVG paths, so restore the known semantic mapping
        # from the component contract instead of dropping the icons.
        mapping = {
            "SettingsPage": [("Settings", "#F7A905", (9, 9, 32, 32)),
                             ("Settings", "#279DFB", (25, 62, 30, 30)),
                             ("Wrench", "#0CCA70", (163, 62, 30, 30)),
                             ("Scissors", "#F57636", (25, 146, 30, 30)),
                             ("MoreHorizontal", "#A45BF7", (163, 146, 30, 30))],
            "BasicSettings": [("Settings", "#279DFB", (9, 9, 32, 32)),
                              ("Languages", "#279DFB", (18, 58, 30, 30)),
                              ("Clock", "#0CCA70", (18, 114, 30, 30)),
                              ("Wifi", "#A45BF7", (18, 170, 30, 30))],
        }[component_name]
        materialized = [{"kind": "icon_tile", "text": "", "icon": icon,
                         "icon_tile_background": color, "icon_color": "#FFFFFF"}
                        for icon, color, _ in mapping]
        boxes = [box for _, _, box in mapping]
        return {"Navigation": nav_geometry,
                component_name: {"box": (px, py, page_w, page_h), "controls": boxes,
                                 "materialized_controls": materialized}}

    if component_name == "TimeSettings":
        inside = [n for n in nodes if n is not page
                  and page["rect"]["x"] <= n["rect"]["x"]
                  and page["rect"]["y"] <= n["rect"]["y"]
                  and n["rect"]["x"] + n["rect"]["width"] <= page["rect"]["x"] + page["rect"]["width"] + 1]

        def time_box(node: dict[str, Any]) -> tuple[int, int, int, int]:
            return (int(round(float(node["rect"]["x"]) - page["rect"]["x"])),
                    int(round(float(node["rect"]["y"]) - page["rect"]["y"])),
                    max(1, int(round(float(node["rect"]["width"])))),
                    max(1, int(round(float(node["rect"]["height"])))))

        header_tile = next((n for n in inside if n.get("tag") == "div" and "bg-[#0CCA70]" in str(n.get("className", ""))), None)
        header_text = next((n for n in inside if n.get("tag") == "h2"), None)
        cards = [n for n in inside if n.get("tag") == "div" and "border-2" in str(n.get("className", ""))
                 and float(n["rect"].get("width", 0)) > 200]
        labels = [n for n in inside if n.get("tag") == "label"]
        inputs = [n for n in inside if n.get("tag") == "input"]
        format_buttons = [n for n in inside if n.get("tag") == "button" and str(n.get("text", "")).strip()]
        current = next((n for n in inside if n.get("tag") == "p" and "\u5f53\u524d\u8bbe\u7f6e" in str(n.get("text", ""))), None)
        materialized: list[dict[str, Any]] = []
        boxes: list[tuple[int, int, int, int]] = []

        def add(control: dict[str, Any], box: tuple[int, int, int, int]) -> None:
            materialized.append(control)
            boxes.append(box)

        if header_tile:
            add({"kind": "icon_tile", "text": "", "icon": "Clock", "icon_tile_background": "#0CCA70",
                 "icon_color": "#FFFFFF", "className": "bg-[#0CCA70] rounded"}, time_box(header_tile))
        if header_text:
            add({"kind": "label", "text": str(header_text.get("text", "\u65f6\u95f4\u8bbe\u7f6e")), "className": "text-[#252C36]"}, time_box(header_text))
        display_values = ["10/20/2025", "02:30 PM"]
        suffix_icons = ["Calendar", "Clock"]
        for row_index in range(min(2, len(cards), len(labels), len(inputs))):
            card_box = time_box(cards[row_index])
            label_box = time_box(labels[row_index])
            input_box = time_box(inputs[row_index])
            add({"kind": "button", "text": "", "className": "bg-white border-2 border-gray-200 rounded-lg"}, card_box)
            add({"kind": "label", "text": str(labels[row_index].get("text", "")), "className": "text-[#252C36] text-sm"}, label_box)
            add({"kind": "button", "text": "", "className": "bg-[#F9FAFB] border border-gray-200 rounded"}, input_box)
            add({"kind": "label", "text": display_values[row_index], "className": "text-[#252C36] text-xs"},
                (input_box[0] + 10, input_box[1] + 7, max(1, input_box[2] - 42), 18))
            add({"kind": "image", "src": f"{_icon_resource_filename(suffix_icons[row_index], '#252C36')}.png"},
                (input_box[0] + input_box[2] - 24, input_box[1] + 9, 14, 14))
        if len(cards) > 2:
            add({"kind": "button", "text": "", "className": "bg-white border-2 border-gray-200 rounded-lg"}, time_box(cards[2]))
        if len(labels) > 2:
            add({"kind": "label", "text": str(labels[2].get("text", "\u65f6\u95f4\u683c\u5f0f")), "className": "text-[#252C36] text-sm"}, time_box(labels[2]))
        for button_index, node in enumerate(format_buttons[:2]):
            add({"kind": "button", "text": str(node.get("text", "")),
                 "className": ("bg-[#0CCA70] text-[#FFFFFF] rounded text-xs" if button_index == 0
                               else "bg-[#F3F4F6] text-[#252C36] rounded text-xs")}, time_box(node))
        if current:
            current_box = time_box(current)
            add({"kind": "button", "text": "", "className": "bg-[#F0FDF4] border border-[#0CCA70] rounded"},
                (9, current_box[1] - 6, 256, current_box[3] + 12))
            add({"kind": "label", "text": str(current.get("text", "\u5f53\u524d\u8bbe\u7f6e: 2025-10-20 14:30")),
                 "className": "text-[#0CCA70] text-xs text-center"}, current_box)
        return {"Navigation": nav_geometry,
                component_name: {"box": (px, py, page_w, page_h), "controls": boxes,
                                 "materialized_controls": materialized}}

    if component_name != "HomePage" and component_controls is not None:
        boxes: list[tuple[int, int, int, int]] = []
        styles: list[dict[str, Any]] = []
        matched_controls: list[dict[str, Any]] = []
        inside = [n for n in nodes if n is not page
                  and page["rect"]["x"] <= n["rect"]["x"]
                  and page["rect"]["y"] <= n["rect"]["y"]
                  and n["rect"]["x"] + n["rect"]["width"] <= page["rect"]["x"] + page["rect"]["width"] + 1]
        used: set[int] = set()
        button_cursor = 0
        progress_cursor = 0
        page_buttons = [n for n in inside if n.get("tag") == "button"]
        progress_nodes = [n for n in inside if n.get("tag") == "div"
                          and "h-[120px]" in str(n.get("className", ""))
                          and "w-[20px]" in str(n.get("className", ""))]

        def relative_box(node: dict[str, Any], *, safe_text: str = "") -> tuple[int, int, int, int]:
            x = int(round(float(node["rect"]["x"]) - page["rect"]["x"]))
            y = int(round(float(node["rect"]["y"]) - page["rect"]["y"]))
            w = int(round(float(node["rect"]["width"])))
            h = int(round(float(node["rect"]["height"])))
            if safe_text:
                style = node.get("style", {})
                font_match = re.search(r"[0-9]+(?:\.[0-9]+)?", str(style.get("fontSize", "")))
                font_size = max(1, round(float(font_match.group(0)))) if font_match else 16
                has_cjk = any("\u2e80" <= ch <= "\u9fff" for ch in safe_text)
                measured = 0
                font_path = font_dir / ("DroidSansFallback.ttf" if has_cjk else "montserratMedium.ttf") if font_dir else None
                if font_path and font_path.is_file():
                    try:
                        from PIL import ImageFont
                        measured = math.ceil(ImageFont.truetype(str(font_path), font_size).getlength(safe_text))
                    except (ImportError, OSError, ValueError):
                        pass
                if not measured:
                    measured = math.ceil(len(safe_text) * font_size * (0.95 if has_cjk else 0.65))
                expanded = max(w, measured + max(3, round(font_size * 0.08)))
                anchor = str(style.get("textAnchor", "")).lower()
                if anchor == "middle":
                    x -= (expanded - w) // 2
                elif anchor == "end":
                    x -= expanded - w
                w = min(expanded, page_w - max(0, x))
                # A chosen DOM node is browser evidence: keep its measured
                # height, including compact footer labels.  The 20px minimum
                # belongs only to static fallback slots, not measured boxes.
            return x, y, max(1, w), max(1, h)

        def node_style(node: dict[str, Any]) -> dict[str, Any]:
            style = node.get("style", {})
            color = str(style.get("color", ""))
            background = str(style.get("backgroundColor", ""))
            classes: list[str] = []
            for prefix, value in (("text", color), ("bg", background)):
                match = re.search(r"rgb\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)", value)
                if match:
                    classes.append(f"{prefix}-[#{int(match.group(1)):02X}{int(match.group(2)):02X}{int(match.group(3)):02X}]")
            if int(round(float(node["rect"].get("height", 20)))) <= 12:
                classes.append("text-xs")
            return {"className": " ".join(classes)} if classes else {}

        for control in component_controls:
            kind = str(control.get("kind", "label")).lower()
            text_value = str(control.get("text", "")).strip()
            dom_index = control.get("dom_index")
            explicit_box = control.get("box")
            if kind == "gauge" and isinstance(explicit_box, (list, tuple)) and len(explicit_box) == 4:
                matched_controls.append(control)
                boxes.append(tuple(int(value) for value in explicit_box))
                styles.append(node_style({"rect": {"height": explicit_box[3]}, "style": control.get("style", {})}))
                continue
            chosen = next((n for n in inside if n.get("index") == dom_index), None) if dom_index is not None else None
            if chosen is None and kind == "button" and button_cursor < len(page_buttons):
                chosen = page_buttons[button_cursor]
                button_cursor += 1
            elif chosen is None and kind == "progress" and progress_cursor < len(progress_nodes):
                chosen = progress_nodes[progress_cursor]
                progress_cursor += 1
            elif chosen is None and kind == "icon_tile":
                chosen = next((n for n in inside if n["index"] not in used and n.get("tag") == "div"
                               and "rounded" in str(n.get("className", ""))
                               and 20 <= float(n["rect"].get("width", 0)) <= 48
                               and 20 <= float(n["rect"].get("height", 0)) <= 48), None)
            elif chosen is None and kind == "label" and text_value and "{" not in text_value:
                chosen = next((n for n in inside if n["index"] not in used
                               and str(n.get("text", "")).strip() == text_value), None)
            elif chosen is None and kind == "gauge":
                chosen = next((n for n in inside if n["index"] not in used and n.get("tag") == "svg"
                               and float(n["rect"].get("width", 0)) >= 40), None)
            if chosen is not None:
                used.add(int(chosen["index"]))
                matched_controls.append(control)
                boxes.append(relative_box(chosen, safe_text=text_value if kind == "label" else ""))
                styles.append(node_style(chosen))
        return {"Navigation": nav_geometry,
                component_name: {"box": (px, py, page_w, page_h), "controls": boxes,
                                 "control_styles": styles, "materialized_controls": matched_controls,
                                 "has_embedded_sidebar": bool(nav)}}

    status_nodes = [n for n in nodes if "h-[60px]" in str(n.get("className", ""))]
    card_nodes = [n for n in nodes if n.get("tag") == "button" and "flex-col" in str(n.get("className", ""))
                  and float(n["rect"].get("width", 0)) > 50]
    card_nodes.sort(key=lambda n: float(n["rect"].get("x", 0)))
    text_nodes = [n for n in nodes if n.get("tag") in {"span", "div"} and str(n.get("text", "")).strip()
                  and status_nodes
                  and status_nodes[0]["rect"]["x"] <= n["rect"]["x"] <= status_nodes[0]["rect"]["x"] + status_nodes[0]["rect"]["width"]
                  and status_nodes[0]["rect"]["y"] <= n["rect"]["y"] <= status_nodes[0]["rect"]["y"] + status_nodes[0]["rect"]["height"]]
    text_nodes.sort(key=lambda n: float(n["rect"].get("x", 0)))
    controls: list[tuple[int, int, int, int]] = []
    control_styles: list[dict[str, Any]] = []
    for n in text_nodes[:2]:
        tx = int(round(float(n["rect"]["x"]) - page["rect"]["x"]))
        ty = int(round(float(n["rect"]["y"]) - page["rect"]["y"]))
        measured_width = int(round(float(n["rect"]["width"])))
        text_value = str(n.get("text", ""))
        char_width = 16 if any("\u2e80" <= ch <= "\u9fff" for ch in text_value) else 8
        safe_width = max(measured_width, len(text_value) * char_width + 8)
        controls.append((tx, ty, max(1, min(safe_width, page_w - tx)),
                         max(20, int(round(float(n["rect"]["height"]))))))
        color = str(n.get("style", {}).get("color", ""))
        rgb = re.search(r"rgb\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)", color)
        style_class = (f"text-[#{int(rgb.group(1)):02X}{int(rgb.group(2)):02X}{int(rgb.group(3)):02X}]"
                       if rgb else "")
        if int(round(float(n["rect"]["height"]))) <= 12:
            style_class += " text-xs"
        control_styles.append({"className": style_class.strip()})
    controls.extend((int(round(float(n["rect"]["x"]) - page["rect"]["x"])),
                     int(round(float(n["rect"]["y"]) - page["rect"]["y"])),
                     int(round(float(n["rect"]["width"]))), int(round(float(n["rect"]["height"]))))
                    for n in card_nodes[:3])
    control_styles.extend({} for _ in card_nodes[:3])
    dot_nodes = [n for n in nodes if "rounded-full" in str(n.get("className", ""))
                 and "bg-[#0CCA70]" in str(n.get("className", ""))]
    decorations = [{"kind": "label", "text": "", "className": "bg-[#0CCA70] rounded-full",
                    "box": (int(round(float(n["rect"]["x"]) - page["rect"]["x"])),
                            int(round(float(n["rect"]["y"]) - page["rect"]["y"])),
                            int(round(float(n["rect"]["width"]))), int(round(float(n["rect"]["height"]))))}
                   for n in dot_nodes[:1]]
    dom_images = [c for c in (component_controls or []) if str(c.get("kind", "")).lower() == "image"]
    materialized_images: list[dict[str, Any]] = []
    for control in dom_images:
        if any(n.get("index") == control.get("dom_index") for n in nodes):
            materialized_images.append(control)
    return {
        "Navigation": nav_geometry,
        component_name: {"box": (px, py, page_w, page_h), "controls": controls,
                      "control_styles": control_styles, "decorations": decorations,
                      "materialized_controls": materialized_images,
                      "status": (int(round(float(status_nodes[0]["rect"]["x"]) - page["rect"]["x"])),
                                 int(round(float(status_nodes[0]["rect"]["y"]) - page["rect"]["y"])),
                                 int(round(float(status_nodes[0]["rect"]["width"]))), int(round(float(status_nodes[0]["rect"]["height"])))) if status_nodes else None},
    }


def _dom_fallback_controls(data: dict[str, Any], screenshot: str | None = None,
                           asset_dir: Path | None = None, state_index: int = 0,
                           gauge_layout: list[tuple[int, int, int, int]] | None = None,
                           model: ReactProjectModel | None = None) -> list[dict[str, Any]]:
    """Build a conservative control list for monolithic Make dashboards.

    Some Make exports put the complete UI in ``App`` and expose no component
    controls to the static scanner. Browser evidence still has semantic tags;
    retain visible buttons/inputs/headings and text leaves so the generated
    LVGL screen is useful instead of an empty root container.
    """
    nodes = [n for n in data.get("nodes", []) if isinstance(n, dict) and isinstance(n.get("rect"), dict)]
    canvas = discover_canvas(data)
    canvas_w, canvas_h = float(canvas["width"]), float(canvas["height"])
    frame = next((n for n in nodes if abs(float(n["rect"].get("width", 0)) - canvas_w) <= 2
                  and abs(float(n["rect"].get("height", 0)) - canvas_h) <= 2), None)
    by_index = {n.get("index"): n for n in nodes}
    scroll_root = next((n for n in nodes if frame and n.get("tag") == "div"
                        and n.get("parentIndex") == frame.get("index")
                        and 100 <= float(n["rect"].get("height", 0)) < 220
                        and any(child.get("parentIndex") == n.get("index")
                                and float(child["rect"].get("y", 0)) + float(child["rect"].get("height", 0))
                                > float(n["rect"].get("y", 0)) + float(n["rect"].get("height", 0)) + 2
                                for child in nodes)), None)

    def in_scroll_tree(node: dict[str, Any]) -> bool:
        parent = node.get("parentIndex")
        while parent in by_index:
            if scroll_root and parent == scroll_root.get("index"):
                return True
            parent = by_index[parent].get("parentIndex")
        return False
    controls: list[dict[str, Any]] = []
    nav_roots: set[int] = set()
    nav_icon_names = ["Zap", "LayoutDashboard", "BarChart3", "Settings", "FileText", "User"]
    select_options = list(data.get("select_options") or [])
    select_cursor = 0

    def painted_color(value: Any) -> bool:
        raw = str(value or "").strip().casefold()
        if not raw or raw == "transparent":
            return False
        match = re.fullmatch(r"rgba?\(\s*[^,]+\s*,\s*[^,]+\s*,\s*[^,]+(?:\s*,\s*([0-9.]+))?\s*\)", raw)
        if match and match.group(1) is not None:
            try:
                return float(match.group(1)) > 0
            except ValueError:
                return False
        return raw not in {"rgba(0, 0, 0, 0)", "rgba(0,0,0,0)"}

    def painted_div(node: dict[str, Any]) -> bool:
        style = node.get("style") if isinstance(node.get("style"), dict) else {}
        try:
            if float(str(style.get("opacity", "1"))) <= 0:
                return False
        except (TypeError, ValueError):
            pass
        if painted_color(style.get("backgroundColor")):
            return True
        for side in ("Top", "Right", "Bottom", "Left"):
            width = str(style.get(f"border{side}Width", ""))
            match = re.search(r"([0-9]+(?:\.[0-9]+)?)", width)
            if match and float(match.group(1)) > 0 and painted_color(style.get(f"border{side}Color")):
                return True
        return False

    for node in nodes:
        parent = node.get("parentIndex")
        ancestor = parent
        skip_nav_child = False
        while ancestor in by_index:
            if ancestor in nav_roots:
                skip_nav_child = True
                break
            ancestor = by_index[ancestor].get("parentIndex")
        if skip_nav_child:
            continue
        tag = str(node.get("tag", "")).lower()
        if tag in {"html", "body", "path", "circle", "rect", "line", "polyline"}:
            continue
        rect = node["rect"]
        if float(rect.get("width", 0)) < 2 or float(rect.get("height", 0)) < 2:
            continue
        # The browser frame/root is a coordinate surface, not a visual node.
        if (tag in {"html", "body"}
                or (abs(float(rect.get("width", 0)) - canvas_w) <= 2
                    and abs(float(rect.get("height", 0)) - canvas_h) <= 2)):
            continue
        if frame:
            # Only materialize pixels that can actually appear in the fixed
            # App Frame; below-frame Settings rows belong to a scroll state,
            # not to the captured screen and must not cover the TabBar.
            fx, fy = float(frame["rect"].get("x", 0)), float(frame["rect"].get("y", 0))
            fr, fb = fx + canvas_w, fy + canvas_h
            if not in_scroll_tree(node) and (float(rect.get("x", 0)) >= fr or float(rect.get("y", 0)) >= fb \
                    or float(rect.get("x", 0)) + float(rect.get("width", 0)) <= fx \
                    or float(rect.get("y", 0)) + float(rect.get("height", 0)) <= fy):
                continue
        text_value = str(node.get("text", "")).strip()
        classes = str(node.get("className", ""))
        if tag in {"image", "img"}:
            attrs = node.get("attrs") if isinstance(node.get("attrs"), dict) else {}
            href = str(node.get("href") or node.get("src") or attrs.get("href") or attrs.get("src") or "")
            if not href:
                match = re.search(r"(?:href|src)=[\"']([^\"']+)", str(node.get("outerHTML", "")))
                href = match.group(1) if match else ""
            source = _materialize_browser_image(model, href, asset_dir) if model else None
            if source:
                identity = (node.get("data-openhmi-id") or node.get("id") or
                            node.get("ariaLabel") or node.get("aria-label") or
                            attrs.get("data-openhmi-id") or attrs.get("id") or
                            attrs.get("aria-label") or "")
                identity_text = str(identity)
                identity_kind = ("status" if re.search(r"status|lamp|indicator", identity_text, re.I)
                                 else "icon" if re.search(r"icon|glyph", identity_text, re.I) else "image")
                controls.append({"kind": "image", "text": "", "src": source,
                                 "href": href, "identity": identity_text,
                                 "identity_kind": identity_kind,
                                 "dom_index": node.get("index"), "style": node.get("style", {})})
            continue
        if tag == "canvas":
            rect_box = tuple(int(round(float(rect.get(key, 0)))) for key in ("x", "y", "width", "height"))
            crop_name = f"uagent_fallback_state_{state_index + 1}_{node.get('index', 0)}"
            source = _write_screenshot_crop(screenshot, asset_dir, crop_name, rect_box)
            if source:
                controls.append({"kind": "image", "text": "", "src": source,
                                 "className": "", "dom_index": node.get("index"),
                                 "style": node.get("style", {})})
            continue
        if tag in {"button", "input", "select", "textarea"}:
            is_select = tag == "select" or "data-[slot=select-value]" in classes or ("justify-between" in classes and "border" in classes)
            input_type = str(node.get("type") or "").lower()
            is_switch = input_type in {"checkbox", "switch"} or node.get("role") == "switch" or "switch" in classes
            is_range = input_type == "range" or "range-slider" in classes
            kind = ("switch" if is_switch else "slider" if is_range else "input") if tag in {"input", "textarea"} else "select" if is_select else "button"
            control = {"kind": kind, "text": text_value, "value": node.get("value") or "",
                       "className": classes, "dom_index": node.get("index"),
                       "style": node.get("style", {})}
            # The fixed bottom tab bar is often made of icon-only buttons;
            # its visible DASH/SET text belongs to child spans. Identify it by
            # its measured position inside the discovered canvas frame.
            if (tag == "button" and frame and not in_scroll_tree(node)
                    and float(frame["rect"].get("y", 0)) + 185 <= float(rect.get("y", 0))
                    < float(frame["rect"].get("y", 0)) + canvas_h):
                rect_box = tuple(int(round(float(rect.get(key, 0)))) for key in ("x", "y", "width", "height"))
                crop_name = f"uagent_nav_state_{state_index + 1}_{node.get('index', 0)}"
                source = _write_screenshot_crop(screenshot, asset_dir, crop_name, rect_box)
                if source:
                    control["kind"] = "image"
                    control["src"] = source
                    nav_roots.add(int(node.get("index", -1)))
            if kind == "switch":
                track = str(node.get("style", {}).get("backgroundColor", ""))
                control["className"] += " bg-[#F59E0B]" if "245, 158, 11" in track else " bg-[#1E2530]"
            elif kind == "slider":
                control["className"] += " bg-[#1E2530]"
            if tag == "button" and not text_value and float(rect.get("x", 9999)) < 100:
                nav_count = sum(1 for c in controls if c.get("nav_icon"))
                if nav_count < len(nav_icon_names):
                    kind = "icon_tile"
                    control["kind"] = kind
                    control["icon"] = nav_icon_names[nav_count]
                    control["nav_icon"] = True
                    control["icon_tile_background"] = "#2F6B5A"
                    control["icon_color"] = "#FFFFFF"
            if kind == "select":
                control["trailing_icon"] = "ChevronDown"
                control["trailing_icon_color"] = "#717182"
                if select_options:
                    # Options are collected in DOM order when a combobox is
                    # opened; retain them on the corresponding select IR.
                    chunk = max(1, len(select_options) // 4)
                    control["items"] = select_options[select_cursor:select_cursor + chunk]
                    select_cursor += chunk
            controls.append(control)
        elif tag == "svg" and float(rect.get("width", 0)) >= 40 and float(rect.get("height", 0)) >= 40:
            svg = str(node.get("svg") or "")
            # A dashboard may draw several gauges inside one full-canvas SVG.
            # Split source-proven boxes when available; otherwise infer a
            # two-column gauge layout from multiple arc paths.
            layouts = list(gauge_layout or [])
            if not layouts and float(rect.get("width", 0)) > float(rect.get("height", 0)) * 1.3:
                path_count = len(re.findall(r"<path\b", svg))
                if path_count >= 5:
                    svg_w, svg_h = int(round(float(rect["width"]))), int(round(float(rect["height"])))
                    box_w = max(40, int(round(svg_h * 0.8)))
                    gap = max(0, svg_w - box_w * 2)
                    layouts = [(gap // 3, (svg_h - box_w) // 2, box_w, box_w),
                               (gap // 3 * 2 + box_w, (svg_h - box_w) // 2, box_w, box_w)]
            if layouts:
                for box in layouts:
                    controls.append({"kind": "gauge", "text": "", "className": classes,
                                     "dom_index": node.get("index"), "box": box,
                                     "svg": node.get("svg"), "style": node.get("style", {}),
                                     "attrs": node.get("attrs", {})})
            else:
                controls.append({"kind": "gauge", "text": "", "className": classes,
                                 "dom_index": node.get("index"), "svg": node.get("svg"),
                                 "style": node.get("style", {}), "attrs": node.get("attrs", {})})
        elif tag == "div" and ("toggle-track" in classes
                                or (30 <= float(rect.get("width", 0)) <= 38
                                    and 16 <= float(rect.get("height", 0)) <= 20
                                    and str(node.get("style", {}).get("backgroundColor", ""))
                                    not in {"", "transparent", "rgba(0, 0, 0, 0)"})):
            controls.append({"kind": "switch", "text": "", "className": classes,
                             "dom_index": node.get("index"), "style": node.get("style", {})})
        elif tag == "div" and ((50 <= float(rect.get("width", 0)) <= 110 and 30 <= float(rect.get("height", 0)) <= 70)
                               or (float(rect.get("width", 0)) >= 300 and float(rect.get("height", 0)) <= 25)):
            controls.append({"kind": "container", "text": text_value, "className": classes,
                             "dom_index": node.get("index"), "style": node.get("style", {})})
        elif tag == "div" and (6 <= float(rect.get("width", 0)) <= 14
                                and 6 <= float(rect.get("height", 0)) <= 14
                                and str(by_index.get(parent, {}).get("tag", "")).lower() == "button"
                                and painted_div(node)):
            # Custom toggles commonly keep the semantic button as the track
            # and draw the thumb with a nested, absolutely positioned div.
            # Materialize only a small, painted child of a button; this keeps
            # the rule generic without promoting arbitrary dashboard dots.
            parent_node = by_index.get(parent)
            background = str(node.get("style", {}).get("backgroundColor", ""))
            painted = background.casefold() not in {
                "", "transparent", "rgba(0, 0, 0, 0)", "rgba(0,0,0,0)",
            }
            if parent_node and str(parent_node.get("tag", "")).lower() == "button" and painted:
                controls.append({"kind": "container", "text": "", "className": classes,
                                 "dom_index": node.get("index"), "style": node.get("style", {})})
        elif tag == "div" and not text_value and painted_div(node):
            # CSS-only sections, segmented bars, and decorative rules are
            # visual DOM evidence even when they have no semantic widget tag.
            # Keep DOM order and avoid re-materializing a thumb/container.
            dom_index = node.get("index")
            if not any(c.get("dom_index") == dom_index for c in controls):
                controls.append({"kind": "container", "text": "", "className": classes,
                                 "dom_index": dom_index, "style": node.get("style", {})})
        elif text_value and tag in {"h1", "h2", "h3", "h4", "p", "span", "label", "text"}:
            style = dict(node.get("style", {}) or {})
            # SVG text paints through fill rather than CSS color.
            if tag == "text" and style.get("fill") not in {None, "transparent", "rgba(0, 0, 0, 0)"}:
                style["color"] = style.get("fill")
            controls.append({"kind": "label", "text": text_value, "className": classes,
                             "dom_index": node.get("index"), "style": style})
        elif text_value and ("bg-" in classes or "border" in classes) and len(text_value) < 120:
            controls.append({"kind": "label", "text": text_value, "className": classes,
                             "dom_index": node.get("index"), "style": node.get("style", {})})
        elif text_value and len(text_value) < 120 and float(rect.get("height", 0)) <= 32:
            # Direct-text leaves are semantic labels even when a short heading
            # spans a wider row. DOM identity prevents duplicate materializing
            # when the same node was already matched from static extraction.
            controls.append({"kind": "label", "text": text_value, "className": classes,
                             "dom_index": node.get("index"), "style": node.get("style", {})})
        elif tag == "div" and "rounded" in classes and "bg-" in classes:
            # Preserve dashboard card surfaces even when their children are
            # rendered by Recharts/compound React components.
            w, h = float(rect.get("width", 0)), float(rect.get("height", 0))
            if w >= 140 and h >= 60:
                controls.append({"kind": "button", "text": "", "className": classes,
                                 "dom_index": node.get("index")})
    return controls


def _snapshot_screens(model: ReactProjectModel, width: int, height: int,
                      browser_layout: dict[str, Any] | None = None,
                      asset_dir: Path | None = None) -> str:
    """Serialize the same shared ScreenIR consumed by compiler.py."""
    ids = _IdAllocator()
    screens_ir = build_screen_ir(model)
    screens: list[str] = []
    captured_all = list(browser_layout.get("screens", [])) if browser_layout else []
    synthetic_captures = len(screens_ir) == 1 and len(captured_all) > 1
    if synthetic_captures:
        # A monolithic Dashboard has no route switch for ScreenIR to resolve.
        # Promote each browser-captured state to a real LVGL Screen while
        # retaining the same React root component and shared resources.
        base = screens_ir[0]
        screens_ir = [replace(base,
                              id=f"screen_{index + 1:02d}_{re.sub(r'[^A-Za-z0-9_-]+', '_', str(item.get('path', ['main'])[-1] if item.get('path') else 'main')).strip('_').lower() or 'main'}",
                              name=str(item.get("path", ["main"])[-1] if item.get("path") else "main"),
                              route=str(index))
                      for index, item in enumerate(captured_all)]
    page_keywords = {
        "HomePage": (), "PaperStatus": ("\u7eb8\u5f20",), "InkStatus": ("\u58a8\u6c34",),
        "SettingsPage": ("\u8bbe\u7f6e",), "BasicSettings": ("\u8bbe\u7f6e", "\u57fa\u672c\u8bbe\u7f6e"),
        "LanguageSettings": ("\u8bbe\u7f6e", "\u57fa\u672c\u8bbe\u7f6e", "\u8bed\u8a00"),
        "TimeSettings": ("\u8bbe\u7f6e", "\u57fa\u672c\u8bbe\u7f6e", "\u65f6\u95f4"),
        "NetworkSettings": ("\u8bbe\u7f6e", "\u57fa\u672c\u8bbe\u7f6e", "\u7f51\u7edc"),
        "MaintenancePage": ("\u8bbe\u7f6e", "\u7ef4\u62a4"), "CutterSetting": ("\u8bbe\u7f6e", "\u5207\u5200"),
    }
    for index, screen in enumerate(screens_ir):
        component_name = str(model.components[screen.root_component].props.get("name", ""))
        captured_screens = captured_all
        keywords = page_keywords.get(component_name, ())
        matched_capture = None
        if captured_screens:
            if synthetic_captures:
                matched_capture = captured_screens[index]
            # State-derived ScreenIR routes map directly to the interaction
            # capture path (case-insensitive). Do this before generic keyword
            # matching so the default capture can never leak into another tab.
            route_aliases = {str(screen.route).casefold()}
            route_aliases.update(
                edge.trigger.casefold() for edge in model.navigation
                if edge.target_route.casefold() == str(screen.route).casefold()
            )
            if matched_capture is None and screen.route:
                matched_capture = next((item for item in captured_screens
                                        if isinstance(item, dict)
                                        and item.get("path")
                                        and str(item["path"][-1]).casefold() in route_aliases), None)
            candidates = []
            for item in captured_screens:
                if matched_capture is not None:
                    break
                path = tuple(str(part) for part in item.get("path", [])) if isinstance(item, dict) else ()
                route_match = bool(path) and path[-1].casefold() in route_aliases
                if route_match:
                    candidates.append(item)
                elif not keywords and not path:
                    candidates.append(item)
                elif keywords and len(path) >= len(keywords) and all(key in path[pos] for pos, key in enumerate(keywords)):
                    candidates.append(item)
            if candidates:
                # For language/setting sub-pages, prefer the shortest path so
                # a selected option does not replace the page's canonical layout.
                matched_capture = min(candidates, key=lambda item: len(item.get("path", [])))
        screen_layout = ({"data": matched_capture["data"],
                          "status": matched_capture.get("status", browser_layout.get("status", "") if isinstance(browser_layout, dict) else ""),
                          "screenshot_png_base64": matched_capture.get("screenshot_png_base64"),
                          "visual_effects": matched_capture.get("visual_effects", [])}
                         if isinstance(matched_capture, dict) else browser_layout)
        root_component = model.components[screen.root_component]
        scene_mode = isinstance(root_component.props.get("gauge_scene"), dict)
        component_controls = [] if scene_mode else _expanded_controls(root_component)
        effect_fallbacks: list[dict[str, Any]] = []
        if isinstance(screen_layout, dict) and isinstance(screen_layout.get("data"), dict):
            browser_controls = [] if scene_mode else _dom_fallback_controls(
                screen_layout["data"], screen_layout.get("screenshot_png_base64"),
                asset_dir, index, list(model.components[screen.root_component].props.get("gauge_layout", [])), model)
            # Source-backed gauge adapters paint the SVG instrument (including
            # its layers and readout). Do not materialize a second native arc
            # for the same browser SVG node; its measured geometry remains in
            # browser evidence for the custom unit.
            reachable = {screen.root_component}
            pending = [screen.root_component]
            while pending:
                current = pending.pop()
                for child in model.component_graph.get(current, []):
                    if child not in reachable:
                        reachable.add(child)
                        pending.append(child)
            has_source_gauges = any(model.components.get(component_id)
                                    and model.components[component_id].props.get("gauges")
                                    for component_id in reachable)
            if has_source_gauges:
                def is_source_gauge_readout(control: dict[str, Any]) -> bool:
                    classes = str(control.get("className") or "")
                    identity = str(control.get("identity") or "")
                    marker = f"{classes} {identity}"
                    return (str(control.get("kind", "")).casefold() == "label"
                            and bool(re.search(r"(?:gauge|meter)[-_ ]?(?:value|readout)|(?:^|[-_ ])readout(?:$|[-_ ])",
                                               marker, re.I)))
                browser_controls = [control for control in browser_controls
                                    if str(control.get("kind", "")).lower() != "gauge"
                                    and not is_source_gauge_readout(control)]
                component_controls = [control for control in component_controls
                                      if not is_source_gauge_readout(control)]
            # Static extraction can return a non-empty but skeletal list for
            # monolithic Make apps. Merge browser evidence whenever it adds
            # DOM identities or substantially more visible text; this keeps
            # dynamic cards/sidebar controls instead of silently discarding
            # them just because one placeholder component was found.
            static_ids = {c.get("dom_index") for c in component_controls if c.get("dom_index") is not None}
            static_text = sum(1 for c in component_controls if str(c.get("text", "")).strip())
            browser_text = sum(1 for c in browser_controls if str(c.get("text", "")).strip())
            browser_authoritative = (str(screen_layout.get("status", "")).casefold() == "browser-interaction"
                                     and bool(browser_controls))
            if browser_authoritative:
                # Browser DOM is the sole visual list. Static scanner records
                # may enrich matching dom_index items, but never add a second
                # geometry slot by source order.
                static_by_dom = {c.get("dom_index"): c for c in component_controls
                                 if c.get("dom_index") is not None}
                component_controls = [{**item, **{k: v for k, v in static_by_dom.get(item.get("dom_index"), {}).items()
                                                  if k not in {"box", "dom_index"}}}
                                     for item in browser_controls]
            elif (component_name in {"Dashboard", "Settings"} or synthetic_captures) and browser_controls:
                # For monolithic Make apps, the browser DOM is the authoritative
                # visual tree. Keep static gauge metadata (range/colors/binding)
                # but do not append a second flat copy of the same labels.
                static_gauges = [c for c in component_controls if c.get("kind") == "gauge"]
                if static_gauges:
                    gauge_index = 0
                    merged_browser = []
                    active_color = None
                    for browser_control in browser_controls:
                        if browser_control.get("kind") == "gauge" and gauge_index < len(static_gauges):
                            active_color = str((static_gauges[gauge_index].get("colors") or {}).get("normal", "#22C55E"))
                            merged_browser.append({**browser_control, **static_gauges[gauge_index] ,
                                                   "dom_index": browser_control.get("dom_index"),
                                                   "style": browser_control.get("style", {})})
                            gauge_index += 1
                        else:
                            item = dict(browser_control)
                            # Numeric SVG/text leaves are separate DOM nodes,
                            # so Arc's own text-color cannot style them.
                            if active_color and item.get("kind") == "label":
                                value = str(item.get("text", "")).strip()
                                if re.fullmatch(r"[-+]?\d+(?:\.\d+)?", value):
                                    item["gauge_color"] = active_color
                            merged_browser.append(item)
                    component_controls = merged_browser
                else:
                    component_controls = browser_controls
            elif (not component_controls or browser_text > static_text * 1.5
                    or len(browser_controls) > len(component_controls) * 1.5):
                merged = list(component_controls)
                static_kinds = {str(c.get("kind", "")) for c in component_controls}
                for control in browser_controls:
                    # SVG fallback controls supplement static gauges only when
                    # no semantic gauge was recovered; never duplicate them.
                    if control.get("kind") == "gauge" and "gauge" in static_kinds:
                        continue
                    if control.get("dom_index") not in static_ids:
                        merged.append(control)
                component_controls = merged
            effect_fallbacks = _effect_fallback_controls(
                model, screen_layout, asset_dir, index, (width, height)
            )
            if effect_fallbacks and component_name != "HomePage":
                component_controls.extend(effect_fallbacks)
        geometry = _browser_geometry(screen_layout, width, height, component_name,
                                     component_controls, asset_dir.parent / "font")
        if isinstance(geometry.get(component_name), dict):
            _size_image_controls(geometry[component_name], asset_dir)
        # The measured frame is the only safe owner for a full-canvas paint;
        # never infer a fixed viewport size from a source class or screenshot.
        if isinstance(screen_layout, dict) and isinstance(screen_layout.get("data"), dict):
            paint = browser_background_paint(screen_layout["data"], width=width, height=height)
            if paint:
                page_geo = geometry.setdefault(component_name, {})
                page_geo["background_paint"] = paint
                page_geo["background_effect_receipt"] = {
                    "support": "native" if paint.get("gradient_type") == "linear" else "partial",
                    "strategy": "lvgl-bg-grad" if paint.get("gradient_type") == "linear" else "bounded-radial-layers",
                    "bounds": paint.get("bounds"), "stops": paint.get("stops", []),
                }
        # HomePage has a specialized status/card geometry path. Add effect
        # crops through a separate semantic overlay so that path does not
        # discard the existing dashboard controls.
        if effect_fallbacks and component_name == "HomePage":
            overlay = _browser_geometry(screen_layout, width, height,
                                        "__uagent_effects__", effect_fallbacks)
            overlay_geo = overlay.get("__uagent_effects__", {})
            target_geo = geometry.setdefault(component_name, {})
            target_geo.setdefault("controls", [])
            target_geo.setdefault("materialized_controls", [])
            target_geo["controls"].extend(overlay_geo.get("controls", []))
            target_geo["materialized_controls"].extend(overlay_geo.get("materialized_controls", []))
        # The measured App Frame is the authoritative canvas background.
        # Propagate it to the page container instead of using a white default.
        if isinstance(screen_layout, dict) and isinstance(screen_layout.get("data"), dict):
            frame = next((n for n in screen_layout["data"].get("nodes", [])
                          if isinstance(n, dict) and isinstance(n.get("rect"), dict)
                          and abs(float(n["rect"].get("width", 0)) - width) <= 2
                          and abs(float(n["rect"].get("height", 0)) - height) <= 2), None)
            if frame:
                bg = str(frame.get("style", {}).get("backgroundColor", ""))
                match = re.fullmatch(r"rgb\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)", bg)
                if match:
                    bg = "#%02X%02X%02X" % tuple(int(match.group(i)) for i in (1, 2, 3))
                if bg.startswith("#"):
                    geometry.setdefault(component_name, {})["background"] = bg
        shared = list(screen.shared_components)
        navigation = [component_id for component_id in shared if component_id.rsplit(":", 1)[-1] == "Navigation"]
        other_shared = [component_id for component_id in shared if component_id not in navigation]
        widgets: list[str] = []
        left = 44 if navigation else 0
        for nav_index, component_id in enumerate(navigation):
            geo = geometry.get("Navigation") if nav_index == 0 else None
            bx = geo.get("box") if geo else None
            widgets.append(_container_widget(model, component_id, "navigation",
                                             *(bx if bx else (nav_index * 44, 0, 44, height)),
                                             len(widgets), ids, screen.id, geo))
        for component_id in other_shared:
            widgets.append(_container_widget(model, component_id, "shared", left, 0, width - left, height,
                                             len(widgets), ids, screen.id))
        # Energy embeds Sidebar inside Layout instead of exposing a shared
        # Navigation component. Materialize its measured browser box as a
        # parent-level LVGL surface so inferred nav buttons are not floating
        # on the page background.
        if not navigation and geometry.get("Navigation"):
            nb = geometry["Navigation"].get("box", (0, 0, 74, height))
            nx, ny, nw, nh = [int(v) for v in nb]
            sid = ids.take()
            widgets.insert(0, f'<Widget name="{escape(screen.id)}_Sidebar" id="{sid}" is-hidden="0" type="28" index="0"><Normal><name>{escape(screen.id)}_Sidebar</name><postion>{nx},{ny}</postion><size>{nw},{nh}</size><scrollbar-mode>0</scrollbar-mode><flag /></Normal><Animate><use-anim>0</use-anim></Animate><ImgStorage><use-img-storage>0</use-img-storage></ImgStorage><FontStorage><use-font-storage>0</use-font-storage></FontStorage><Attribute /><Style><Part name="Main" value="0"><State name="Default" value="0"><bg-color>#2C5F4E</bg-color><bg-opa>255</bg-opa><radius>18</radius></State></Part></Style><Event /></Widget>')
        geo = geometry.get(component_name)
        bx = geo.get("box") if geo else None
        widgets.append(_container_widget(model, screen.root_component, "page",
                                         *(bx if bx else (left, 0, max(1, width - left), height)),
                                         len(widgets), ids, screen.id, geo))
        screen_id = ids.take()
        screens.append(
            f'  <Screen name="{escape(screen.id)}" id="{screen_id}" is-default="{"1" if index == 0 else "0"}" index="{index}">'
            f'<Normal><name>{escape(screen.id)}</name><scrollbar-mode>0</scrollbar-mode><flag /></Normal>'
            '<Animate><use-anim>0</use-anim></Animate><ImgStorage><use-img-storage>0</use-img-storage></ImgStorage><FontStorage><use-font-storage>0</use-font-storage></FontStorage><Attribute />'
            f'<Style><Part name="Main" value="0"><State name="Default" value="0"><bg-color>{escape(str(geometry.get(component_name, {}).get("background", "#101218")))}</bg-color><bg-opa>255</bg-opa></State></Part></Style><Event />'
            f'<Widgets>{"".join(widgets)}</Widgets></Screen>\n'
        )
    if not screens:
        screens.append(
            '  <Screen name="screen_01_main" id="1000000000001" is-default="1" index="0">'
            '<Normal><name>screen_01_main</name><scrollbar-mode>0</scrollbar-mode><flag /></Normal><Animate><use-anim>0</use-anim></Animate>'
            '<ImgStorage><use-img-storage>0</use-img-storage></ImgStorage><FontStorage><use-font-storage>0</use-font-storage></FontStorage><Attribute /><Style /><Event /><Widgets /></Screen>\n'
        )
    return ''.join(screens)


def write_project_scaffold(project_root: Path, model: ReactProjectModel, include_runtime: bool = False) -> dict[str, Path]:
    """Write an openable UIBuilder project, not merely loose custom C files."""
    parser_profile = detect_parser(model.source_dir)
    name = _project_name(project_root, model)
    width, height = detect_canvas(model)
    # Explicit target override is useful for fixed embedded displays when the
    # web source is responsive and has no pixel canvas declaration.
    try:
        override_w = int(os.environ.get("UAGENT_CANVAS_WIDTH", "0"))
        override_h = int(os.environ.get("UAGENT_CANVAS_HEIGHT", "0"))
        if override_w > 0 and override_h > 0:
            width, height = override_w, override_h
    except ValueError:
        pass
    # Apply explicit target dimensions last so they take precedence over a
    # stale output project configuration.
    try:
        override_w = int(os.environ.get("UAGENT_CANVAS_WIDTH", "0"))
        override_h = int(os.environ.get("UAGENT_CANVAS_HEIGHT", "0"))
        if override_w > 0 and override_h > 0:
            width, height = override_w, override_h
    except ValueError:
        pass
    for relative in (
        "resources/font", "resources/image", "resources/snapshot", "resources/video",
        "ui_builder/assets", "ui_builder/custom/assets", "openhmi", "data", "simulator",
    ):
        (project_root / relative).mkdir(parents=True, exist_ok=True)
    font_resources = _copy_project_fonts(project_root)
    icon_resources = _write_icon_resources(project_root, model)
    # Browser evidence is best-effort: source-only fixtures still generate a
    # valid project, but are explicitly marked as static so coordinates are
    # never mistaken for measured browser geometry.
    browser_layout = None
    try:
        if os.environ.get("UAGENT_SKIP_BROWSER_CAPTURE") == "1":
            raise RuntimeError("browser capture skipped by regression mode")
        # Capture at a desktop viewport so the centered Figma Make frame and
        # its absolute origin are preserved; _browser_geometry normalizes it
        # back to the LVGL canvas afterwards.
        browser_layout = capture_layout(model.source_dir, viewport=(1024, 600))
    except Exception as exc:
        model.add_warning(f"Browser layout capture failed; using static fallback: {exc}")
    if browser_layout is None:
        # A previously verified capture is preferable to silently regenerating
        # from static JSX when Chrome/Vite cannot be relaunched.
        for evidence_file in (model.source_dir / "browser-layout-v2.json",
                              model.source_dir / "browser-layout.json"):
            if evidence_file.is_file():
                try:
                    cached = json.loads(evidence_file.read_text(encoding="utf-8"))
                    if isinstance(cached, dict) and isinstance(cached.get("screens"), list):
                        browser_layout = cached
                        break
                except (OSError, ValueError, TypeError):
                    continue
    model.browser_evidence = browser_layout or {}
    plan_capabilities(model, browser_layout)
    layout_file = project_root / "openhmi" / "browser-layout.json"
    layout_file.write_text(json.dumps(browser_layout or {
        "schema": "uagent.browser-layout/v1", "source": str(model.source_dir),
        "status": "static-fallback", "reason": "No runnable node_modules/Vite page was found",
    }, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")

    project_file = project_root / f"{name}.aicpro"
    project_file.write_text(
        "<?xml version='1.0' encoding='utf-8'?>\n"
        "<ai_lvgl>\n"
        f"  <project_name>{escape(name)}</project_name>\n"
        "  <ver_major>2</ver_major><ver_minor>2</ver_minor><ver_release>0</ver_release>\n"
        "  <lvgl_version>2</lvgl_version><board_id>1</board_id><template_id>0</template_id>\n"
        "  <use_multi_language>0</use_multi_language><color_depth>32</color_depth>\n"
        f"  <resolution><width>{width}</width><height>{height}</height></resolution>\n"
        "</ai_lvgl>\n",
        encoding="utf-8", newline="\n",
    )
    snapshot = project_root / f"{name}.snapshot"
    snapshot.write_text(
        "<?xml version='1.0' encoding='utf-8'?>\n"
        "<ailv-app>\n"
        + _snapshot_screens(model, width, height, browser_layout,
                            project_root / "resources" / "image") + "</ailv-app>\n",
        encoding="utf-8", newline="\n",
    )
    _sync_existing_uibuilder_geometry(project_root, snapshot, model)
    _ensure_simulator_frame_capture(project_root, model)
    # Screenshot fallback crops are discovered after the initial icon-resource
    # pass; mirror them as well so Image Widgets resolve in both AiBuilder
    # resource layouts.
    for mirror in (project_root / "ui_builder" / "assets" / "image",
                   project_root / "ui_builder" / "custom" / "assets" / "image"):
        mirror.mkdir(parents=True, exist_ok=True)
        for asset in (project_root / "resources" / "image").iterdir():
            if asset.is_file():
                destination = mirror / asset.name
                copy2(asset, destination)
                _normalize_simulator_png(destination)
    (project_root / "CMakeLists.txt").write_text(
        f"cmake_minimum_required(VERSION 3.16)\nproject({name} C)\n", encoding="utf-8", newline="\n"
    )
    (project_root / "config.ini").write_text("[project]\nbackend=uibuilder\n", encoding="utf-8", newline="\n")
    openhmi = project_root / "openhmi" / "model.json"
    openhmi.write_text(json.dumps({
        "schema_version": "1.0", "project": name, "backend": "UIBuilder",
        "canvas": {"width": width, "height": height},
        "generation_mode": "react-figma-make-semantic",
        "source": str(model.source_dir),
        "components": len(model.components), "events": len(model.events),
        "instances": [asdict(item) for item in model.instances.values()],
        "binding_summary": dict(model.binding_summary),
    }, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    (project_root / "openhmi" / "README.md").write_text(
        "# OpenHMI configuration\n\nUAgent source provenance and semantic generation metadata.\n",
        encoding="utf-8", newline="\n",
    )
    runtime_ready = (_copy_uibuilder_runtime(project_root) if include_runtime else
                     (project_root / "simulator" / "lvgl").is_dir()
                     and (project_root / "simulator" / "lib").is_dir())
    from core.validation import validate_project
    validation = validate_project(project_root, layout_file, snapshot, width, height,
                                  run_simulator=True, model=model)
    status = project_root / "uagent-project.json"
    status.write_text(json.dumps({
        "schema": "uagent.aibuilder-project/v1", "project": name,
        "project_file": project_file.name, "snapshot": snapshot.name,
        "canvas": {"width": width, "height": height},
        "simulator_runtime_ready": runtime_ready,
        "layout_source": "browser" if browser_layout else "static-fallback",
        "parser": {"id": parser_profile.parser_id, "confidence": parser_profile.confidence,
                    "evidence": list(parser_profile.evidence)},
        "browser_layout": str(layout_file.relative_to(project_root)),
        "font_resources": [str(path.relative_to(project_root)) for path in font_resources],
        "icon_resources": [str(path.relative_to(project_root)) for path in icon_resources],
        "capability_plan": {
            "decisions": len(model.capability_plan),
            "render_items": len(model.render_plan.items),
            "blockers": list(model.render_plan.blockers),
        },
        "validation": {"status": validation["status"], "completed": validation["completed"],
                       "report": str(Path(validation["report_file"]).relative_to(project_root))},
        "next_step": ("Generation accepted." if validation["completed"] else
                      "Validation failed or simulator evidence is missing; do not treat this project as complete."),
    }, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    return {"project": project_file, "snapshot": snapshot, "openhmi": openhmi,
            "browser_layout": layout_file, "project_status": status,
            "validation_report": Path(validation["report_file"])}
