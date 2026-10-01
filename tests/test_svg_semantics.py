from pathlib import Path

from core.capability import _browser_counts
from core.pipeline import analyze
from core.capability import source_browser_plan_counts


def test_browser_counts_explicit_multi_gauge_svg_groups():
    browser = {"screens": [{"data": {"nodes": [{
        "tag": "svg",
        "outerHTML": (
            '<svg><path data-openhmi-id="radial-left-track"/>'
            '<path data-openhmi-id="radial-right-track"/>'
            '<path data-openhmi-id="side-arc-left" stroke-dasharray="1"/>'
            '<path data-openhmi-id="side-arc-right" stroke-dasharray="1"/></svg>'
        ),
    }]}}]}
    counts = _browser_counts(browser)
    assert counts["Gauge"] == 4
    assert counts["Arc"] == 4


def test_browser_gauge_groups_ignore_weak_left_right_identities():
    browser = {"screens": [{"data": {"nodes": [{
        "tag": "svg",
        "outerHTML": (
            '<svg><path data-openhmi-id="GAUGE-LEFT" stroke-dasharray="1"/>'
            '<path data-openhmi-id="GAUGE-RIGHT" stroke-dasharray="1"/>'
            '<path data-openhmi-id="LAMPS-LEFT" stroke-dasharray="1"/>'
            '<path data-openhmi-id="LAMPS-RIGHT" stroke-dasharray="1"/></svg>'
        ),
    }]}, "overflow": {"hasOverflow": False}}]}
    counts = _browser_counts(browser)
    assert counts["Gauge"] == 2
    assert counts["Arc"] == 2
    assert counts["Scroll"] == 0


def test_browser_screen_extents_override_css_overflow_style():
    browser = {"screens": [{
        "data": {
            "scroll": {"clientWidth": 1024, "clientHeight": 600,
                       "scrollWidth": 1024, "scrollHeight": 600},
            "nodes": [{"tag": "body", "style": {"overflow": "auto"}}],
        }
    }]}
    assert _browser_counts(browser)["Scroll"] == 0


def test_one_svg_filter_reference_and_nested_image_are_materialized(tmp_path: Path):
    source = tmp_path / "src"
    source.mkdir()
    public = tmp_path / "public" / "figma-assets"
    public.mkdir(parents=True)
    (public / "badge.png").write_bytes(b"asset")
    (source / "badge.svg").write_text(
        '<svg><image href="../public/figma-assets/badge.png" /></svg>', encoding="utf-8"
    )
    (source / "App.tsx").write_text(
        """export default function App() { return <svg><defs><filter id='glow'>
        <feGaussianBlur stdDeviation='4'/><feMerge><feMergeNode/></feMerge></filter></defs>
        <path d='M 0 0 A 10 10 0 0 1 20 20' filter='url(#glow)'/>
        <path d='M 0 0 A 10 10 0 0 1 20 20' filter='url(#glow)'/>
        <image href='./badge.svg'/></svg>; }""", encoding="utf-8"
    )
    model = analyze(tmp_path)
    app = next(item for item in model.components.values() if item.id.endswith(":App"))
    assert "src/badge.svg" in app.resource_ids
    assert "public/figma-assets/badge.png" in model.resources
    assert sum(1 for effect in model.visual_effects.values()
               if effect.filter_id == "glow" and effect.kind == "filter") == 1
    assert sum(1 for effect in model.visual_effects.values()
               if effect.filter_id == "glow" and effect.kind == "glow") == 1


def test_gauge_calls_and_side_arc_groups_keep_structural_instances(tmp_path: Path):
    source = tmp_path / "src"
    source.mkdir()
    (source / "App.tsx").write_text(
        """function Gauge({id}) { return <svg><circle data-openhmi-id={id}
        strokeDasharray='10 5' /></svg>; }
        export default function App() { return <div>
          <Gauge id='radial-left'/><Gauge id='radial-right'/>
          <svg><path data-openhmi-id='side-left' d='M 0 0 A 10 10 0 0 1 20 20'/>
          <path data-openhmi-id='side-right' d='M 40 0 A 10 10 0 0 1 60 20'/></svg>
        </div>; }""", encoding="utf-8"
    )
    model = analyze(tmp_path)
    counts = source_browser_plan_counts(model)["source"]
    assert counts["Gauge"] >= 2
    assert counts["Arc"] >= 2
