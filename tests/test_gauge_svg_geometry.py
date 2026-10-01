import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from adapters.contracts import AdaptedWidget
from core.model import Component, ReactProjectModel
from generator.lvgl import LVGLRenderer, _svg_gauge_metrics


def _adapted() -> AdaptedWidget:
    return AdaptedWidget(
        "HudGauge#1#gauge_1", "GaugeAdapter", "lv_arc", "compound",
        {"gauges": [{"min": -10, "max": 50, "value": 0, "label": "T", "unit": "C",
                      "colors": {"normal": "#22c55e", "track": "#263241"}}]},
    )


def test_svg_metrics_reads_foreground_progress_angles_and_actual_ticks():
    svg = ('<svg><circle r="34" stroke="#22c55e" stroke-width="5" '
           'stroke-dasharray="166.9 47.2" stroke-dashoffset="83.45" '
           'transform="rotate(144 54 54)"/>'
           '<line x1="1" y1="2" x2="3" y2="4" stroke="#22c55e" stroke-width="1.5" opacity=".9"/>'
           '</svg>')
    metrics = _svg_gauge_metrics(svg)
    assert round(metrics["ratio"], 3) == 0.5
    assert metrics["start"] == 144
    assert round(metrics["sweep"], 1) == 281.3
    assert metrics["stroke_width"] == 5
    assert metrics["ticks"][0]["x1"] == 1

    path_metrics = _svg_gauge_metrics(
        '<svg viewBox="0 0 320 240"><path class="gauge-progress" pathLength="100" '
        'stroke-dasharray="37 100" d="M 0 0 A 10 10 0 0 1 20 0"/></svg>')
    assert path_metrics["ratio"] == 0.37
    assert path_metrics["view_box"] == [0.0, 0.0, 320.0, 240.0]


def test_gauge_renderer_uses_browser_svg_but_keeps_source_fallback():
    model = ReactProjectModel(Path("."))
    model.browser_evidence = {"screens": [{"data": {"nodes": [
        {"index": 0, "rect": {"x": 0, "y": 0, "width": 320, "height": 240}, "tag": "div"},
        {"index": 1, "rect": {"x": 10, "y": 12, "width": 108, "height": 108}, "tag": "svg",
         "svg": ('<svg><circle r="34" stroke="#22c55e" stroke-width="5" '
                 'stroke-dasharray="166.9 47.2" stroke-dashoffset="83.45" '
                 'transform="rotate(144 54 54)"/>'
                 '<line x1="1" y1="2" x2="3" y2="4" stroke="#22c55e" opacity=".9"/></svg>')},
        {"index": 2, "rect": {"x": 45, "y": 48, "width": 38, "height": 34}, "tag": "text",
         "text": "0", "className": "gauge-value"},
    ]}}]}
    component = Component("HudGauge", "gauge", "App.tsx", {})
    code = LVGLRenderer().render(_adapted(), component, model).c_code
    assert "lv_arc_set_bg_angles(hudgauge_1_gauge_1, 144, 65);" in code
    assert "lv_arc_set_range(hudgauge_1_gauge_1, -10, 50);" in code
    assert "static const lv_point_precise_t hudgauge_1_gauge_1_tick_points[1][2]" in code
    assert "lv_obj_set_style_arc_opa(hudgauge_1_gauge_1_glow_0, LV_OPA_TRANSP, LV_PART_MAIN);" in code
    assert "#define UAGENT_GAUGE_SIMULATION 1" in code
    assert "lv_timer_create(hudgauge_1_gauge_1_simulation_timer, 40, NULL);" in code
    assert "direction = -1" in code
    assert "hudgauge_1_gauge_1_set_display_value(next);" in code
    assert "lv_label_set_text(hudgauge_1_gauge_1_value_label, text);" in code
    assert "hudgauge_1_gauge_1_hide_captured_readout(parent);" in code

    model.browser_evidence = {}
    fallback = LVGLRenderer().render(_adapted(), component, model).c_code
    assert "lv_arc_set_bg_angles(hudgauge_1_gauge_1, 135, 45);" in fallback


def test_full_canvas_svg_ticks_are_scoped_and_translated_to_gauge_box():
    model = ReactProjectModel(Path("."))
    svg = ('<svg viewBox="0 0 800 400">'
           '<line x1="110" y1="100" x2="120" y2="110" stroke="#fff"/>'
           '<line x1="610" y1="100" x2="620" y2="110" stroke="#fff"/>'
           '<circle r="50" stroke="#fff" stroke-dasharray="100 100" '
           'stroke-dashoffset="50"/></svg>')
    model.browser_evidence = {"screens": [{"data": {"nodes": [
        {"index": 0, "rect": {"x": 0, "y": 0, "width": 800, "height": 400},
         "tag": "svg", "svg": svg},
    ]}}]}
    adapted = _adapted()
    adapted.properties["gauges"][0]["geometry"] = {"x": 80, "y": 60, "width": 180, "height": 180}
    code = LVGLRenderer().render(adapted, Component("HudGauge", "gauge", "App.tsx", {}), model).c_code
    assert "_tick_points[1][2]" in code
    assert "{{30, 40}, {40, 50}}" in code
    assert "{610, 100}" not in code


def test_side_gauge_direction_uses_adjacent_low_and_high_labels():
    component = Component("HudGauge", "gauge", "App.tsx", {})

    def render(start, end, low_text, low_box, high_text, high_box):
        geometry_x = 46 if low_box["x"] < 500 else 630
        model = ReactProjectModel(Path("."))
        model.browser_evidence = {"screens": [{"data": {"nodes": [
            {"tag": "text", "text": low_text, "className": "side-label", "rect": low_box},
            {"tag": "text", "text": high_text, "className": "side-label", "rect": high_box},
            {"tag": "text", "text": "0", "className": "tick", "rect": {"x": geometry_x + 80, "y": 350, "width": 10, "height": 16}},
            {"tag": "text", "text": "160", "className": "tick", "rect": {"x": geometry_x + 240, "y": 350, "width": 24, "height": 16}},
        ]}}]}
        adapted = _adapted()
        gauge = adapted.properties["gauges"][0]
        gauge.update({"label": "", "unit": "", "geometry": {"x": geometry_x, "y": 118, "width": 348, "height": 348},
                      "arc_paints": [{"start": start, "end": end}]})
        return LVGLRenderer().render(adapted, component, model).c_code

    coolant = render(75, 137, "C", {"x": 885, "y": 470, "width": 16, "height": 24},
                      "H", {"x": 954, "y": 358, "width": 18, "height": 24})
    assert "lv_arc_set_mode(hudgauge_1_gauge_1, LV_ARC_MODE_REVERSE);" in coolant
    assert "lv_timer_create(hudgauge_1_gauge_1_simulation_timer, 280, NULL);" in coolant
    assert "direction * 7" in coolant

    fuel = render(-137, -75, "E", {"x": 131, "y": 470, "width": 15, "height": 24},
                  "F", {"x": 60, "y": 358, "width": 14, "height": 24})
    assert "LV_ARC_MODE_REVERSE" not in fuel
    assert "lv_timer_create(hudgauge_1_gauge_1_simulation_timer, 280, NULL);" in fuel
