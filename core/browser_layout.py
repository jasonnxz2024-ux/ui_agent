"""Browser-backed layout evidence for React/Figma Make projects.

The static scanner is intentionally conservative.  This module supplements it
with the browser's computed boxes (``getBoundingClientRect``), so flex/grid,
Tailwind and responsive rules are measured rather than guessed.
"""
from __future__ import annotations

import json
import hashlib
import os
import re
import socket
import subprocess
import tempfile
import time
import urllib.parse
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from core.dynamic_validation import (discover_canvas, make_state_samples, overflow_evidence,
                                     region_manifest, interactive_control_specs)


# Browser capture must never send localhost through a corporate/http proxy.
# Some desktop environments return a misleading 503 "Forwarding failure" when
# they do, even though Vite is healthy and listening on 127.0.0.1.
_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_PERSISTENT_RUNTIMES: dict[str, dict[str, Any]] = {}


_DETERMINISTIC_BROWSER_SCRIPT = r"""(()=>{
  let seed=1, clock=new Date(2026,9,1,12,34,56).getTime();const OriginalDate=Date,jobs=[];
  Math.random=()=>{seed=(Math.imul(seed,1664525)+1013904223)>>>0;return seed/4294967296;};
  window.__uagentResetRandom=()=>{seed=1;};
  window.Date=class extends OriginalDate {constructor(...args){super(...(args.length?args:[clock]));} static now(){return clock;}};
  window.__uagentClock=clock;
  window.setInterval=(fn,delay)=>{jobs.push({fn,delay,next:clock+delay});return jobs.length;};
  window.clearInterval=id=>{if(jobs[id-1])jobs[id-1].fn=null;};
  window.__uagentAdvance=ms=>{const end=clock+ms;while(true){const next=Math.min(...jobs.filter(j=>j.fn).map(j=>j.next));if(next>end)break;clock=next;for(const j of jobs)if(j.fn&&j.next<=clock){j.fn();j.next+=j.delay;}}clock=end;window.__uagentClock=clock;};
})();"""


_RUNTIME_SCENE_CAPTURE = r"""(() => {
  const elements = [...document.querySelectorAll('body *')].filter(e => !['SCRIPT','STYLE','LINK'].includes(e.tagName));
  const ids = new Map(elements.map((e, i) => [e, i]));
  const fonts = {};
  const chars = new Set([...Array(95)].map((_, i) => String.fromCharCode(i + 32)));
  for (const e of elements) for (const c of e.textContent || '') chars.add(c);
  const glyphFont = (s,e) => {
    const size = parseFloat(s.fontSize), css = `${s.fontWeight} ${s.fontSize} ${s.fontFamily}`;
    let background='rgb(255,255,255)';for(let p=e;p;p=p.parentElement){const c=getComputedStyle(p).backgroundColor;if(c!=='rgba(0, 0, 0, 0)'&&c!=='transparent'){background=c;break;}}
    const foreground=e.tagName.toLowerCase()==='text'?s.fill:s.color,key=css+'|'+foreground+'|'+background;
    if (fonts[key]) return key;
    const canvas = document.createElement('canvas'); canvas.width = canvas.height = Math.max(96, Math.ceil(size * 4));
    const ctx = canvas.getContext('2d', {willReadFrequently:true,alpha:false}); ctx.font = css;
    const rgb=c=>{ctx.fillStyle=c;ctx.fillRect(0,0,1,1);return [...ctx.getImageData(0,0,1,1).data].slice(0,3);};
    const bg=rgb(background),fg=rgb(foreground),weights=[.2126,.7152,.0722],contrast=fg.reduce((v,c,i)=>v+(c-bg[i])*weights[i],0);
    const m = ctx.measureText('Mg'), ascent = Math.ceil(m.fontBoundingBoxAscent || size), descent = Math.ceil(m.fontBoundingBoxDescent || size * .25);
    const glyphs = [], baseline = canvas.height - 16;
    for (const c of [...chars].sort((a,b) => a.codePointAt(0)-b.codePointAt(0))) {
      if (c.codePointAt(0) < 32) continue;
      ctx.fillStyle=background;ctx.fillRect(0,0,canvas.width,canvas.height);ctx.fillStyle=foreground;ctx.fillText(c,16,baseline);
      const pixels = ctx.getImageData(0,0,canvas.width,canvas.height).data;
      const coverage=(x,y)=>Math.max(0,Math.min(255,Math.round(255*weights.reduce((v,w,i)=>v+(pixels[(y*canvas.width+x)*4+i]-bg[i])*w,0)/(contrast||1))));
      let left=canvas.width, top=canvas.height, right=-1, bottom=-1;
      for(let y=0;y<canvas.height;y++) for(let x=0;x<canvas.width;x++) if(coverage(x,y)) {left=Math.min(left,x);top=Math.min(top,y);right=Math.max(right,x);bottom=Math.max(bottom,y);}
      let bytes=''; const w=Math.max(0,right-left+1), h=Math.max(0,bottom-top+1);
      for(let y=top;y<=bottom;y++) for(let x=left;x<=right;x++) bytes+=String.fromCharCode(coverage(x,y));
      glyphs.push({code:c.codePointAt(0),advance:ctx.measureText(c).width,w,h,x:w?left-16:0,y:h?baseline-bottom-1:0,bitmap:btoa(bytes)});
    }
    fonts[key]={css,size,ascent,descent,glyphs,raster:'opaque browser canvas luminance coverage',foreground,background}; return key;
  };
  const box = r => ({x:r.x,y:r.y,width:r.width,height:r.height});
  const nodes = elements.map((e,i) => {
    const s=getComputedStyle(e), r=e.getBoundingClientRect(), attrs=Object.fromEntries([...e.attributes].map(a=>[a.name,a.value]));
    const textNodes=[...e.childNodes].filter(n=>n.nodeType===3 && n.textContent.trim());
    const texts=textNodes.length?[(()=>{const range=document.createRange();range.setStartBefore(textNodes[0]);range.setEndAfter(textNodes[textNodes.length-1]);return {text:textNodes.map(n=>n.textContent).join(''),rect:box(range.getBoundingClientRect())};})()]:[];
    const props=['display','visibility','opacity','position','color','backgroundColor','borderRadius','borderTopWidth','borderRightWidth','borderBottomWidth','borderLeftWidth','borderTopColor','borderRightColor','borderBottomColor','borderLeftColor','fontSize','fontWeight','fontFamily','lineHeight','letterSpacing','textAlign','textAnchor','overflowX','overflowY','fill','stroke','strokeWidth','strokeDasharray','strokeDashoffset','strokeLinecap','transform','zIndex'];
    const style=Object.fromEntries(props.map(k=>[k,s[k]]));
    for(const k of ['transitionProperty','transitionDuration','transitionTimingFunction','boxShadow','textShadow','filter','clipPath','maskImage','backgroundImage'])style[k]=s[k];
    const native={value:e.value??null,checked:!!e.checked,min:e.min??null,max:e.max??null,step:e.step??null,disabled:!!e.disabled};
    let geometry=null;
    let effect=null;
    const filterId=(attrs.filter||s.filter||'').match(/#([^"')]+)/)?.[1],filter=filterId&&document.getElementById(filterId);
    if(filter){const primitives=[...filter.children].map(n=>n.localName.toLowerCase()),blur=filter.querySelector('feGaussianBlur'),merge=filter.querySelector('feMerge'),inputs=merge?[...merge.children].map(n=>n.getAttribute('in')):[];
      effect=primitives.length===2&&primitives.includes('fegaussianblur')&&primitives.includes('femerge')&&inputs.length===2&&inputs[1]==='SourceGraphic'&&inputs[0]===blur?.getAttribute('result')&&[null,'SourceGraphic'].includes(blur?.getAttribute('in'))?{kind:'gaussian-merge-glow',sigma:Number(blur.getAttribute('stdDeviation'))}:{kind:'unsupported',primitives};}
    if(typeof e.getTotalLength==='function'){
      try{const length=e.getTotalLength(),matrix=e.getScreenCTM(),count=Math.max(1,Math.min(1024,Math.ceil(length/1.5)));
        geometry={points:Array.from({length:count+1},(_,i)=>{const p=e.getPointAtLength(length*i/count),q=new DOMPoint(p.x,p.y).matrixTransform(matrix);return [q.x-r.x,q.y-r.y];}),length};
      }catch(error){geometry={error:String(error)};}
    }
    if(e.matches('input[type=range]')) {
      const declarations={};
      const scan=rules=>{for(const rule of rules||[]) {if(rule.cssRules) scan(rule.cssRules); if(rule.selectorText && rule.style) for(const selector of rule.selectorText.split(',')) {const suffix='::-webkit-slider-thumb'; if(selector.trim().endsWith(suffix)) {try {if(e.matches(selector.trim().slice(0,-suffix.length))) for(const p of rule.style) declarations[p]=rule.style.getPropertyValue(p);} catch {}}}}};
      for(const sheet of document.styleSheets) {try {scan(sheet.cssRules);} catch {}}
      native.thumb=declarations;
    }
    return {id:i,parent:ids.get(e.parentElement)??null,tag:e.tagName.toLowerCase(),attrs,rect:box(r),style,texts,font:texts.length?glyphFont(s,e):null,native,geometry,effect,scroll:{x:e.scrollLeft,y:e.scrollTop,width:e.scrollWidth,height:e.scrollHeight,clientWidth:e.clientWidth,clientHeight:e.clientHeight}};
  });
  const freeze=document.createElement('style'); freeze.textContent='*{transition:none!important}';document.head.appendChild(freeze);
  for(const e of elements.filter(e=>e.matches('input[type=checkbox]'))) {
    const saved=e.checked, node=nodes[ids.get(e)]; node.native.paintStates={};
    for(const checked of [false,true]) {
      e.checked=checked;
      node.native.paintStates[String(checked)]=[...e.parentElement.querySelectorAll('*')].filter(c=>c!==e).map(c=>({id:ids.get(c),rect:box(c.getBoundingClientRect()),backgroundColor:getComputedStyle(c).backgroundColor}));
    }
    e.checked=saved;
  }
  freeze.remove();
  const animations={},keyframes={},animationRules=[];
  const scanAnimations=rules=>{for(const rule of rules||[]){if(rule.type===CSSRule.KEYFRAMES_RULE){keyframes[rule.name]=[...rule.cssRules].map(k=>({offset:k.keyText,opacity:k.style.opacity}));}else{if(rule.style?.animationName&&rule.style.animationName!=='none')animationRules.push(rule);if(rule.cssRules)scanAnimations(rule.cssRules);}}};
  for(const sheet of document.styleSheets){try{scanAnimations(sheet.cssRules);}catch{}}
  for(const rule of animationRules)if(/^\.[\w-]+$/.test(rule.selectorText))animations[rule.selectorText.slice(1)]={duration:rule.style.animationDuration,easing:rule.style.animationTimingFunction,iterations:rule.style.animationIterationCount,frames:keyframes[rule.style.animationName]||[]};
  return {schema:'uagent.runtime-scene/v1',viewport:{width:innerWidth,height:innerHeight},nodes,fonts,animations,testClock:window.__uagentClock??null,logicalState:window.__uagentState??null};
})()"""


def close_browser_runtime(runtime_id: str | None) -> None:
    """Close one Browser/Vite runtime retained for a validation session."""
    runtime = _PERSISTENT_RUNTIMES.pop(str(runtime_id or ""), None)
    if not runtime:
        return
    for name in ("chrome", "server"):
        process = runtime.get(name)
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
    stream = runtime.get("log_stream")
    if stream:
        stream.close()


def _browser() -> str | None:
    candidates = [
        os.environ.get("CHROME_PATH"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    ]
    return next((p for p in candidates if p and Path(p).is_file()), None)


def _node() -> str:
    for candidate in (os.environ.get("UAGENT_NODE"), r"D:\node_js\node.exe"):
        if candidate and Path(candidate).is_file():
            return candidate
    return "node"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _spawn_server(command: list[str], cwd: Path, log_stream):
    """Keep Vite alive independently of the Python coordinator on Windows."""
    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    return subprocess.Popen(command, cwd=cwd, stdout=log_stream, stderr=subprocess.STDOUT,
                            creationflags=flags, close_fds=os.name != "nt")


def _wait_url(url: str, timeout: float = 20) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            with _DIRECT.open(url, timeout=1):
                return True
        except urllib.error.HTTPError:
            # A Vite transform error still proves the server is reachable;
            # let Chrome load it so the actual module error can be diagnosed.
            return True
        except Exception:
            time.sleep(.25)
    return False


def _ensure_runtime(source_dir: Path, timeout: float) -> bool:
    """Install a runnable Vite runtime for Figma Make exports when needed.

    Figma Make often emits npm alias keys such as ``lucide-react@0.x`` or
    ``figma:asset`` which npm rejects as package names.  We remove only those
    invalid manifest entries in a temporary backup, install, then restore the
    original package.json.  The resulting node_modules is kept for capture.
    """
    vite = source_dir / "node_modules" / ".bin" / ("vite.cmd" if os.name == "nt" else "vite")
    vite_package = source_dir / "node_modules" / "vite" / "bin" / "vite.js"
    if (vite.is_file() or vite_package.is_file()) and _vite_runtime_works(vite_package, timeout=min(timeout, 12)):
        return True
    manifest = source_dir / "package.json"
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
        cleaned = {}
        for section in ("dependencies", "devDependencies", "optionalDependencies"):
            values = data.get(section, {})
            if not isinstance(values, dict):
                continue
            valid = {}
            for name, version in values.items():
                # npm package names cannot contain an @version suffix in the
                # key and Figma's virtual figma:asset protocol is browser-only.
                if ":" in name or ("@" in name and (not name.startswith("@") or name.count("@") > 1)):
                    continue
                if str(version).startswith("figma:"):
                    continue
                valid[name] = version
            cleaned[section] = valid
        cleaned.setdefault("dependencies", {}).setdefault("react", "18.3.1")
        cleaned.setdefault("dependencies", {}).setdefault("react-dom", "18.3.1")
        cleaned.setdefault("devDependencies", {}).setdefault("vite", "6.3.5")
        original = manifest.read_bytes()
        manifest.write_text(json.dumps({**data, **cleaned}, indent=2), encoding="utf-8", newline="\n")
        try:
            # Figma Make exports commonly include pnpm-lock.yaml.  Use the
            # project's lockfile so dependency resolution is reproducible;
            # fall back to npm/yarn only when their lockfile is present.
            if (source_dir / "pnpm-lock.yaml").is_file():
                manager, args = ("pnpm.cmd" if os.name == "nt" else "pnpm"), ["install", "--ignore-scripts"]
            elif (source_dir / "yarn.lock").is_file():
                manager, args = ("yarn.cmd" if os.name == "nt" else "yarn"), ["install", "--ignore-scripts"]
            else:
                manager, args = ("npm.cmd" if os.name == "nt" else "npm"), ["install", "--ignore-scripts", "--no-audit", "--no-fund"]
            result = subprocess.run([manager, *args], cwd=source_dir,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, encoding="utf-8", errors="replace",
                                    timeout=max(30, int(timeout)))
            (source_dir / ".uagent-install.log").write_text(result.stdout or "", encoding="utf-8")
        finally:
            manifest.write_bytes(original)
        return result.returncode == 0 and _vite_runtime_works(vite_package, timeout=min(timeout, 12))
    except (OSError, ValueError, subprocess.SubprocessError):
        try:
            if 'original' in locals(): manifest.write_bytes(original)
        except OSError: pass
        return False


def _vite_runtime_works(vite_package: Path, timeout: float = 12) -> bool:
    """Probe Vite itself, including optional Rolldown native bindings.

    Checking only ``vite.js`` is insufficient: npm/pnpm can leave a partially
    installed optional platform package behind while the JavaScript entrypoint
    still exists. The probe makes collection repair that state automatically.
    """
    if not vite_package.is_file():
        return False
    try:
        result = subprocess.run(
            [_node(), str(vite_package), "--version"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            timeout=max(3, int(timeout)), cwd=vite_package.parents[3],
        )
        return result.returncode == 0 and bool(result.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        return False


def _source_navigation_labels(source_dir: Path) -> list[str]:
    """Find labels attached to explicit state/router/link navigation controls."""
    labels: list[str] = []
    for path in source_dir.rglob("*"):
        if path.suffix.lower() not in {".js", ".jsx", ".ts", ".tsx"} or any(
            part in {"node_modules", "dist", "build", ".next", "simulator", "docs", "doc", "reference", "references", "assets"} for part in path.parts
        ):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in re.finditer(r"<(?:button|Button|a|Link)\b(?P<attrs>[^>]*)>", text):
            attrs = match.group("attrs")
            if not (re.search(r"\bset[A-Z]\w*\s*\(\s*['\"]", attrs) or
                    re.search(r"\b(?:navigate|(?:router|history)\.(?:push|replace))\s*\(", attrs) or
                    re.search(r"\b(?:href|to|data-page)\s*=", attrs)):
                continue
            attr_label = re.search(r"\b(?:data-page|aria-label)\s*=\s*['\"]([^'\"]+)", attrs)
            if attr_label:
                labels.append(attr_label.group(1))
    return list(dict.fromkeys(labels))


def browser_visual_effects(data: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Normalize computed visual-effect evidence from one browser snapshot."""
    if not isinstance(data, dict):
        return []
    effects: list[dict[str, Any]] = []
    style_keys = {
        "filter": "filter", "boxShadow": "shadow", "textShadow": "shadow",
        "backgroundImage": "gradient", "maskImage": "mask", "clipPath": "clip",
    }
    for node in data.get("nodes", []):
        if not isinstance(node, dict):
            continue
        style = node.get("style") if isinstance(node.get("style"), dict) else {}
        rect = node.get("rect") if isinstance(node.get("rect"), dict) else {}
        for key, kind in style_keys.items():
            value = str(style.get(key, "") or "").strip()
            if not value or value in {"none", "none none"}:
                continue
            effects.append({
                "effect_id": f"browser:{node.get('index', 0)}:{kind}",
                "source_id": f"browser:{node.get('index', 0)}:{kind}",
                "kind": kind, "value": value,
                "bounds": [float(rect.get("x", 0)), float(rect.get("y", 0)),
                            float(rect.get("width", 0)), float(rect.get("height", 0))],
                "node_index": node.get("index"),
            })
            if kind == "gradient":
                parsed = parse_css_gradient(value)
                if parsed:
                    effects[-1].update(parsed)
        svg = str(node.get("svg") or "")
        for gradient in parse_svg_gradient(svg):
            effects.append({
                "effect_id": f"browser:{node.get('index', 0)}:gradient:{gradient.get('id') or len(effects)}",
                "source_id": f"browser:{node.get('index', 0)}:gradient:{gradient.get('id') or len(effects)}",
                "kind": "gradient", "value": gradient.get("id") or "svg-gradient",
                "gradient_type": gradient.get("gradient_type"), "stops": gradient.get("stops", []),
                "bounds": [float(rect.get("x", 0)), float(rect.get("y", 0)), float(rect.get("width", 0)), float(rect.get("height", 0))],
                "node_index": node.get("index"), "svg_gradient": True,
            })
        if svg and re.search(r"<filter\b|feGaussianBlur|filter\s*=", svg, re.I):
            effects.append({
                "effect_id": f"browser:{node.get('index', 0)}:filter",
                "source_id": f"browser:{node.get('index', 0)}:filter",
                "kind": "filter", "value": "svg-filter",
                "bounds": [float(rect.get("x", 0)), float(rect.get("y", 0)),
                            float(rect.get("width", 0)), float(rect.get("height", 0))],
                "node_index": node.get("index"),
            })
    return effects


def _split_css_args(value: str) -> list[str]:
    """Split a CSS function argument list without breaking rgba()/color stops."""
    parts: list[str] = []
    start = 0
    depth = 0
    for index, char in enumerate(value):
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
        elif char == "," and depth == 0:
            part = value[start:index].strip()
            if part:
                parts.append(part)
            start = index + 1
    tail = value[start:].strip()
    if tail:
        parts.append(tail)
    return parts


def parse_css_gradient(value: str) -> dict[str, Any]:
    """Parse linear/radial CSS gradients into renderer-neutral paint data.

    This intentionally keeps direction/position as source evidence.  Target
    backends can choose native two-stop gradients or bounded approximations.
    """
    match = re.search(r"(?P<type>linear|radial)-gradient\s*\((?P<body>.*)\)\s*$", str(value or "").strip(), re.I | re.S)
    if not match:
        return {}
    parts = _split_css_args(match.group("body"))
    if not parts:
        return {}
    header = ""
    if re.match(r"(?:to\s+|(?:-?\d+(?:\.\d+)?\s*(?:deg|turn|rad)\b)|(?:circle|ellipse)\b|.*\bat\s+)", parts[0], re.I):
        header = parts.pop(0)
    stops: list[dict[str, Any]] = []
    for part in parts:
        color = re.match(r"(?P<color>#[0-9a-f]{3,8}|(?:rgba?|hsla?)\([^)]*\)|[a-z]+)", part, re.I)
        if not color:
            continue
        stop: dict[str, Any] = {"color": color.group("color")}
        position = part[color.end():].strip()
        if position:
            stop["position"] = position
        stops.append(stop)
    if not stops:
        return {}
    gradient_type = match.group("type").lower()
    result: dict[str, Any] = {
        "gradient_type": gradient_type,
        "direction": header if gradient_type == "linear" else None,
        "position": header if gradient_type == "radial" else None,
        "stops": stops,
    }
    angle = re.search(r"(-?\d+(?:\.\d+)?)\s*(deg|turn|rad)\b", header, re.I)
    if angle:
        number = float(angle.group(1))
        unit = angle.group(2).lower()
        result["angle"] = number * (360.0 if unit == "turn" else 57.2957795 if unit == "rad" else 1.0)
    return result


def parse_svg_gradient(svg: str) -> list[dict[str, Any]]:
    """Extract SVG gradient definitions from a browser-captured SVG node."""
    gradients: list[dict[str, Any]] = []
    pattern = r"<(?P<type>linear|radial)Gradient\b(?P<attrs>[^>]*)>(?P<body>.*?)</(?P=type)Gradient>"
    for match in re.finditer(pattern, str(svg or ""), re.I | re.S):
        attrs = match.group("attrs")
        id_match = re.search(r"\bid\s*=\s*[\"']([^\"']+)[\"']", attrs, re.I)
        stops: list[dict[str, Any]] = []
        for stop in re.finditer(r"<stop\b(?P<attrs>[^>]*)/?>", match.group("body"), re.I):
            raw = stop.group("attrs")
            def attr(name: str) -> str | None:
                found = re.search(rf"\b{name}\s*=\s*[\"']([^\"']+)[\"']", raw, re.I)
                return found.group(1) if found else None
            color = attr("stop-color")
            if color:
                item: dict[str, Any] = {"color": color}
                if attr("offset"):
                    item["position"] = attr("offset")
                if attr("stop-opacity"):
                    item["opacity"] = attr("stop-opacity")
                stops.append(item)
        if stops:
            gradients.append({"gradient_type": match.group("type").lower(), "id": id_match.group(1) if id_match else None, "stops": stops, "attributes": attrs})
    return gradients


def browser_background_paint(data: dict[str, Any] | None, *, width: float = 0, height: float = 0) -> dict[str, Any] | None:
    """Return the measured app-frame background paint, if it is a gradient."""
    if not isinstance(data, dict):
        return None
    nodes = [node for node in data.get("nodes", []) if isinstance(node, dict)]
    candidates = []
    for node in nodes:
        rect = node.get("rect") if isinstance(node.get("rect"), dict) else {}
        w, h = float(rect.get("width", 0)), float(rect.get("height", 0))
        style = node.get("style") if isinstance(node.get("style"), dict) else {}
        image = str(style.get("backgroundImage", "") or "").strip()
        if image and image.casefold() != "none":
            exact = int((not width or abs(w - width) <= 2) and (not height or abs(h - height) <= 2))
            distance = abs(w - width) + abs(h - height) if width and height else 0.0
            candidates.append((exact, -distance, w * h, -int(node.get("index", 0)), node, image))
        for gradient in parse_svg_gradient(str(node.get("svg") or "")):
            exact = int((not width or abs(w - width) <= 2) and (not height or abs(h - height) <= 2))
            distance = abs(w - width) + abs(h - height) if width and height else 0.0
            preferred = int(str(gradient.get("id", "")).casefold() in {"ambient", "background", "backdrop"})
            candidates.append((exact, preferred, -distance, w * h, -int(node.get("index", 0)), node, gradient))
    if not candidates:
        return None
    chosen = max(candidates, key=lambda item: item[:4])
    if isinstance(chosen[-1], dict):
        node, gradient = chosen[-2], chosen[-1]
        rect = node.get("rect") or {}
        opacity = 1.0
        # SVG gradient definitions do not carry the opacity of their paint
        # consumer. Recover it from the measured element that references the
        # selected id (ambient-pulse in the common root-SVG case).
        gradient_id = str(gradient.get("id") or "")
        for consumer in nodes:
            consumer_svg = str(consumer.get("svg") or "")
            attrs = consumer.get("attrs") if isinstance(consumer.get("attrs"), dict) else {}
            if consumer is node or (f"url(#{gradient_id})" not in consumer_svg and str(attrs.get("fill", "")) != f"url(#{gradient_id})"):
                continue
            style = consumer.get("style") if isinstance(consumer.get("style"), dict) else {}
            try:
                opacity = max(0.0, min(1.0, float(style.get("opacity", 1.0))))
            except (TypeError, ValueError):
                pass
        return {**gradient, "value": f"svg-gradient:{gradient_id or 'anonymous'}", "bounds": [float(rect.get(k, 0)) for k in ("x", "y", "width", "height")], "node_index": node.get("index"), "svg_gradient": True, "opacity": opacity}
    _, _, _, _, node, image = chosen
    parsed = parse_css_gradient(image)
    if not parsed:
        return None
    rect = node.get("rect") or {}
    return {**parsed, "value": image, "bounds": [float(rect.get(k, 0)) for k in ("x", "y", "width", "height")],
            "node_index": node.get("index")}


class _CDP:
    """Tiny WebSocket client; avoids adding a Python browser dependency."""
    def __init__(self, url: str):
        parsed = urllib.parse.urlparse(url)
        self.sock = socket.create_connection((parsed.hostname, parsed.port), timeout=10)
        key = "dGhlIHNhbXBsZSBub25jZQ=="
        path = parsed.path or "/"
        self.sock.sendall((f"GET {path} HTTP/1.1\r\nHost: {parsed.hostname}:{parsed.port}\r\n"
                           f"Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                           "Sec-WebSocket-Version: 13\r\n\r\n").encode())
        response = self.sock.recv(4096)
        if b"101" not in response.split(b"\r\n", 1)[0]:
            raise RuntimeError("Chrome DevTools websocket handshake failed")
        self.seq = 0

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.seq += 1
        payload = json.dumps({"id": self.seq, "method": method, "params": params or {}}).encode()
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        size = len(masked)
        header = bytes([0x81, 0x80 | size]) if size < 126 else bytes([0x81, 0xFE]) + size.to_bytes(2, "big")
        self.sock.sendall(header + mask + masked)
        while True:
            first = self.sock.recv(2)
            if len(first) < 2:
                raise RuntimeError("Chrome DevTools closed the connection")
            length = first[1] & 127
            if length == 126: length = int.from_bytes(self.sock.recv(2), "big")
            elif length == 127: length = int.from_bytes(self.sock.recv(8), "big")
            if first[1] & 128: self.sock.recv(4)
            data = b""
            while len(data) < length: data += self.sock.recv(length - len(data))
            if first[0] & 15 == 8: raise RuntimeError("Chrome DevTools closed the connection")
            message = json.loads(data.decode())
            if message.get("id") == self.seq: return message

    def close(self) -> None:
        try: self.sock.close()
        except OSError: pass


def capture_layout(source_dir: Path, *, viewport: tuple[int, int] = (1024, 600), timeout: float = 25,
                   visible: bool = False, keep_open: bool = False) -> dict[str, Any] | None:
    """Run the project in Chrome and return measured visible nodes.

    Returns ``None`` when the source cannot be run (for example a source-only
    fixture without node_modules).  Generation then records a static fallback.
    """
    browser = _browser()
    from generator.parser_registry import detect_parser
    parser_id = detect_parser(source_dir).parser_id
    interaction_labels = _source_navigation_labels(source_dir)
    if parser_id == "meter":
        interaction_labels.extend(label for label in ("Dashboard", "Settings") if label not in interaction_labels)
    package = source_dir / "package.json"
    vite_bin = source_dir / "node_modules" / "vite" / "bin" / "vite.js"
    runnable_vite = _ensure_runtime(source_dir, min(timeout, 90)) or vite_bin.is_file()
    dist_index = source_dir / "dist" / "index.html"
    if not browser or not package.is_file() or (not runnable_vite and not dist_index.is_file()):
        return None
    external_url = os.environ.get("UAGENT_REACT_URL", "").strip()
    port = _free_port() if not external_url else urllib.parse.urlparse(external_url).port
    npm = "npm.cmd" if os.name == "nt" else "npm"
    vite_log = source_dir / ".uagent-vite.log"
    log_stream = vite_log.open("w", encoding="utf-8") if not external_url else None
    if external_url:
        server = None
    elif runnable_vite:
        command = ([_node(), str(vite_bin), "--host", "0.0.0.0", "--port", str(port)]
                   if vite_bin.is_file() else
                   [npm, "run", "dev", "--", "--host", "127.0.0.1", "--port", str(port)])
        server = _spawn_server(command, source_dir, log_stream)
    else:
        server = _spawn_server([os.environ.get("PYTHON", "python"), "-m", "http.server", str(port), "--bind", "127.0.0.1"],
                               source_dir / "dist", log_stream)
    chrome = None
    runtime_id: str | None = None
    runtime_registered = False
    try:
        # First Vite request can compile a large Figma Make graph; allow it
        # enough time instead of treating a slow compile as missing runtime.
        page_url = external_url or f"http://127.0.0.1:{port}/"
        if not _wait_url(page_url, min(timeout, 180)):
            try:
                (source_dir / ".uagent-browser-error.json").write_text(json.dumps({"stage": "vite-start", "error": vite_log.read_text(encoding="utf-8", errors="replace")[-4000:]}, ensure_ascii=False, indent=2), encoding="utf-8")
            except OSError: pass
            return None
        profile = tempfile.mkdtemp(prefix="uagent-chrome-")
        debug_port = _free_port()
        end = time.time() + min(timeout, 10)
        version = None
        chrome_args = [browser]
        if not visible:
            chrome_args.append("--headless=new")
        chrome_args.extend(["--disable-gpu", "--no-first-run", "--no-default-browser-check",
                            f"--remote-debugging-port={debug_port}",
                            "--remote-debugging-address=127.0.0.1", "--remote-allow-origins=*",
                            f"--user-data-dir={profile}", f"--window-size={viewport[0]},{viewport[1]}",
                            "about:blank"])
        chrome = subprocess.Popen(chrome_args,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        while time.time() < end and not version:
            try:
                with _DIRECT.open(f"http://127.0.0.1:{debug_port}/json/version", timeout=1) as f:
                    version = json.load(f)
            except Exception: time.sleep(.2)
        if not version:
            try: (source_dir / ".uagent-browser-error.json").write_text(json.dumps({"stage":"cdp-version","port":debug_port}), encoding="utf-8")
            except OSError: pass
            return None
        target_url = "http://127.0.0.1:%d/json/new?%s" % (debug_port, urllib.parse.quote(page_url, safe=""))
        request = urllib.request.Request(target_url, method="PUT")
        with _DIRECT.open(request, timeout=3) as f: target = json.load(f)
        cdp = _CDP(target["webSocketDebuggerUrl"])
        try:
            cdp.call("Emulation.setDeviceMetricsOverride", {"width": viewport[0], "height": viewport[1], "deviceScaleFactor": 1, "mobile": False})
            cdp.call("Page.enable")
            cdp.call("Runtime.enable")
            if os.environ.get('UAGENT_TEST_DETERMINISTIC') == '1':
                cdp.call('Page.addScriptToEvaluateOnNewDocument', {'source': _DETERMINISTIC_BROWSER_SCRIPT})
            nav_reply = cdp.call("Page.navigate", {"url": page_url})
            if nav_reply.get("error"):
                try: (source_dir / ".uagent-browser-error.json").write_text(json.dumps({"stage":"page-navigate","reply":nav_reply}, ensure_ascii=False), encoding="utf-8")
                except OSError: pass
            # Figma Make/Vite may return index.html before the large React
            # graph has mounted. A short fixed wait captured only html/body.
            time.sleep(2.0)
            # Wait for React to mount instead of assuming a fixed delay.
            mount_deadline = time.time() + 20.0
            while time.time() < mount_deadline:
                try:
                    probe = cdp.call("Runtime.evaluate", {"expression": "document.body && document.body.innerHTML.length", "returnByValue": True})
                    length = probe.get("result", {}).get("result", {}).get("value", 0)
                    if length and int(length) > 200: break
                except Exception: pass
                time.sleep(0.25)
            expression = r"""(()=>{const all=Array.from(document.querySelectorAll('*'));const visible=all.filter(e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden';});const root=(document.body||document.documentElement).getBoundingClientRect();const index=new Map(visible.map((e,i)=>[e,i]));const nodes=visible.map((e,i)=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e),tag=e.tagName.toLowerCase(),attrs={},directText=Array.from(e.childNodes).filter(n=>n.nodeType===3).map(n=>n.textContent||'').join(' ').replace(/\s+/g,' ').trim().slice(0,200);for(const k of ['cx','cy','r','stroke','fill','stroke-width','stroke-dasharray','stroke-dashoffset','transform','viewBox','filter','d']){const v=e.getAttribute(k);if(v!==null)attrs[k]=v;}return {index:i,parentIndex:index.has(e.parentElement)?index.get(e.parentElement):null,tag:tag,text:directText||(!e.children.length?(e.textContent||'').trim().slice(0,200):''),className:e.getAttribute('class')||'',rect:{x:r.x,y:r.y,width:r.width,height:r.height},style:{display:s.display,position:s.position,opacity:s.opacity,color:s.color,backgroundColor:s.backgroundColor,stroke:s.stroke,fill:s.fill,strokeWidth:s.strokeWidth,strokeDasharray:s.strokeDasharray,strokeDashoffset:s.strokeDashoffset,borderRadius:s.borderRadius,borderColor:s.borderColor,borderWidth:s.borderWidth,borderTopColor:s.borderTopColor,borderRightColor:s.borderRightColor,borderBottomColor:s.borderBottomColor,borderLeftColor:s.borderLeftColor,borderTopWidth:s.borderTopWidth,borderRightWidth:s.borderRightWidth,borderBottomWidth:s.borderBottomWidth,borderLeftWidth:s.borderLeftWidth,padding:s.padding,gap:s.gap,flexDirection:s.flexDirection,fontSize:s.fontSize,lineHeight:s.lineHeight,textAlign:s.textAlign,textAnchor:s.textAnchor,filter:s.filter,boxShadow:s.boxShadow,textShadow:s.textShadow,backgroundImage:s.backgroundImage,maskImage:s.maskImage,clipPath:s.clipPath,overflow:s.overflow,overflowX:s.overflowX,overflowY:s.overflowY},attrs:attrs,src:e.currentSrc||e.getAttribute('src')||null,aria:e.getAttribute('aria-label')||null,ariaValueNow:e.getAttribute('aria-valuenow')||null,ariaValueMin:e.getAttribute('aria-valuemin')||null,ariaValueMax:e.getAttribute('aria-valuemax')||null,role:e.getAttribute('role')||null,type:e.getAttribute('type')||null,svg:tag==='svg'?e.outerHTML.slice(0,20000):null};});return {viewport:{width:innerWidth,height:innerHeight},root:{x:root.x,y:root.y,width:root.width,height:root.height},nodes:nodes};})()"""

            def evaluate_value(source: str, *, await_promise: bool = False) -> Any:
                reply = cdp.call("Runtime.evaluate", {"expression": source, "returnByValue": True,
                                                       "awaitPromise": await_promise})
                result = reply.get("result", {}).get("result", {})
                if result.get("subtype") == "error" or "description" in result and result.get("type") == "object":
                    raise RuntimeError(result.get("description", "Runtime.evaluate failed"))
                return result.get("value")

            def click_named(label: str) -> bool:
                encoded = json.dumps(label, ensure_ascii=False)
                source = f"""(async()=>{{const norm=s=>(s||'').replace(/\\s+/g,' ').trim();const target={encoded};const els=[...document.querySelectorAll('button,[role=tab],a[href]')].filter(e=>!e.disabled&&e.getBoundingClientRect().width>0&&e.getBoundingClientRect().height>0);let b=null;if(target.startsWith('__uagent_click_'))b=els[Number(target.slice(15))]||null;if(!b)b=els.find(e=>norm(e.getAttribute('data-page')||e.getAttribute('aria-label')||e.innerText)===target);if(!b)return false;b.click();await new Promise(r=>setTimeout(r,350));return true}})()"""
                return bool(evaluate_value(source, await_promise=True))

            def button_labels() -> list[str]:
                allowed = json.dumps(interaction_labels, ensure_ascii=False)
                source = rf"""(()=>{{const norm=s=>(s||'').replace(/\s+/g,' ').trim(),allowed=new Set({allowed});return [...document.querySelectorAll('button,[role=tab],a[href]')].filter(e=>!e.disabled&&e.getBoundingClientRect().width>0&&e.getBoundingClientRect().height>0).filter(e=>e.hasAttribute('data-page')||e.getAttribute('role')==='tab'||e.hasAttribute('href')||allowed.has(norm(e.getAttribute('data-page')||e.getAttribute('aria-label')||e.innerText))).map((e,i)=>norm(e.getAttribute('data-page')||e.getAttribute('aria-label')||e.innerText)||('__uagent_click_'+i))}})()"""
                return list(evaluate_value(source) or [])

            identity_expression = r"""(()=>Array.from(document.querySelectorAll('*')).filter(e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden'}).map((e,i)=>({index:i,attrs:Object.fromEntries(['id','data-openhmi-id','data-figma-node-id','href','pathLength','aria-pressed','aria-checked','checked'].filter(k=>e.hasAttribute(k)).map(k=>[k,e.getAttribute(k)]))})))()"""
            def merge_identities(snapshot: dict[str, Any]) -> dict[str, Any]:
                evaluate_value('document.fonts.ready.then(()=>true)', await_promise=True)
                if os.environ.get('UAGENT_TEST_DETERMINISTIC') == '1':
                    advance=max(0,int(os.environ.get('UAGENT_CAPTURE_ADVANCE_MS','0')))
                    evaluate_value(f'(async()=>{{if(!window.__uagentCapturePrepared){{window.__uagentCapturePrepared=true;window.__uagentResetRandom?.();window.__uagentAdvance?.({advance});await new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)));}}return true;}})()',await_promise=True)
                    snapshot=evaluate_value(expression)
                identities = evaluate_value(identity_expression) or []
                by_index = {int(item.get('index')): item.get('attrs', {}) for item in identities if isinstance(item, dict)}
                for node in snapshot.get('nodes', []):
                    attrs = node.setdefault('attrs', {})
                    attrs.update(by_index.get(int(node.get('index', -1)), {}))
                    explicit = attrs.get('data-openhmi-id') or attrs.get('data-figma-node-id') or attrs.get('id')
                    node['identity'] = explicit or f"dom:{node.get('index', 0)}"
                    node['identity_confidence'] = 1.0 if attrs.get('data-openhmi-id') else (0.95 if attrs.get('data-figma-node-id') else (0.85 if attrs.get('id') else 0.25))
                # Preserve paint and control metadata omitted by the legacy
                # semantic sampler, including zero-sized checkbox inputs.
                runtime = evaluate_value(_RUNTIME_SCENE_CAPTURE)
                if isinstance(runtime, dict):
                    snapshot['runtime_scene'] = runtime
                return snapshot

            control_expression = r"""(()=>Array.from(document.querySelectorAll('input[type=checkbox],[role=switch],button[aria-pressed]')).filter(e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden'}).map((e,index)=>{const identity=e.getAttribute('data-openhmi-id')||e.getAttribute('data-figma-node-id')||e.id||null;return identity?{identity,kind:e.matches('input[type=checkbox]')?'checkbox':e.getAttribute('role')==='switch'?'switch':'button-pressed',index,checked:e.checked===true,ariaChecked:e.getAttribute('aria-checked'),ariaPressed:e.getAttribute('aria-pressed')}:null}).filter(Boolean))()"""

            def control_specs() -> list[dict[str, Any]]:
                return list(evaluate_value(control_expression) or [])

            def interactive_sample(path: list[str], spec: dict[str, Any]) -> dict[str, Any] | None:
                """Reset to one screen, trigger one control, then capture real evidence."""
                cdp.call('Page.navigate', {'url': page_url})
                time.sleep(.75)
                for step in path:
                    if not click_named(step):
                        return None
                identity = json.dumps(str(spec.get('identity')), ensure_ascii=False)
                before = evaluate_value(f"(()=>{{const id={identity};const e=[...document.querySelectorAll('input[type=checkbox],[role=switch],button[aria-pressed]')].find(e=>(e.getAttribute('data-openhmi-id')||e.getAttribute('data-figma-node-id')||e.id)===id);return e?{{checked:e.checked===true,ariaChecked:e.getAttribute('aria-checked'),ariaPressed:e.getAttribute('aria-pressed')}}:null}})()")
                if not before:
                    return None
                clicked = evaluate_value(f"(async()=>{{const id={identity};const e=[...document.querySelectorAll('input[type=checkbox],[role=switch],button[aria-pressed]')].find(e=>(e.getAttribute('data-openhmi-id')||e.getAttribute('data-figma-node-id')||e.id)===id);if(!e)return false;e.click();await new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)));return true}})()", await_promise=True)
                if not clicked:
                    return None
                after = evaluate_value(f"(()=>{{const id={identity};const e=[...document.querySelectorAll('input[type=checkbox],[role=switch],button[aria-pressed]')].find(e=>(e.getAttribute('data-openhmi-id')||e.getAttribute('data-figma-node-id')||e.id)===id);return e?{{checked:e.checked===true,ariaChecked:e.getAttribute('aria-checked'),ariaPressed:e.getAttribute('aria-pressed')}}:null}})()")
                data = evaluate_value(expression)
                data = merge_identities(data) if data else None
                if not data:
                    return None
                data['canvas'] = discover_canvas(data, viewport)
                data['overflow'] = overflow_evidence(data)
                data['region_manifest'] = region_manifest(data.get('nodes', []), data['canvas'])
                shot = cdp.call('Page.captureScreenshot', {'format': 'png', 'fromSurface': True})
                return {'variant': f"control:{spec.get('identity')}", 'type': 'interactive',
                        'timestamp': time.time(), 'trigger': {'kind': 'click', 'identity': spec.get('identity'),
                        'control': spec.get('kind'), 'before': before, 'after': after}, 'data': data,
                        'signature': hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False, default=str).encode('utf-8')).hexdigest(),
                        'screenshot_png_base64': shot.get('result', {}).get('data', '')}

            value = evaluate_value(expression)
            value = merge_identities(value) if value else value
            if not value:
                try: (source_dir / ".uagent-browser-error.json").write_text(json.dumps({"stage":"runtime-evaluate-empty","url":page_url}), encoding="utf-8")
                except OSError: pass
                return None
            try:
                value["scroll"] = evaluate_value("(()=>({clientWidth:Math.max(document.documentElement.clientWidth,document.body?.clientWidth||0),clientHeight:Math.max(document.documentElement.clientHeight,document.body?.clientHeight||0),scrollWidth:Math.max(document.documentElement.scrollWidth,document.body?.scrollWidth||0),scrollHeight:Math.max(document.documentElement.scrollHeight,document.body?.scrollHeight||0)}))()")
            except Exception:
                value["scroll"] = {}
            screens: list[dict[str, Any]] = []
            queue: list[list[str]] = [[]]
            queued = {()}
            signatures: set[str] = set()
            active_identities: set[str] = set()
            while queue and len(screens) < 24:
                path = queue.pop(0)
                if path:
                    cdp.call("Page.navigate", {"url": page_url})
                    time.sleep(.75)
                reachable = True
                captured_options: list[str] = []
                for step in path:
                    if not click_named(step):
                        reachable = False
                        break
                    step_options = evaluate_value(r"""(()=>{const norm=s=>(s||'').replace(/\s+/g,' ').trim();return [...document.querySelectorAll('[role=option],[data-slot=select-item],[data-slot=select-content] [data-value]')].map(e=>norm(e.innerText||e.textContent||e.getAttribute('data-value'))).filter(Boolean)})()""")
                    if step_options:
                        captured_options.extend(step_options)
                if not reachable:
                    continue
                screen_data = evaluate_value(expression)
                screen_data = merge_identities(screen_data) if screen_data else screen_data
                if not screen_data:
                    continue
                # Re-read extents for every state. The initial page snapshot
                # is not authoritative after tab/navigation clicks.
                try:
                    screen_data["scroll"] = evaluate_value(
                        "(()=>({clientWidth:Math.max(document.documentElement.clientWidth,document.body?.clientWidth||0),clientHeight:Math.max(document.documentElement.clientHeight,document.body?.clientHeight||0),scrollWidth:Math.max(document.documentElement.scrollWidth,document.body?.scrollWidth||0),scrollHeight:Math.max(document.documentElement.scrollHeight,document.body?.scrollHeight||0)}))()"
                    ) or {}
                except Exception:
                    screen_data["scroll"] = {}
                option_values = evaluate_value(r"""(()=>{const norm=s=>(s||'').replace(/\s+/g,' ').trim();return [...document.querySelectorAll('[role=option],[data-slot=select-item],[data-slot=select-content] [data-value]')].map(e=>norm(e.innerText||e.textContent||e.getAttribute('data-value'))).filter(Boolean)})()""")
                if option_values:
                    screen_data["select_options"] = list(dict.fromkeys(option_values))
                elif captured_options:
                    screen_data["select_options"] = list(dict.fromkeys(captured_options))
                labels = button_labels()
                active_state = evaluate_value(r"""(()=>{const e=document.querySelector('[role=tab][aria-selected=true],[aria-current],button.active,.tab-btn.active');return e?((e.getAttribute('data-page')||e.getAttribute('aria-label')||e.innerText||'').trim()):''})()""")
                active_identity = str(active_state).strip().casefold()
                # Clicking an already-active tab can change transient classes,
                # focus rings, clocks or sensor values. It is still the same
                # application state and must map to exactly one ScreenIR.
                if active_identity and active_identity in active_identities:
                    continue
                # Dynamic values and clocks must not turn the same active tab
                # into multiple states. Deduplicate by active identity plus
                # stable DOM structure/geometry instead of current text.
                signature = json.dumps([str(active_state).casefold(), [
                    (n.get("tag"), n.get("className"), n.get("role"), n.get("type"),
                     round(float(n.get("rect", {}).get("x", 0))), round(float(n.get("rect", {}).get("y", 0))),
                     round(float(n.get("rect", {}).get("width", 0))), round(float(n.get("rect", {}).get("height", 0))))
                    for n in screen_data.get("nodes", [])]], ensure_ascii=False)
                if signature in signatures:
                    continue
                signatures.add(signature)
                if active_identity:
                    active_identities.add(active_identity)
                screen_data["canvas"] = discover_canvas(screen_data, viewport)
                screen_data["overflow"] = overflow_evidence(screen_data)
                screen_data["region_manifest"] = region_manifest(screen_data.get("nodes", []), screen_data["canvas"])
                screenshot = cdp.call("Page.captureScreenshot", {"format": "png", "fromSurface": True})
                default_png = screenshot.get("result", {}).get("data", "")
                # Capture a real post-rAF sample without creating another
                # Screen. This catches clock/animation state while keeping a
                # deterministic, bounded sample count per screen.
                raf_data = evaluate_value("(async()=>{await new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)));return " + expression + "})()", await_promise=True)
                raf_data = merge_identities(raf_data) if raf_data else screen_data
                raf_data["canvas"] = discover_canvas(raf_data, viewport)
                raf_data["overflow"] = overflow_evidence(raf_data)
                raf_data["region_manifest"] = region_manifest(raf_data.get("nodes", []), raf_data["canvas"])
                raf_signature = hashlib.sha256(json.dumps(raf_data, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()
                raf_shot = cdp.call("Page.captureScreenshot", {"format": "png", "fromSurface": True})
                raf_png = raf_shot.get("result", {}).get("data", "")
                state_path = [active_identity] if active_identity else path
                samples = [
                                    {"variant": "default", "type": "default", "timestamp": time.time(),
                                     "trigger": None, "data": screen_data,
                                     "signature": hashlib.sha256(json.dumps(screen_data, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).hexdigest(),
                                     "screenshot_png_base64": default_png},
                                    {"variant": "time:raf-2", "type": "requestAnimationFrame", "timestamp": time.time(),
                                     "trigger": {"kind": "requestAnimationFrame", "frames": 2}, "data": raf_data,
                                     "signature": raf_signature, "screenshot_png_base64": raf_png},
                                ]
                # Discover a small, explicit set once per clean screen and
                # exercise each independently.  These remain samples of this
                # Screen; they must never enter screen deduplication.
                for spec in interactive_control_specs(screen_data.get("nodes", []), limit=6):
                    if len(samples) >= 8:
                        break
                    sample = interactive_sample(path, spec)
                    if sample:
                        samples.append(sample)
                screens.append({"path": state_path, "state": str(active_state), "data": screen_data,
                                "state_samples": samples, "screenshot_png_base64": default_png})
                for label in labels:
                    child = tuple([*path, label])
                    if len(path) < 3 and len(queued) < 200 and child not in queued:
                        queued.add(child)
                        queue.append(list(child))
            verified_paths = list(dict.fromkeys(step for screen in screens for step in screen.get("path", []) if step))
            visual_effects = []
            for screen_index, screen in enumerate(screens):
                for effect in browser_visual_effects(screen.get("data")):
                    effect["screen_index"] = screen_index
                    visual_effects.append(effect)
            state_contract = []
            for screen_index, screen in enumerate(screens):
                controls = []
                for sample in screen.get("state_samples", []):
                    trigger = sample.get("trigger") if isinstance(sample, dict) else None
                    if isinstance(trigger, dict) and trigger.get("kind") == "click":
                        controls.append({"identity": trigger.get("identity"), "control": trigger.get("control"),
                                         "before": trigger.get("before"), "after": trigger.get("after")})
                state_contract.append({"state_id": f"screen:{screen_index}", "path": screen.get("path", []),
                                       "navigation": [{"kind": "click-label", "value": step} for step in screen.get("path", [])],
                                       "controls": controls})
            runtime_id = f"browser:{os.getpid()}:{debug_port}"
            if keep_open:
                _PERSISTENT_RUNTIMES[runtime_id] = {"chrome": chrome, "server": server,
                                                    "log_stream": log_stream, "url": page_url,
                                                    "profile": profile}
                runtime_registered = True
            return {"schema": "uagent.browser-layout/v4", "source": str(source_dir),
                    "captured_at": time.time(), "status": "browser-interaction",
                    "data": value, "screens": screens,
                    "navigation_verification": {"attempted": interaction_labels, "verified": verified_paths},
                    "state_contract": state_contract,
                    "browser_runtime": {"id": runtime_id if keep_open else None, "url": page_url,
                                        "visible": visible, "persistent": bool(keep_open)},
                    "visual_effects": visual_effects}
        finally: cdp.close()
    except Exception as exc:
        try:
            (source_dir / ".uagent-browser-error.json").write_text(
                json.dumps({"stage": "browser-capture", "error": str(exc)}, ensure_ascii=False, indent=2),
                encoding="utf-8", newline="\n")
        except OSError:
            pass
        return None
    finally:
        if not runtime_registered:
            if chrome: chrome.terminate()
            if server: server.terminate()
            if log_stream: log_stream.close()
