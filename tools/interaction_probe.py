"""Bounded visible-state exploration; evidence, never a replacement for logic compilation."""
from __future__ import annotations

import base64
from collections import Counter, deque
import hashlib
import json
from pathlib import Path
import time

from core import browser_layout


_SNAPSHOT = r"""(()=>{
 const norm=s=>(s||'').replace(/\s+/g,' ').trim(),counts={};
 const nodes=[...document.querySelectorAll('body *')].map(e=>{
  const r=e.getBoundingClientRect(),s=getComputedStyle(e),source=e.getAttribute('data-uagent-source')||'';
  const rect=[r.x,r.y,r.width,r.height].map(v=>Math.round(v*10)/10);
  return {tag:e.tagName.toLowerCase(),source,rect,text:norm([...e.childNodes].filter(n=>n.nodeType===3).map(n=>n.textContent).join(' ')),
   paint:[s.display,s.visibility,s.color,s.backgroundColor,s.borderColor,s.opacity],
   value:('value' in e)?e.value:null,checked:('checked' in e)?e.checked:null,
   effects:{filter:s.filter,backdropFilter:s.backdropFilter,clipPath:s.clipPath,animation:s.animationName,transition:s.transitionDuration}};
 }).filter(n=>!['script','style'].includes(n.tag));
 const controls=[...document.querySelectorAll('button,input,select,textarea,[role=button],[role=tab],a[href]')].map((e,index)=>{
  const r=e.getBoundingClientRect(),s=getComputedStyle(e),source=e.getAttribute('data-uagent-source')||'';
  const ordinal=counts[source]||0;counts[source]=ordinal+1;
  let left=Math.max(0,r.left),top=Math.max(0,r.top),right=Math.min(innerWidth,r.right),bottom=Math.min(innerHeight,r.bottom);
  for(let p=e.parentElement;p;p=p.parentElement){const ps=getComputedStyle(p),pr=p.getBoundingClientRect();
   if(ps.overflowX!=='visible'){left=Math.max(left,pr.left);right=Math.min(right,pr.right);}
   if(ps.overflowY!=='visible'){top=Math.max(top,pr.top);bottom=Math.min(bottom,pr.bottom);}}
  return {index,source,ordinal,tag:e.tagName.toLowerCase(),type:e.type||'',disabled:!!e.disabled,
   label:norm(e.getAttribute('aria-label')||e.labels?.[0]?.textContent||e.innerText||e.name||e.type),
   rendered:r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden',
   in_view:right>left&&bottom>top,value:e.value??null,min:e.min||'',max:e.max||''};
 });
 return {state:window.__uagentState||{},nodes,controls};
})()"""


def visible_signature(snapshot: dict) -> str:
    # History and unmounted component state are retained in the evidence but
    # deliberately not used to claim exhaustive state-space exploration.
    value={'nodes':snapshot['nodes'],'controls':snapshot['controls']}
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def node_delta(before: dict, after: dict) -> dict:
    def counts(s):return Counter((n['source'],n['tag']) for n in s['nodes'])
    a,b=counts(before),counts(after)
    return {'added':sum((b-a).values()),'removed':sum((a-b).values())}


def candidates(snapshot: dict) -> list[dict]:
    actions=[]
    for control in snapshot['controls']:
        if control['disabled'] or not control['rendered']:continue
        base={k:control[k] for k in ('source','ordinal','index','label','in_view')}
        if control['tag']=='input' and control['type'] not in {'button','submit','checkbox','radio'}:
            samples={'date':['2030-02-14'],'time':['09:45'],
                     'range':[control['min'] or '0',control['max'] or '100'],'number':['1']}
            for value in samples.get(control['type'],[]):actions.append({**base,'kind':'input','value':value})
        elif control['tag'] in {'button','a'} or control['type'] in {'checkbox','radio'}:
            actions.append({**base,'kind':'click'})
    return actions


def probe(source: Path, output: Path, width: int, height: int, contract: dict,
          *, max_states: int = 48, max_actions: int = 180, max_depth: int = 8, capture_scenes: bool = False) -> dict:
    if min(max_states,max_actions,max_depth)<1:raise ValueError('Probe budgets must be positive')
    destination=output/'interaction-probe';destination.mkdir(exist_ok=True)
    capture=browser_layout.capture_layout(source,viewport=(width,height),timeout=90,keep_open=True)
    if not capture or not capture.get('browser_runtime',{}).get('id'):raise RuntimeError('Probe browser unavailable')
    runtime=capture['browser_runtime'];port=int(runtime['id'].rsplit(':',1)[1]);cdp=None
    report={'schema':'uagent.interaction-probe/v1','scope':'bounded visible-state and edge samples; not exhaustive history, async, or program equivalence',
            'accepted_as_compiled':False,'budgets':{'states':max_states,'actions':max_actions,'depth':max_depth},
            'animation_policy':'finite animation endpoints; infinite animation frozen at phase zero',
            'execution':'programmatic DOM events; clipped controls are recorded, not proof of physical click reachability',
            'states':[],'edges':[],'truncated':False,'errors':[],
            'source_states':[{k:s.get(k) for k in ('name','owner','initial')} for s in contract.get('states',[])]}
    authored={f'{n["file"]}:{n["start"]}':bool(n['attrs'].get('onClick') or n['attrs'].get('onChange')) for n in contract.get('jsx',[])}
    scenes=[]
    try:
        targets=json.load(browser_layout._DIRECT.open(f'http://127.0.0.1:{port}/json/list'))
        target=next(t for t in targets if t.get('url')==runtime['url']);cdp=browser_layout._CDP(target['webSocketDebuggerUrl'])
        cdp.call('Page.enable');cdp.call('Runtime.enable')
        cdp.call('Emulation.setDeviceMetricsOverride',{'width':width,'height':height,'deviceScaleFactor':1,'mobile':False})
        cdp.call('Page.addScriptToEvaluateOnNewDocument',{'source':browser_layout._DETERMINISTIC_BROWSER_SCRIPT})
        def evaluate(code,wait=False):
            reply=cdp.call('Runtime.evaluate',{'expression':code,'returnByValue':True,'awaitPromise':wait})
            result=reply.get('result',{})
            if result.get('exceptionDetails'):raise RuntimeError(str(result['exceptionDetails']))
            return result.get('result',{}).get('value')
        def settle():
            evaluate("(async()=>{await document.fonts.ready;await new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)));for(const a of document.getAnimations()){if(a.effect.getTiming().iterations===Infinity){a.pause();a.currentTime=0;}else{try{a.finish();}catch{a.pause();}}}await new Promise(r=>requestAnimationFrame(r));return true;})()",True)
        def reset():
            cdp.call('Page.navigate',{'url':runtime['url']})
            for _ in range(200):
                if evaluate('!!window.__uagentState && document.readyState==="complete"'):break
                time.sleep(.03)
            else:raise RuntimeError('React state instrumentation did not initialize')
            settle()
        def act(action):
            evaluate("(()=>{window.__uagentMutations={added:0,removed:0};window.__uagentObserver=new MutationObserver(records=>{for(const r of records)for(const [key,items] of [['added',r.addedNodes],['removed',r.removedNodes]])for(const n of items)if(n.nodeType===1)window.__uagentMutations[key]+=1+n.querySelectorAll('*').length;});window.__uagentObserver.observe(document.body,{childList:true,subtree:true});})()")
            data=json.dumps(action,ensure_ascii=False)
            code=r"""(()=>{const a=ACTION,els=[...document.querySelectorAll('button,input,select,textarea,[role=button],[role=tab],a[href]')];
             const e=a.source?els.filter(e=>e.getAttribute('data-uagent-source')===a.source)[a.ordinal]:els[a.index];
             if(!e||e.disabled)throw Error('Probe target missing/disabled: '+a.label);
             if(a.kind==='click')e.click();else{Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(e,a.value);e.dispatchEvent(new Event('input',{bubbles:true}));e.dispatchEvent(new Event('change',{bubbles:true}));}return true;})()""".replace('ACTION',data)
            evaluate(code);settle()
            return evaluate('(()=>{window.__uagentObserver.disconnect();return window.__uagentMutations;})()')
        reset();initial=evaluate(_SNAPSHOT);seen={visible_signature(initial):0};queue=deque([(0,[],initial)])
        def save(index,trace,snapshot):
            name=f'state-{index:03}'
            (destination/(name+'.json')).write_text(json.dumps(snapshot,ensure_ascii=False),encoding='utf-8')
            shot=cdp.call('Page.captureScreenshot',{'format':'png','fromSurface':True})['result']['data']
            (destination/(name+'.png')).write_bytes(base64.b64decode(shot))
            if capture_scenes:
                runtime_scene=evaluate(browser_layout._RUNTIME_SCENE_CAPTURE,wait=True)
                legacy=[{**n,'index':n['id'],'parentIndex':n.get('parent'),'identity':f'dom:{n["id"]}',
                         'text':' '.join(t['text'] for t in n.get('texts',[])),'className':n['attrs'].get('class','')} for n in runtime_scene['nodes']]
                data={'nodes':legacy,'runtime_scene':runtime_scene,'canvas':{'x':0,'y':0,'width':width,'height':height}}
                data['overflow']=browser_layout.overflow_evidence(data)
                data['region_manifest']=browser_layout.region_manifest(legacy,data['canvas'])
                scenes.append({'state':f'variant-{index}','path':[a['label'] for a in trace],
                               'variant_state':snapshot['state'],'data':data,'screenshot_png_base64':shot})
            report['states'].append({'id':index,'trace':trace,'state':snapshot['state'],'nodes':len(snapshot['nodes']),
                                     'controls':len(snapshot['controls']),'evidence':name+'.json','screenshot':name+'.png'})
        save(0,[],initial)
        while queue and len(report['edges'])<max_actions:
            index,trace,before=queue.popleft()
            if len(trace)>=max_depth:report['truncated']=True;continue
            for action in candidates(before):
                if len(report['edges'])>=max_actions:report['truncated']=True;break
                reset()
                for step in trace:act(step)
                replay=evaluate(_SNAPSHOT)
                if visible_signature(replay)!=visible_signature(before):
                    report['errors'].append({'state':index,'error':'Replay diverged; edge not accepted'});continue
                mutations=act(action);after=evaluate(_SNAPSHOT);signature=visible_signature(after)
                destination_index=seen.get(signature)
                if destination_index is None and len(seen)<max_states:
                    destination_index=len(seen);seen[signature]=destination_index
                    save(destination_index,trace+[action],after);queue.append((destination_index,trace+[action],after))
                elif destination_index is None:report['truncated']=True
                changed={k:{'before':replay['state'].get(k),'after':v} for k,v in after['state'].items() if replay['state'].get(k)!=v}
                report['edges'].append({'from':index,'to':destination_index,'action':action,'source_handler':authored.get(action['source']),
                                        'changed_state':changed,'node_delta':mutations,'net_node_delta':node_delta(replay,after),
                                        'visible_changed':signature!=visible_signature(replay)})
            print(f'Probe state {index}: {len(seen)} visible states, {len(report["edges"])} edges',flush=True)
        report['truncated']=report['truncated'] or bool(queue)
        report['coverage']={'visible_states':len(seen),'sampled_edges':len(report['edges']),
                            'source_handler_no_observed_change':sum(e['source_handler'] is True and not e['changed_state'] and not e['visible_changed'] for e in report['edges']),
                            'no_source_handler':sum(e['source_handler'] is False for e in report['edges']),
                            'node_add_remove_edges':sum(any(e['node_delta'].values()) for e in report['edges'])}
        return report
    except Exception as exc:
        report['errors'].append({'error':str(exc),'stage':'probe-execution'})
        raise
    finally:
        if capture_scenes:
            (destination/'scenes.json').write_text(json.dumps({'screens':scenes,'complete':not report['truncated'] and not report['errors']},ensure_ascii=False),encoding='utf-8')
        (destination/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        if cdp:cdp.close()
        browser_layout.close_browser_runtime(runtime['id'])
