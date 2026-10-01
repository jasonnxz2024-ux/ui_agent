from core.scanner import scan


def test_component_calls_with_nested_jsx_props_are_instances(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    (source / "App.tsx").write_text(
        'export function Gauge(p){return <svg/>}\n'
        'export function App(){return <main>'
        '<Gauge id="GAUGE-SPEED" footer={<text x={1}>S</text>} />'
        '<Gauge id="GAUGE-RPM" footer={<text x={2}>R</text>} />'
        '</main>}', encoding="utf-8")
    model = scan(tmp_path)
    gauges = [item for item in model.instances.values() if item.tag == "Gauge"]
    assert len(gauges) == 2
    assert {item.semantic_id for item in gauges} == {"GAUGE-SPEED", "GAUGE-RPM"}
    assert gauges[0].props_raw["footer"].startswith("<text")
