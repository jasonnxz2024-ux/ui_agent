import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.pipeline import analyze
from generator.compiler import compile_lvgl, write_aibuilder_custom


def _by_adapter(bundle, name):
    return [unit for unit in bundle.units if unit.adapter == name]


def test_energy_generates_native_table_and_select_code(tmp_path):
    bundle = compile_lvgl(analyze(Path(r"D:\aiassit\energy")))
    tables = _by_adapter(bundle, "TableAdapter")
    dropdowns = _by_adapter(bundle, "DropdownAdapter")

    assert tables
    assert dropdowns
    table = next(unit for unit in tables if unit.source_id.endswith("SummaryReportTable.tsx:SummaryReportTable"))
    assert table.decisions["table"]["headers"][:2] == ["Period", "Energy (kWh)"]
    assert table.decisions["table"]["row_count"] == 4
    assert "lv_table_set_col_cnt" in table.c_code
    assert "lv_table_set_cell_value" in table.c_code
    assert len({unit.symbol for unit in bundle.units}) == len(bundle.units)
    device_picker = next(unit for unit in dropdowns if unit.source_id.endswith("Analytics.tsx:Analytics#dropdown_1"))
    assert device_picker.decisions["dropdown"]["options"][0] == "Main Power Panel A"
    assert "lv_dropdown_set_options" in device_picker.c_code
    export_menu = next(unit for unit in dropdowns if unit.source_id.endswith("SummaryReportTable.tsx:SummaryReportTable#dropdown_1"))
    assert export_menu.decisions["dropdown"]["options"] == ["Export as Excel", "Export as PDF"]

    written = write_aibuilder_custom(bundle, tmp_path)
    audit = written["audit"].read_text(encoding="utf-8")
    assert '"adapter": "TableAdapter"' in audit
    assert '"adapter": "DropdownAdapter"' in audit


def test_test_project_generates_vertical_scroll_containers():
    bundle = compile_lvgl(analyze(Path(r"D:\aiassit\test")))
    scrolls = _by_adapter(bundle, "ScrollContainerAdapter")
    assert scrolls
    language_settings = next(unit for unit in scrolls if unit.source_id.endswith("LanguageSettings.tsx:LanguageSettings"))
    assert language_settings.decisions["scroll"]["axis"] == "y"
    assert "lv_obj_set_scroll_dir" in language_settings.c_code
    assert "LV_DIR_VER" in language_settings.c_code


def test_card_materializes_real_images_and_uses_uibuilder_macro(tmp_path):
    model = analyze(Path(r"D:\aiassit\card"))
    bundle = compile_lvgl(model)
    written = write_aibuilder_custom(bundle, tmp_path / "ui_builder" / "custom", model)

    images = [p for p in written["images"].glob("*.png") if not p.name.startswith("uagent_icon_")]
    assert len(images) == 4
    custom_c = written["source"].read_text(encoding="utf-8")
    assert "LVGL_IMAGE_PATH(uagent_" in custom_c
    assert 'lv_img_set_src(' in custom_c
    assert 'lv_img_set_src(src_app_components_carouselstack' in custom_c


def test_export_is_an_openable_uibuilder_project_not_only_custom_files(tmp_path):
    model = analyze(Path(r"D:\aiassit\test"))
    bundle = compile_lvgl(model)
    root = tmp_path / "printer_demo"
    written = write_aibuilder_custom(bundle, root / "ui_builder" / "custom", model)

    assert written["project"] == root / "printer_demo.aicpro"
    assert written["project"].is_file()
    assert written["snapshot"].is_file()
    assert (root / "config.ini").is_file()
    assert (root / "CMakeLists.txt").is_file()
    assert written["openhmi"].is_file()
    assert (root / "resources" / "font").is_dir()
    assert (root / "resources" / "video").is_dir()
    assert "<width>320</width>" in written["project"].read_text(encoding="utf-8")
    assert "void custom_init(void)" in written["source"].read_text(encoding="utf-8")


def test_chart_data_binding_and_random_seed_are_materialized():
    model = analyze(Path(r"D:\aiassit\energy"))
    analytics = model.components["src/app/components/Analytics.tsx:Analytics"]
    mock = next(item for item in analytics.props["arrays"] if item["name"] == "mockData")
    assert mock["length"] == 24
    assert mock["random_locked"] is True
    assert mock["items"][0]["kwh"] != mock["items"][1]["kwh"]

    bundle = compile_lvgl(model)
    charts = {unit.source_id.rsplit("#", 1)[-1]: unit for unit in _by_adapter(bundle, "RechartsAdapter")
              if "AnalyticsChart.tsx" in unit.source_id}
    assert len(charts["area"].decisions["series"]) == 1
    assert len(charts["line"].decisions["series"]) == 5
    assert len(charts["bar"].decisions["series"]) == 2
    assert "lv_chart_set_next_value" in charts["line"].c_code
    assert charts["line"].support == "native"
