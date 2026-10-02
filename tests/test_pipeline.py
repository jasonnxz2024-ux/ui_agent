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


def _svg_scene(nodes, contract=None):
    from types import SimpleNamespace
    from generator.lvgl import runtime_scene_bundle
    model=SimpleNamespace(browser_evidence={'screens':[{'state':'home','data':{'runtime_scene':{
        'schema':'uagent.runtime-scene/v1','viewport':{'width':80,'height':80},'nodes':nodes,'fonts':{}}}}]})
    return runtime_scene_bundle(model,{'reactive_contract':contract or {}})


def _svg_node(index, tag, parent=None):
    return {'id':index,'parent':parent,'tag':tag,'attrs':{},'texts':[],
            'rect':{'x':10,'y':10,'width':20,'height':20},
            'style':{'display':'block','visibility':'visible','overflowX':'visible','overflowY':'visible',
                     'opacity':'1','backgroundColor':'transparent','borderRadius':'0','fill':'none',
                     'stroke':'rgb(0, 128, 255)','strokeWidth':'2','strokeLinecap':'round'}}


def test_svg_original_asset_retains_children_and_reuses_pixels():
    import base64
    root=_svg_node(0,'svg');child=_svg_node(1,'path',0)
    root['svgAsset']={'recipe':'source-svg-rgba/v1','width':1,'height':1,'x':10,'y':10,
                      'rgba':base64.b64encode(bytes([17,34,51,128])).decode()}
    other=_svg_node(2,'svg');other['svgAsset']=dict(root['svgAsset'])
    result=_svg_scene([root,child,other])
    assert not result.screen_tree['runtime_scene']['blockers']
    assert len(result.units)==3  # Asset conversion must not drop source elements.
    assert 'original SVG element retained' in result.units[1].decisions['reason']
    assert result.custom_c.count('static const lv_image_dsc_t runtime_svg_')==1
    assert '51,34,17,128' in result.custom_c  # Explicit RGBA -> BGRA, including alpha.
    assert result.custom_c.count('lv_image_set_src(')==2


def test_svg_mutable_view_does_not_freeze_to_asset():
    root=_svg_node(0,'svg');root['attrs']={'data-uagent-source':'App.tsx:1'}
    root['svgAsset']={'width':0}  # Must never be consumed for a dynamic view.
    result=_svg_scene([root],{'views':[{'source_id':'App.tsx:1','identity':'shape','children':[]}]})
    assert 'lv_image_set_src(' not in result.custom_c


def test_svg_circle_uses_screen_transform_and_source_dash_units():
    circle=_svg_node(0,'circle');circle['attrs']={'cx':'12','cy':'12','r':'3'}
    circle['geometry']={'matrix':[2,0,0,2,5,7],'strokeScale':2,'uniform':True}
    circle['style']['strokeDasharray']='9.42477796076938'
    result=_svg_scene([circle])
    assert 'lv_obj_set_pos(runtime_0_0,21,23)' in result.custom_c
    assert 'lv_obj_set_size(runtime_0_0,16,16)' in result.custom_c
    assert 'lv_arc_set_angles(runtime_0_0,0,180)' in result.custom_c
    assert 'lv_obj_set_style_arc_width(runtime_0_0,4' in result.custom_c


def test_svg_compound_mutable_path_is_blocked_instead_of_joined_silently():
    path=_svg_node(0,'path');path['geometry']={'points':[[0,0],[2,2],[10,10]],'subpaths':2}
    result=_svg_scene([path])
    assert any('multiple subpaths' in b for b in result.screen_tree['runtime_scene']['blockers'])


def test_svg_corrupt_asset_is_rejected():
    import pytest
    root=_svg_node(0,'svg');root['svgAsset']={'width':2,'height':2,'rgba':'AAAA'}
    with pytest.raises(ValueError,match='Invalid source SVG asset'):_svg_scene([root])


def test_svg_local_quality_gate_detects_missing_icon_despite_page_score():
    from PIL import Image, ImageDraw
    from tools.runtime_convert import ssim, svg_region_scores
    reference=Image.new('RGB',(320,240),'white');actual=reference.copy()
    ImageDraw.Draw(reference).ellipse((20,20,35,35),outline='black',width=3)
    assert ssim(reference,actual)>.90
    scores=svg_region_scores(reference,actual,[{'node':'icon','rect':[18,18,20,20]}])
    assert not scores[0]['passed']


def test_nested_state_binding_reports_blocker_instead_of_crashing(tmp_path):
    import pytest
    from core.reactive_contract import extract_reactive_contract
    parser=Path(r'D:\aiassit\meter\node_modules\typescript\lib\typescript.js')
    if not parser.is_file():pytest.skip('Existing local TypeScript parser required')
    (tmp_path/'App.tsx').write_text('function App(){const [{deep:{page}},setPage]=useState([{deep:{page:0}}]);return <div/>;}',encoding='utf-8')
    result=extract_reactive_contract(tmp_path,['App.tsx'],parser_module=parser)
    assert any('state destructuring recipe' in b for b in result['blockers'])
    assert result['states']==[]


def test_updater_locals_are_evaluated_once_before_commit(tmp_path):
    import pytest
    from core.reactive_contract import extract_reactive_contract, resolve_actions
    parser=Path(r'D:\aiassit\meter\node_modules\typescript\lib\typescript.js')
    if not parser.is_file():pytest.skip('Existing local TypeScript parser required')
    (tmp_path/'App.tsx').write_text('''function App(){const [count,setCount]=useState(1);
      const tick=()=>setCount(previous=>{const delta=Math.random();const twice=delta+delta;return previous+twice;});
      return <button onClick={tick}/>;}''',encoding='utf-8')
    contract=extract_reactive_contract(tmp_path,['App.tsx'],parser_module=parser)
    actions=resolve_actions(['id','tick'],contract)
    assert len(actions[0]['bindings'])==2
    assert actions[0]['bindings'][1]['value']==['binary','+',['local','updater_0'],['local','updater_0']]
    contract['timers']=[{'period':['literal',100],'actions':actions}]
    result=_svg_scene([],contract)
    assert not result.screen_tree['runtime_scene']['blockers']
    assert result.custom_c.count('double runtime_local_updater_0=runtime_random();')==1
    assert 'runtime_local_updater_0 + runtime_local_updater_0' in result.custom_c


def test_interval_extraction_preserves_event_owned_lifecycle(tmp_path):
    import pytest
    from core.reactive_contract import extract_reactive_contract
    parser=Path(r'D:\aiassit\meter\node_modules\typescript\lib\typescript.js')
    if not parser.is_file():pytest.skip('Existing local TypeScript parser required')
    (tmp_path/'App.tsx').write_text('''function App(){const [value,setValue]=useState(0);
      useEffect(()=>{const id=setInterval(()=>setValue(1),100);return()=>clearInterval(id);},[]);
      const upload=()=>{setInterval(()=>setValue(2),200);};return <button onClick={upload}/>;}''',encoding='utf-8')
    contract=extract_reactive_contract(tmp_path,['App.tsx'],parser_module=parser)
    assert [t['effectOwned'] for t in contract['timers']]==[True,False]


def test_parameterized_dynamic_views_have_source_callsites(tmp_path):
    import pytest
    from core.reactive_contract import extract_reactive_contract, source_view_bindings
    parser=Path(r'D:\aiassit\meter\node_modules\typescript\lib\typescript.js')
    if not parser.is_file():pytest.skip('Existing local TypeScript parser required')
    (tmp_path/'App.tsx').write_text('''function Dial({value,hidden=false}){const doubled=value*2;return hidden?<b>Hidden</b>:<span>{doubled}</span>;}
      function App(){const [left,setLeft]=useState(1);const [right,setRight]=useState(2);return <div><Dial value={left}/><Dial value={right}/><Dial value={right} hidden={true}/></div>;}''',encoding='utf-8')
    contract=extract_reactive_contract(tmp_path,['App.tsx'],parser_module=parser)
    views=[v for v in source_view_bindings(contract) if v.get('callsite')]
    assert len(views)==2 and len({v['callsite'] for v in views})==2
    assert views[0]['source_id']==views[1]['source_id']
    assert [v['children'][0]['texts'][0][2] for v in views]==[['id','left'],['id','right']]


def test_explicit_list_capacity_rejects_overflow_and_ambiguous_empty_type():
    import pytest
    state={'name':'items','initial':['array',[['literal',1],['literal',2]]],'owner':'App'}
    result=_svg_scene([],{'states':[state],'list_capacity':2})
    assert not result.screen_tree['runtime_scene']['blockers']
    assert '#define UAGENT_LIST_CAPACITY 2' in result.custom_c
    assert 'a.count>=UAGENT_LIST_CAPACITY' in result.custom_c
    assert _svg_scene([],{'states':[state],'list_capacity':1}).screen_tree['runtime_scene']['blockers']
    assert _svg_scene([],{'states':[{**state,'initial':['array',[]]}]}).screen_tree['runtime_scene']['blockers']
    for capacity in [0,1025,True]:
        with pytest.raises(ValueError,match='List capacity'):_svg_scene([],{'list_capacity':capacity})


def test_numeric_tuple_state_and_permutation_compile(tmp_path):
    import pytest
    from core.reactive_contract import extract_reactive_contract
    from generator.lvgl import _runtime_expression, _runtime_string
    parser=Path(r'D:\aiassit\meter\node_modules\typescript\lib\typescript.js')
    if not parser.is_file():pytest.skip('Existing local TypeScript parser required')
    (tmp_path/'App.tsx').write_text('function App(){const [[page,direction],setPage]=useState([0,0]);return <div/>;}',encoding='utf-8')
    contract=extract_reactive_contract(tmp_path,['App.tsx'],parser_module=parser)
    assert not contract['blockers']
    assert contract['constants']['page'][0]=='member'
    state=contract['states'][0]['name']
    element=['member',['id',state],['literal',1]]
    assert not _runtime_string(element,contract)
    assert 'runtime_num_get' in _runtime_expression(element,contract)
    assert 'runtime_num_append' in _runtime_expression(['array',[element,['literal',3]]],contract)


def test_pointer_lowering_keeps_instance_state_and_early_return(tmp_path):
    import pytest
    from core.reactive_contract import extract_reactive_contract
    from core.pointer_model import expand_pointer_instances, reduce_expression, value_ast
    parser=Path(r'D:\aiassit\meter\node_modules\typescript\lib\typescript.js')
    if not parser.is_file():pytest.skip('Existing local TypeScript parser required')
    (tmp_path/'App.tsx').write_text('''function Follow({bias=0}){
      const ref=useRef(null);const [position,setPosition]=useState({x:0,y:0});
      useEffect(()=>{setPosition({x:bias,y:0});},[bias]);
      useEffect(()=>{const move=e=>{if(!ref.current)return;const r=ref.current.getBoundingClientRect();let x=e.clientX-r.left;
        if(x<1){setPosition({x:bias,y:0});return;}x+=bias;setPosition({x:x,y:e.clientY-r.top});};
        window.addEventListener("mousemove",move);return()=>window.removeEventListener("mousemove",move);},[bias]);
      return <div ref={ref}/>;}''',encoding='utf-8')
    contract=extract_reactive_contract(tmp_path,['App.tsx'],parser_module=parser)
    scene={'instances':{'left':{'owner':'Follow','props':{'bias':-3}},'right':{'owner':'Follow','props':{'bias':7}}},'nodes':[
      {'id':i,'attrs':{'data-uagent-instance':identity,'data-uagent-ref':'ref'},'rect':{'x':i*100+.25,'y':20,'width':50,'height':50}}
      for i,identity in enumerate(('left','right'))]}
    lowered=expand_pointer_instances(contract,[scene])
    assert not lowered['blockers']
    assert [s['name'] for s in lowered['states']]==['left:position','right:position']
    def evaluate(expr):
        if not isinstance(expr,list):return expr
        if expr and expr[0]=='pointer':return ['literal',0]
        if expr and expr[0]=='layout':return ['literal',100 if expr[2]=='left' else 20]
        return [evaluate(e) for e in expr]
    for pointer,bias in zip(lowered['pointers'],(-3,7)):
        x=next(a for a in pointer['actions'] if a['field']=='x')
        assert reduce_expression(evaluate(x['value']),{pointer['instance']+':position':value_ast({'x':0,'y':0})})==['literal',bias]
    assert lowered['layout_offsets']['0']['left']==.25


def test_keyed_gesture_threshold_permutation_and_control_coverage(tmp_path):
    import pytest
    from core.reactive_contract import extract_reactive_contract, literal
    from core.pointer_model import expand_pointer_instances, reduce_expression, value_ast
    parser=Path(r'D:\aiassit\meter\node_modules\typescript\lib\typescript.js')
    if not parser.is_file():pytest.skip('Existing local TypeScript parser required')
    (tmp_path/'App.tsx').write_text('''const Stack=()=>{const [indices,setIndices]=useState([0,1,2,3]);
      const rotate=()=>setIndices(previous=>[previous[1],previous[2],previous[3],previous[0]]);
      return <div><button onClick={()=>rotate()}/>{indices.map((index,i)=><motion.div key={index} onDragEnd={(e,{offset,velocity})=>{
        const power=Math.abs(offset.x)*velocity.x;if(power < -10000 || power > 10000)rotate();}}/>)}</div>;};''',encoding='utf-8')
    contract=extract_reactive_contract(tmp_path,['App.tsx'],parser_module=parser)
    gesture=contract['gestures'][0]
    nodes=[{'id':i,'attrs':{'data-uagent-instance':'one','data-uagent-source':gesture['source_id'],'data-uagent-key':str(i)},
            'rect':{'x':0,'y':0,'width':100,'height':100}} for i in range(4)]
    scene={'instances':{'one':{'owner':'Stack','props':{}}},'nodes':nodes}
    lowered=expand_pointer_instances(contract,[scene]);keyed=lowered['keyed_lists'][0]
    assert keyed['nodes']==[0,1,2,3]
    assert any('onClick requires an instance-scoped' in b for b in lowered['blockers'])
    assert not any('onDragEnd requires' in b for b in lowered['blockers'])
    def release(order,dx,vx):
        def replace(v):
            if not isinstance(v,list):return v
            if v and v[0]=='gesture':return ['literal',dx if v[1]=='offset' else vx]
            return [replace(x) for x in v]
        return literal(reduce_expression(replace(keyed['actions'][0]['value']),{'one:indices':value_ast(order)}))
    for dx,vx in [(0,1000),(100,0),(100,100),(100,-100)]:assert release([0,1,2,3],dx,vx)==[0,1,2,3]
    for dx,vx in [(100,101),(-100,-101)]:assert release([0,1,2,3],dx,vx)==[1,2,3,0]
    order=[0,1,2,3]
    for _ in range(4):order=release(order,100,101)
    assert order==[0,1,2,3]
    nodes[-1]['attrs']['data-uagent-key']='0'
    with pytest.raises(ValueError,match='duplicated'):expand_pointer_instances(contract,[scene])


def test_uncompiled_pointer_listeners_and_drag_handlers_are_not_silent(tmp_path):
    import pytest
    from core.reactive_contract import extract_reactive_contract
    parser=Path(r'D:\aiassit\meter\node_modules\typescript\lib\typescript.js')
    if not parser.is_file():pytest.skip('Existing local TypeScript parser required')
    (tmp_path/'App.tsx').write_text('''function App(){useEffect(()=>{window.addEventListener("mousemove",move);},[]);
      return <motion.div onDragEnd={end}/>;}''',encoding='utf-8')
    result=extract_reactive_contract(tmp_path,['App.tsx'],parser_module=parser)
    assert any('subscription/lifecycle' in b for b in result['blockers'])
    assert any('onDragEnd' in b for b in result['blockers'])


def test_original_svg_asset_matches_browser_at_half_pixel_origin(tmp_path):
    import base64, json, subprocess, time
    import pytest
    from io import BytesIO
    from PIL import Image
    from core import browser_layout as browser
    from tools.runtime_convert import ssim
    binary=browser._browser()
    if not binary:pytest.skip('Local Chromium required for SVG raster regression')
    port=browser._free_port();cdp=None
    process=subprocess.Popen([binary,'--headless=new','--no-first-run','--disable-gpu','--remote-allow-origins=*',
        f'--remote-debugging-port={port}',f'--user-data-dir={tmp_path / "browser"}','about:blank'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            try:targets=json.load(browser._DIRECT.open(f'http://127.0.0.1:{port}/json/list'));break
            except OSError:time.sleep(.1)
        else:pytest.fail('Local SVG fixture browser did not start')
        cdp=browser._CDP(next(t['webSocketDebuggerUrl'] for t in targets if t['type']=='page'))
        cdp.call('Page.enable')
        cdp.call('Emulation.setDeviceMetricsOverride',{'width':80,'height':60,'deviceScaleFactor':1,'mobile':False})
        html='''<style>body{margin:0;background:white}</style><svg width="24" height="24" viewBox="0 0 12 12"
          style="position:absolute;left:10.5px;top:10.5px;display:block" fill="none" stroke="#0080ff" stroke-width="1.25" stroke-linecap="round">
          <path d="M1 1L4 4 M8 1L11 4"/><circle cx="6" cy="8" r="2"/></svg>'''
        cdp.call('Runtime.evaluate',{'expression':'document.body.innerHTML='+json.dumps(html)})
        cdp.call('Runtime.evaluate',{'expression':'new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))','awaitPromise':True})
        reference=Image.open(BytesIO(base64.b64decode(cdp.call('Page.captureScreenshot',{'format':'png'})['result']['data'])))
        result=cdp.call('Runtime.evaluate',{'expression':browser._RUNTIME_SCENE_CAPTURE,'returnByValue':True,'awaitPromise':True})
        scene=result['result']['result']['value'];asset=next(n['svgAsset'] for n in scene['nodes'] if n.get('svgAsset'))
        rendered=Image.new('RGBA',reference.size,'white')
        rendered.alpha_composite(Image.frombytes('RGBA',(asset['width'],asset['height']),base64.b64decode(asset['rgba'])),(asset['x'],asset['y']))
        assert ssim(reference.crop((10,10,36,36)),rendered.crop((10,10,36,36)))>=.98
        path=next(n for n in scene['nodes'] if n['tag']=='path')
        assert path['geometry']['subpaths']==2
        assert path['geometry']['strokeScale']==2
    finally:
        if cdp:cdp.close()
        process.terminate();process.wait(timeout=10)


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


def test_sdl_capture_waits_for_a_later_completed_frame(tmp_path,monkeypatch):
    import os
    from PIL import Image
    from tools import runtime_convert as converter
    clock=[0.0];target=tmp_path/'sdl.bmp'
    class Process:
        def __init__(self,*args,**kwargs):
            Image.new('RGB',(4,4),'black').save(target)
            os.utime(target,ns=(1000000000,1000000000))
            (tmp_path/'sdl-state.json').write_text('{}')
        def poll(self):return None
        def terminate(self):pass
        def wait(self,timeout):return 0
    def sleep(seconds):
        clock[0]+=seconds
        if clock[0]>=2 and target.stat().st_mtime_ns==1000000000:
            Image.new('RGB',(4,4),'white').save(target)
            os.utime(target,ns=(2000000000,2000000000))
    monkeypatch.setattr(converter.subprocess,'Popen',Process)
    monkeypatch.setattr(converter.time,'monotonic',lambda:clock[0])
    monkeypatch.setattr(converter.time,'sleep',sleep)
    path=converter.capture_sdl(tmp_path/'mock.exe',tmp_path,0)
    assert clock[0]>=3
    with Image.open(path) as actual:assert actual.getpixel((0,0))==(255,255,255)


def test_missing_make_host_entry_preserves_application_and_existing_html(tmp_path):
    from tools.runtime_convert import prepare_browser_entry
    import pytest
    app=tmp_path/'src/app/App.tsx';app.parent.mkdir(parents=True)
    app.write_text('export default function App(){return <div/>}',encoding='utf-8')
    styles=tmp_path/'src/styles/index.css';styles.parent.mkdir();styles.write_text('body{margin:0}',encoding='utf-8')
    before=app.read_bytes()
    assert prepare_browser_entry(tmp_path)
    assert app.read_bytes()==before
    assert "import './src/styles/index.css'" in (tmp_path/'uagent-browser-entry.tsx').read_text()
    (tmp_path/'index.html').write_text('custom host',encoding='utf-8')
    assert not prepare_browser_entry(tmp_path)
    assert (tmp_path/'index.html').read_text()=='custom host'
    other=tmp_path/'ambiguous';(other/'src/app').mkdir(parents=True)
    (other/'src/App.tsx').write_text('export default 1')
    (other/'src/app/App.tsx').write_text('export default 2')
    with pytest.raises(ValueError,match='ambiguous'):prepare_browser_entry(other)
    assert not (other/'index.html').exists()


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


def test_interaction_probe_is_visible_coverage_not_history_equivalence():
    from copy import deepcopy
    from tools.interaction_probe import visible_signature, node_delta
    first={'state':{'history':['home']},'nodes':[{'source':'App.tsx:1','tag':'button','text':'Open'}],'controls':[]}
    history=deepcopy(first);history['state']['history'].append('home')
    assert visible_signature(first)==visible_signature(history)
    changed=deepcopy(history);changed['nodes'].append({'source':'App.tsx:2','tag':'svg','text':''})
    assert visible_signature(first)!=visible_signature(changed)
    assert node_delta(first,changed)=={'added':1,'removed':0}
    assert node_delta(changed,first)=={'added':0,'removed':1}


def test_interaction_probe_records_clipped_controls_and_native_input_samples():
    from tools.interaction_probe import candidates
    base={'source':'App.tsx:1','ordinal':0,'index':0,'label':'Date','in_view':False,
          'rendered':True,'disabled':False,'tag':'input','type':'date','min':'','max':''}
    actions=candidates({'controls':[base,{**base,'disabled':True},{**base,'rendered':False}]})
    assert len(actions)==1 and actions[0]['kind']=='input'
    assert actions[0]['value']=='2030-02-14' and not actions[0]['in_view']


def test_runtime_conversion_stops_before_build_when_capability_gate_fails(tmp_path,monkeypatch):
    import json
    from types import SimpleNamespace
    from tools import runtime_convert as converter
    source=tmp_path/'input';source.mkdir()
    output=tmp_path/'output';staging=output/'source';staging.mkdir(parents=True)
    (staging/'package.json').write_text('{}',encoding='utf-8')
    (output/'source-sha256.json').write_text('{}',encoding='utf-8')
    (output/'browser-reference.json').write_text(json.dumps({'uagent_run':{'width':320,'height':240,'sample_ms':0,'production':True}}),encoding='utf-8')
    monkeypatch.setattr(converter.browser_layout,'_vite_runtime_works',lambda *a,**k:True)
    monkeypatch.setattr(converter,'analyze',lambda *a:None)
    bundle=SimpleNamespace(screen_tree={'runtime_scene':{'blockers':['Unsupported history stack']}},agent_planning={'blockers':[]},warnings=[])
    monkeypatch.setattr(converter,'compile_lvgl',lambda *a:bundle)
    monkeypatch.setattr(converter,'write_aibuilder_custom',lambda *a,**k:{})
    def forbidden(*a,**k):raise AssertionError('Blocked code must not be built')
    monkeypatch.setattr(converter,'_build_snapshot_simulator',forbidden)
    monkeypatch.setattr(converter,'generate_snapshot_harness',forbidden)
    result=converter.convert(source,output,320,240,resume=True,sample_ms=0)
    assert result['status']=='blocked' and not result['accepted']
    assert result['blockers']==['Unsupported history stack']


def test_source_navigation_stack_and_loop_callbacks(tmp_path):
    import pytest
    from core.reactive_contract import extract_reactive_contract, source_control_bindings
    from generator.lvgl import _runtime_expression, _runtime_text_format
    parser=Path(r'D:\uiagent_oct\print-baseline-20261001\source\node_modules\typescript\lib\typescript.js')
    if not parser.is_file():pytest.skip('Existing local TypeScript parser required')
    (tmp_path/'App.tsx').write_text('''function Menu({onOpen}){
      const entries=[{page:"alpha"},{page:"beta"}];
      return <div>{entries.map(entry=><button onClick={()=>onOpen(entry.page)}><span>{entry.page}</span></button>)}</div>;
    }
    function App(){const [route,setRoute]=useState("home");const [trail,setTrail]=useState(["home"]);
      const open=page=>{setRoute(page);setTrail([...trail,page]);};
      const back=()=>{if(trail.length>1){const copy=[...trail];copy.pop();setRoute(copy[copy.length-1]);setTrail(copy);}else{setRoute("home");setTrail(["home"]);}};
      const render=()=>{switch(route){case "alpha":return <Alpha/>;case "beta":return <Beta/>;default:return <Menu onOpen={open}/>;}};
      return <div><button aria-label="Previous" onClick={back}/>{render()}</div>;
    }''',encoding='utf-8')
    c=extract_reactive_contract(tmp_path,['App.tsx'],parser_module=parser);bindings=source_control_bindings(c)
    assert not any(b.get('blocker') for b in bindings)
    assert c['navigation'][0]['state']=='route'
    links=[b for b in bindings if b['owner']=='Menu']
    assert len(links)==2 and [b['ordinal'] for b in links]==[0,1]
    assert [b['actions'][0]['value'] for b in links]==[['literal','alpha'],['literal','beta']]
    back=next(b for b in bindings if b['identity']=='Previous')
    route=next(a['value'] for a in back['actions'] if a['state']=='route')
    assert _runtime_text_format([route],c)[0]=='%s'
    assert 'runtime_array_get(runtime_array_drop(runtime_state_trail)' in _runtime_expression(route,c)


def test_native_text_event_and_unsupported_array_element(tmp_path):
    import pytest
    from core.reactive_contract import extract_reactive_contract, source_control_bindings
    from generator.lvgl import _runtime_expression
    parser=Path(r'D:\uiagent_oct\print-baseline-20261001\source\node_modules\typescript\lib\typescript.js')
    if not parser.is_file():pytest.skip('Existing local TypeScript parser required')
    (tmp_path/'App.tsx').write_text('''function App(){const [day,setDay]=useState("2025-01-01");
      const change=e=>setDay(e.target.value);return <input type="date" value={day} onChange={change}/>;}''',encoding='utf-8')
    c=extract_reactive_contract(tmp_path,['App.tsx'],parser_module=parser)
    assert source_control_bindings(c)[0]['actions']==[{'state':'day','field':None,'value':['event-text']}]
    with pytest.raises(ValueError,match='require strings'):_runtime_expression(['array',[['literal',{}]]],c)


def _extract_rules_fixture(tmp_path, source):
    import pytest
    from core.reactive_contract import extract_reactive_contract
    parser = Path(r'D:\uiagent_oct\print-baseline-20261001\source\node_modules\typescript\lib\typescript.js')
    if not parser.is_file():
        pytest.skip('Existing local TypeScript parser required')
    (tmp_path/'App.tsx').write_text(source, encoding='utf-8')
    return extract_reactive_contract(tmp_path, ['App.tsx'], parser_module=parser)


def test_scoped_rules_preserve_same_named_component_states_and_handlers(tmp_path):
    from core.state_rules import compile_state_rules
    contract = _extract_rules_fixture(tmp_path, '''
      function First(){const [activeTab,setActiveTab]=useState('one');
        const reset=()=>setActiveTab('one');return <button onClick={reset}/>;}
      function Second(){const [activeTab,setActiveTab]=useState('two');
        const reset=()=>setActiveTab('two');return <button onClick={reset}/>;}
      function App(){return <><First/><Second/></>;}
    ''')
    rules = compile_state_rules(contract)
    first, second = rules['components'][:2]
    assert first['states'][0]['id'] != second['states'][0]['id']
    assert [c['states'][0]['initial_value'] for c in (first, second)] == ['one', 'two']
    assert first['functions'][1]['id'] != second['functions'][1]['id']
    # The legacy backend must still block, not flatten these two instances.
    assert any('duplicate state' in b for b in contract['blockers'])
    assert rules['summary']['target_execution'] == 'legacy-backend-gated'


def test_typed_empty_records_nested_lists_and_explicit_capacity(tmp_path):
    import pytest
    from copy import deepcopy
    from core.state_rules import compile_state_rules, validate_value, RuleError
    contract = _extract_rules_fixture(tmp_path, '''
      interface Row {id:number; title:string; options:string[]; selected?:boolean;}
      function App(){const [rows,setRows]=useState<Row[]>([]);
        return <div>{rows.map(row=><button key={row.id}>{row.title}</button>)}</div>;}
    ''')
    rules = compile_state_rules(contract, 2)
    assert rules['blockers'] == []
    component = rules['components'][0]
    schema = component['states'][0]['type']
    assert schema['element']['fields']['options']['capacity'] == 2
    rows = [{'id': 1, 'title': 'Alpha', 'options': ['yes', 'no']}]
    original = deepcopy(rows)
    validate_value(schema, rows)
    with pytest.raises(RuleError, match='list capacity 2 exceeded'):
        validate_value(schema, [*rows, *rows, *rows])
    with pytest.raises(RuleError, match='unknown fields'):
        validate_value(schema, [{**rows[0], 'lost': 1}])
    with pytest.raises(RuleError, match='number'):
        validate_value(schema, [{**rows[0], 'id': True}])
    assert rows == original
    assert component['loops'][0]['key'] == ['member', ['id', 'row'], ['literal', 'id']]


def test_rules_reject_unknown_initializers_and_recursive_or_ambiguous_lists(tmp_path):
    from core.state_rules import compile_state_rules
    contract = _extract_rules_fixture(tmp_path, '''
      interface Recursive {children: Recursive[]}
      function App(){const [a,setA]=useState([]);
        const [b,setB]=useState<Recursive[]>([]);
        const [c,setC]=useState(loadData());return <div/>;}
    ''')
    rules = compile_state_rules(contract)
    assert rules['summary']['typed_states'] == 0
    assert any('empty list' in b for b in rules['blockers'])
    assert any('recursive' in b for b in rules['blockers'])
    assert any('requires lowering' in b for b in rules['blockers'])


def test_rules_retain_timeout_origin_and_custom_control_events(tmp_path):
    from core.state_rules import compile_state_rules
    contract = _extract_rules_fixture(tmp_path, '''
      function App(){const [value,setValue]=useState('');
        useEffect(()=>{const t=setTimeout(()=>setValue('ready'),10);return()=>clearTimeout(t);},[]);
        const upload=()=>{setTimeout(()=>setValue('done'),20);alert('queued');};
        return <Select value={value} onValueChange={setValue} onOpenChange={upload}/>;}
    ''')
    rules = compile_state_rules(contract)
    assert [(t['kind'], t['trigger']) for t in rules['tasks']] == [('timeout', 'effect'), ('timeout', 'call')]
    assert rules['tasks'][0]['scope'] != rules['tasks'][1]['scope']
    assert [e['event'] for e in rules['components'][0]['events']] == ['onValueChange', 'onOpenChange']
    assert rules['summary']['host_calls'] == 1


def test_batched_functional_updates_consume_queue_but_direct_reads_do_not(tmp_path):
    import pytest
    from core.reactive_contract import resolve_actions
    from generator.lvgl import _runtime_expression
    c = _extract_rules_fixture(tmp_path, '''function App(){const [n,setN]=useState(0);
      const twice=()=>{setN(p=>p+1);setN(p=>p+1);};
      const replace=()=>{setN(p=>p+1);setN(n+1);};
      const locals=()=>{setN(p=>{const a=p+1;return a;});setN(p=>{const b=p+1;return b;});};
      const impure=()=>{setN(Math.random());setN(p=>p+p);};
      return <div/>;}''')
    twice = resolve_actions(['id', 'twice'], c)
    replace = resolve_actions(['id', 'replace'], c)
    assert _runtime_expression(twice[0]['value'], c).count(' + 1') == 2
    assert _runtime_expression(replace[0]['value'], c).count(' + 1') == 1
    actions = resolve_actions(['id', 'locals'], c)
    assert len(actions[0]['bindings']) == 2
    assert actions[0]['bindings'][1]['value'][2] == ['local', 'updater_0']
    with pytest.raises(ValueError, match='repeated evaluation'):
        resolve_actions(['id', 'impure'], c)


def test_state_rules_reject_utf8_overflow_instead_of_silent_string_truncation():
    import pytest
    from core.state_rules import validate_value, RuleError
    schema = {'kind': 'string', 'capacity_bytes': 4}
    validate_value(schema, '中a')
    with pytest.raises(RuleError, match='capacity'):
        validate_value(schema, '中文')
    with pytest.raises(RuleError, match='NUL'):
        validate_value(schema, 'a\0b')


def test_queued_record_updates_keep_prior_fields_and_render_snapshot(tmp_path):
    from core.reactive_contract import resolve_actions
    from generator.lvgl import _runtime_expression
    c = _extract_rules_fixture(tmp_path, '''function App(){const [prefs,setPrefs]=useState({gain:2,level:3});
      const reset=()=>{setPrefs(p=>({...p,gain:17}));setPrefs(p=>({...p,level:p.gain+1}));};
      const twice=()=>{setPrefs(p=>({...p,gain:p.gain+1}));setPrefs(p=>({...p,gain:p.gain+1}));};
      return <div/>;}''')
    reset = resolve_actions(['id', 'reset'], c)
    assert _runtime_expression(reset[1]['value'], c) == '(17 + 1)'
    twice = resolve_actions(['id', 'twice'], c)
    assert len(twice) == 1 and _runtime_expression(twice[0]['value'], c).count(' + 1') == 2


def _desktop_job_fixture(tmp_path, monkeypatch, status='sample-passed', accepted=True, exit_code=0, gate=None, write_report=True):
    import json
    from server import runtime_jobs
    source = tmp_path/'source'; source.mkdir()
    (source/'package.json').write_text('{}')
    for filename in ('vite/bin/vite.js', 'typescript/lib/typescript.js'):
        target = source/'node_modules'/filename
        target.parent.mkdir(parents=True, exist_ok=True); target.write_text('fixture')
    monkeypatch.setattr(runtime_jobs.shutil, 'which', lambda name: 'available')
    captured = {}
    class Process:
        def __init__(self, command, **kwargs):
            output = Path(command[command.index('--width')-1])
            assert not list(output.iterdir())  # Live log must not break new-output validation.
            captured.update(command=command, env=kwargs['env'], output=output)
            kwargs['stdout'].write(b'Compiling source contract and runtime scene\n')
            self.output = output
        def wait(self, timeout=None):
            if gate: gate.wait(3)
            if write_report:
                exe = self.output/'generated/main.exe'; exe.parent.mkdir(exist_ok=True); exe.write_bytes(b'fixture')
                report = {'status':status,'accepted':accepted,'exe':str(exe),
                          'blockers': ['Unknown SVG filter primitive'] if status == 'blocked' else [],
                          'initial_frame_ssim':.97}
                (self.output/'conversion-report.json').write_text(json.dumps(report))
            return exit_code
    manager = runtime_jobs.ConversionJobs(tmp_path/'outputs', process_factory=Process)
    return manager, runtime_jobs.ConversionRequest(source_dir=str(source)), captured


def _wait_desktop_job(manager, job_id):
    import time
    end = time.monotonic()+4
    while time.monotonic()<end:
        job = manager.get(job_id)
        if job['status'] not in {'queued','running'}: return job
        time.sleep(.01)
    raise AssertionError('Desktop job did not finish')


def test_desktop_converter_uses_isolated_command_and_empty_output(tmp_path, monkeypatch):
    monkeypatch.setenv('UAGENT_HEADLESS','1')
    monkeypatch.setenv('UAGENT_STARTUP_LOG','parent-trace')
    manager, request, captured = _desktop_job_fixture(tmp_path, monkeypatch)
    job = _wait_desktop_job(manager, manager.start(request)['id'])
    assert job['status']=='passed' and job['exe']
    assert Path(job['log_path']).parent == Path(job['output'])
    assert 'UAGENT_HEADLESS' not in captured['env'] and 'UAGENT_STARTUP_LOG' not in captured['env']
    assert (Path(request.source_dir)/'package.json').read_text()=='{}'
    assert '--width' in captured['command'] and '--height' in captured['command']


def test_desktop_gate_blocks_preview_even_when_report_claims_accepted(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from server import runtime_jobs
    from server.app import app
    manager, request, _ = _desktop_job_fixture(tmp_path, monkeypatch, status='blocked', accepted=True, exit_code=2)
    monkeypatch.setattr(runtime_jobs, 'jobs', manager)
    with TestClient(app) as client:
        created = client.post('/api/runtime-convert/jobs',json=request.model_dump())
        assert created.status_code == 202
        job = _wait_desktop_job(manager, created.json()['id'])
        assert job['status']=='blocked' and job['exe'] is None
        assert client.post(f'/api/runtime-convert/jobs/{job["id"]}/preview').status_code==409
        assert client.get('/api/runtime-convert/jobs/missing').status_code==404
        assert client.post('/api/runtime-convert/jobs',json={**request.model_dump(),'width':0}).status_code==422


def test_desktop_missing_report_and_nonzero_exit_cannot_pass(tmp_path, monkeypatch):
    manager, request, _ = _desktop_job_fixture(tmp_path, monkeypatch, write_report=False, exit_code=1)
    job = _wait_desktop_job(manager, manager.start(request)['id'])
    assert job['status']=='failed' and '未生成报告' in job['blockers'][0]
    from server.runtime_jobs import report_result
    output = tmp_path/'result'; output.mkdir(); exe=output/'test.exe'; exe.write_bytes(b'fixture')
    result = report_result({'status':'sample-passed','accepted':True,'exe':str(exe)},output,1)
    assert result['status']=='failed' and result['exe'] is None
    outside = tmp_path/'other.exe'; outside.write_bytes(b'fixture')
    assert report_result({'status':'sample-passed','accepted':True,'exe':str(outside)},output,0)['status']=='failed'


def test_desktop_rejects_second_running_job_without_new_output(tmp_path, monkeypatch):
    import threading
    import pytest
    gate = threading.Event()
    manager, request, _ = _desktop_job_fixture(tmp_path, monkeypatch, gate=gate)
    first = manager.start(request)
    before = set(manager.output_base.iterdir())
    try:
        with pytest.raises(RuntimeError,match='已有转换'): manager.start(request)
        assert set(manager.output_base.iterdir()) == before
    finally: gate.set()
    assert _wait_desktop_job(manager, first['id'])['status']=='passed'


def test_desktop_preflight_rejects_missing_dependencies_and_source_output_overlap(tmp_path, monkeypatch):
    import pytest
    from server.runtime_jobs import ConversionJobs, ConversionRequest
    source=tmp_path/'source'; source.mkdir(); (source/'package.json').write_text('{}')
    root=tmp_path/'output'
    with pytest.raises(ValueError,match='缺少工程依赖'):
        ConversionJobs(root).start(ConversionRequest(source_dir=str(source)))
    assert not root.exists()
    with pytest.raises(ValueError,match='输出目录不能'):
        ConversionJobs(source/'output').start(ConversionRequest(source_dir=str(source)))
    assert not (source/'output').exists()


def test_desktop_frozen_worker_uses_self_contained_gui_executable(tmp_path, monkeypatch):
    import sys
    from server.runtime_jobs import command_for, ConversionRequest
    monkeypatch.setattr(sys,'frozen',True,raising=False)
    monkeypatch.setattr(sys,'executable',str(tmp_path/'UAgent.exe'))
    request=ConversionRequest(source_dir=str(tmp_path/'source'))
    command=command_for(tmp_path/'source',tmp_path/'output',request)
    assert command[:2]==[str(tmp_path/'UAgent.exe'),'--convert'] and '--width' in command


def test_production_browser_host_works_without_python_interpreter_in_frozen_exe(tmp_path, monkeypatch):
    import sys
    import urllib.request
    from types import SimpleNamespace
    from tools import runtime_convert as converter
    source=tmp_path/'source'; source.mkdir()
    output=tmp_path/'output'; output.mkdir()
    def build(command, **kwargs):
        folder=Path(command[command.index('--outDir')+1]);folder.mkdir()
        (folder/'index.html').write_text('<h1>React host</h1>')
        (folder/'app.js').write_text('const count=1;')
        return SimpleNamespace(returncode=0,stdout='built',stderr='')
    def no_child(*args, **kwargs):
        raise AssertionError('A frozen EXE must not be invoked with Python -c')
    monkeypatch.setattr(sys,'frozen',True,raising=False)
    monkeypatch.setattr(sys,'executable',str(tmp_path/'UAgent.exe'))
    monkeypatch.setattr(converter.subprocess,'run',build)
    monkeypatch.setattr(converter.subprocess,'Popen',no_child)
    server,url,log=converter.serve_production(source,output)
    try:
        assert urllib.request.urlopen(url,timeout=2).read()==b'<h1>React host</h1>'
        script=urllib.request.urlopen(url+'app.js',timeout=2)
        assert script.headers['Content-Type'].startswith('text/javascript')
        assert script.read()==b'const count=1;'
    finally:
        server.terminate();assert server.wait(timeout=2)==0;log.close()
    assert not server.thread.is_alive()
