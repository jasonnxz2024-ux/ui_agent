from core.browser_layout import browser_background_paint, browser_visual_effects, parse_css_gradient
from core.model import Component, ReactProjectModel, Screen
from generator.project import _snapshot_screens
from pathlib import Path
import re


def _capture(image: str):
    return {"nodes": [{
        "index": 3,
        "tag": "div",
        "rect": {"x": 11, "y": 7, "width": 480, "height": 272},
        "style": {"backgroundImage": image, "backgroundColor": "rgb(2, 6, 17)"},
    }]}


def test_css_gradient_parser_keeps_direction_positions_and_nested_rgba():
    parsed = parse_css_gradient(
        "linear-gradient(135deg, #07152f 0%, rgba(10,30,80,.8) 55%, #020611 100%)"
    )
    assert parsed["gradient_type"] == "linear"
    assert parsed["angle"] == 135
    assert parsed["stops"][1] == {"color": "rgba(10,30,80,.8)", "position": "55%"}


def test_browser_gradient_evidence_has_measured_bounds_and_receipt_data():
    image = "radial-gradient(circle at 35% 20%, #113d72 0%, #07152f 65%, #020611 100%)"
    data = _capture(image)
    effects = browser_visual_effects(data)
    assert effects[0]["gradient_type"] == "radial"
    assert effects[0]["bounds"] == [11.0, 7.0, 480.0, 272.0]
    paint = browser_background_paint(data, width=480, height=272)
    assert paint and paint["position"] == "circle at 35% 20%"
    assert len(paint["stops"]) == 3


def test_radial_snapshot_layers_use_monotonic_alpha():
    svg = '<svg><defs><radialGradient id="ambient"><stop offset="0" stop-color="#151cae"/><stop offset=".48" stop-color="#040a66"/><stop offset="1" stop-color="#050608"/></radialGradient></defs></svg>'
    data = {"viewport": {"width": 480, "height": 272}, "root": {"x": 0, "y": 0, "width": 480, "height": 272},
            "nodes": [{"index": 1, "tag": "svg", "rect": {"x": 0, "y": 0, "width": 480, "height": 272}, "style": {}, "svg": svg}]}
    model = ReactProjectModel(Path('.'))
    component = Component('component:App', 'component', 'App.tsx', {'name': 'Dashboard', 'gauge_scene': {'width': 480, 'height': 272}})
    model.components[component.id] = component
    model.screens = [Screen('screen_01_main', 'main', 480, 272, component.id)]
    snapshot = _snapshot_screens(model, 480, 272, {'screens': [{'path': [], 'data': data}]})
    opacities = [int(value) for value in re.findall(r'gradient_\d+.*?<bg-opa>(\d+)</bg-opa>', snapshot)]
    assert len(opacities) == 6
    assert opacities[0] == 255 and opacities == sorted(opacities, reverse=True)
    assert '#151CAE' in snapshot and '#040A66' in snapshot
