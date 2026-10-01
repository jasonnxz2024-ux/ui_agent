import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from generator.project import _IdAllocator, _browser_geometry, _control_widget, _dom_fallback_controls, _snapshot_style


def _node(index, parent, tag, rect, classes="", text=""):
    return {"index": index, "parentIndex": parent, "tag": tag,
            "className": classes, "text": text, "rect": rect,
            "style": {"color": "rgb(26, 26, 26)", "backgroundColor": "rgb(255, 255, 255)"}}


def test_energy_geometry_uses_app_frame_main_and_all_sidebar_buttons():
    root = {"x": 0, "y": 0, "width": 1024, "height": 600}
    nodes = [_node(0, None, "div", root, "flex h-screen bg-background"),
             # Regression fixture for the old 68x84 first-relative-node bug.
             _node(1, 0, "div", {"x": 22, "y": 22, "width": 68, "height": 84}, "p-6 relative"),
             _node(2, 0, "div", {"x": 0, "y": 0, "width": 91, "height": 600}, "flex flex-col h-[calc(100vh-3rem)]"),
             *[_node(3 + i, 2, "button", {"x": 35, "y": 126 + i * 56, "width": 42, "height": 42}, "rounded-full") for i in range(5)],
             _node(8, 0, "main", {"x": 91, "y": 0, "width": 933, "height": 600}, "flex-1 overflow-auto"),
             _node(9, 8, "button", {"x": 120, "y": 40, "width": 120, "height": 32}, "border bg-background")]
    geometry = _browser_geometry({"data": {"root": root, "nodes": nodes}}, 1024, 600,
                                 "Other", [{"kind": "select", "dom_index": 9, "text": ""}])
    assert geometry["Navigation"]["box"] == (0, 0, 91, 600)
    assert len(geometry["Navigation"]["controls"]) == 5
    assert geometry["Other"]["box"] == (91, 0, 933, 600)
    assert geometry["Other"]["controls"] == [(29, 40, 120, 32)]


def test_measured_label_height_is_not_padded_for_compact_footer_text():
    root = {"x": 0, "y": 0, "width": 320, "height": 240}
    nodes = [
        _node(0, None, "div", root, "w-[320px] h-[240px]"),
        _node(1, 0, "main", root, "flex-1 overflow-auto"),
        _node(2, 1, "span", {"x": 12, "y": 228, "width": 28, "height": 8}, "text-xs", "Footer"),
    ]
    geometry = _browser_geometry(
        {"data": {"root": root, "nodes": nodes}}, 320, 240, "FooterPage",
        [{"kind": "label", "text": "Footer", "dom_index": 2}],
    )
    assert geometry["FooterPage"]["controls"] == [(12, 228, 56, 8)]


def test_cropped_button_image_keeps_text_metadata_without_harness_label():
    xml = _control_widget(
        {"kind": "image", "src": "uagent_nav_state_1.png", "text": "SET"},
        "nav_crop", (0, 200, 64, 40), 0, 1, _IdAllocator(), "page",
    )
    assert "<src>uagent_nav_state_1.png</src>" in xml
    assert "<text>SET</text>" in xml


def test_select_snapshot_style_declares_non_blue_parts_and_states():
    xml = _snapshot_style({"className": "border border-border/50 bg-background/50"}, "select", "Today")
    for part in ('name="Main"', 'name="Selected"', 'name="List"', 'name="Scrollbar"'):
        assert part in xml
    for state in ('name="Checked" value="1"', 'name="Focused" value="2"', 'name="Pressed" value="32"'):
        assert state in xml
    assert "#0000FF" not in xml.upper()


def test_transparent_slider_keeps_anchor_but_clears_all_visual_parts():
    xml = _snapshot_style({"className": "bg-[#1E2530]", "style": {"opacity": "0"}}, "slider")
    assert xml.count('name="Main"') == 1
    assert xml.count('name="Indicator"') == 1
    assert xml.count('name="Knob"') == 1
    assert xml.count("<bg-opa>0</bg-opa>") == 3

    data = {"root": {"x": 0, "y": 0, "width": 320, "height": 240}, "nodes": [
        _node(0, None, "div", {"x": 0, "y": 0, "width": 320, "height": 240}),
        {**_node(1, 0, "input", {"x": 180, "y": 90, "width": 100, "height": 12}, "range-slider"),
         "type": "range", "style": {"opacity": "0", "backgroundColor": "rgba(0, 0, 0, 0)"}},
        _node(2, 0, "button", {"x": 220, "y": 130, "width": 36, "height": 18}),
        {**_node(3, 2, "div", {"x": 243, "y": 134, "width": 10, "height": 10}),
         "style": {"opacity": "1", "backgroundColor": "rgb(245, 158, 11)"}},
    ]}
    controls = _dom_fallback_controls(data)
    assert any(c["kind"] == "slider" and c["dom_index"] == 1 for c in controls)
    assert any(c["kind"] == "container" and c["dom_index"] == 3 for c in controls)


def test_wide_direct_text_row_is_materialized_once_as_a_label():
    data = {"root": {"x": 0, "y": 0, "width": 320, "height": 240}, "nodes": [
        _node(0, None, "div", {"x": 0, "y": 0, "width": 320, "height": 240}),
        _node(1, 0, "div", {"x": 140, "y": 40, "width": 172, "height": 10.5}, "text-xs", "CLIMATE"),
    ]}
    controls = _dom_fallback_controls(data)
    labels = [c for c in controls if c.get("dom_index") == 1]
    assert len(labels) == 1
    assert labels[0]["kind"] == "label"


def test_painted_div_section_and_segmented_bar_are_preserved_in_dom_order():
    data = {"root": {"x": 0, "y": 0, "width": 320, "height": 240}, "nodes": [
        _node(0, None, "div", {"x": 0, "y": 0, "width": 320, "height": 240}),
        {**_node(1, 0, "div", {"x": 12, "y": 20, "width": 296, "height": 80}),
         "style": {"backgroundColor": "rgba(20, 30, 40, 0.35)",
                    "borderTopWidth": "1px", "borderTopColor": "rgb(80, 90, 100)"}},
        *[{**_node(2 + i, 0, "div", {"x": 20 + i * 28, "y": 120, "width": 24, "height": 8}),
           "style": {"backgroundColor": "rgb(245, 158, 11)"}} for i in range(4)],
    ]}
    controls = _dom_fallback_controls(data)
    painted = [c for c in controls if c.get("kind") == "container"]
    assert [c["dom_index"] for c in painted] == [1, 2, 3, 4, 5]


def test_painted_div_excludes_transparent_and_deduplicates_switch_thumb():
    data = {"root": {"x": 0, "y": 0, "width": 320, "height": 240}, "nodes": [
        _node(0, None, "div", {"x": 0, "y": 0, "width": 320, "height": 240}),
        {**_node(1, 0, "div", {"x": 10, "y": 20, "width": 120, "height": 30}),
         "style": {"backgroundColor": "rgba(0, 0, 0, 0)"}},
        {**_node(2, 0, "button", {"x": 10, "y": 70, "width": 36, "height": 18}),
         "style": {"backgroundColor": "rgb(30, 40, 50)"}},
        {**_node(3, 2, "div", {"x": 30, "y": 74, "width": 10, "height": 10}),
         "style": {"backgroundColor": "rgb(245, 158, 11)"}},
    ]}
    controls = _dom_fallback_controls(data)
    assert not any(c.get("dom_index") == 1 for c in controls)
    assert sum(c.get("dom_index") == 3 for c in controls) == 1


def test_small_painted_segment_under_ordinary_div_is_preserved_once():
    data = {"root": {"x": 0, "y": 0, "width": 320, "height": 240}, "nodes": [
        _node(0, None, "div", {"x": 0, "y": 0, "width": 320, "height": 240}),
        _node(1, 0, "div", {"x": 12, "y": 100, "width": 120, "height": 16}),
        {**_node(2, 1, "div", {"x": 14, "y": 104, "width": 8, "height": 8}),
         "style": {"backgroundColor": "rgb(245, 158, 11)"}},
    ]}
    controls = _dom_fallback_controls(data)
    segments = [c for c in controls if c.get("dom_index") == 2]
    assert len(segments) == 1
    assert segments[0]["kind"] == "container"


def test_snapshot_style_preserves_partial_rgba_alpha_and_per_side_border():
    xml = _snapshot_style({"style": {
        "backgroundColor": "rgba(20, 30, 40, 0.35)",
        "borderRightWidth": "2px", "borderRightColor": "rgba(80, 90, 100, 0.5)",
    }}, "container")
    assert '<bg-opa>89</bg-opa>' in xml
    assert '<border-width>2</border-width>' in xml
    assert '<border-color>#505A64</border-color>' in xml
