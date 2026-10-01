from pathlib import Path

from core.pipeline import analyze
from generator.project import _dom_fallback_controls, _snapshot_screens


def test_browser_svg_image_is_rasterized_and_materialized_as_type5(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "public" / "figma-assets").mkdir(parents=True)
    (tmp_path / "public" / "figma-assets" / "lamp.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="12" height="12">'
        '<circle cx="6" cy="6" r="5" fill="#0c0"/></svg>', encoding="utf-8"
    )
    (tmp_path / "src" / "App.tsx").write_text(
        "export default function App(){return <main><img src='/figma-assets/lamp.svg' /></main>}",
        encoding="utf-8"
    )
    model = analyze(tmp_path)
    asset_dir = tmp_path / "resources" / "image"
    controls = _dom_fallback_controls({"nodes": [{
        "index": 2, "tag": "image", "rect": {"x": 4, "y": 5, "width": 12, "height": 12},
        "href": None, "src": None,
        "attrs": {"href": "/figma-assets/lamp.svg", "data-openhmi-id": "status-lamp"}
    }]}, asset_dir=asset_dir, model=model)
    assert controls and controls[0]["kind"] == "image"
    assert controls[0]["identity_kind"] == "status"
    assert controls[0]["src"].endswith(".png")
    png = Path(asset_dir / controls[0]["src"])
    assert png.is_file()
    from PIL import Image
    image = Image.open(png).convert("RGBA")
    assert image.getextrema()[3][0] < 255
    assert len(set(image.getdata())) > 1

    component = next(iter(model.components.values()))
    component.props["controls"] = controls
    snapshot = _snapshot_screens(model, 32, 24, {
        "screens": [], "data": {"root": {"x": 0, "y": 0, "width": 32, "height": 24},
        "nodes": []}
    }, asset_dir)
    assert 'type="5"' in snapshot
    assert "reference-end" not in snapshot
