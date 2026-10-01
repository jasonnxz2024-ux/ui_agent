"""Semantic adapters for native LVGL constructs.

They only classify component evidence and normalise JSON-like props.  C code
is emitted by :mod:`generator.lvgl`, so these adapters remain reusable by a
future AiBuilder XML/project backend.
"""
from __future__ import annotations

from dataclasses import asdict, replace
from typing import Any

from core.model import Component, ReactProjectModel

from .contracts import AdaptedWidget
from .registry import AdapterResult, register


def _props(component: Component) -> dict[str, Any]:
    """Copy props and accept common React spelling without mutating the IR."""
    props = dict(component.props or {})
    if "chartType" in props and "chart_type" not in props:
        props["chart_type"] = props["chartType"]
    if "slideCount" in props and "slide_count" not in props:
        props["slide_count"] = props["slideCount"]
    return props


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _status_item_decision(item: dict[str, Any]) -> tuple[str, str | None]:
    """Classify one status primitive without losing unsupported evidence."""
    kind = str(item.get("type", "")).casefold()
    if kind in {"value_with_unit", "clock", "signal_bars"}:
        if kind == "value_with_unit" and not item.get("unit"):
            return "fallback", "Status value has no unit evidence; keep it as a review item."
        if kind == "clock" and not item.get("format"):
            return "fallback", "Status clock has no source format evidence; keep it as a review item."
        if kind == "signal_bars" and _int(item.get("bar_count"), 0) <= 0:
            return "fallback", "Signal bars have no repeat count evidence; keep them as a review item."
        return "custom", None
    if kind in {"vector_icon", "connectivity_indicator"}:
        primitives = item.get("primitives") if isinstance(item.get("primitives"), list) else []
        unknown_paths = [primitive for primitive in primitives
                         if isinstance(primitive, dict) and str(primitive.get("type", "")).casefold() == "path"
                         and not primitive.get("d")]
        name = str(item.get("name", "")).casefold()
        # The renderer has exact geometric recipes for these status icons. An
        # arbitrary path must remain visible in the audit instead of vanishing.
        if unknown_paths or (any(str(p.get("type", "")).casefold() == "path" for p in primitives)
                             and name not in {"user", "lock", "wifi", "wlan"}):
            return "fallback", "Status SVG contains a path outside the supported icon recipes."
        if not primitives:
            return "fallback", "Status SVG has no drawable primitive evidence."
        return "custom", None
    return "fallback", f"Unsupported status item type: {item.get('type', 'unknown')}."


def _normalise_status_bars(scene: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """Copy status-bar evidence and attach a per-item target support level."""
    bars: list[dict[str, Any]] = []
    warnings: list[str] = []
    raw_bars = scene.get("status_bars") if isinstance(scene.get("status_bars"), list) else []
    for bar_index, raw_bar in enumerate(raw_bars):
        if not isinstance(raw_bar, dict):
            warnings.append(f"Status bar {bar_index + 1} is not an object and was kept in audit only.")
            continue
        bar = dict(raw_bar)
        slots: dict[str, list[dict[str, Any]]] = {}
        raw_slots = raw_bar.get("slots") if isinstance(raw_bar.get("slots"), dict) else {}
        for slot_name in ("start", "center", "end"):
            items: list[dict[str, Any]] = []
            raw_items = raw_slots.get(slot_name) if isinstance(raw_slots.get(slot_name), list) else []
            for item_index, raw_item in enumerate(raw_items):
                if not isinstance(raw_item, dict):
                    warnings.append(f"Status {slot_name}[{item_index}] is not an object and was kept in audit only.")
                    continue
                item = dict(raw_item)
                support, warning = _status_item_decision(item)
                item["support"] = support
                item["slot"] = slot_name
                item["order"] = item_index
                if warning:
                    warning = f"Status {slot_name}[{item_index}]: {warning}"
                    item["warning"] = warning
                    warnings.append(warning)
                items.append(item)
            slots[slot_name] = items
        bar["slots"] = slots
        bar["support"] = "custom" if all(item.get("support") != "fallback" for items in slots.values() for item in items) else "fallback"
        bars.append(bar)
    return bars, warnings


class _LVGLAdapter:
    name: str
    component_kind: str
    widget_type: str

    def matches(self, component: Component) -> bool:
        return component.kind.casefold() == self.component_kind

    def convert(self, component: Component, model: ReactProjectModel) -> AdapterResult:
        adapted = self.adapt(component, model)
        return AdapterResult(adapted.support, adapted.widget_type, adapted.warnings)

    def adapt(self, component: Component, model: ReactProjectModel) -> AdaptedWidget:
        raise NotImplementedError


@register
class GaugeSceneLVGLAdapter(_LVGLAdapter):
    """One custom-draw host for a coordinated fixed-canvas gauge scene."""

    name = "GaugeSceneAdapter"
    component_kind = "composite_gauge_scene"
    widget_type = "lv_obj_custom"

    def __init__(self) -> None:
        pass

    def matches(self, component: Component) -> bool:
        return component.kind.casefold() == self.component_kind or isinstance((component.props or {}).get("gauge_scene"), dict)

    def adapt(self, component: Component, model: ReactProjectModel) -> AdaptedWidget:
        props = _props(component)
        raw_scene = props.get("gauge_scene") if isinstance(props.get("gauge_scene"), dict) else {}
        scene = dict(raw_scene)
        status_bars, status_warnings = _normalise_status_bars(scene)
        scene["status_bars"] = status_bars
        props["scene"] = scene
        props["status_bars"] = status_bars
        props["arc_count"] = len(scene.get("gauges", [])) if isinstance(scene.get("gauges"), list) else 0
        warnings = ["Composite SVG scene is rendered by one transparent LVGL draw host; no generic Arc widgets are materialized."]
        warnings.extend(status_warnings)
        return AdaptedWidget(component.id, self.name, self.widget_type, "compound", props,
                             tuple(component.resource_ids), tuple(component.event_ids), tuple(warnings))


@register
class GaugeLVGLAdapter(_LVGLAdapter):
    """Inline SVG/circle or Gauge JSX -> native LVGL arc intent.

    The scanner stores one JSON gauge record per visual meter.  Keeping the
    records in properties makes this adapter reusable for any Figma Make app
    that exposes label/unit/min/max/value semantics, without project names.
    """

    name = "GaugeAdapter"
    component_kind = "gauge"
    widget_type = "lv_arc"

    def __init__(self) -> None:
        pass

    def matches(self, component: Component) -> bool:
        if (component.props or {}).get("_instantiated_as_template"):
            return False
        return (component.kind.casefold() == self.component_kind or bool((component.props or {}).get("gauges"))) and not isinstance((component.props or {}).get("gauge_scene"), dict)

    def adapt(self, component: Component, model: ReactProjectModel) -> AdaptedWidget:
        props = _props(component)
        gauges = props.get("gauges") if isinstance(props.get("gauges"), list) else []
        if not gauges:
            gauges = [{"label": component.id.rsplit(":", 1)[-1], "unit": "", "min": "0", "max": "100", "value": "0"}]
        props["gauges"] = gauges
        props["arc_count"] = 1
        gauge = gauges[0] if isinstance(gauges[0], dict) else {}
        gauge_id = str(props.get("gauge_id") or gauge.get("gauge_id") or "gauge_1")
        props["gauge_id"] = gauge_id
        if gauge.get("geometry"):
            props["arc_geometry"] = gauge["geometry"]
        elif gauge.get("arc"):
            props["arc_geometry"] = gauge["arc"]
        warnings = []
        if any(isinstance(g, dict) and isinstance(g.get("value"), str) and not str(g.get("value", "")).replace(".", "", 1).isdigit() for g in gauges):
            warnings.append("Gauge 数值来自 React 状态表达式；已保留表达式，并生成可关闭的通用演示模拟")
        props["simulation"] = {
            "enabled": True,
            "mode": "ping-pong",
            "period_ms": 40,
            "steps_per_sweep": 120,
            "compact_mode": "stepped",
            "compact_period_ms": 280,
            "compact_steps_per_sweep": 8,
        }
        source_paints = [item for item in props.get("arc_paints", [])
                         if isinstance(item, dict) and str(item.get("gauge_id") or item.get("gauge")) == gauge_id]
        if not source_paints and isinstance(gauge.get("arc_paints"), list):
            source_paints = [item for item in gauge["arc_paints"] if isinstance(item, dict)]
        group = next((item.properties for item in model.render_plan.items
                      if item.operation == "arc-glow-layers"
                      and str(item.properties.get("gauge_id")) == gauge_id
                      and (item.source_id == component.id or item.source_id.startswith(component.id + "#"))), None)
        if group is None:
            component_decision = model.capability_plan.get(component.id)
            candidates = component_decision.properties.get("gauge_groups", []) if component_decision else []
            group = next((item for item in candidates
                          if isinstance(item, dict) and str(item.get("gauge_id")) == gauge_id), None)
        if isinstance(group, dict):
            props["gauge_group"] = group
            if group.get("geometry"):
                props["arc_geometry"] = group["geometry"]
            layers = group.get("layers", []) if isinstance(group.get("layers"), list) else []
        else:
            layers = source_paints
        foreground = next((item for item in reversed(source_paints)
                           if item.get("role") == "foreground" and item.get("stroke_color")), None)
        colors = dict(gauge.get("colors") if isinstance(gauge.get("colors"), dict) else {})
        if foreground and foreground.get("stroke_color"):
            colors["normal"] = foreground["stroke_color"]
        props["colors"] = colors
        props["gauges"] = [{**gauge, "gauge_id": gauge_id, "colors": colors}]
        glow_layers = []
        for layer in layers:
            if not isinstance(layer, dict) or str(layer.get("role")) not in {"glow-wide", "glow-soft"}:
                continue
            opacity = layer.get("opacity", 1)
            try:
                opacity = round(float(opacity) * 100) if float(opacity) <= 1 else round(float(opacity))
            except (TypeError, ValueError):
                opacity = 100
            glow_layers.append({
                "role": layer.get("role"), "filter_id": layer.get("filter_id"),
                "blur": layer.get("blur"), "width": layer.get("stroke_width", 6),
                "opacity": opacity, "color": layer.get("stroke_color") or colors.get("normal"),
                "stroke": layer.get("stroke"), "stroke_width": layer.get("stroke_width"),
                "geometry": layer.get("geometry") or layer.get("source_geometry"),
                "start": layer.get("start"), "end": layer.get("end"),
                "value_binding": layer.get("value_binding"),
                "layer_order": layer.get("layer_order", len(glow_layers)),
                "stroke_binding": layer.get("stroke_binding"),
                "stroke_branches": layer.get("stroke_branches", []),
            })
        glow_layers.sort(key=lambda item: int(item.get("layer_order", 0)))
        if glow_layers:
            props["glow_layers"] = glow_layers
            warnings.append("Source glow is represented by synchronized, gauge-scoped layered arcs; foreground value remains native")
        else:
            props.pop("glow_layers", None)
        linked_filters = {str(item.get("filter_id")) for item in source_paints if item.get("filter_id")}
        glow_effects = [effect for effect in model.visual_effects.values()
                        if effect.filter_id in linked_filters
                        and str(effect.filter_id or "").casefold() != "text-glow"
                        and (effect.target_component == component.id or effect.source_file == component.source_file)]
        props["effect_ids"] = [effect.id for effect in glow_effects]
        support = "partial" if gauge.get("instance") else ("compound" if glow_layers or props.get("arc_geometry") else "native")
        return AdaptedWidget(component.id, self.name, self.widget_type, support, props,
                             tuple(component.resource_ids), tuple(component.event_ids), tuple(warnings))

    def adapt_many(self, component: Component, model: ReactProjectModel) -> list[AdaptedWidget]:
        gauges = (component.props or {}).get("gauges")
        if not isinstance(gauges, list) or len(gauges) <= 1:
            return [self.adapt(component, model)]
        result = []
        for index, gauge in enumerate(gauges, 1):
            gauge_id = str(gauge.get("gauge_id") or f"gauge_{index}") if isinstance(gauge, dict) else f"gauge_{index}"
            clone = Component(f"{component.id}#gauge_{index}", "gauge", component.source_file,
                              {**component.props, "gauges": [gauge], "gauge_id": gauge_id}, component.children,
                              component.resource_ids, component.event_ids)
            result.append(self.adapt(clone, model))
        return result


@register
class CarouselLVGLAdapter(_LVGLAdapter):
    """React drag/Embla/Framer carousel -> scroll-snapped LVGL object row."""

    name = "CarouselAdapter"
    component_kind = "carousel"
    widget_type = "lv_obj_scroll_snap"

    def __init__(self) -> None:
        # ``register`` accepts instances for historical compatibility.
        pass

    def adapt(self, component: Component, model: ReactProjectModel) -> AdaptedWidget:
        props = _props(component)
        slides = props.get("slides")
        # Current scanner records a literal React ``items`` array under data.
        # Preserve those dictionaries verbatim in the manifest and use their
        # cardinality for the native LVGL object row.
        if not isinstance(slides, list) and isinstance(props.get("data"), dict):
            candidate = props["data"].get("items")
            if isinstance(candidate, list):
                slides = candidate
                props["slides"] = candidate
        if not isinstance(slides, list):
            slides = []
        resource_ids = tuple(component.resource_ids)
        count = len(slides) or int(props.get("slide_count", 0) or len(resource_ids) or 1)
        props["slide_count"] = max(1, count)
        props["orientation"] = str(props.get("orientation", "horizontal")).lower()
        props["snap"] = str(props.get("snap", "center")).lower()
        warnings: list[str] = []
        if not slides:
            warnings.append("未解析到轮播页内容；已生成可编辑的空卡片和资源占位符")
        if props["orientation"] not in {"horizontal", "vertical"}:
            warnings.append("轮播方向未知，已按 horizontal 生成")
            props["orientation"] = "horizontal"
        support = "native" if slides or resource_ids else "partial"
        return AdaptedWidget(
            component.id, self.name, self.widget_type, support, props,
            resource_ids, tuple(component.event_ids), tuple(warnings),
        )


@register
class RechartsLVGLAdapter(_LVGLAdapter):
    """Recharts Line/Bar/Area/Pie -> LVGL chart/arc rendering intent."""

    name = "RechartsAdapter"
    component_kind = "chart"
    widget_type = "lv_chart"

    _types = {"line", "bar", "area", "pie"}

    def __init__(self) -> None:
        pass

    def adapt(self, component: Component, model: ReactProjectModel) -> AdaptedWidget:
        props = _props(component)
        return self._adapt_type(component, props, self.chart_types(component)[0], model=model)

    def chart_types(self, component: Component) -> list[str]:
        """Read scanner evidence as well as explicit component props."""
        props = component.props or {}
        explicit = props.get("chart_type", props.get("chartType", props.get("type")))
        if explicit:
            raw_types = [explicit]
        else:
            chart = props.get("chart")
            raw_types = chart.get("types", []) if isinstance(chart, dict) else []
        names = {"linechart": "line", "barchart": "bar", "areachart": "area", "piechart": "pie"}
        result = []
        for raw in raw_types or ["line"]:
            normalized = names.get(str(raw).replace("_", "").replace("-", "").replace(" ", "").lower(), str(raw).lower())
            if normalized not in result:
                result.append(normalized)
        return result or ["line"]

    def adapt_many(self, component: Component, model: ReactProjectModel) -> list[AdaptedWidget]:
        """A component may contain an Area, Line and Bar branch.

        Each branch remains a separate generated function and audit item rather
        than silently choosing whichever JSX tag was encountered first.
        """
        props = _props(component)
        types = self.chart_types(component)
        return [self._adapt_type(component, props, chart_type, suffix=len(types) > 1, model=model) for chart_type in types]

    def _adapt_type(
        self,
        component: Component,
        props: dict[str, Any],
        chart_type: str,
        suffix: bool = False,
        model: ReactProjectModel | None = None,
    ) -> AdaptedWidget:
        props = dict(props)
        warnings: list[str] = []
        support = "native"
        widget_type = "lv_chart"
        if chart_type not in self._types:
            chart_type = "line"
            support = "partial"
            warnings.append("未识别的 Recharts 图表类型，已降级为 line")
        elif chart_type == "area":
            support = "partial"
            warnings.append("LVGL lv_chart 没有 Recharts Area 的精确渐变填充；保留折线和数据")
        elif chart_type == "pie":
            widget_type = "lv_arc_segments"
            support = "partial"
            warnings.append("Pie 使用多个 LVGL Arc 片段生成，图例/tooltip 需在 AiBuilder 中继续编辑")
        chart_evidence = props.get("chart")
        data_name = chart_evidence.get("data") if isinstance(chart_evidence, dict) else None
        arrays = props.get("arrays", [])
        source_array = next((item for item in arrays if isinstance(item, dict) and item.get("name") == data_name), None)
        if source_array is None and model is not None:
            component_name = component.id.rsplit(":", 1)[-1]
            for parent_id, children in model.component_graph.items():
                if component.id not in children:
                    continue
                parent = model.components.get(parent_id)
                bindings = (parent.props.get("child_bindings", {}) if parent else {}).get(component_name, [])
                bound_name = bindings[0].get("data") if bindings else None
                source_array = next((item for item in (parent.props.get("arrays", []) if parent else []) if isinstance(item, dict) and item.get("name") == bound_name), None)
                if source_array is not None:
                    props["data_binding"] = {"parent": parent_id, "prop": "data", "source": bound_name}
                    break
        if source_array and isinstance(chart_evidence, dict):
            rows = source_array.get("items", [])
            series_values = []
            for series in chart_evidence.get("series", []):
                key = series.get("data_key") if isinstance(series, dict) else None
                if isinstance(series, dict) and series.get("type") != chart_type:
                    continue
                values = [row.get(key) for row in rows if isinstance(row, dict) and isinstance(row.get(key), (int, float))]
                if values:
                    series_values.append({"name": key, "values": values})
            if series_values:
                props["series"] = series_values
                props["point_count"] = len(rows)
                if source_array.get("random_locked"):
                    warnings.append(f"Math.random() 数据已在导入时固定种子 {source_array.get('seed')}，确保模拟可复现")
        if isinstance(chart_evidence, dict) and isinstance(chart_evidence.get("data"), str):
            if not props.get("series"):
                support = "partial"
                warnings.append(f"图表数据来自运行时表达式 {chart_evidence['data']}；已生成可替换的数据序列")
        props["chart_type"] = chart_type
        source_id = f"{component.id}#{chart_type}" if suffix else component.id
        return AdaptedWidget(
            source_id, self.name, widget_type, support, props,
            tuple(component.resource_ids), tuple(component.event_ids), tuple(warnings),
        )


@register
class TableLVGLAdapter(_LVGLAdapter):
    """HTML/shadcn table -> the native, scrollable LVGL v8 table widget."""

    name = "TableAdapter"
    component_kind = "table"
    widget_type = "lv_table"

    def __init__(self) -> None:
        pass

    def matches(self, component: Component) -> bool:
        return isinstance((component.props or {}).get("table"), dict)

    def adapt(self, component: Component, model: ReactProjectModel) -> AdaptedWidget:
        props = _props(component)
        table = dict(props.get("table", {}))
        headers = [str(item) for item in table.get("headers", []) if str(item).strip()]
        columns = max(1, _int(table.get("column_count"), len(headers) or 1))
        row_template = [str(item) for item in table.get("row_template", [])]
        row_count = max(1, _int(table.get("row_count"), 1))
        warnings: list[str] = []
        if not headers:
            headers = [f"Column {index + 1}" for index in range(columns)]
            warnings.append("未解析到表头；已生成可编辑的通用列名")
        if not table.get("row_source"):
            warnings.append("未解析到表格行数据源；已生成一行可编辑占位数据")
        elif not table.get("row_count"):
            warnings.append(f"表格数据源 {table['row_source']} 为运行时表达式；已保留列结构和占位行")
        if any("…" in value for value in row_template):
            warnings.append("表格单元格含运行时 JSX 表达式；已生成布局占位文本，数值绑定需在应用回调中完成")
        props["table"] = {
            **table,
            "headers": headers,
            "column_count": columns,
            "row_count": row_count,
            "row_template": row_template,
        }
        props.setdefault("width", max(360, columns * 116))
        props.setdefault("height", 240)
        props.setdefault("header_color", "263552")
        props.setdefault("cell_color", "18233a")
        return AdaptedWidget(
            component.id, self.name, self.widget_type,
            "native" if table.get("row_count") and headers and not any("…" in value for value in row_template) else "partial", props,
            tuple(component.resource_ids), tuple(component.event_ids), tuple(warnings),
        )


@register
class DropdownLVGLAdapter(_LVGLAdapter):
    """Radix/shadcn Select and DropdownMenu -> LVGL's native dropdown."""

    name = "DropdownAdapter"
    component_kind = "dropdown"
    widget_type = "lv_dropdown"

    def __init__(self) -> None:
        pass

    def matches(self, component: Component) -> bool:
        return bool((component.props or {}).get("selects"))

    def adapt(self, component: Component, model: ReactProjectModel) -> AdaptedWidget:
        return self.adapt_many(component, model)[0]

    def adapt_many(self, component: Component, model: ReactProjectModel) -> list[AdaptedWidget]:
        props = _props(component)
        widgets = props.get("selects", [])
        results: list[AdaptedWidget] = []
        for number, evidence in enumerate(widgets):
            if not isinstance(evidence, dict):
                continue
            item_props = dict(props)
            options = [str(option) for option in evidence.get("options", []) if str(option).strip()]
            warnings: list[str] = []
            if not options:
                options = ["未解析选项"]
                warnings.append("下拉选项来自运行时 JSX 或未识别的数据源；已生成可编辑占位项")
            if component.event_ids:
                warnings.append("已生成原生选择控件；React change 回调已记录在 audit，需由应用绑定")
            selected = evidence.get("selected")
            selected_index = next((index for index, option in enumerate(options) if str(selected) == option), 0)
            item_props["dropdown"] = {
                **evidence,
                "options": options,
                "selected_index": selected_index,
            }
            item_props.setdefault("width", 240)
            item_props.setdefault("height", 46)
            item_props.setdefault("background", "18233a")
            support = "native" if evidence.get("option_source") is not None or len(options) > 1 else "partial"
            source_id = f"{component.id}#dropdown_{number + 1}"
            results.append(AdaptedWidget(
                source_id, self.name, self.widget_type, support, item_props,
                tuple(component.resource_ids), tuple(component.event_ids), tuple(warnings),
            ))
        return results


@register
class ScrollLVGLAdapter(_LVGLAdapter):
    """CSS overflow / ScrollArea -> a native LVGL scroll container."""

    name = "ScrollContainerAdapter"
    component_kind = "scroll"
    widget_type = "lv_obj_scroll"

    def __init__(self) -> None:
        pass

    def matches(self, component: Component) -> bool:
        return isinstance((component.props or {}).get("scroll"), dict)

    def adapt(self, component: Component, model: ReactProjectModel) -> AdaptedWidget:
        props = _props(component)
        scroll = dict(props.get("scroll", {}))
        axis = str(scroll.get("axis", "y")).lower()
        warnings: list[str] = []
        if axis not in {"x", "y"}:
            axis = "y"
            warnings.append("滚动方向未知，已按 vertical 生成")
        props["scroll"] = {**scroll, "axis": axis}
        props.setdefault("width", 360)
        props.setdefault("height", 240)
        props.setdefault("background", "18233a")
        warnings.append("已生成原生滚动容器；其 React 子节点需由屏幕组合阶段挂载到该容器")
        return AdaptedWidget(
            component.id, self.name, self.widget_type, "partial", props,
            tuple(component.resource_ids), tuple(component.event_ids), tuple(warnings),
        )


@register
class GenericControlsLVGLAdapter(_LVGLAdapter):
    """Concrete React/HTML/shadcn controls -> native LVGL v8 controls.

    This adapter deliberately consumes only ``scanner._generic_control_evidence``.
    It never uses component names, styling classes, screenshots, or imported UI
    library declarations as a signal.  In particular, an ``Image`` whose JSX
    source cannot be resolved to a local asset is *not* emitted as a misleading
    blank ``lv_img``: the source audit keeps the evidence and a later asset
    resolver may decide how to supply it.
    """

    name = "GenericControlsAdapter"
    component_kind = "component"
    widget_type = "lv_obj"

    _native = {
        "button": "lv_btn",
        "label": "lv_label",
        "image": "lv_img",
        "switch": "lv_switch",
        "checkbox": "lv_checkbox",
        "radio": "lv_checkbox",
        "radio_group": "lv_btnmatrix",
        "slider": "lv_slider",
        "input": "lv_textarea",
        "list": "lv_list",
        "tabs": "lv_tabview",
        "select": "lv_dropdown",
    }

    def __init__(self) -> None:
        pass

    def matches(self, component: Component) -> bool:
        # shadcn/Radix implementation modules define primitives such as
        # ``Button`` and ``Input`` but are not page instances.  The caller's
        # `<Button>` / `<Input>` JSX is captured at the use site instead.
        # Without this guard both the call site and `components/ui/button.tsx`
        # generated a native control, doubling the visual tree.
        source = component.source_file.replace("\\", "/").lower()
        if "/components/ui/" in f"/{source}" or source.startswith("components/ui/"):
            return False
        if isinstance((component.props or {}).get("gauge_scene"), dict):
            return False
        return bool((component.props or {}).get("controls"))

    def adapt(self, component: Component, model: ReactProjectModel) -> AdaptedWidget:
        # Registry callers normally use adapt_many; retain this method for the
        # legacy public API without fabricating an arbitrary control.
        return self.adapt_many(component, model)[0]

    def adapt_many(self, component: Component, model: ReactProjectModel) -> list[AdaptedWidget]:
        controls = (component.props or {}).get("controls", [])
        result: list[AdaptedWidget] = []
        expanded_controls: list[dict] = []
        for raw in controls:
            if not isinstance(raw, dict):
                continue
            clone_source = str(raw.get("icon_source", ""))
            if str(raw.get("kind", "")).lower() == "button" and clone_source.startswith("array:"):
                array_name = clone_source.split(":", 1)[1].split(".", 1)[0]
                evidence = next((a for a in component.props.get("arrays", [])
                                 if isinstance(a, dict) and a.get("name") == array_name), None)
                items = evidence.get("items", []) if isinstance(evidence, dict) else []
                concrete = [item for item in items if isinstance(item, dict)]
                if concrete:
                    for item in concrete:
                        clone = dict(raw)
                        clone["text"] = next((item[k] for k in ("title", "label", "name", "value", "id")
                                              if item.get(k) not in (None, "")), raw.get("text", "Button"))
                        if item.get("icon"):
                            clone["icon"] = str(item["icon"])
                        expanded_controls.append(clone)
                    continue
            expanded_controls.append(dict(raw))
        for index, raw in enumerate(expanded_controls):
            if not isinstance(raw, dict):
                continue
            kind = str(raw.get("kind", "")).lower()
            widget_type = self._native.get(kind)
            if widget_type is None:
                continue
            props = _props(component)
            control = dict(raw)
            warnings: list[str] = []
            resources: tuple[str, ...] = ()
            if kind == "image":
                resource_id = control.get("resource_id")
                if not isinstance(resource_id, str) or resource_id not in model.resources:
                    # The JSX node is real, but a native LVGL image source is
                    # not.  Do not generate a nonfunctional image placeholder.
                    continue
                resources = (resource_id,)
            if kind == "radio" and not control.get("value"):
                warnings.append("独立 HTML radio 未包含 value；保留原生可选状态，组互斥需由 React 事件绑定")
            if kind == "radio_group" and not control.get("options"):
                warnings.append("RadioGroup 未解析到 RadioGroupItem；未生成空原生组")
                continue
            if kind == "list" and not control.get("items"):
                warnings.append("List 内容是运行时 JSX；生成空原生列表容器供后续数据绑定")
            if kind == "tabs" and not control.get("tabs"):
                warnings.append("Tabs 未解析到 TabsTrigger；未生成空原生页签")
                continue
            if kind == "select" and not control.get("options"):
                warnings.append("HTML select 未解析到 literal option；未生成空原生下拉框")
                continue
            if control.get("events"):
                warnings.append("控件真实 React 事件已记录；LVGL 原生交互已创建，业务回调仍需在应用层绑定")
            visibility = [item for item in (component.props or {}).get("visibility", [])
                          if isinstance(item, dict) and str(item.get("target")) == str(control.get("tag"))]
            visibility_bindings = [item for item in (component.props or {}).get("visibility_bindings", [])
                                  if isinstance(item, dict)]
            if visibility or visibility_bindings:
                warnings.append("控件受 React 条件可见性控制；已保留 expression/source_offset 审计，运行时显隐需应用层绑定")
            props["control"] = control
            # The exact control record (tag, source offset, props, event prop
            # names) remains in JSON audit.  A suffix stops two Buttons from
            # sharing one C symbol.
            source_id = f"{component.id}#control_{index + 1}_{kind}"
            support = "partial" if visibility or visibility_bindings or kind in {"radio"} else "native"
            result.append(AdaptedWidget(
                source_id, self.name, widget_type, support, props, resources,
                tuple(component.event_ids), tuple(warnings),
            ))
        return result


def adapt_component(component: Component, model: ReactProjectModel) -> AdaptedWidget | None:
    """Return the first registered concrete LVGL adaptation, if any."""
    # Importing here preserves the old registry's lightweight public surface.
    from .registry import resolve

    adapter = resolve(component)
    adapt = getattr(adapter, "adapt", None)
    return adapt(component, model) if callable(adapt) else None


def adapt_components(component: Component, model: ReactProjectModel) -> list[AdaptedWidget]:
    """Adapt every concrete intent in a component, retaining chart branches."""
    from .registry import resolve_all

    result: list[AdaptedWidget] = []
    for adapter in resolve_all(component):
        # The legacy semantic module also registers classification-only
        # adapters.  They deliberately have no ``adapt`` method and must not
        # generate a duplicate LVGL unit.
        adapt_many = getattr(adapter, "adapt_many", None)
        if callable(adapt_many):
            result.extend(adapt_many(component, model))
            continue
        adapt = getattr(adapter, "adapt", None)
        if callable(adapt):
            result.append(adapt(component, model))
    planned: list[AdaptedWidget] = []
    for adapted in result:
        decision = model.capability_plan.get(adapted.source_id) or model.capability_plan.get(component.id)
        if decision is None:
            planned.append(adapted)
            continue
        properties = dict(adapted.properties)
        properties["capability"] = asdict(decision)
        render_item = next((item for item in model.render_plan.items if item.id == decision.target_id), None)
        properties["render_operation"] = render_item.operation if render_item else decision.strategy
        # A source-level compound/fallback/unsupported decision must be visible
        # to the generator. Preserve historical adapter partial statuses when a
        # native plan only describes the enclosing component.
        support = decision.support if decision.support in {"compound", "fallback", "unsupported"} else adapted.support
        planned.append(replace(adapted, support=support, properties=properties))
    return planned
