import json
import importlib.util
from pathlib import Path
import generator.project as project_module
from generator.project import _copy_uibuilder_runtime

_SPEC = importlib.util.spec_from_file_location("cross_validate_runner", Path(__file__).parents[1] / "tools" / "cross_validate.py")
runner = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(runner)


def test_simulator_build_always_reconfigures_stale_cached_tree(tmp_path, monkeypatch):
    simulator = tmp_path / "simulator"
    build = simulator / "build"
    build.mkdir(parents=True)
    (build / "CMakeCache.txt").write_text("CMAKE_GENERATOR:INTERNAL=Ninja", encoding="utf-8")
    calls = []

    class Result:
        returncode = 0
        stdout = "ok"

    def fake_run(command, **kwargs):
        calls.append(command)
        if "--build" in command:
            (build / "main.exe").write_bytes(b"MZ")
        return Result()

    compiler = tmp_path / "toolchain" / "gcc.exe"
    compiler.parent.mkdir()
    compiler.write_bytes(b"MZ")
    monkeypatch.setattr(runner, "_discover_c_compiler", lambda: compiler)
    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    executable, error = runner._build_snapshot_simulator(tmp_path)
    assert error is None
    assert executable == build / "main.exe"
    assert calls[0][1:3] == ["-S", str(simulator)]
    assert f"-DCMAKE_C_COMPILER={compiler}" in calls[0]


def test_round_comparison_embeds_browser_and_simulator_images(tmp_path):
    evidence = tmp_path / "openhmi" / "validation"
    reference = evidence / "reference"
    simulator = evidence / "simulator"
    reference.mkdir(parents=True)
    simulator.mkdir()
    (reference / "01.png").write_bytes(b"browser-png")
    (simulator / "01.png").write_bytes(b"simulator-png")
    report = evidence / "report.json"
    report.write_text("{}", encoding="utf-8")
    comparison = runner._round_comparison({"report_file": str(report)})
    assert comparison["browser_image"].startswith("data:image/png;base64,")
    assert comparison["simulator_image"].startswith("data:image/png;base64,")


def test_cross_validate_isolates_source_and_requires_simulator(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    marker = source / "App.jsx"
    marker.write_text("export default function App(){return <div>fixture</div>}", encoding="utf-8")
    output = tmp_path / "out"
    calls = {}

    monkeypatch.setattr(runner, "capture_layout", lambda staging, **_: {
        "status": "browser-interaction", "screens": [{"data": {"nodes": []}}]
    })
    monkeypatch.setattr(runner, "analyze", lambda staging: calls.setdefault("staging", Path(staging)) or object())
    monkeypatch.setattr(runner, "compile_lvgl", lambda model: calls.setdefault("compiled", True) or object())

    def fake_write(bundle, destination, *, model, include_runtime):
        calls["include_runtime"] = include_runtime
        destination.mkdir(parents=True)
        report = destination.parent.parent / "openhmi" / "validation" / "report.json"
        report.parent.mkdir(parents=True)
        report.write_text(json.dumps({"status": "failed", "completed": False,
                                      "stages": [], "errors": ["simulator_missing"]}), encoding="utf-8")
        return {"source": destination / "custom.c"}

    monkeypatch.setattr(runner, "write_aibuilder_custom", fake_write)
    result = runner.cross_validate(source, output)

    assert result["staging_used"] != str(source.resolve())
    assert Path(result["staging_used"]).is_relative_to(output / "openhmi" / "runtime")
    assert not list(source.glob("browser-layout*"))
    assert calls["include_runtime"] is True
    assert result["validation"]["status"] != "passed"
    saved = json.loads((output / "cross-validation.json").read_text(encoding="utf-8"))
    for key in ("source", "staging_used", "output", "browser", "validation", "generated_files"):
        assert key in saved


def test_snapshot_harness_is_standalone_and_uses_dynamic_canvas(tmp_path):
    (tmp_path / "simulator").mkdir()
    (tmp_path / "simulator" / "lv_conf.h").write_text(
        "#define LV_USE_FREETYPE 1\n#define LV_USE_FFMPEG 1\n", encoding="utf-8")
    snapshot = tmp_path / "screen.snapshot"
    snapshot.write_text(
        '<ailv-app><Screen><Widget type="28" id="root">'
        '<Normal><postion>0,0</postion><size>1024,600</size></Normal><Attribute/>'
        '</Widget></Screen></ailv-app>', encoding="utf-8")
    paths = runner.generate_snapshot_harness(tmp_path, snapshot)
    assert paths["aic_ui"].is_file()
    assert paths["source"].is_file()
    assert paths["main"].is_file()
    assert 'lv_sdl_window_create(1024, 600)' in paths["main"].read_text(encoding="utf-8")
    header = paths["aic_ui"].read_text(encoding="utf-8")
    assert "LVGL_IMAGE_PATH" in header
    assert "LVGL_STORAGE_PATH" in header
    assert "#define LV_USE_FREETYPE 0" in paths["lv_conf"].read_text(encoding="utf-8")
    assert "LV_CONF_PATH=lv_conf_uagent.h" in paths["cmake"].read_text(encoding="utf-8")


def test_snapshot_harness_stringizes_hyphenated_image_and_uses_resource_dir(tmp_path):
    snapshot = tmp_path / "screen.snapshot"
    snapshot.write_text(
        '<ailv-app><Screen><Widget type="5" id="lamp"><Normal><postion>0,0</postion>'
        '<size>16,16</size></Normal><Attribute><src>uagent_dom_image_parking-lights_ab12.png</src>'
        '</Attribute></Widget></Screen></ailv-app>', encoding="utf-8")
    paths = runner.generate_snapshot_harness(tmp_path, snapshot)
    source = paths["source"].read_text(encoding="utf-8")
    header = paths["aic_ui"].read_text(encoding="utf-8")
    cmake = paths["cmake"].read_text(encoding="utf-8")
    assert "LVGL_IMAGE_PATH(uagent_dom_image_parking-lights_ab12.png)" in source
    assert "LVGL_DIR UAGENT_STRINGIZE(path)" in header
    assert 'LVGL_DIR="L:' in cmake
    assert "../resources/image/" in cmake
    assert "../ui_builder/assets/" not in cmake


def test_generated_runtime_enables_lodepng_decoder(tmp_path, monkeypatch):
    install = tmp_path / "uibuilder"
    package = install / "tool" / "simulator" / "lvgl" / "9.1.0"
    (package / "lvgl").mkdir(parents=True)
    (install / "tool" / "simulator" / "ffmpeg").mkdir(parents=True)
    (package / "lv_conf.h").write_text("#define LV_USE_LODEPNG 0\n#define LV_USE_FFMPEG 1\n#define LV_USE_FREETYPE 0\n", encoding="utf-8")
    monkeypatch.setattr(project_module, "_uibuilder_root", lambda: install)
    ready = _copy_uibuilder_runtime(tmp_path / "output")
    assert ready
    conf = (tmp_path / "output" / "simulator" / "lv_conf.h").read_text(encoding="utf-8")
    assert "#define LV_USE_LODEPNG 1" in conf
    assert "#define LV_USE_FREETYPE 1" in conf
