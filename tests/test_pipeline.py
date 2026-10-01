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


def test_effect_accumulator_readonly_state_and_conditional_string_setter(tmp_path):
    import pytest
    from core.reactive_contract import extract_reactive_contract, resolve_actions, source_view_bindings
    from generator.lvgl import _runtime_expression, _runtime_text_format
    parser=Path(r'D:\aiassit\meter\node_modules\typescript\lib\typescript.js')
    if not parser.is_file():pytest.skip('Existing local TypeScript parser required')
    (tmp_path/'App.tsx').write_text('''function App(){
      const [fixed]=useState(7);const [speed,setSpeed]=useState(0);const [mode,setMode]=useState("idle");
      useEffect(()=>{let phase=0;const id=setInterval(()=>{phase+=0.25;
        const wave=Math.sin(phase);setSpeed(Math.round(wave*10));
        if(wave<0)setMode("negative");else if(wave<0.5)setMode("low");else setMode("high");
      },80);return()=>clearInterval(id);},[]);
      return <div><path d="M0,0 L1,1"/><span>{speed}</span></div>;
    }''',encoding='utf-8')
    contract=extract_reactive_contract(tmp_path,['App.tsx'],parser_module=parser)
    assert not contract['blockers']
    assert contract['states'][0]['setter'] is None
    actions=resolve_actions(contract['timers'][0]['callback'],contract)
    accumulator=next(a for a in actions if a['state'].startswith('effect_'))
    assert accumulator['value'][2][0]=='state'  # Old snapshot must not be substituted twice.
    speed=next(a for a in actions if a['state']=='speed')
    assert _runtime_expression(speed['value'],contract).count('0.25')==1
    mode=next(a for a in actions if a['state']=='mode')
    assert _runtime_text_format([mode['value']],contract)[0]=='%s'
    path=next(n for n in contract['jsx'] if n['tag']=='path')
    assert path['children']==[]
    assert not any(v.get('source_id')==f'App.tsx:{path["start"]}' for v in source_view_bindings(contract))


def test_runtime_computed_paint_recipes_and_js_rounding():
    from generator.lvgl import _runtime_gradient, _runtime_shadows, _runtime_expression
    gradient=_runtime_gradient('radial-gradient(at 50% 40%, rgb(13, 16, 32) 0%, rgb(7, 8, 16) 55%, rgb(2, 3, 6) 100%)')
    assert gradient['center']==(.5,.4)
    assert [s['position'] for s in gradient['stops']]==[0,.55,1]
    assert _runtime_gradient('conic-gradient(red,blue)') is None
    assert _runtime_gradient('linear-gradient(45deg, rgb(0,0,0), rgb(255,255,255))') is None
    shadows=_runtime_shadows('rgba(0, 0, 0, 0.8) 0px 0px 60px 0px, rgba(255, 255, 255, 0.06) 0px 0px 1px 0px inset')
    assert len(shadows)==2 and shadows[1]['inset']
    assert _runtime_expression(['call',['member',['id','Math'],['literal','round']],[['literal',-1.5]]],{})=='floor((-1.5)+0.5)'


def test_conversion_rejects_source_output_alias_without_writing(tmp_path):
    import pytest
    from tools.runtime_convert import convert
    source=tmp_path/'source';source.mkdir();(source/'keep.txt').write_text('unchanged',encoding='utf-8')
    for output in (source,source/'child',tmp_path):
        with pytest.raises(ValueError,match='disjoint'):convert(source,output,480,272)
    assert list(source.iterdir())==[source/'keep.txt']
    output=tmp_path/'old-output';output.mkdir();(output/'conversion-report.json').write_text('history',encoding='utf-8')
    with pytest.raises(ValueError,match='already contains'):convert(source,output,480,272)
    assert (output/'conversion-report.json').read_text()=='history'


def test_effect_completeness_requires_source_bound_classified_node():
    from copy import deepcopy
    from generator.compiler import _merge_runtime_effect_decisions
    message='Runtime capture contains a VisualEffect with no source-backed effect node.'
    plan={'counts':{'VisualEffect':{'source':0,'browser':1,'planned':0}},'summary':{},
          'blockers':['Completeness blocker: VisualEffect browser=1 > planned=0',message],
          'decisions':[{'node_id':'browser:2:filter','kind':'VisualEffect','support':'unsupported','blockers':[message],
                        'properties':{'source_id':'browser:2:filter','screen_index':0,'kind':'filter','value':'url("#halo")','bounds':[0,0,20,10]}}]}
    node={'id':4,'attrs':{'data-uagent-source':'App.tsx:10'},'rect':{'x':0,'y':0,'width':20,'height':10},'style':{'filter':'url("#halo")'}}
    captures=[{'data':{'runtime_scene':{'nodes':[node]}}}]
    runtime={'nodes':[{'screen':0,'node':4,'support':'custom','reason':'Gaussian merge'}],'blockers':[]}
    missing=deepcopy(plan);_merge_runtime_effect_decisions(missing,runtime,captures,{'jsx':[]})
    assert missing['blockers']==plan['blockers']
    classified=deepcopy(plan);_merge_runtime_effect_decisions(classified,runtime,captures,{'jsx':[{'file':'App.tsx','start':10}]})
    assert classified['blockers']==[] and classified['counts']['VisualEffect']['planned']==1
    runtime['blockers']=['0:4: unknown filter primitive']
    blocked=deepcopy(plan);_merge_runtime_effect_decisions(blocked,runtime,captures,{'jsx':[{'file':'App.tsx','start':10}]})
    assert blocked['blockers']==plan['blockers']
