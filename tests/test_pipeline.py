import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.pipeline import analyze
from generator.compiler import reachable_components


def test_card_resources_and_events():
    model = analyze(Path(r"D:\aiassit\card"))
    assert len(model.resources) >= 4
    assert any(component.kind == "carousel" for component in model.components.values())
    assert any(event.kind == "drag" for event in model.events.values())


def test_energy_semantics():
    model = analyze(Path(r"D:\aiassit\energy"))
    kinds = {component.kind for component in model.components.values()}
    assert "chart" in kinds
    assert "dropdown" in kinds
    assert "table" in kinds


def test_component_graph_and_component_scoped_evidence():
    model = analyze(Path(r"D:\aiassit\energy"))
    analytics = next(component for component in model.components.values()
                     if component.id.endswith("Analytics.tsx:Analytics"))
    chart = next(component for component in model.components.values()
                 if component.id.endswith("AnalyticsChart.tsx:AnalyticsChart"))
    assert analytics.kind != "chart"  # importing a chart is not rendering one
    assert len(analytics.props["state"]) == 3
    assert chart.id in model.component_graph[analytics.id]
    assert {item["data_key"] for item in chart.props["chart"]["series"]} >= {"kwh", "kw", "kvah"}


def test_concise_arrow_component_does_not_swallow_following_layout():
    model = analyze(Path(r"D:\aiassit\energy"))
    layout_id = "src/app/components/Layout.tsx:Layout"
    app_id = "src/app/App.tsx:App"
    assert layout_id in model.components
    assert model.component_graph[app_id] == [layout_id]
    assert any(child.endswith("Analytics.tsx:Analytics") for child in model.component_graph[layout_id])
    reachable = reachable_components(model)
    assert layout_id in reachable
    assert "src/app/components/AnalyticsChart.tsx:AnalyticsChart" in reachable
    assert "src/app/components/ui/accordion.tsx:Accordion" not in reachable


def test_expression_arrow_entry_is_not_forced_into_scan_all_fallback():
    model = analyze(Path(r"D:\aiassit\test"))
    assert any(component.id.endswith(":App") for component in model.components.values())
    assert len(reachable_components(model)) < len(model.components)


def test_carousel_binds_imported_data_and_assets():
    model = analyze(Path(r"D:\aiassit\card"))
    carousel = next(component for component in model.components.values()
                    if component.id.endswith("CarouselStack.tsx:CarouselStack"))
    assert carousel.props["carousel"]["item_source"] == "items"
    assert carousel.props["data"]["length"] == 4
    assert len(carousel.resource_ids) == 4
    assert any(model.events[event_id].kind == "drag" for event_id in carousel.event_ids)
