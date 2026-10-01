import sys
import re
from xml.etree import ElementTree
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.capability import _browser_counts, plan_capabilities, reachable_component_ids, source_browser_plan_counts
from core.model import Component, ReactProjectModel, VisualEffectIR
from core.pipeline import analyze
from generator.compiler import compile_lvgl
from generator.project import _snapshot_screens, detect_canvas


def test_cluster_has_source_backed_composite_gauge_scene():
    model = analyze(Path(r"D:\aiassit\cluster"))
    app = next(component for component in model.components.values() if component.id.endswith(":App"))
    assert app.kind == "composite_gauge_scene"
    assert detect_canvas(model) == (480, 272)
    assert app.props["gauge_layout"] == [(10, 44, 216, 216), (254, 44, 216, 216)]
    scene = app.props["gauge_scene"]
    assert scene["canvas"] == {"width": 480, "height": 272, "view_box": [0, 0, 480, 272], "source_offset": scene["canvas"]["source_offset"]}
    assert scene["constants"]["start"] == -135
    assert scene["constants"]["sweep"] == 270
    assert scene["regions"]["battery_segments"] == 10
    assert scene["background"]["radial_gradient"]
    assert len(scene["gauges"]) == 2
    status_bars = scene["status_bars"]
    assert len(status_bars) == 1
    status = status_bars[0]
    assert status["edge"] == "top"
    assert status["frame"] == {"x": 0, "y": 0, "width": 480, "height": 32}
    assert set(status["slots"]) == {"start", "center", "end"}
    assert [item["name"] for item in status["slots"]["start"] if item["type"] == "vector_icon"] == ["user", "lock"]
    temperature = next(item for item in status["slots"]["start"] if item["type"] == "value_with_unit")
    assert temperature["value"] == 22
    assert temperature["unit"] == "°C"
    clock = next(item for item in status["slots"]["center"] if item["type"] == "clock")
    assert clock["binding"] == "timeStr"
    assert clock["format"] == "HH:mm" and clock["hour12"] is False and clock["interval_ms"] == 1000
    signal = next(item for item in status["slots"]["end"] if item["type"] == "signal_bars")
    assert signal["bar_count"] == 3 and signal["level"] == 3
    wifi = next(item for item in status["slots"]["end"] if item["type"] == "connectivity_indicator")
    assert wifi["name"] == "wifi" and len(wifi["primitives"]) == 3
    assert [(g["gauge_id"], g["geometry"]["cx"], g["geometry"]["cy"], g["geometry"]["radius"]) for g in scene["gauges"]] == [
        ("gauge_L", 118.0, 152.0, 108.0), ("gauge_R", 362.0, 152.0, 108.0)
    ]
    assert scene["gauges"][0]["ticks"]["total"] == 40
    assert scene["gauges"][0]["ticks"]["major_every"] == 5
    assert scene["gauges"][0]["ticks"]["labels"][-1] == "200"
    assert scene["gauges"][1]["ticks"]["total"] == 32
    assert scene["gauges"][1]["ticks"]["major_every"] == 4
    assert scene["gauges"][1]["redline"]["tick"] == 24
    assert scene["gauges"][0]["readout"]["unit"] == "KM/H"
    assert scene["gauges"][1]["readout"]["value_binding"].startswith("(rpm")
    assert all({"track", "glow-wide", "glow-soft", "foreground"} <= {p["role"] for p in g["paint_layers"]} for g in scene["gauges"])
    paints = app.props["arc_paints"]
    assert "text-glow" not in {paint.get("filter_id") for paint in paints}
    assert next(p for p in paints if p.get("gauge_id") == "gauge_L" and p.get("role") == "glow-soft").get("blur") == 3.5
    counts = source_browser_plan_counts(model)
    assert counts["source"]["Gauge"] == counts["plan"]["Gauge"] == 2
    assert counts["source"]["Arc"] == counts["plan"]["Arc"] == 2
    assert counts["source"]["VisualEffect"] > 0
    assert not model.render_plan.blockers
    scene_items = [item for item in model.render_plan.items if item.operation == "custom-draw-scene"]
    assert len(scene_items) == 1
    assert scene_items[0].adapter == "GaugeSceneAdapter"
    assert not [item for item in model.render_plan.items if item.operation == "arc-glow-layers"]

    snapshot = _snapshot_screens(model, 480, 272)
    root = ElementTree.fromstring(f"<ailv-app>{snapshot}</ailv-app>")
    arc_widgets = [node for node in root.iter("Widget") if node.get("type") == "12"]
    assert not arc_widgets
    assert '<size>480,272</size>' in snapshot
    assert '<bg-opa>0</bg-opa>' in snapshot
    assert not re.search(r'<Widget[^>]+type="5"[^>]+><Normal>.*?<size>480,272</size>', snapshot, re.S)

    bundle = compile_lvgl(model)
    assert "LV_EVENT_DRAW_MAIN" in bundle.custom_c
    assert "lv_draw_arc" in bundle.custom_c
    assert "lv_draw_line" in bundle.custom_c
    assert "lv_draw_label" in bundle.custom_c
    assert "dsc.text = text" in bundle.custom_c
    assert "const lv_font_t * font" in bundle.custom_c
    assert "dsc.font = font" in bundle.custom_c
    for font in ("lv_font_montserrat_8", "lv_font_montserrat_10", "lv_font_montserrat_12", "lv_font_montserrat_14",
                 "lv_font_montserrat_18", "lv_font_montserrat_20", "lv_font_montserrat_32",
                 "lv_font_montserrat_38"):
        assert font in bundle.custom_c
    assert "UAgent font compatibility" in bundle.custom_c
    assert "#define lv_font_montserrat_48 lv_font_montserrat_16" in bundle.custom_c
    assert "for(int i = 0; i <= 40" in bundle.custom_c
    assert "for(int i = 0; i <= 32" in bundle.custom_c
    assert "glow-wide" in bundle.custom_c
    assert "27/6, 23/10, 19/18, 15/35, 10/255" in bundle.custom_c
    assert all(f", {width}," in bundle.custom_c for width in (27, 23, 19, 15, 10))
    assert all(f", {opa});" in bundle.custom_c for opa in (6, 10, 18, 35, 255))
    assert "130 + i * 14" in bundle.custom_c
    assert '"+0 kW"' in bundle.custom_c and '"POWER"' in bundle.custom_c
    assert '"78%"' in bundle.custom_c and '"BATTERY"' in bundle.custom_c
    assert '"287 KM"' in bundle.custom_c and '"RANGE"' in bundle.custom_c
    assert '"kW POWER"' not in bundle.custom_c and '"KM RANGE"' not in bundle.custom_c
    assert "redline" in bundle.custom_c
    assert "uagent_gauge_scene_set_values" in bundle.custom_c
    assert "uagent_gauge_scene_update" in bundle.custom_c
    for api in ("uagent_status_set_time", "uagent_status_set_temperature", "uagent_status_set_signal", "uagent_status_set_wifi"):
        assert api in bundle.custom_c and api in bundle.custom_h
    assert "draw_circle" in bundle.custom_c
    assert "draw_rect" in bundle.custom_c
    assert "draw_status_icon" in bundle.custom_c
    assert "22 C" not in bundle.custom_c
    assert "--:--" not in bundle.custom_c
    assert "lv_arc_create" not in bundle.custom_c
    assert "if (lv_obj_get_child_count(parent) > 0) return" not in bundle.custom_c
    assert "capability_plan" in bundle.audit_dict()


def test_full_canvas_blur_is_a_blocker_not_a_png(tmp_path):
    model = ReactProjectModel(tmp_path)
    component = Component("src/App.tsx:App", "component", "src/App.tsx")
    model.components[component.id] = component
    model.entry_components.append(component.id)
    model.visual_effects["effect:full"] = VisualEffectIR(
        "effect:full", "blur", "src/App.tsx", source_offset=10,
    )
    plan_capabilities(model, {
        "viewport": {"width": 320, "height": 240},
        "visual_effects": [{
            "effect_id": "effect:full", "source_id": "effect:full", "kind": "blur",
            "bounds": [0, 0, 320, 240],
        }],
    })
    decision = model.capability_plan["effect:full"]
    assert decision.support == "unsupported"
    assert model.render_plan.blockers
    assert not decision.fallback_bounds


def test_template_component_is_not_counted_beside_call_site_instances(tmp_path):
    model = ReactProjectModel(tmp_path)
    app = Component("src/App.tsx:App", "component", "src/App.tsx")
    template = Component("src/App.tsx:HudGauge", "gauge", "src/App.tsx",
                         {"_instantiated_as_template": True, "gauges": [{"gauge_id": "template"}]})
    app.props["gauges"] = [{"gauge_id": "left"}, {"gauge_id": "right"}]
    model.components.update({app.id: app, template.id: template})
    model.entry_components.append(app.id)
    model.component_graph[app.id] = [template.id]
    assert template.id not in reachable_component_ids(model)
    counts = source_browser_plan_counts(model)
    assert counts["source"]["Gauge"] == 2


def test_browser_gauge_counts_svg_once_not_each_circle_paint_layer():
    browser = {"screens": [{"data": {"nodes": [
        {"tag": "svg", "outerHTML": '<svg stroke-dasharray="10 5"><circle stroke-dasharray="10 5"/><circle stroke-dashoffset="2"/></svg>'},
        {"tag": "circle", "attrs": {"stroke-dasharray": "10 5"}},
    ]}}]}
    assert _browser_counts(browser)["Gauge"] == 1
    assert _browser_counts(browser)["Arc"] == 1


def test_ordinary_render_path_keeps_snapshot_child_guard(tmp_path):
    model = ReactProjectModel(tmp_path)
    component = Component(
        "src/App.tsx:App", "component", "src/App.tsx",
        {"name": "App", "controls": [{"kind": "label", "text": "Ready"}]},
    )
    model.components[component.id] = component
    model.entry_components.append(component.id)
    bundle = compile_lvgl(model)
    assert "if (lv_obj_get_child_count(parent) > 0) return" in bundle.custom_c


def test_scanner_effect_fixture_records_filters_and_multiple_arcs(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    (source / "App.tsx").write_text(
        """
        const CX_LEFT = 40, CX_RIGHT = 120, CY = 80, R = 30;
        const arc = (cx, value) => `M ${cx} ${CY}`;
        export default function App() {
          return <div style={{filter:'blur(3px)', boxShadow:'0 0 8px #00ffff',
            background:'linear-gradient(90deg,#000,#0ff)'}}>
            <svg><defs><filter id='glow'><feGaussianBlur stdDeviation='4'/></filter></defs>
              <path d={arc(CX_LEFT, 40)} filter='url(#glow)' />
              <path d={arc(CX_RIGHT, 60)} filter='url(#glow)' />
            </svg>
          </div>;
        }
        """,
        encoding="utf-8",
    )
    model = analyze(tmp_path)
    app = next(component for component in model.components.values() if component.id.endswith(":App"))
    assert len(app.props.get("svg_arcs", [])) == 2
    kinds = {effect.kind for effect in model.visual_effects.values()}
    assert {"filter", "blur", "glow", "shadow", "gradient"} <= kinds


def test_dynamic_template_filter_id_keeps_glow_compound_decisions(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    (source / "App.tsx").write_text(
        """
        export default function App({label}) {
          return <svg><defs><filter id={`glow-${label}`}>
            <feGaussianBlur stdDeviation="4" />
          </filter></defs><circle filter={`url(#glow-${label})`} /></svg>;
        }
        """, encoding="utf-8")
    model = analyze(tmp_path)
    plan_capabilities(model)
    effects = [decision for decision in model.capability_plan.values()
               if decision.adapter == "VisualEffectAdapter"]
    assert effects
    assert any(decision.target_id in model.visual_effects
               and model.visual_effects[decision.target_id].kind == "blur"
               and decision.support == "compound" for decision in effects)
    assert not model.render_plan.blockers
