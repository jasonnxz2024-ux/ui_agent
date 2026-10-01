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
    reachable = reachable_components(model)
    assert any(component_id.endswith(":App") for component_id in reachable)
    assert set(reachable) <= set(model.components)


def test_carousel_binds_imported_data_and_assets():
    model = analyze(Path(r"D:\aiassit\card"))
    carousel = next(component for component in model.components.values()
                    if component.id.endswith("CarouselStack.tsx:CarouselStack"))
    assert carousel.props["carousel"]["item_source"] == "items"
    assert carousel.props["data"]["length"] == 4
    assert len(carousel.resource_ids) == 4
    assert any(model.events[event_id].kind == "drag" for event_id in carousel.event_ids)


def test_remote_model_apis_are_disabled_and_local_planning_remains_available():
    from core import model_provider
    from core.planning_agent import RuntimeFirstPlanningAgent

    settings = model_provider.ModelSettings(enabled=True, agent_enabled=True)
    assert not settings.agent_is_enabled
    assert settings.api_key() is None
    assert model_provider.PROVIDER_PRESETS == {}
    assert not hasattr(model_provider, "httpx")
    assert model_provider.list_available_models(settings)["status"] == "disabled"
    assert model_provider.test_model_connection(settings)["status"] == "disabled"
    assert model_provider.analyze_capability_candidates(settings, [])["status"] == "disabled"
    assert RuntimeFirstPlanningAgent().name == "runtime-first-local"


def test_project_scaffold_reuses_cached_browser_evidence_without_capture(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from generator import project

    evidence = {"status": "browser-interaction", "screens": [{"index": 0}]}
    model = SimpleNamespace(source_dir=tmp_path, browser_evidence=evidence, add_warning=lambda _msg: None)
    monkeypatch.setattr(project, "capture_layout", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("capture must not run")))
    assert project._browser_evidence_for_scaffold(model) is evidence

    source_only = SimpleNamespace(source_dir=tmp_path, browser_evidence={}, add_warning=lambda _msg: None)
    assert project._browser_evidence_for_scaffold(source_only) is None


def test_reactive_contract_resolves_prop_callbacks_and_preserves_partial_reset(tmp_path):
    import pytest
    from core.reactive_contract import extract_reactive_contract, source_control_bindings, resolve_actions

    parser = Path(r'D:\aiassit\meter\node_modules\typescript\lib\typescript.js')
    if not parser.is_file():
        pytest.skip('Requires the existing sample TypeScript parser; no dependency is installed by the test')
    (tmp_path/'App.tsx').write_text('''
      function Panel({prefs,onCommit}) { return <div>
        <input aria-label="Gain" value={prefs.gain} onChange={e=>onCommit("gain",Number(e.target.value))}/>
        <Toggle id="armed" checked={prefs.armed} onChange={v=>onCommit("armed",v)}/>
        <button aria-label="Reset gain" onClick={()=>{onCommit("gain",17);}}>Reset</button>
      </div>; }
      function App() {
        const [prefs,setPrefs]=useState({gain:17,armed:true});
        const commit=useCallback((key,value)=>{setPrefs(old=>({...old,[key]:value}));},[]);
        useEffect(()=>{const id=setInterval(()=>{setPrefs(old=>({...old,gain:Math.min(99,old.gain+2)}));},250);return()=>clearInterval(id);},[]);
        useEffect(()=>{const tick=()=>setPrefs(old=>({...old,gain:old.gain+1}));tick();const id=setInterval(tick,1000);return()=>clearInterval(id);},[]);
        useEffect(()=>{const later=()=>setPrefs(old=>({...old,gain:old.gain+1}));const id=setInterval(later,2000);return()=>clearInterval(id);},[]);
        return <Panel prefs={prefs} onCommit={commit}/>;
      }
    ''',encoding='utf-8')
    contract=extract_reactive_contract(tmp_path,['App.tsx'],parser_module=parser)
    controls={control['identity']:control for control in source_control_bindings(contract)}
    assert controls['Gain']['actions']==[{'state':'prefs','field':'gain','value':['event']}]
    assert controls['armed']['actions']==[{'state':'prefs','field':'armed','value':['event']}]
    assert controls['Reset gain']['actions']==[{'state':'prefs','field':'gain','value':['literal',17]}]
    timer=resolve_actions(contract['timers'][0]['callback'],contract)
    assert timer[0]['field']=='gain'
    assert ['member',['id','prefs'],['literal','gain']] in timer[0]['value'][2][1][2:]
    assert [timer['immediate'] for timer in contract['timers']]==[False,True,False]


def test_unresolved_reactive_callback_is_a_blocker():
    from core.reactive_contract import source_control_bindings
    contract={'functions':{},'jsx':[{'tag':'button','attrs':{'aria-label':['literal','Send'],'onClick':['id','externalSend']},'children':[],'file':'App.tsx','start':12}]}
    controls=source_control_bindings(contract)
    assert controls[0]['blocker']=='Unresolved event callback'
    assert controls[0]['actions']==[]


def test_runtime_scene_retains_unsupported_nodes_and_blocks_incomplete_export():
    from types import SimpleNamespace
    from generator.lvgl import runtime_scene_bundle
    node={'id':0,'parent':None,'tag':'canvas','attrs':{},'rect':{'x':0,'y':0,'width':40,'height':30},
          'style':{'display':'block','visibility':'visible','overflowX':'hidden','overflowY':'hidden','opacity':'1'},'texts':[]}
    model=SimpleNamespace(browser_evidence={'screens':[{'state':'home','data':{'runtime_scene':{'schema':'uagent.runtime-scene/v1','viewport':{'width':40,'height':30},'nodes':[node],'fonts':{}}}}]})
    result=runtime_scene_bundle(model,{'reactive_contract':{}})
    assert len(result.units)==1
    assert result.units[0].support=='unsupported'
    assert result.screen_tree['runtime_scene']['blockers']
    assert '#define UAGENT_RUNTIME_OWNS_SCENE 1' in result.custom_h


def test_runtime_harness_does_not_replay_the_snapshot_or_infer_canvas_from_children(tmp_path):
    from tools.cross_validate import generate_snapshot_harness
    custom=tmp_path/'ui_builder/custom';custom.mkdir(parents=True)
    (custom/'custom.h').write_text('#define UAGENT_RUNTIME_OWNS_SCENE 1\n',encoding='utf-8')
    (custom/'custom.c').write_text('void custom_init(void) {}',encoding='utf-8')
    (tmp_path/'demo.aicpro').write_text('<ai_lvgl><resolution><width>480</width><height>272</height></resolution></ai_lvgl>',encoding='utf-8')
    snapshot=tmp_path/'demo.snapshot'
    snapshot.write_text('<ailv-app><Screen><Widgets><Widget type="999"/></Widgets></Screen></ailv-app>',encoding='utf-8')
    paths=generate_snapshot_harness(tmp_path,snapshot)
    assert 'snapshot_0' not in paths['source'].read_text(encoding='utf-8')
    main=paths['main'].read_text(encoding='utf-8')
    assert 'lv_sdl_window_create(480, 272)' in main
    assert '#ifndef UAGENT_RUNTIME_OWNS_SCENE\n    uagent_snapshot_build(screen);\n#endif' in main
