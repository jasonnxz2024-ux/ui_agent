import sys
import xml.etree.ElementTree as ET
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.pipeline import analyze
from core.preflight import inspect_source
from generator.compiler import compile_lvgl, write_aibuilder_custom
from generator.project import _IdAllocator, _control_widget, _sync_existing_uibuilder_geometry
from core.model import ReactProjectModel


def test_existing_uibuilder_screen_geometry_follows_snapshot(tmp_path):
    snapshot = tmp_path / "demo.snapshot"
    snapshot.write_text('<ailv-app><Screen name="screen_01_main"><Widgets>'
                        '<Widget name="screen_01_main_label" type="1"><Normal>'
                        '<postion>12,13</postion><size>80,22</size></Normal>'
                        '<Attribute><text>New text</text></Attribute></Widget>'
                        '</Widgets></Screen></ailv-app>', encoding="utf-8")
    screen_c = tmp_path / "ui_builder" / "screen_01_main.c"
    screen_c.parent.mkdir()
    screen_c.write_text('scr->screen_01_main_label = lv_label_create(parent);\n'
                        'lv_label_set_text(scr->screen_01_main_label, "Old text");\n'
                        'lv_obj_set_pos(scr->screen_01_main_label, 10, 11);\n'
                        'lv_obj_set_size(scr->screen_01_main_label, 60, 20);\n'
                        'scr->obsolete_readout = lv_label_create(parent);\n', encoding="utf-8")
    model = ReactProjectModel(tmp_path)
    _sync_existing_uibuilder_geometry(tmp_path, snapshot, model)
    code = screen_c.read_text(encoding="utf-8")
    assert "lv_obj_set_pos(scr->screen_01_main_label, 12, 13);" in code
    assert "lv_obj_set_size(scr->screen_01_main_label, 80, 22);" in code
    assert 'lv_label_set_text(scr->screen_01_main_label, "New text");' in code
    assert "lv_obj_add_flag(scr->obsolete_readout, LV_OBJ_FLAG_HIDDEN);" in code
    assert not model.warnings


def _write(root: Path, relative: str, text: str) -> None:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def test_route_screen_ir_is_shared_by_audit_and_uibuilder_snapshot(tmp_path):
    source = tmp_path / "source"
    output = tmp_path / "output"
    _write(source, "src/main.tsx", "import App from './App'; createRoot(document.body).render(<App />);")
    _write(source, "src/Navigation.tsx", "export function Navigation(){return <nav><button>Home</button></nav>}")
    _write(source, "src/HomePage.tsx", "export function HomePage(){return <main><span>待机</span><button>纸张</button></main>}")
    _write(source, "src/PaperPage.tsx", "export function PaperPage(){return <main><span>纸张状态</span></main>}")
    _write(source, "src/UnusedHelper.tsx", "export function UnusedHelper(){return <span>helper</span>}")
    _write(source, "src/App.tsx", """
      import { Navigation } from './Navigation';
      import { HomePage } from './HomePage';
      import { PaperPage } from './PaperPage';
      export default function App() {
        const currentPage = 'home';
        const renderPage = () => {
          switch (currentPage) {
            case 'paper': return <PaperPage />;
            default: return <HomePage />;
          }
        };
        return <div><Navigation />{renderPage()}</div>;
      }
    """)

    model = analyze(source)
    bundle = compile_lvgl(model)
    paths = write_aibuilder_custom(bundle, output / "ui_builder" / "custom", model=model)
    root = ET.parse(paths["snapshot"]).getroot()
    screens = root.findall("Screen")

    assert [screen.get("name") for screen in screens] == ["screen_01_home", "screen_02_paper"]
    assert [screen["route"] for screen in bundle.screen_tree["screens"]] == ["home", "paper"]
    assert all(screen.find("Widgets") is not None and len(screen.findall("Widgets")) == 1 for screen in screens)

    widgets = root.findall(".//Widget")
    ids = [widget.get("id") for widget in widgets]
    assert len(ids) == len(set(ids))
    known = set(ids)
    assert any(widget.get("parent-id") for widget in widgets)
    assert all(widget.get("parent-id") in known for widget in widgets if widget.get("parent-id"))

    chinese_widgets = [
        widget for widget in widgets
        if any("\u2e80" <= char <= "\u9fff" for char in (widget.findtext("Attribute/text") or ""))
    ]
    assert chinese_widgets
    assert all(widget.findtext("Style/Part/State/text-font-path") == "DroidSansFallback.ttf"
               for widget in chinese_widgets)


def test_select_exports_as_native_uibuilder_dropdown_with_all_visual_parts():
    xml = _control_widget(
        {
            "kind": "select",
            "value": "Individual Device",
            "items": ["Individual Device", "Virtual Group"],
            "className": "border border-input rounded-lg text-foreground",
        },
        "filter_type",
        (10, 20, 160, 32),
        0,
        100,
        _IdAllocator(),
        "content",
    )
    widget = ET.fromstring(xml)

    assert widget.get("type") == "7"
    assert [item.text for item in widget.findall("Attribute/options/option")] == [
        "Individual Device", "Virtual Group"
    ]
    assert widget.findtext("Attribute/is-open") == "0"
    parts = {part.get("name"): part for part in widget.findall("Style/Part")}
    assert {"Main", "Selected", "List", "Scrollbar"}.issubset(parts)
    assert parts["Main"].findtext("State/bg-color") == "#FFFFFF"
    assert parts["List"].findtext("State/bg-color") == "#FFFFFF"
    assert parts["Selected"].findtext("State/bg-color") == "#D9F8E2"


def test_local_state_navigation_builds_verified_screens_and_callbacks(tmp_path):
    source = tmp_path / "state-navigation"
    _write(source, "package.json", '{"scripts":{"dev":"vite"}}')
    _write(source, "src/main.tsx", "import App from './App'; createRoot(document.body).render(<App />);")
    _write(source, "src/App.tsx", """
      function Dashboard(){ return <main>DASH</main> }
      function Settings(){ return <main>SET</main> }
      export default function App(){
        const [page, setPage] = useState<'dash' | 'settings'>('dash');
        return <div>
          {page === 'dash' ? <Dashboard /> : <Settings />}
          <nav>
            <button aria-label="Dashboard" data-page="dashboard" onClick={() => setPage('dash')}>DASH</button>
            <button aria-label="Settings" data-page="settings" onClick={() => setPage('settings')}>SET</button>
          </nav>
        </div>;
      }
    """)

    model = analyze(source)
    assert [(edge.trigger, edge.target_route, edge.target_component) for edge in model.navigation] == [
        ("Dashboard", "dash", "Dashboard"), ("Settings", "settings", "Settings")
    ]
    bundle = compile_lvgl(model)
    assert [screen["route"] for screen in bundle.screen_tree["screens"]] == ["dash", "settings"]
    assert "uagent_bind_image_nav_tree" in bundle.custom_c
    assert "screen_02_settings_get(&ui_manager)->obj" in bundle.custom_c
    preflight = inspect_source(source)
    assert preflight["navigation"]["confirmed"] == 2
    assert "navigation" in preflight["capabilities"]
