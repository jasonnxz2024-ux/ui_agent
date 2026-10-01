import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.pipeline import analyze
from generator.compiler import compile_lvgl
from adapters.lvgl import adapt_components


def _write(root: Path, relative: str, text: str) -> None:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def test_real_jsx_controls_map_to_native_lvgl_without_ui_library_duplicate(tmp_path):
    _write(tmp_path, "src/main.tsx", "import App from './App'; createRoot(document.body).render(<App />);")
    _write(tmp_path, "src/components/ui/button.tsx", "export function Button({children}) { return <button>{children}</button>; }")
    _write(tmp_path, "src/App.tsx", """
      import hero from './hero.png';
      import { Button } from './components/ui/button';
      export default function App() {
        const [on, setOn] = useState(true);
        const [level, setLevel] = useState(40);
        const [choice, setChoice] = useState('fast');
        return <main>
          <Button onClick={() => setOn(!on)}><span>Apply</span></Button>
          <label>Power</label><img src={hero} alt="hero" />
          <Switch checked={on} onCheckedChange={setOn}/>
          <Checkbox checked={on} />
          <RadioGroup value={choice} onValueChange={setChoice}><RadioGroupItem value="fast"/><RadioGroupItem value="safe"/></RadioGroup>
          <Slider min={0} max={100} value={level} onValueChange={setLevel}/>
          <Input placeholder="Name"/><Textarea placeholder="Notes"/>
          <select value={choice} onChange={setChoice}><option>fast</option><option>safe</option></select>
          <List><li>One</li><li>Two</li></List>
          <Tabs value="overview"><TabsTrigger value="overview">Overview</TabsTrigger><TabsTrigger value="detail">Detail</TabsTrigger></Tabs>
        </main>;
      }
    """)
    (tmp_path / "src/hero.png").write_bytes(b"not-a-real-png-but-a-real-source-resource")

    bundle = compile_lvgl(analyze(tmp_path))
    code = bundle.custom_c
    types = [unit.widget_type for unit in bundle.units if unit.adapter == "GenericControlsAdapter"]

    assert types.count("lv_btn") == 1  # not one more from components/ui/button.tsx
    assert types.count("lv_label") == 1  # Button/tab/list captions stay owned by their native widget
    assert {"lv_label", "lv_img", "lv_switch", "lv_checkbox", "lv_btnmatrix", "lv_slider", "lv_textarea", "lv_dropdown", "lv_list", "lv_tabview"}.issubset(types)
    assert "lv_btn_create" in code
    assert "lv_img_set_src" in code
    assert "lv_switch_create" in code
    assert "lv_checkbox_create" in code
    assert "lv_btnmatrix_create" in code
    assert "lv_slider_set_range" in code
    assert "lv_textarea_set_placeholder_text" in code
    assert "lv_dropdown_set_options" in code
    assert "lv_list_add_btn" in code
    assert "lv_tabview_add_tab" in code

    button = next(unit for unit in bundle.units if unit.widget_type == "lv_btn")
    assert button.decisions["control"]["tag"] == "Button"
    assert button.decisions["control"]["events"] == ["onClick"]


def test_dynamic_image_is_not_lied_about_as_a_native_asset(tmp_path):
    _write(tmp_path, "src/main.tsx", "import App from './App'; createRoot(document.body).render(<App />);")
    _write(tmp_path, "src/App.tsx", "export default function App() { return <img src={remoteUrl} alt='remote' />; }")
    bundle = compile_lvgl(analyze(tmp_path))
    assert not [unit for unit in bundle.units if unit.widget_type == "lv_img"]


def test_lucide_react_icons_are_preserved_as_native_symbol_evidence(tmp_path):
    _write(tmp_path, "src/main.tsx", "import App from './App'; createRoot(document.body).render(<App />);")
    _write(tmp_path, "src/App.tsx", """
      import { Settings, Wrench } from 'lucide-react';
      export default function App() {
        const options = [{title: 'Basic', icon: Settings}, {title: 'Service', icon: Wrench}];
        return <main>{options.map(option => { const Icon = option.icon; return <button onClick={() => {}}><Icon />{option.title}</button>; })}</main>;
      }
    """)
    bundle = compile_lvgl(analyze(tmp_path))
    buttons = [unit for unit in bundle.units if unit.widget_type == "lv_btn"]
    assert buttons
    assert any(unit.decisions["control"].get("icon") == "Settings" for unit in buttons)
    assert "LV_SYMBOL_SETTINGS" in bundle.custom_c
    assert "□□" not in bundle.custom_c


def test_climate_hud_gauge_merges_jsx_circle_layers_and_visibility_is_partial():
    model = analyze(Path(r"D:\aiassit\climate"))
    hud = next(component for component in model.components.values() if component.id.endswith(":HudGauge"))
    assert len(hud.props["svg_arcs"]) == 1
    assert hud.props["svg_arcs"][0]["paint_count"] == 4
    assert hud.props["svg_arcs"][0]["geometry"]["cx"] == 54
    assert hud.props["svg_arcs"][0]["geometry"]["cy"] == 54
    bright = next(component for component in model.components.values() if component.id.endswith(":BrightBar"))
    units = adapt_components(bright, model)
    assert units and all(unit.support == "partial" for unit in units)
    assert any("条件可见性" in warning for unit in units for warning in unit.warnings)
    glow = next(component for component in model.components.values() if component.id.endswith(":GlowToggle"))
    assert any(unit.widget_type == "lv_switch" for unit in adapt_components(glow, model))
    app = next(component for component in model.components.values() if component.id.endswith(":App"))
    assert any(control.get("kind") == "button" for control in app.props.get("controls", []))
    bundle = compile_lvgl(model)
    gauges = [unit for unit in bundle.units if unit.adapter == "GaugeAdapter"]
    assert len(gauges) == 2
    assert {unit.decisions["gauges"][0].get("label") for unit in gauges} == {"TEMP", "HUMID"}
