"""Reproducible, offline-planned React -> LVGL/SDL conversion command.

All build/capture writes take place under a new output directory. Unsupported
semantics are reported, never repaired by a remote model or counted as success.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import time
import urllib.request

from core import browser_layout
from core.pipeline import analyze
from core.reactive_contract import instrument_browser_source, extract_reactive_contract, literal
from generator.screen_ir import reachable_components
from generator.compiler import compile_lvgl, write_aibuilder_custom
from tools.cross_validate import generate_snapshot_harness, _build_snapshot_simulator


def stage_source(source: Path, output: Path, dependencies: Path | None = None) -> Path:
    source, output = source.resolve(), output.resolve()
    if source == output or source in output.parents or output in source.parents:
        raise ValueError('Source and output must be disjoint; source is read-only')
    if output.exists() and any(output.iterdir()):
        raise ValueError('Output already contains files; choose a new output directory')
    output.mkdir(parents=True, exist_ok=True)
    staging = output / 'source'
    staging.mkdir()
    # Copy the application, not nested exports, historical builds or caches.
    for entry in source.iterdir():
        if entry.is_file() and not entry.name.startswith('.uagent'):
            shutil.copy2(entry, staging / entry.name)
        elif entry.is_dir() and entry.name in {'src', 'public', '.figma'}:
            shutil.copytree(entry, staging / entry.name,
                            ignore=shutil.ignore_patterns('node_modules', '.git', 'dist', '__pycache__'))
    dependencies = (dependencies or source / 'node_modules').resolve()
    if not (dependencies / 'vite/bin/vite.js').is_file():
        raise ValueError('Existing local Vite dependencies required; this command does not install packages')
    if os.name == 'nt':
        project_dependencies=source/'node_modules'
        if project_dependencies.is_dir() and project_dependencies.resolve()!=dependencies:
            seeded=subprocess.run(['robocopy',str(project_dependencies),str(staging/'node_modules'),'/E','/NFL','/NDL','/NJH','/NJS','/NP','/R:0','/W:0'],capture_output=True,timeout=180)
            if seeded.returncode>=8:raise RuntimeError('Failed to copy existing project dependencies')
        copied = subprocess.run(['robocopy', str(dependencies), str(staging/'node_modules'),
                                 '/E', '/NFL', '/NDL', '/NJH', '/NJS', '/NP', '/R:0', '/W:0'],
                                capture_output=True, timeout=180)
        if copied.returncode >= 8:
            raise RuntimeError('Failed to copy existing dependencies')
    else:
        shutil.copytree(dependencies, staging/'node_modules')
    fingerprints = {str(p.relative_to(source)): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in (source/'src').rglob('*') if p.is_file()}
    (output/'source-sha256.json').write_text(json.dumps(fingerprints, indent=2), encoding='utf-8')
    return staging


def ssim(reference, actual) -> float:
    a, b = reference.convert('L'), actual.convert('L')
    if a.size != b.size:
        raise ValueError('Image dimensions differ')
    scores = []
    for y in range(0, a.height, 8):
        for x in range(0, a.width, 8):
            box = (x, y, min(x+8, a.width), min(y+8, a.height))
            def pixels(image):return list(image.get_flattened_data()) if hasattr(image,'get_flattened_data') else list(image.getdata())
            p, q = pixels(a.crop(box)), pixels(b.crop(box))
            n = len(p); u, v = sum(p)/n, sum(q)/n
            va = sum((i-u)**2 for i in p)/n; vb = sum((i-v)**2 for i in q)/n
            cov = sum((i-u)*(j-v) for i,j in zip(p,q))/n
            scores.append(((2*u*v+6.5025)*(2*cov+58.5225))/((u*u+v*v+6.5025)*(va+vb+58.5225)))
    return sum(scores)/len(scores)


def svg_region_scores(reference, actual, regions: list[dict]) -> list[dict]:
    """Small authored assets must pass locally, not hide in page background."""
    from PIL import ImageChops, ImageStat
    result=[]
    for item in regions:
        x,y,w,h=item['rect'];width,height=reference.size
        box=(max(0,math.floor(x)),max(0,math.floor(y)),min(width,math.ceil(x+w)),min(height,math.ceil(y+h)))
        if box[2]<=box[0] or box[3]<=box[1]:continue
        a,b=reference.crop(box),actual.crop(box)
        score=ssim(a,b)
        rgb_error=sum(ImageStat.Stat(ImageChops.difference(a.convert('RGB'),b.convert('RGB'))).mean)/(3*255)
        result.append({'node':item.get('node'),'box':list(box),'ssim':score,'rgb_mae':rgb_error,
                       'passed':score>=.90 and rgb_error<=.06})
    return result


def capture_sdl(exe: Path, output: Path, clock: int, actions: str = '', name: str = 'sdl') -> Path:
    from PIL import Image
    target = output / (name+'.bmp')
    target.unlink(missing_ok=True)
    state_file=output/(name+'-state.json')
    state_file.unlink(missing_ok=True)
    env = os.environ.copy()
    env.update(UAGENT_TEST_DETERMINISTIC='1', UAGENT_TEST_CLOCK_MS=str(clock),
               UAGENT_SIMULATOR_CAPTURE=str(target), UAGENT_TEST_ACTIONS=actions,
               UAGENT_TEST_STATE_FILE=str(output/(name+'-state.json')))
    with (output/(name+'.log')).open('w', encoding='utf-8') as log:
        process = subprocess.Popen([str(exe)], cwd=exe.parent, env=env, stdout=log, stderr=log)
        try:
            started=time.monotonic();stable_since=started;previous=None;first_frame_stamp=None
            for _ in range(200):
                if process.poll() is not None:
                    raise RuntimeError(f'SDL exited with code {process.returncode}')
                try:
                    with Image.open(target) as image:
                        image.load()
                        # Both outputs must belong to this run and be complete
                        # before terminating the capture process.
                        state=json.loads(state_file.read_text(encoding='utf-8'))
                        fingerprint=(hashlib.sha256(image.tobytes()).digest(),json.dumps(state,sort_keys=True))
                        now=time.monotonic()
                        frame_stamp=target.stat().st_mtime_ns
                        if first_frame_stamp is None:first_frame_stamp=frame_stamp
                        if fingerprint!=previous:previous=fingerprint;stable_since=now
                        if now-started>=3 and frame_stamp!=first_frame_stamp and now-stable_since>=.3:
                            image.save(output/(name+'.png'))
                            return output/(name+'.png')
                except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                    previous=None;stable_since=time.monotonic()
                time.sleep(.1)
            raise RuntimeError('SDL capture timed out')
        finally:
            process.terminate(); process.wait(timeout=10)


def state_differences(a, b, path='') -> list[str]:
    if isinstance(a,dict):return [v for key,value in a.items() for v in state_differences(value,b.get(key) if isinstance(b,dict) else None,path+'.'+key)]
    equal=abs(a-b)<=1e-7 if isinstance(a,(int,float)) and not isinstance(a,bool) and isinstance(b,(int,float)) else a==b
    return [] if equal else [f'{path}: browser={a!r}, SDL={b!r}']


def prepare_browser_entry(source: Path) -> bool:
    """Supply only the missing host shell of an isolated Make export."""
    if (source/'index.html').exists():return False
    candidates=[p for p in ('src/main.tsx','src/main.jsx') if (source/p).is_file()]
    if candidates:
        if len(candidates)!=1:raise ValueError('Ambiguous browser entry modules')
        entry=candidates[0]
    else:
        apps=[p for p in ('src/app/App.tsx','src/App.tsx','src/App.jsx') if (source/p).is_file()]
        if len(apps)!=1:raise ValueError('Missing or ambiguous App entry; provide an explicit browser entry')
        entry='uagent-browser-entry.tsx'
        if (source/entry).exists():raise ValueError('Generated browser entry would overwrite an existing file')
        styles="import './src/styles/index.css';\n" if (source/'src/styles/index.css').is_file() else ''
        (source/entry).write_text("import React from 'react';\nimport {createRoot} from 'react-dom/client';\n"+
            f"import App from './{apps[0]}';\n"+styles+"createRoot(document.getElementById('root')).render(<App/>);\n",encoding='utf-8')
    (source/'index.html').write_text('<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head><body><div id="root"></div><script type="module" src="/'+entry+'"></script></body></html>',encoding='utf-8')
    return True


def serve_production(source: Path, output: Path):
    result=subprocess.run([browser_layout._node(),str(source/'node_modules/vite/bin/vite.js'),'build','--outDir',str(output/'browser-build')],cwd=source,capture_output=True,text=True,encoding='utf-8',timeout=90)
    (output/'browser-build.log').write_text(result.stdout+result.stderr,encoding='utf-8')
    if result.returncode:raise RuntimeError('Production browser build failed; see browser-build.log')
    port=browser_layout._free_port();url=f'http://127.0.0.1:{port}/'
    import sys
    script="import http.server,mimetypes,sys;mimetypes.add_type('text/javascript','.js');http.server.test(HandlerClass=lambda *a,**k:http.server.SimpleHTTPRequestHandler(*a,directory=sys.argv[2],**k),port=int(sys.argv[1]),bind='127.0.0.1')"
    log=(output/'browser-server.log').open('w',encoding='utf-8')
    server=subprocess.Popen([sys.executable,'-c',script,str(port),str(output/'browser-build')],cwd=output,stdout=log,stderr=log)
    if not browser_layout._wait_url(url,15):server.terminate();server.wait(timeout=10);log.close();raise RuntimeError('Production browser server failed')
    return server,url,log


def verify_scenarios(source: Path, output: Path, exe: Path, width: int, height: int, cases: dict) -> dict:
    """Compare a supplied event trace against React's actual state and pixels."""
    from PIL import Image
    import re
    capture=browser_layout.capture_layout(source,viewport=(width,height),timeout=90,keep_open=True)
    if not capture or not capture.get('browser_runtime',{}).get('id'):raise RuntimeError('Verification browser unavailable')
    runtime=capture['browser_runtime'];port=int(runtime['id'].rsplit(':',1)[1]);cdp=None
    try:
        targets=json.load(browser_layout._DIRECT.open(f'http://127.0.0.1:{port}/json/list'))
        target=next(t for t in targets if t.get('url')==runtime['url'])
        cdp=browser_layout._CDP(target['webSocketDebuggerUrl'])
        cdp.call('Page.enable');cdp.call('Runtime.enable')
        cdp.call('Emulation.setDeviceMetricsOverride',{'width':width,'height':height,'deviceScaleFactor':1,'mobile':False})
        cdp.call('Page.addScriptToEvaluateOnNewDocument',{'source':browser_layout._DETERMINISTIC_BROWSER_SCRIPT})
        def evaluate(code,wait=False):
            reply=cdp.call('Runtime.evaluate',{'expression':code,'returnByValue':True,'awaitPromise':wait})
            if reply.get('result',{}).get('exceptionDetails'):raise RuntimeError(str(reply['result']['exceptionDetails']))
            return reply.get('result',{}).get('result',{}).get('value')
        results={}
        for name,spec in cases.items():
            if not re.fullmatch(r'[a-zA-Z0-9_-]+',name):raise ValueError('Scenario names must be safe file names')
            actions=spec if isinstance(spec,list) else spec['actions']
            cdp.call('Page.navigate',{'url':runtime['url']})
            for _ in range(200):
                if evaluate('!!window.__uagentState'):break
                time.sleep(.05)
            else:raise RuntimeError('Instrumented React did not mount')
            evaluate('document.fonts.ready.then(()=>true)',True)
            evaluate('window.__uagentResetRandom()')
            clock=evaluate('window.__uagentClock')
            for action in actions:
                key,_,value=action.partition('|')
                if key=='@advance':evaluate(f'window.__uagentAdvance({float(value)})')
                elif key.startswith('@pointer:'):
                    cdp.call('Input.dispatchMouseEvent',{'type':'mouseMoved','x':float(key[9:]),'y':float(value)})
                elif key.startswith('scroll:'):
                    evaluate(f"(()=>{{const e=[...document.querySelectorAll('body *')].find(e=>['auto','scroll'].includes(getComputedStyle(e).overflowY)&&e.scrollHeight>e.clientHeight);if(!e)throw Error('Internal scroll surface missing');e.scrollTop={float(value)};}})()")
                elif key.startswith('source:'):
                    identity,separator,text_value=key.partition('=')
                    source_id,ordinal=identity[7:].rsplit('#',1)
                    operation=(f"Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(e,{json.dumps(text_value)});e.dispatchEvent(new Event('input',{{bubbles:true}}));e.dispatchEvent(new Event('change',{{bubbles:true}}));" if separator else 'e.click();')
                    evaluate("(()=>{const source="+json.dumps(source_id)+",e=[...document.querySelectorAll('[data-uagent-source]')].filter(e=>e.getAttribute('data-uagent-source')===source)["+str(int(ordinal))+"];if(!e)throw Error('Source control missing');"+operation+'})()')
                else:
                    operation=(f"Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(e,{json.dumps(value)});e.dispatchEvent(new Event('input',{{bubbles:true}}));e.dispatchEvent(new Event('change',{{bubbles:true}}));" if value else 'e.click();')
                    evaluate("(()=>{const key="+json.dumps(key)+",e=document.getElementById(key)||[...document.querySelectorAll('[aria-label]')].find(e=>e.getAttribute('aria-label')===key);if(!e)throw Error('Control missing '+key);"+operation+'})()')
                evaluate('new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(()=>r(true))))',True)
            time.sleep(.65)
            if isinstance(spec,dict) and 'animation_ms' in spec:
                evaluate('document.getAnimations().filter(a=>a instanceof CSSAnimation).forEach(a=>{a.pause();a.currentTime='+str(float(spec['animation_ms']))+';})')
            if isinstance(spec,dict) and spec.get('static_endpoints'):
                evaluate("document.getAnimations().forEach(a=>{if(a.effect.getTiming().iterations===Infinity){a.pause();a.currentTime=0;}else{try{a.finish();}catch{a.pause();}}})")
                evaluate('new Promise(r=>requestAnimationFrame(()=>r(true)))',True)
            state=evaluate('window.__uagentState')
            svg_regions=evaluate("""(()=>[...document.querySelectorAll('svg')].filter(e=>!e.ownerSVGElement).map((e,i)=>{
              let r=e.getBoundingClientRect(),x=r.left,y=r.top,right=r.right,bottom=r.bottom;
              for(let p=e;p;p=p.parentElement){const s=getComputedStyle(p);if(s.display==='none'||s.visibility==='hidden'||Number(s.opacity)===0)return null;
                if(p!==e){const b=p.getBoundingClientRect();if(['hidden','clip','scroll','auto'].includes(s.overflowX)){x=Math.max(x,b.left);right=Math.min(right,b.right);}if(['hidden','clip','scroll','auto'].includes(s.overflowY)){y=Math.max(y,b.top);bottom=Math.min(bottom,b.bottom);}}}
              return {node:e.getAttribute('data-uagent-source')||i,rect:[x,y,Math.max(0,right-x),Math.max(0,bottom-y)]};}).filter(Boolean))()""")
            image=cdp.call('Page.captureScreenshot',{'format':'png','fromSurface':True})['result']['data']
            reference=output/(name+'-browser.png');reference.write_bytes(base64.b64decode(image))
            (output/(name+'-browser-state.json')).write_text(json.dumps(state,indent=2),encoding='utf-8')
            actual=capture_sdl(exe,output,clock,';'.join(actions),name=name+'-sdl')
            actual_state=json.loads((output/(name+'-sdl-state.json')).read_text(encoding='utf-8'))
            with Image.open(reference) as a,Image.open(actual) as b:
                if a.size!=(width,height) or b.size!=(width,height):raise RuntimeError('Verification viewport does not match the generated display')
                regions={'full':(0,0,width,height),'top-left':(0,0,width//2,height//2),'top-right':(width//2,0,width,height//2),
                         'bottom-left':(0,height//2,width//2,height),'bottom-right':(width//2,height//2,width,height)}
                scores={key:ssim(a.crop(box),b.crop(box)) for key,box in regions.items()}
                svg_scores=svg_region_scores(a,b,svg_regions)
            errors=state_differences(state,actual_state)
            results[name]={'ssim':scores,'svg_regions':svg_scores,'state_errors':errors,'passed':not errors and min(scores.values())>=.90 and all(s['passed'] for s in svg_scores)}
            print(f'Verified {name}: SSIM={scores["full"]:.4f}, state differences={len(errors)}',flush=True)
        (output/'scenario-results.json').write_text(json.dumps(results,indent=2),encoding='utf-8')
        return results
    finally:
        if cdp:cdp.close()
        browser_layout.close_browser_runtime(runtime['id'])


def convert(source: Path, output: Path, width: int, height: int, *, resume: bool = False, dependencies: Path | None = None, sample_ms: int | None = None, recapture: bool = False, scenarios: dict | None = None, probe_interactions: bool = False, probe_only: bool = False, probe_states: int = 48, probe_actions: int = 180, list_capacity: int = 64) -> dict:
    source, output = source.resolve(), output.resolve()
    # Validate before entering the report-writing finally block. A rejected
    # source/output alias must never create a report inside the source tree.
    if source==output or source in output.parents or output in source.parents:
        raise ValueError('Source and output must be disjoint; source is read-only')
    if not resume and output.exists() and any(output.iterdir()):
        raise ValueError('Output already contains files; choose a new output directory')
    if resume and not (output/'source-sha256.json').is_file():
        raise ValueError('Resume requires an existing conversion manifest')
    if width<=0 or height<=0 or sample_ms is not None and sample_ms<0:raise ValueError('Invalid dimensions or sample time')
    if min(probe_states,probe_actions)<1:raise ValueError('Probe budgets must be positive')
    if type(list_capacity) is not int or not 1<=list_capacity<=1024:raise ValueError('List capacity must be an integer between 1 and 1024')
    report = {'source': str(source), 'output': str(output), 'status': 'failed', 'stages': [], 'blockers': []}
    report['list_capacity']=list_capacity
    saved_env = {key: os.environ.get(key) for key in ['UAGENT_TEST_DETERMINISTIC', 'UAGENT_SKIP_BROWSER_CAPTURE', 'UAGENT_CANVAS_WIDTH', 'UAGENT_CANVAS_HEIGHT','UAGENT_CAPTURE_ADVANCE_MS','UAGENT_REACT_URL','UAGENT_LIST_CAPACITY']}
    old_runtime = browser_layout._ensure_runtime
    server=None;server_log=None
    try:
        if resume:
            staging = output/'source'
            if not (staging/'package.json').is_file() or not (output/'source-sha256.json').is_file():
                raise ValueError('Resume requires output from this command')
            recorded = json.loads((output/'source-sha256.json').read_text(encoding='utf-8'))
            if any(hashlib.sha256((source/p).read_bytes()).hexdigest()!=digest for p,digest in recorded.items()):
                raise ValueError('Source changed; use a fresh output directory')
        else:
            staging = stage_source(source, output, dependencies)
        report['stages'].append('isolated-source')
        if not browser_layout._vite_runtime_works(staging/'node_modules/vite/bin/vite.js', timeout=12):
            raise RuntimeError('Local Vite runtime is incompatible; pass --dependencies with an existing compatible node_modules directory')
        if sample_ms is None:
            source_model=analyze(staging)
            contract=extract_reactive_contract(staging,[source_model.components[key].source_file for key in reachable_components(source_model)])
            periods=[literal(timer['period']) for timer in contract.get('timers',[])]
            sample_ms=int(min((p for p in periods if isinstance(p,(int,float)) and p>0),default=0))
        os.environ.update(UAGENT_TEST_DETERMINISTIC='1', UAGENT_SKIP_BROWSER_CAPTURE='1',
                          UAGENT_CANVAS_WIDTH=str(width), UAGENT_CANVAS_HEIGHT=str(height),UAGENT_CAPTURE_ADVANCE_MS=str(sample_ms),UAGENT_LIST_CAPACITY=str(list_capacity))
        browser_layout._ensure_runtime = lambda path, timeout: browser_layout._vite_runtime_works(path/'node_modules/vite/bin/vite.js', timeout=12)
        receipt = output/'browser-reference.json'
        if resume and receipt.is_file() and not recapture:
            capture = json.loads(receipt.read_text(encoding='utf-8'))
            if capture.get('uagent_run')!={'width':width,'height':height,'sample_ms':sample_ms,'production':True}:
                raise ValueError('Cached capture settings differ; use --recapture')
        else:
            print('Capturing browser evidence', flush=True)
            browser_source=output/'browser-source'
            shutil.copytree(staging,browser_source,dirs_exist_ok=True,ignore=shutil.ignore_patterns('node_modules','browser-layout-v2.json','.uagent-*'))
            if not (browser_source/'node_modules').exists():
                if os.name=='nt':
                    subprocess.run(['cmd','/c','mklink','/J',str(browser_source/'node_modules'),str(staging/'node_modules')],check=True,capture_output=True)
                else:(browser_source/'node_modules').symlink_to(staging/'node_modules',target_is_directory=True)
            files=[str(p.relative_to(staging)).replace('\\','/') for p in (staging/'src').rglob('*.tsx')]
            instrument_browser_source(browser_source,files,staging/'node_modules/typescript/lib/typescript.js')
            report['browser_entry_synthesized']=prepare_browser_entry(browser_source)
            server,url,server_log=serve_production(browser_source,output)
            os.environ['UAGENT_REACT_URL']=url
            capture = browser_layout.capture_layout(browser_source, viewport=(width, height), timeout=90)
            if not capture or not capture.get('screens'):
                raise RuntimeError('Browser capture returned no screens')
            capture['uagent_run']={'width':width,'height':height,'sample_ms':sample_ms,'production':True}
            receipt.write_text(json.dumps(capture, ensure_ascii=False), encoding='utf-8')
        (staging/'browser-layout-v2.json').write_text(json.dumps(capture, ensure_ascii=False), encoding='utf-8')
        report['stages'].append('browser-capture')
        print('Compiling source contract and runtime scene', flush=True)
        model = analyze(staging)
        if probe_interactions or probe_only:
            from tools.interaction_probe import probe
            if server is None:
                server,url,server_log=serve_production(output/'browser-source',output);os.environ['UAGENT_REACT_URL']=url
            contract=extract_reactive_contract(staging,[model.components[key].source_file for key in reachable_components(model)])
            evidence=probe(output/'browser-source',output,width,height,contract,max_states=probe_states,max_actions=probe_actions,capture_scenes=not probe_only)
            report['interaction_probe']={k:evidence[k] for k in ('scope','coverage','truncated','errors') if k in evidence}
            report['stages'].append('bounded-interaction-probe')
            if probe_only:
                report.update(status='evidence-collected',accepted=False,validation_scope='browser evidence only; no LVGL equivalence claimed')
                return report
        scene_file=output/'interaction-probe/scenes.json'
        if scene_file.is_file() and not probe_only:
            probed=json.loads(scene_file.read_text(encoding='utf-8'))
            if not probed.get('complete'):raise ValueError('Interaction scene evidence is incomplete; increase probe budgets or resolve errors')
            contract=extract_reactive_contract(staging,[model.components[key].source_file for key in reachable_components(model)])
            navigations=contract.get('navigation',[])
            if len(navigations)!=1:raise ValueError('Compiled interaction variants require one explicit source switch route')
            route=navigations[0]['state'];grouped={}
            for scene in probed['screens']:
                state=scene['variant_state'].get(route)
                if not isinstance(state,str):raise ValueError('Scene lacks source route state')
                if state not in grouped:grouped[state]={**scene,'state':state,'state_samples':[]}
                else:grouped[state]['state_samples'].append({**scene,'compile_variant':True})
            capture={**capture,'screens':list(grouped.values()),'interaction_variants':True}
            capture['data']=capture['screens'][0]['data']
            capture['visual_effects']=[]
            for index,scene in enumerate(capture['screens']):
                scene['data']['visual_effects']=[]
                for effect in browser_layout.browser_visual_effects(scene['data']):
                    effect['screen_index']=index
                    effect['effect_id']=effect['source_id']=f'browser:{index}:node:{effect["node_index"]}:{effect["kind"]}'
                    scene['data']['visual_effects'].append(effect)
            (staging/'browser-layout-v2.json').write_text(json.dumps(capture,ensure_ascii=False),encoding='utf-8')
            model=analyze(staging)
        bundle = compile_lvgl(model)
        rules = bundle.screen_tree.get('reactive_contract', {}).get('state_rules')
        if rules:
            rules_path = output/'state-rules.json'
            rules_path.write_text(json.dumps(rules, ensure_ascii=False, indent=2), encoding='utf-8')
            report['state_rules'] = {**rules['summary'], 'path': str(rules_path), 'status': rules['status']}
        runtime = bundle.screen_tree.get('runtime_scene', {})
        report.update(nodes=len(runtime.get('nodes', [])), controls=runtime.get('bound_controls', 0),
                      timers=runtime.get('timers', 0), limitations=runtime.get('limitations', []))
        report['blockers'] = list(dict.fromkeys([*runtime.get('blockers', []), *bundle.agent_planning.get('blockers', [])]))
        report['warnings'] = bundle.warnings
        generated = output/'generated'
        existing = next(generated.glob('*.snapshot'), None) if generated.is_dir() else None
        paths = write_aibuilder_custom(bundle, generated/'ui_builder/custom', model=None if existing else model, include_runtime=True)
        report['stages'].append('generated-c')
        if report['blockers']:
            report.update(status='blocked',accepted=False,validation_scope='source/runtime capability gate failed; generated draft is not an executable acceptance')
            return report
        harness=generate_snapshot_harness(generated, existing or paths['snapshot'])
        # The existing scaffold connects a mouse only. Native editable values
        # also need the SDL keypad connected to LVGL's default focus group.
        main=harness['main'];main_source=main.read_text(encoding='utf-8')
        anchor='lv_indev_set_group(mouse, lv_group_get_default());'
        if anchor not in main_source:raise RuntimeError('SDL input scaffold changed; keyboard hook not applied')
        main.write_text(main_source.replace(anchor,anchor+'\n    lv_indev_t * keyboard = lv_sdl_keyboard_create();\n    lv_indev_set_group(keyboard, lv_group_get_default());',1),encoding='utf-8')
        main.write_text(main.read_text(encoding='utf-8').replace('lv_indev_set_group(keyboard, lv_group_get_default());',
            'lv_indev_set_group(keyboard, lv_group_get_default());\n    if(getenv("UAGENT_TEST_DETERMINISTIC")){lv_indev_enable(mouse,false);lv_indev_enable(keyboard,false);}',1),encoding='utf-8')
        print('Building SDL executable', flush=True)
        exe, error = _build_snapshot_simulator(generated)
        if error or not exe:
            raise RuntimeError(error or 'SDL executable missing')
        report.update(exe=str(exe), exe_bytes=exe.stat().st_size, exe_sha256=hashlib.sha256(exe.read_bytes()).hexdigest())
        report['stages'].append('built-sdl')
        from PIL import Image
        screen = capture['screens'][0]
        reference = output/'browser.png'
        reference.write_bytes(base64.b64decode(screen['screenshot_png_base64'].split(',')[-1]))
        clock = screen['data']['runtime_scene'].get('testClock') or 0
        actual = capture_sdl(exe, output, clock-sample_ms,f'@advance|{sample_ms}' if sample_ms else '')
        with Image.open(reference) as a, Image.open(actual) as b:
            report['initial_frame_ssim'] = ssim(a, b)
            scene_nodes={n['id']:n for n in screen['data']['runtime_scene']['nodes']}
            svg_regions=[]
            for node in scene_nodes.values():
                if node['tag']!='svg':continue
                rect=node['rect'];x,y=rect['x'],rect['y'];right,bottom=x+rect['width'],y+rect['height']
                current=node;visible=True
                while current:
                    style=current['style']
                    if style.get('display')=='none' or style.get('visibility')=='hidden' or style.get('opacity')=='0':visible=False
                    if current is not node:
                        if current['tag']=='svg':visible=False
                        box=current['rect']
                        if style.get('overflowX') in {'hidden','clip','scroll','auto'}:x=max(x,box['x']);right=min(right,box['x']+box['width'])
                        if style.get('overflowY') in {'hidden','clip','scroll','auto'}:y=max(y,box['y']);bottom=min(bottom,box['y']+box['height'])
                    current=scene_nodes.get(current.get('parent'))
                if visible:svg_regions.append({'node':node['id'],'rect':[x,y,max(0,right-x),max(0,bottom-y)]})
            report['initial_svg_regions']=svg_region_scores(a,b,svg_regions)
        report['stages'].append('sdl-started-and-captured')
        expected=screen['data']['runtime_scene'].get('logicalState')
        actual_state=json.loads((output/'sdl-state.json').read_text(encoding='utf-8'))
        report['state_errors']=state_differences(expected,actual_state) if expected else ['Browser state oracle missing']
        report['sample_ms']=sample_ms
        report['logic_validation'] = 'sampled-state-only; exhaustive behaviour not proven'
        report['validation_scope']='one timed state at the requested viewport; not exhaustive interaction coverage'
        report['accepted'] = not report['blockers'] and not report['state_errors'] and report['initial_frame_ssim']>=.90 and all(s['passed'] for s in report['initial_svg_regions'])
        if scenarios:
            if server is None:
                server,url,server_log=serve_production(output/'browser-source',output);os.environ['UAGENT_REACT_URL']=url
            report['scenarios']=verify_scenarios(output/'browser-source',output,exe,width,height,scenarios)
            report['accepted']=report['accepted'] and all(case['passed'] for case in report['scenarios'].values())
            report['validation_scope']='declared event/time scenarios at the requested viewport; not exhaustive program equivalence'
        report['status'] = 'sample-passed' if report['accepted'] else 'blocked'
    except Exception as exc:
        report['blockers'].append(str(exc))
        report['status'] = 'failed'
        report['accepted'] = False
    finally:
        if server:server.terminate();server.wait(timeout=10)
        if server_log:server_log.close()
        browser_layout._ensure_runtime = old_runtime
        for key, value in saved_env.items():
            if value is None: os.environ.pop(key, None)
            else: os.environ[key] = value
        if output.is_dir():
            (output/'conversion-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--width', type=int, required=True)
    parser.add_argument('--height', type=int, required=True)
    parser.add_argument('--resume', action='store_true', help='Reuse this command\'s isolated source/evidence')
    parser.add_argument('--dependencies', type=Path, help='Copy a known compatible local node_modules; never install dependencies')
    parser.add_argument('--sample-ms',type=int,help='Deterministic timer advance; default is the first source timer tick')
    parser.add_argument('--recapture',action='store_true',help='Refresh evidence on resume')
    parser.add_argument('--verify-times',help='Comma-separated virtual times in milliseconds, compared against production React')
    parser.add_argument('--scenarios',type=Path,help='JSON mapping scenario names to event lists or {actions,animation_ms}')
    parser.add_argument('--probe-interactions',action='store_true',help='Collect bounded browser click/input and node-lifecycle evidence before compilation')
    parser.add_argument('--probe-only',action='store_true',help='Collect browser interaction evidence only; does not claim LVGL support')
    parser.add_argument('--probe-states',type=int,default=48)
    parser.add_argument('--probe-actions',type=int,default=180)
    parser.add_argument('--list-capacity',type=int,default=64,help='Explicit compiled scalar-list capacity (1..1024); overflow is a BLOCKER')
    args = parser.parse_args()
    if args.width <= 0 or args.height <= 0:
        parser.error('Dimensions must be positive')
    try:
        cases=json.loads(args.scenarios.read_text(encoding='utf-8-sig')) if args.scenarios else {}
        for value in (args.verify_times or '').split(','):
            if value.strip():
                milliseconds=int(value)
                if milliseconds<0:raise ValueError('Verification times must be nonnegative')
                cases[f'time-{milliseconds}']=[f'@advance|{milliseconds}']
        result = convert(args.source, args.output, args.width, args.height, resume=args.resume, dependencies=args.dependencies,sample_ms=args.sample_ms,recapture=args.recapture,scenarios=cases,probe_interactions=args.probe_interactions,probe_only=args.probe_only,probe_states=args.probe_states,probe_actions=args.probe_actions,list_capacity=args.list_capacity)
    except ValueError as exc:
        print(json.dumps({'status':'rejected','error':str(exc)},ensure_ascii=False));return 2
    print(json.dumps({k:v for k,v in result.items() if k not in {'warnings','limitations'}}, ensure_ascii=False, indent=2))
    return 0 if result['status'] in {'sample-passed','evidence-collected'} else 2


if __name__ == '__main__':
    raise SystemExit(main())
