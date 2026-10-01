"""Reproducible, offline-planned React -> LVGL/SDL conversion command.

All build/capture writes take place under a new output directory. Unsupported
semantics are reported, never repaired by a remote model or counted as success.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
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


def capture_sdl(exe: Path, output: Path, clock: int, actions: str = '', name: str = 'sdl') -> Path:
    from PIL import Image
    target = output / (name+'.bmp')
    target.unlink(missing_ok=True)
    env = os.environ.copy()
    env.update(UAGENT_TEST_DETERMINISTIC='1', UAGENT_TEST_CLOCK_MS=str(clock),
               UAGENT_SIMULATOR_CAPTURE=str(target), UAGENT_TEST_ACTIONS=actions,
               UAGENT_TEST_STATE_FILE=str(output/(name+'-state.json')))
    with (output/(name+'.log')).open('w', encoding='utf-8') as log:
        process = subprocess.Popen([str(exe)], cwd=exe.parent, env=env, stdout=log, stderr=log)
        try:
            for _ in range(200):
                if process.poll() is not None:
                    raise RuntimeError(f'SDL exited with code {process.returncode}')
                try:
                    with Image.open(target) as image:
                        image.load(); image.save(output/(name+'.png'))
                    return output/(name+'.png')
                except (OSError, PermissionError):
                    time.sleep(.1)
            raise RuntimeError('SDL capture timed out')
        finally:
            process.terminate(); process.wait(timeout=10)


def state_differences(a, b, path='') -> list[str]:
    if isinstance(a,dict):return [v for key,value in a.items() for v in state_differences(value,b.get(key) if isinstance(b,dict) else None,path+'.'+key)]
    equal=abs(a-b)<=1e-7 if isinstance(a,(int,float)) and not isinstance(a,bool) and isinstance(b,(int,float)) else a==b
    return [] if equal else [f'{path}: browser={a!r}, SDL={b!r}']


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
                elif key.startswith('scroll:'):
                    evaluate(f"(()=>{{const e=[...document.querySelectorAll('body *')].find(e=>['auto','scroll'].includes(getComputedStyle(e).overflowY)&&e.scrollHeight>e.clientHeight);if(!e)throw Error('Internal scroll surface missing');e.scrollTop={float(value)};}})()")
                else:
                    operation=(f"Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(e,{json.dumps(value)});e.dispatchEvent(new Event('input',{{bubbles:true}}));e.dispatchEvent(new Event('change',{{bubbles:true}}));" if value else 'e.click();')
                    evaluate("(()=>{const key="+json.dumps(key)+",e=document.getElementById(key)||[...document.querySelectorAll('[aria-label]')].find(e=>e.getAttribute('aria-label')===key);if(!e)throw Error('Control missing '+key);"+operation+'})()')
                evaluate('new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(()=>r(true))))',True)
            time.sleep(.65)
            if isinstance(spec,dict) and 'animation_ms' in spec:
                evaluate('document.getAnimations().filter(a=>a instanceof CSSAnimation).forEach(a=>{a.pause();a.currentTime='+str(float(spec['animation_ms']))+';})')
            state=evaluate('window.__uagentState')
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
            errors=state_differences(state,actual_state)
            results[name]={'ssim':scores,'state_errors':errors,'passed':not errors and min(scores.values())>=.90}
            print(f'Verified {name}: SSIM={scores["full"]:.4f}, state differences={len(errors)}',flush=True)
        (output/'scenario-results.json').write_text(json.dumps(results,indent=2),encoding='utf-8')
        return results
    finally:
        if cdp:cdp.close()
        browser_layout.close_browser_runtime(runtime['id'])


def convert(source: Path, output: Path, width: int, height: int, *, resume: bool = False, dependencies: Path | None = None, sample_ms: int | None = None, recapture: bool = False, scenarios: dict | None = None) -> dict:
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
    report = {'source': str(source), 'output': str(output), 'status': 'failed', 'stages': [], 'blockers': []}
    saved_env = {key: os.environ.get(key) for key in ['UAGENT_TEST_DETERMINISTIC', 'UAGENT_SKIP_BROWSER_CAPTURE', 'UAGENT_CANVAS_WIDTH', 'UAGENT_CANVAS_HEIGHT','UAGENT_CAPTURE_ADVANCE_MS','UAGENT_REACT_URL']}
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
                          UAGENT_CANVAS_WIDTH=str(width), UAGENT_CANVAS_HEIGHT=str(height),UAGENT_CAPTURE_ADVANCE_MS=str(sample_ms))
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
        bundle = compile_lvgl(model)
        runtime = bundle.screen_tree.get('runtime_scene', {})
        report.update(nodes=len(runtime.get('nodes', [])), controls=runtime.get('bound_controls', 0),
                      timers=runtime.get('timers', 0), limitations=runtime.get('limitations', []))
        report['blockers'] = list(dict.fromkeys([*runtime.get('blockers', []), *bundle.agent_planning.get('blockers', [])]))
        report['warnings'] = bundle.warnings
        generated = output/'generated'
        existing = next(generated.glob('*.snapshot'), None) if generated.is_dir() else None
        paths = write_aibuilder_custom(bundle, generated/'ui_builder/custom', model=None if existing else model, include_runtime=True)
        report['stages'].append('generated-c')
        generate_snapshot_harness(generated, existing or paths['snapshot'])
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
        report['stages'].append('sdl-started-and-captured')
        expected=screen['data']['runtime_scene'].get('logicalState')
        actual_state=json.loads((output/'sdl-state.json').read_text(encoding='utf-8'))
        report['state_errors']=state_differences(expected,actual_state) if expected else ['Browser state oracle missing']
        report['sample_ms']=sample_ms
        report['logic_validation'] = 'sampled-state-only; exhaustive behaviour not proven'
        report['validation_scope']='one timed state at the requested viewport; not exhaustive interaction coverage'
        report['accepted'] = not report['blockers'] and not report['state_errors'] and report['initial_frame_ssim']>=.90
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
        result = convert(args.source, args.output, args.width, args.height, resume=args.resume, dependencies=args.dependencies,sample_ms=args.sample_ms,recapture=args.recapture,scenarios=cases)
    except ValueError as exc:
        print(json.dumps({'status':'rejected','error':str(exc)},ensure_ascii=False));return 2
    print(json.dumps({k:v for k,v in result.items() if k not in {'warnings','limitations'}}, ensure_ascii=False, indent=2))
    return 0 if result['status']=='sample-passed' else 2


if __name__ == '__main__':
    raise SystemExit(main())
