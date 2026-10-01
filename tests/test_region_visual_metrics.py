from PIL import Image, ImageDraw

from core.dynamic_validation import region_manifest
from core.validation import _is_blank_frame, region_visual_metrics


def test_blank_frame_detection_handles_black_and_rendered_images(tmp_path):
    blank = tmp_path / "blank.png"
    rendered = tmp_path / "rendered.png"
    Image.new("RGB", (32, 24), (0, 0, 0)).save(blank)
    image = Image.new("RGB", (32, 24), (0, 0, 0))
    ImageDraw.Draw(image).rectangle((2, 2, 8, 8), fill=(20, 20, 20))
    image.save(rendered)
    assert _is_blank_frame(blank)
    assert not _is_blank_frame(rendered)


def test_missing_gauge_fails_even_when_global_background_matches(tmp_path):
    ref = Image.new("RGB", (100, 80), "white")
    ImageDraw.Draw(ref).rectangle((35, 20, 64, 49), fill="red")
    got = Image.new("RGB", (100, 80), "white")
    ref_path, got_path = tmp_path / "ref.png", tmp_path / "got.png"
    ref.save(ref_path)
    got.save(got_path)
    manifest = region_manifest([{"tag": "svg", "role": "meter",
                                "rect": {"x": 35, "y": 20, "width": 30, "height": 30}}],
                               {"width": 100, "height": 80})
    metrics = region_visual_metrics(ref_path, got_path, manifest)
    assert metrics["gauge"]["passed"] is False
    assert metrics["gauge"]["scores"]["coverage"] > 0
    assert metrics["header"]["status"] == "N/A"
    assert metrics["control"]["status"] == "N/A"


def test_status_identity_and_svg_image_are_manifested():
    manifest = region_manifest([
        {"tag": "img", "attrs": {"src": "warning.svg"},
         "rect": {"x": 2, "y": 2, "width": 8, "height": 8}},
    ], {"width": 20, "height": 20})
    assert manifest["regions"]["status"]["status"] == "measured"
    assert manifest["regions"]["status"]["boxes"]
    assert manifest["regions"]["status"]["union_bounds"]["width"] == 8


def test_full_canvas_svg_container_uses_semantic_gauge_children():
    manifest = region_manifest([
        {"index": 0, "tag": "svg", "rect": {"x": 0, "y": 0, "width": 100, "height": 80}},
        {"index": 1, "parentIndex": 0, "tag": "g", "className": "gauge-left",
         "rect": {"x": 10, "y": 20, "width": 20, "height": 20}},
        {"index": 2, "parentIndex": 0, "tag": "path", "className": "arc-right",
         "attrs": {"stroke-dasharray": "10 20"},
         "rect": {"x": 70, "y": 20, "width": 20, "height": 20}},
    ], {"width": 100, "height": 80})
    gauge = manifest["regions"]["gauge"]
    assert gauge["union_bounds"]["width"] == 80
    assert gauge["union_bounds"]["height"] == 20
    assert all(box["width"] < 100 for box in gauge["boxes"])
