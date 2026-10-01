"""LVGL v8/AiBuilder custom.c renderer.

Generated functions receive a parent object, so AiBuilder can call them from a
screen's setup function without the generator owning the full project file.
No application callback is invented: detected React handlers become safe C
comments plus audit metadata for a later binding pass.
"""
from __future__ import annotations

import json
import base64
import hashlib
import math
import re
from pathlib import Path
from typing import Any, Iterable

from adapters.contracts import AdaptedWidget
from core.model import Component, ReactProjectModel

from .model import GeneratedUnit, GenerationBundle
from core.reactive_contract import literal as reactive_literal, substitute as reactive_substitute


def _runtime_expression(value: Any, contract: dict[str, Any], depth: int = 0) -> str:
    if depth > 24 or not isinstance(value,list):
        raise ValueError('Unsupported or recursive reactive expression')
    kind = value[0]
    if kind == 'literal':
        return _c_string(value[1]) if isinstance(value[1],str) else ('1' if value[1] is True else '0' if value[1] in (False,None) else repr(value[1]))
    if kind == 'event':
        return 'value'
    if kind == 'id':
        if value[1] in contract.get('constants',{}):
            return _runtime_expression(contract['constants'][value[1]],contract,depth+1)
        if any(s['name']==value[1] for s in contract.get('states',[])):
            return 'runtime_state_'+c_symbol(value[1])
    if kind == 'member':
        if value[1] == ['id','Math'] and reactive_literal(value[2]) == 'PI':
            return '3.14159265358979323846'
        if value[1][0] == 'id' and isinstance(reactive_literal(value[2]),str) and any(s['name']==value[1][1] for s in contract.get('states',[])):
            return 'runtime_state_'+c_symbol(value[1][1])+'_'+c_symbol(reactive_literal(value[2]))
    if kind == 'binary':
        operator={'===':'==','!==':'!='}.get(value[1],value[1])
        if operator not in {'+','-','*','/','%','>','<','>=','<=','==','!=','&&','||'}:
            raise ValueError(f'Unsupported operator {operator}')
        return f'({_runtime_expression(value[2],contract,depth+1)} {operator} {_runtime_expression(value[3],contract,depth+1)})'
    if kind == 'unary' and value[1] in {'-','+','!'}:
        return f'({value[1]}{_runtime_expression(value[2],contract,depth+1)})'
    if kind == 'conditional':
        return '('+_runtime_expression(value[1],contract,depth+1)+' ? '+_runtime_expression(value[2],contract,depth+1)+' : '+_runtime_expression(value[3],contract,depth+1)+')'
    if kind == 'call':
        callee,args=value[1],value[2]
        if callee == ['id','Number']:
            return _runtime_expression(args[0],contract,depth+1)
        if callee[0] == 'member':
            name=reactive_literal(callee[2])
            if callee[1] == ['id','Math']:
                if name == 'random': return 'runtime_random()'
                functions={'min':'fmin','max':'fmax','floor':'floor','ceil':'ceil','round':'round','abs':'fabs'}
                if name in functions:
                    parts=[_runtime_expression(a,contract,depth+1) for a in args]
                    if name in {'min','max'}:
                        result=parts[0]
                        for part in parts[1:]: result=f'{functions[name]}({result},{part})'
                        return result
                    return f'{functions[name]}('+','.join(parts)+')'
            if callee[1] == ['new',['id','Date'],[]] and name in {'getHours','getMinutes','getSeconds'}:
                return f'runtime_clock_part({["getHours","getMinutes","getSeconds"].index(name)})'
        if callee[0]=='id' and callee[1] in contract.get('functions',{}):
            function=contract['functions'][callee[1]]
            env={p['name']:a for p,a in zip(function[1],args) if p['name']}
            def returned(statements: list[Any], local: dict[str,Any]) -> Any:
                for index, statement in enumerate(statements):
                    if statement[0]=='variables':
                        for name,expression in statement[1]: local[name]=reactive_substitute(expression,local)
                    elif statement[0]=='return': return reactive_substitute(statement[1],local)
                    elif statement[0]=='if':
                        yes=statement[2][1] if statement[2][0]=='block' else [statement[2]]
                        no=statement[3][1] if statement[3] and statement[3][0]=='block' else [statement[3]] if statement[3] else statements[index+1:]
                        return ['conditional',reactive_substitute(statement[1],local),returned(yes,dict(local)),returned(no,dict(local))]
                raise ValueError('Helper function has no supported return')
            return _runtime_expression(returned(function[2],env),contract,depth+1)
    raise ValueError('Unsupported reactive expression: '+json.dumps(value,ensure_ascii=False)[:160])


def _runtime_text_format(expressions: list[Any], contract: dict[str,Any]) -> tuple[str,list[str]]:
    fmt=''; arguments=[]
    def add(value: Any) -> None:
        nonlocal fmt
        if not value: return
        if value[0]=='literal':
            fmt+=str(value[1]).replace('%','%%'); return
        if value[0]=='template':
            fmt+=value[1].replace('%','%%')
            for part,tail in value[2]: add(part); fmt+=tail.replace('%','%%')
            return
        if value[0]=='call' and value[1][0]=='member':
            method=reactive_literal(value[1][2]); target=value[1][1]
            if method=='toFixed':
                precision=reactive_literal(value[2][0]) if value[2] else 0
                fmt+=f'%.{int(precision)}f';arguments.append('(double)'+_runtime_expression(target,contract));return
            if method=='padStart' and target[0]=='call' and target[1]==['id','String'] and reactive_literal(value[2][1])=='0':
                fmt+=f'%0{int(reactive_literal(value[2][0]))}d';arguments.append('(int)'+_runtime_expression(target[2][0],contract));return
        if value[0]=='id' and any(s['name']==value[1] and isinstance(reactive_literal(s['initial']),str) for s in contract.get('states',[])):
            fmt+='%s';arguments.append(_runtime_expression(value,contract));return
        fmt+='%g';arguments.append('(double)'+_runtime_expression(value,contract))
    for expression in expressions: add(expression)
    return fmt,arguments


def runtime_scene_bundle(model: ReactProjectModel, screen_tree: dict[str, Any]) -> GenerationBundle | None:
    """Compile measured DOM paint into one editable, source-traceable LVGL tree.

    The browser is a build-time layout/font oracle. Neither browser code nor
    page screenshots are embedded in the generated runtime.
    """
    captures = (model.browser_evidence or {}).get('screens', [])
    if not captures or not all((s.get('data', {}).get('runtime_scene') or {}).get('schema') == 'uagent.runtime-scene/v1' for s in captures):
        return None
    scenes = [s['data']['runtime_scene'] for s in captures]
    contract = screen_tree.get('reactive_contract', {})
    fonts: dict[str, Any] = {}
    for scene in scenes:
        fonts.update(scene.get('fonts', {}))
    font_ids = {key: f'uagent_font_{i}' for i, key in enumerate(fonts)}
    lines = ['#include "custom.h"', '#include <math.h>', '#include <stdlib.h>', '#include <string.h>', '#include <stdio.h>',
             'typedef struct {const lv_font_t *font; const uint32_t *codes; const float *advance; unsigned count;} runtime_font_t;']
    for key, font in fonts.items():
        symbol = font_ids[key]
        bitmap = bytearray()
        glyphs = ['{0}']
        codes = []
        for glyph in font['glyphs']:
            codes.append(int(glyph['code']))
            glyphs.append('{.bitmap_index=%d,.adv_w=%d,.box_w=%d,.box_h=%d,.ofs_x=%d,.ofs_y=%d}' % (
                len(bitmap), round(glyph['advance'] * 16), glyph['w'], glyph['h'], glyph['x'], glyph['y']))
            # LVGL's fmt_txt decoder supports 1/2/4 bpp plain glyphs. Quantize
            # the captured alpha mask to 4 bpp, preserving glyph advances.
            alpha = base64.b64decode(glyph['bitmap'])
            levels = [min(15, (value + 8) // 17) for value in alpha]
            bitmap.extend((levels[i] << 4) | (levels[i+1] if i+1 < len(levels) else 0) for i in range(0,len(levels),2))
        lines += [f'static const uint8_t {symbol}_bitmap[] = {{' + ','.join(map(str, bitmap or b'\0')) + '};',
                  f'static const lv_font_fmt_txt_glyph_dsc_t {symbol}_glyphs[] = {{' + ','.join(glyphs) + '};',
                  f'static const uint16_t {symbol}_unicode[] = {{' + ','.join(str(c - codes[0]) for c in codes) + '};',
                  f'static const lv_font_fmt_txt_cmap_t {symbol}_cmap[] = {{{{.range_start={codes[0]},.range_length={codes[-1]-codes[0]+1},.glyph_id_start=1,.unicode_list={symbol}_unicode,.list_length={len(codes)},.type=LV_FONT_FMT_TXT_CMAP_SPARSE_TINY}}}};',
                  f'static lv_font_fmt_txt_dsc_t {symbol}_dsc = {{.glyph_bitmap={symbol}_bitmap,.glyph_dsc={symbol}_glyphs,.cmaps={symbol}_cmap,.cmap_num=1,.bpp=4,.bitmap_format=0}};',
                  f'static const lv_font_t {symbol} = {{.get_glyph_dsc=lv_font_get_glyph_dsc_fmt_txt,.get_glyph_bitmap=lv_font_get_bitmap_fmt_txt,.line_height={font["ascent"]+font["descent"]},.base_line={font["descent"]},.dsc=&{symbol}_dsc}};']
        lines += [f'static const uint32_t {symbol}_codes[]={{'+','.join(map(str,codes))+'};',
                  f'static const float {symbol}_advances[]={{'+','.join(f'{g["advance"]:.6f}f' for g in font['glyphs'])+'};',
                  f'static const runtime_font_t {symbol}_info={{&{symbol},{symbol}_codes,{symbol}_advances,{len(codes)}}};']

    def number(value: Any, default: float = 0) -> float:
        try:
            return float(re.match(r'[-+\d.eE]+', str(value)).group())
        except (ValueError, AttributeError):
            return default

    def pixel(value: float) -> int:
        return math.floor(value + .5)

    def rgba(value: Any) -> tuple[str, int]:
        value = str(value or '')
        values = re.findall(r'[\d.]+', value)
        if value.startswith('rgb') and len(values) >= 3:
            rgb = [max(0, min(255, round(float(v)))) for v in values[:3]]
            return '0x%02x%02x%02x' % tuple(rgb), round(float(values[3]) * 255) if len(values) > 3 else 255
        if re.fullmatch(r'#[0-9a-fA-F]{6}', value):
            return '0x' + value[1:], 255
        return '0x000000', 0

    lines += [f'static lv_obj_t * runtime_pages[{len(scenes)}];',
              'static void runtime_nav(lv_event_t *e) { if(lv_event_get_code(e)==LV_EVENT_CLICKED) lv_screen_load(runtime_pages[(intptr_t)lv_event_get_user_data(e)]); }',
              'static lv_obj_t * runtime_box(lv_obj_t *parent,int x,int y,int w,int h) { lv_obj_t *o=lv_obj_create(parent); lv_obj_remove_style_all(o); lv_obj_set_pos(o,x,y); lv_obj_set_size(o,w,h); lv_obj_clear_flag(o,LV_OBJ_FLAG_SCROLLABLE|LV_OBJ_FLAG_CLICKABLE); return o; }']
    lines += [r'''
typedef struct {const runtime_font_t *font; float spacing, origin, initial_width, align; uint32_t color;} runtime_text_info_t;
static void runtime_text_delete(lv_event_t *e) { free(lv_obj_get_user_data(lv_event_get_target_obj(e))); }
static float runtime_text_width(const runtime_text_info_t *info,const char *text) {
    float width=0;
    for(const unsigned char *p=(const unsigned char*)text;*p;) {
        int count=(*p<128)?1:((*p&224)==192)?2:((*p&240)==224)?3:4;uint32_t code=*p&((count==1)?127:(1<<(7-count))-1);
        for(int i=1;i<count;i++)code=(code<<6)|(p[i]&63);p+=count;
        for(unsigned i=0;i<info->font->count;i++)if(info->font->codes[i]==code){width+=info->font->advance[i]+info->spacing;break;}
    }return width;
}
static void runtime_text_set(lv_obj_t *o,const char *text) {
    runtime_text_info_t *info=lv_obj_get_user_data(o); lv_obj_clean(o); float x=info->origin-(runtime_text_width(info,text)-info->initial_width)*info->align;
    for(const unsigned char *p=(const unsigned char*)text;*p;) {
        int count=(*p<128)?1:((*p&224)==192)?2:((*p&240)==224)?3:4; uint32_t code=*p & ((count==1)?127:(1<<(7-count))-1);
        for(int i=1;i<count;i++) code=(code<<6)|(p[i]&63);
        char value[5]={0}; memcpy(value,p,count); p+=count;
        lv_obj_t *glyph=lv_label_create(o);lv_obj_remove_style_all(glyph);lv_obj_set_style_text_font(glyph,info->font->font,0);
        lv_label_set_text(glyph,value);lv_obj_set_pos(glyph,(int)floorf(x+.5f),0);lv_obj_set_style_text_color(glyph,lv_color_hex(info->color),0);
        lv_obj_add_flag(glyph,LV_OBJ_FLAG_OVERFLOW_VISIBLE);
        float advance=0;for(unsigned i=0;i<info->font->count;i++)if(info->font->codes[i]==code){advance=info->font->advance[i];break;}
        x+=advance+info->spacing;
    }
}
static lv_obj_t *runtime_text(lv_obj_t *parent,float x,int y,int w,const runtime_font_t *font,uint32_t color,float spacing,float align,const char *text) {
    int ix=(int)floorf(x);lv_obj_t *o=runtime_box(parent,ix,y,w,font->font->line_height);lv_obj_add_flag(o,LV_OBJ_FLAG_OVERFLOW_VISIBLE);
    runtime_text_info_t *info=malloc(sizeof(*info));*info=(runtime_text_info_t){font,spacing,x-ix,0,align,color};info->initial_width=runtime_text_width(info,text);lv_obj_set_user_data(o,info);
    lv_obj_add_event_cb(o,runtime_text_delete,LV_EVENT_DELETE,NULL);runtime_text_set(o,text);return o;
}
''']
    units = []
    blockers: list[str] = list(contract.get('blockers', []))
    limitations: list[str] = []
    controls = contract.get('controls', [])
    control_ids = {c['identity']:i for i,c in enumerate(controls)}
    control_kinds: dict[int,str] = {}
    state_values = {s['name']:reactive_literal(s['initial']) for s in contract.get('states', [])}
    state_variables: dict[tuple[str,str|None],str] = {}
    for state, value in state_values.items():
        fields = value.items() if isinstance(value,dict) else [(None,value)]
        for field, initial in fields:
            name='runtime_state_'+c_symbol(state)+('_'+c_symbol(field) if field else '')
            state_variables[(state,field)] = name
            if isinstance(initial,str): lines.append(f'static char {name}[128]={_c_string(initial)};')
            elif isinstance(initial,(int,float,bool)): lines.append(f'static double {name}={int(initial) if isinstance(initial,bool) else repr(initial)};')
            else: blockers.append(f'Unsupported initial state: {state}.{field}')
    lines += ['#include <time.h>', 'static uint32_t runtime_seed=1;', 'static int64_t runtime_clock_ms;',
              'static double runtime_random(void) { runtime_seed=runtime_seed*1664525u+1013904223u;return runtime_seed/4294967296.0; }',
              'static int runtime_clock_part(int part) {time_t value=(time_t)(runtime_clock_ms/1000);struct tm *t=localtime(&value);return part==0?t->tm_hour:part==1?t->tm_min:t->tm_sec;}',
              'static uint32_t runtime_css_color(const char *s) {return s&&s[0]==\'#\'?(uint32_t)strtoul(s+1,NULL,16):0;}',
              'static void runtime_sync(void);', 'static void runtime_advance(int milliseconds);']
    for i in range(len(controls)):
        lines.append(f'static void runtime_control_{i}(lv_event_t *event);')
    sync: list[str] = []
    bound_controls: set[int] = set()
    receipts = []
    object_refs: list[tuple[str, str, str]] = []
    routes = [str(s.get('state') or (s.get('path') or [''])[ -1]).casefold() for s in captures]
    for screen_index, scene in enumerate(scenes):
        nodes = {int(n['id']): n for n in scene['nodes']}
        declarations = []
        body = [f'static void runtime_build_{screen_index}(void) {{', f'    runtime_pages[{screen_index}]=runtime_box(NULL,0,0,{scene["viewport"]["width"]},{scene["viewport"]["height"]});']
        rendered: dict[int, str] = {}
        for node_id, node in nodes.items():
            tag, rect, style, attrs = node['tag'], node['rect'], node['style'], node['attrs']
            ref = f'runtime_{screen_index}_{node_id}'
            reason = 'measured DOM node'
            support = 'native'
            if any(number(duration)>0 for duration in style.get('transitionDuration','0s').split(',')):
                support,reason='partial','CSS transition endpoint is rendered; intermediate easing frames are not compiled'
                limitations.append(f'{screen_index}:{node_id}: {reason}')
            for effect in ('boxShadow','textShadow','filter','clipPath','maskImage','backgroundImage'):
                if style.get(effect) not in (None,'','none'):
                    support,reason='unsupported',f'{effect} has no runtime paint recipe'
                    blockers.append(f'{screen_index}:{node_id}: {reason}')
            if style['display'] == 'none' or style['visibility'] == 'hidden':
                support, reason = 'native', 'source node retained; not painted in this state'
            elif rect['width'] <= 0 or rect['height'] <= 0:
                support, reason = 'partial', 'zero-size semantic node retained for event binding'
            elif tag in {'defs', 'filter', 'fegaussianblur', 'femerge', 'femergenode', 'clippath'}:
                support, reason = 'unsupported', 'SVG effect requires a classified paint recipe'
                blockers.append(f'{screen_index}:{node_id}: {reason}')
            else:
                parent_id = node.get('parent')
                while parent_id is not None and parent_id not in rendered:
                    parent_id = nodes.get(parent_id, {}).get('parent')
                parent = rendered.get(parent_id, f'runtime_pages[{screen_index}]')
                parent_rect = nodes[parent_id]['rect'] if parent_id is not None else {'x': 0, 'y': 0}
                x, y = pixel(rect['x']) - pixel(parent_rect['x']), pixel(rect['y']) - pixel(parent_rect['y'])
                width, height = pixel(rect['x']+rect['width'])-pixel(rect['x']), pixel(rect['y']+rect['height'])-pixel(rect['y'])
                declarations.append(f'static lv_obj_t * {ref};')
                if tag == 'circle':
                    stroke, opacity = rgba(style.get('stroke'))
                    stroke_width = number(style.get('strokeWidth'), 1)
                    radius = number(attrs.get('r'))
                    cx, cy = number(attrs.get('cx')), number(attrs.get('cy'))
                    rotation = re.search(r'rotate\(\s*([-+\d.]+)', attrs.get('transform', ''))
                    angle = number(rotation.group(1)) if rotation else 0
                    dash = [number(v) for v in re.findall(r'[-+\d.]+', style.get('strokeDasharray', ''))]
                    offset = number(style.get('strokeDashoffset'))
                    span = 360 * dash[0] / (2 * math.pi * radius) if dash and radius else 360
                    start = (angle - offset / radius * 180 / math.pi) % 360 if radius else angle
                    diameter = round(2 * radius + stroke_width)
                    body += [f'    {ref}=lv_arc_create({parent}); lv_obj_remove_style_all({ref});',
                             f'    lv_obj_set_pos({ref},{round(cx-radius-stroke_width/2)},{round(cy-radius-stroke_width/2)}); lv_obj_set_size({ref},{diameter},{diameter});',
                             f'    lv_obj_clear_flag({ref},LV_OBJ_FLAG_CLICKABLE); lv_arc_set_bg_angles({ref},0,360);',
                             f'    lv_arc_set_angles({ref},{round(start)},{round(start+span)}); lv_obj_set_style_arc_width({ref},{round(stroke_width)},LV_PART_INDICATOR);',
                             f'    lv_obj_set_style_arc_color({ref},lv_color_hex({stroke}),LV_PART_INDICATOR); lv_obj_set_style_arc_opa({ref},{opacity},LV_PART_INDICATOR);',
                             f'    lv_obj_set_style_arc_rounded({ref},{str(style.get("strokeLinecap")=="round").lower()},LV_PART_INDICATOR);']
                elif tag in {'path', 'polygon', 'polyline', 'ellipse', 'canvas', 'img'}:
                    support, reason = 'unsupported', f'{tag} paint has no runtime recipe'
                    blockers.append(f'{screen_index}:{node_id}: {reason}')
                    body.append(f'    {ref}=runtime_box({parent},{x},{y},{width},{height});')
                else:
                    body.append(f'    {ref}=runtime_box({parent},{x},{y},{width},{height});')
                    color, opacity = rgba(style['backgroundColor'])
                    body += [f'    lv_obj_set_style_bg_color({ref},lv_color_hex({color}),0); lv_obj_set_style_bg_opa({ref},{opacity},0);',
                             f'    lv_obj_set_style_radius({ref},{round(number(style["borderRadius"]))},0);']
                    for edge in ('Top','Right','Bottom','Left'):
                        thick = round(number(style.get(f'border{edge}Width')))
                        if not thick:
                            continue
                        color, alpha = rgba(style.get(f'border{edge}Color'))
                        bx, by, bw, bh = {'Top':(0,0,width,thick),'Right':(width-thick,0,thick,height),'Bottom':(0,height-thick,width,thick),'Left':(0,0,thick,height)}[edge]
                        body.append(f'    {{ lv_obj_t *b=runtime_box({ref},{bx},{by},{bw},{bh}); lv_obj_set_style_bg_color(b,lv_color_hex({color}),0); lv_obj_set_style_bg_opa(b,{alpha},0); }}')
                rendered[node_id] = ref
                if style['overflowX'] == 'visible' and style['overflowY'] == 'visible':
                    body.append(f'    lv_obj_add_flag({ref},LV_OBJ_FLAG_OVERFLOW_VISIBLE);')
                if style['overflowY'] in {'auto', 'scroll'}:
                    body += [f'    lv_obj_add_flag({ref},LV_OBJ_FLAG_SCROLLABLE|LV_OBJ_FLAG_CLICKABLE); lv_obj_set_scroll_dir({ref},LV_DIR_VER); lv_obj_set_scrollbar_mode({ref},LV_SCROLLBAR_MODE_OFF);']
                    object_refs.append((f'scroll:{screen_index}', ref, 'scroll'))
                opacity = round(number(style.get('opacity'), 1) * 255)
                if opacity < 255:
                    body.append(f'    lv_obj_set_style_opa({ref},{opacity},0);')
                for ti, text in enumerate(node.get('texts', [])):
                    tr = text['rect']
                    txt = text['text'].strip()
                    if not txt or not node.get('font'):
                        continue
                    color, alpha = rgba(style.get('fill') if tag == 'text' else style['color'])
                    text_ref = f'{ref}_text_{ti}'
                    declarations.append(f'static lv_obj_t * {text_ref};')
                    alignment=.5 if style.get('textAnchor')=='middle' or style.get('textAlign')=='center' else 1 if style.get('textAnchor')=='end' or style.get('textAlign')=='right' else 0
                    body += [f'    {text_ref}=runtime_text({ref},{tr["x"]-pixel(rect["x"]):.6f}f,{math.ceil(tr["y"])-pixel(rect["y"])},{max(1,math.ceil(tr["width"]))},&{font_ids[node["font"]]}_info,{color},{number(style["letterSpacing"]):.6f}f,{alignment:.1f}f,{_c_string(txt)});',
                             f'    lv_obj_set_style_opa({text_ref},{alpha},0);']
                if tag == 'button':
                    body.append(f'    lv_obj_add_flag({ref},LV_OBJ_FLAG_CLICKABLE);')
                    page = str(attrs.get('data-page', '')).casefold()
                    key=attrs.get('aria-label') or ''.join(t['text'].strip() for t in node['texts'])
                    if key in control_ids:
                        ci=control_ids[key];bound_controls.add(ci);control_kinds[ci]='button'
                        body.append(f'    lv_obj_add_event_cb({ref},runtime_control_{ci},LV_EVENT_CLICKED,NULL);')
                    elif page in routes:
                        body.append(f'    lv_obj_add_event_cb({ref},runtime_nav,LV_EVENT_CLICKED,(void*)(intptr_t){routes.index(page)});')
                    object_refs.append((key, ref, 'button'))
                if tag == 'input' and attrs.get('type') == 'range':
                    native = node['native']
                    declarations.append(f'static lv_obj_t * {ref}_slider;')
                    body.append(f'    lv_obj_add_flag({ref},LV_OBJ_FLAG_OVERFLOW_VISIBLE);')
                    thumb=native.get('thumb',{});thumb_size=round(number(thumb.get('width'),max(2,height)))
                    thumb_color,thumb_opa=rgba(thumb.get('background') or thumb.get('background-color'))
                    if not thumb: blockers.append(f'{screen_index}:{node_id}: range thumb style unavailable')
                    track_color,track_opa=rgba(style['backgroundColor']);track_offset=round((thumb_size-height)/2)
                    body += [f'    lv_obj_set_size({ref},{width},{thumb_size});lv_obj_set_y({ref},{y-track_offset});lv_obj_set_style_bg_opa({ref},0,0);',
                             f'    {{lv_obj_t *track=runtime_box({ref},0,{track_offset},{width},{height});lv_obj_set_style_bg_color(track,lv_color_hex({track_color}),0);lv_obj_set_style_bg_opa(track,{track_opa},0);lv_obj_set_style_radius(track,{height},0);}}',
                             f'    {ref}_slider=lv_slider_create({ref}); lv_obj_remove_style_all({ref}_slider);',
                             f'    lv_obj_set_size({ref}_slider,{max(1,width-thumb_size+1)},{thumb_size}); lv_obj_set_pos({ref}_slider,{thumb_size//2},0);',
                             f'    lv_slider_set_range({ref}_slider,{round(number(native["min"]))},{round(number(native["max"],100))}); lv_slider_set_value({ref}_slider,{round(number(native["value"]))},LV_ANIM_OFF);',
                             f'    lv_obj_set_style_bg_color({ref}_slider,lv_color_hex({thumb_color}),LV_PART_KNOB); lv_obj_set_style_bg_opa({ref}_slider,{thumb_opa},LV_PART_KNOB); lv_obj_set_style_radius({ref}_slider,LV_RADIUS_CIRCLE,LV_PART_KNOB); lv_obj_set_style_pad_all({ref}_slider,0,LV_PART_KNOB);',
                             f'    lv_obj_add_flag({ref}_slider,LV_OBJ_FLAG_OVERFLOW_VISIBLE);lv_obj_set_ext_click_area({ref}_slider,{max(4,thumb_size)});']
                    key=attrs.get('aria-label') or attrs.get('id') or f'node:{node_id}'
                    object_refs.append((key, ref+'_slider', 'range'))
                    if key in control_ids:
                        ci=control_ids[key];bound_controls.add(ci);control_kinds[ci]='range'
                        body.append(f'    lv_obj_add_event_cb({ref}_slider,runtime_control_{ci},LV_EVENT_VALUE_CHANGED,NULL);')
                        actions=controls[ci]['actions']
                        if len(actions)==1:
                            action=actions[0];variable=state_variables.get((action['state'],action['field']))
                            if variable:
                                sync.append(f'    lv_slider_set_value({ref}_slider,(int){variable},LV_ANIM_OFF);')
                                target=['member',['id',action['state']],['literal',action['field']]]
                                def contains(expression:Any,wanted:Any) -> bool:
                                    return expression==wanted or isinstance(expression,list) and any(contains(item,wanted) for item in expression)
                                formats=[j['children'] for j in contract.get('jsx',[]) if j['tag']=='span' and contains(j['children'],target)]
                                labels=[n for n in nodes.values() if n['parent']==node['parent'] and n['texts']]
                                if len(formats)==1 and len(labels)==1:
                                    fmt,args=_runtime_text_format(formats[0],contract)
                                    label=f'runtime_{screen_index}_{labels[0]["id"]}_text_0'
                                    sync.append(f'    {{char text[128];snprintf(text,sizeof(text),{_c_string(fmt)}'+(' ,'+','.join(args) if args else '')+f');runtime_text_set({label},text);}}')
            if tag=='input' and attrs.get('type')=='checkbox':
                key=attrs.get('id') or attrs.get('aria-label')
                ci=control_ids.get(key)
                parent_ref=rendered.get(node['parent'])
                if ci is not None and parent_ref:
                    bound_controls.add(ci);control_kinds[ci]='checkbox'
                    body.append(f'    lv_obj_add_flag({parent_ref},LV_OBJ_FLAG_CLICKABLE);lv_obj_add_event_cb({parent_ref},runtime_control_{ci},LV_EVENT_CLICKED,NULL);')
                    object_refs.append((key,parent_ref,'checkbox'))
                    action=controls[ci]['actions'][0] if controls[ci]['actions'] else None
                    variable=state_variables.get((action['state'],action['field'])) if action else None
                    if variable:
                        for checked,paints in node['native'].get('paintStates',{}).items():
                            sync.append(f'    if({"" if checked=="true" else "!"}{variable}) {{')
                            for paint in paints:
                                child=nodes.get(paint['id']); child_ref=f'runtime_{screen_index}_{paint["id"]}'
                                if not child: continue
                                parent_rect=nodes[child['parent']]['rect'];r=paint['rect'];color,opa=rgba(paint['backgroundColor'])
                                sync += [f'        lv_obj_set_pos({child_ref},{pixel(r["x"])-pixel(parent_rect["x"])},{pixel(r["y"])-pixel(parent_rect["y"])});',
                                         f'        lv_obj_set_style_bg_color({child_ref},lv_color_hex({color}),0);lv_obj_set_style_bg_opa({child_ref},{opa},0);']
                            sync.append('    }')
                    support,reason='native','zero-size input bound to its measured, visible checkbox surface'
            receipts.append({'screen':screen_index,'node':node_id,'parent':node.get('parent'),'tag':tag,'support':support,'reason':reason})
            units.append(GeneratedUnit(source_id=f'browser:{screen_index}:{node_id}',symbol=ref,adapter='RuntimeSceneAdapter',widget_type=tag,support=support,c_code='',decisions={'reason':reason,'rect':rect,'parent':node.get('parent')}))
        body.append('}')
        lines += declarations + body
        def descendants(parent_id:int) -> list[dict[str,Any]]:
            result=[]
            for item in nodes.values():
                ancestor=item.get('parent')
                while ancestor is not None:
                    if ancestor==parent_id:
                        result.append(item);break
                    ancestor=nodes.get(ancestor,{}).get('parent')
            return result
        for view in contract.get('views',[]):
            roots=[n for n in nodes.values() if n['attrs'].get('aria-label')==view['identity'] and n['attrs'].get('role')==view['role']]
            for root_node in roots:
                children=descendants(root_node['id']);ordinals={}
                for source_child in view['children']:
                    tag=source_child['tag'];ordinal=ordinals.get(tag,0);ordinals[tag]=ordinal+1
                    matches=[n for n in children if n['tag']==tag]
                    if ordinal>=len(matches): continue
                    child=matches[ordinal];ref=f'runtime_{screen_index}_{child["id"]}'
                    try:
                        if tag=='circle':
                            stroke=source_child['attrs'].get('stroke');offset=source_child['attrs'].get('strokeDashoffset')
                            if stroke:
                                sync.append(f'    lv_obj_set_style_arc_color({ref},lv_color_hex(runtime_css_color({_runtime_expression(stroke,contract)})),LV_PART_INDICATOR);')
                            if offset:
                                radius=number(child['attrs'].get('r'));rotation=re.search(r'rotate\(\s*([-+\d.]+)',child['attrs'].get('transform',''))
                                angle=number(rotation.group(1)) if rotation else 0;dash=[number(v) for v in re.findall(r'[-+\d.]+',child['style'].get('strokeDasharray',''))]
                                span=360*dash[0]/(2*math.pi*radius) if dash and radius else 360
                                sync.append(f'    {{double start=fmod({angle}-({_runtime_expression(offset,contract)})/{radius}*180.0/3.141592653589793+720.0,360.0);lv_arc_set_angles({ref},(int)round(start),(int)round(start+{span}));}}')
                        if child['texts'] and source_child['texts']:
                            fmt,args=_runtime_text_format(source_child['texts'],contract)
                            if args:
                                sync.append(f'    {{char text[128];snprintf(text,sizeof(text),{_c_string(fmt)},'+','.join(args)+f');runtime_text_set({ref}_text_0,text);}}')
                            fill=source_child['attrs'].get('fill')
                            if fill and reactive_literal(fill) is None:
                                sync.append(f'    {{uint32_t color=runtime_css_color({_runtime_expression(fill,contract)});((runtime_text_info_t*)lv_obj_get_user_data({ref}_text_0))->color=color;for(uint32_t i=0;i<lv_obj_get_child_count({ref}_text_0);i++)lv_obj_set_style_text_color(lv_obj_get_child({ref}_text_0,i),lv_color_hex(color),0);}}')
                        css_class=source_child['attrs'].get('className')
                        if css_class and css_class[0]=='conditional':
                            for variant in css_class[2:]:
                                name=reactive_literal(variant)
                                if not name: continue
                                animation=scene.get('animations',{}).get(name)
                                if not animation: raise ValueError(f'Dynamic class {name} has no compiled style recipe')
                                frames=animation.get('frames',[])
                                if animation.get('easing')!='step-start' or animation.get('iterations')!='infinite' or len(frames)!=1 or frames[0].get('offset')!='50%' or frames[0].get('opacity')!='0':
                                    raise ValueError(f'Unsupported CSS animation recipe: {name}')
                                duration=animation['duration'];period=round(number(duration)*(1 if duration.endswith('ms') else 1000))
                                if period<=0: raise ValueError('Invalid CSS animation duration')
                                # CSS step-start jumps to the following keyframe at
                                # each segment's start; the implicit endpoints are 1.
                                sync.append(f'    {{static int active=0;static int64_t start=0;int enabled=!strcmp({_runtime_expression(css_class,contract)},{_c_string(name)});if(enabled&&!active)start=runtime_clock_ms;active=enabled;lv_obj_set_style_opa({ref},enabled&&((runtime_clock_ms-start)%{period}<{period//2})?0:255,0);}}')
                    except ValueError as exc:
                        blockers.append(f'{view["identity"]}: {exc}')
    def assignments(actions:list[dict[str,Any]]) -> list[str]:
        statements=[];commits=[]
        for index,action in enumerate(actions):
            variable=state_variables.get((action['state'],action['field']))
            if not variable: raise ValueError(f'Unresolved state destination {action["state"]}.{action["field"]}')
            initial=state_values[action['state']]
            if action['field'] is not None: initial=initial[action['field']]
            if isinstance(initial,str):
                fmt,args=_runtime_text_format([action['value']],contract)
                statements.append(f'    char next_{index}[128];snprintf(next_{index},sizeof(next_{index}),{_c_string(fmt)}'+(','+','.join(args) if args else '')+');')
                commits.append(f'    snprintf({variable},sizeof({variable}),"%s",next_{index});')
            else:
                statements.append(f'    double next_{index}={_runtime_expression(action["value"],contract)};')
                commits.append(f'    {variable}=next_{index};')
        return statements+commits
    for i,control in enumerate(controls):
        lines.append(f'static void runtime_control_{i}(lv_event_t *event) {{')
        kind=control_kinds.get(i)
        if kind=='range': lines.append('    double value=lv_slider_get_value(lv_event_get_target_obj(event));')
        elif kind=='checkbox' and control['actions']:
            action=control['actions'][0];variable=state_variables.get((action['state'],action['field']),'0')
            lines.append(f'    double value=!{variable};')
        else: lines.append('    double value=0;(void)event;')
        try:
            if control.get('blocker'): raise ValueError(control['blocker'])
            lines+=assignments(control['actions'])
        except ValueError as exc: blockers.append(f'{control["identity"]}: {exc}')
        lines+=['    runtime_sync();','}']
        if i not in bound_controls: blockers.append(f'{control["identity"]}: source event has no runtime object')
    timers=[];clock_states=[]
    for index,timer in enumerate(contract.get('timers',[])):
        try:
            period=reactive_literal(timer['period'])
            if not isinstance(period,(int,float)) or period<=0: raise ValueError('Unsupported timer period')
            if timer.get('blocker'): raise ValueError(timer['blocker'])
            statements=assignments(timer.get('actions',[]))
            lines+=[f'static void runtime_tick_{index}(void) {{']+statements+['}']
            lines.append(f'static int64_t runtime_due_{index};')
            timers.append((index,int(period),bool(timer.get('immediate'))))
            for action in timer.get('actions',[]):
                if action['field'] is None and isinstance(state_values[action['state']],str):
                    fmt,args=_runtime_text_format([action['value']],contract)
                    if args and all('runtime_clock_part' in arg for arg in args): clock_states.append((action['state'],fmt))
        except ValueError as exc: blockers.append(f'timer {index}: {exc}')
    for screen_index,scene in enumerate(scenes):
        for state,fmt in clock_states:
            pattern=re.escape(fmt).replace('%02d',r'\d{2}')
            matches=[(node,ti) for node in scene['nodes'] for ti,text in enumerate(node['texts']) if re.fullmatch(pattern,text['text'].strip())]
            if len(matches)==1:
                node,ti=matches[0];sync.append(f'    runtime_text_set(runtime_{screen_index}_{node["id"]}_text_{ti},{state_variables[(state,None)]});')
            else: blockers.append(f'{screen_index}: clock text mapping is ambiguous')
    for state,value in state_values.items():
        if isinstance(value,str) and value.casefold() in routes:
            for index,route in enumerate(routes):
                sync.append(f'    if(!strcmp({state_variables[(state,None)]},{_c_string(route)}) && lv_screen_active()!=runtime_pages[{index}]) lv_screen_load(runtime_pages[{index}]);')
    lines+=['static void runtime_sync(void) {']+sync+['}']
    lines+=['static void runtime_advance(int milliseconds) {', '    int64_t end=runtime_clock_ms+milliseconds;', '    while(1) {int64_t next=end+1;']
    for index,period,_ in timers: lines.append(f'        if(runtime_due_{index}<next)next=runtime_due_{index};')
    lines+=['        if(next>end)break;runtime_clock_ms=next;']
    for index,period,_ in timers: lines.append(f'        if(runtime_due_{index}<=next){{runtime_tick_{index}();runtime_due_{index}+={period};}}')
    lines+=['    }runtime_clock_ms=end;runtime_sync();}', 'static void runtime_timer(lv_timer_t *timer) {(void)timer;runtime_advance(100);}']
    lines+=['void uagent_runtime_dump(const char *path) {', '    FILE *file=fopen(path,"wb");if(!file)return;', '    fputs("{",file);']
    first=True
    for state,value in state_values.items():
        prefix=('' if first else ',')+json.dumps(state)+':'
        lines.append(f'    fputs({_c_string(prefix)},file);');first=False
        fields=value.items() if isinstance(value,dict) else [(None,value)]
        if isinstance(value,dict): lines.append('    fputs("{",file);')
        for fi,(field,initial) in enumerate(fields):
            if field is not None: lines.append(f'    fputs({_c_string(("," if fi else "")+json.dumps(field)+":")},file);')
            variable=state_variables[(state,field)]
            if isinstance(initial,str): lines.append(f'    fprintf(file,"\\\"%s\\\"",{variable});')
            elif isinstance(initial,bool): lines.append(f'    fputs({variable}?"true":"false",file);')
            else: lines.append(f'    fprintf(file,"%.12g",{variable});')
        if isinstance(value,dict): lines.append('    fputs("}",file);')
    lines+=['    fputs("}",file);fclose(file);','}']
    # Public event entry lets the test runner exercise the same objects and
    # event callbacks as SDL input, without automating the user's desktop.
    lines += ['int uagent_runtime_action(const char *key,double value) {']
    lines.append('    if(!strcmp(key,"@advance")){runtime_advance((int)value);return 1;}')
    for key, ref, kind in object_refs:
        if kind == 'range':
            action=f'lv_slider_set_value({ref},(int)value,LV_ANIM_OFF);lv_obj_send_event({ref},LV_EVENT_VALUE_CHANGED,NULL);'
        else:
            action = f'lv_obj_scroll_to_y({ref},(int)value,LV_ANIM_OFF);' if kind == 'scroll' else f'lv_obj_send_event({ref},LV_EVENT_CLICKED,NULL);'
        lines.append(f'    if(!strcmp(key,{_c_string(key)})) {{ {action} return 1; }}')
    lines += ['    return 0;', '}', 'void custom_init(void) {']
    lines+=['    const char *clock=getenv("UAGENT_TEST_CLOCK_MS");runtime_clock_ms=clock?strtoll(clock,NULL,10):(int64_t)time(NULL)*1000;',
            '    if(!getenv("UAGENT_TEST_DETERMINISTIC"))runtime_seed=(uint32_t)time(NULL);']
    for i in range(len(scenes)):
        lines.append(f'    runtime_build_{i}();')
    for index,period,immediate in timers:
        lines.append(f'    runtime_due_{index}=runtime_clock_ms+{period};')
        if immediate: lines.append(f'    runtime_tick_{index}();')
    lines += ['    lv_screen_load(runtime_pages[0]);runtime_sync();', '    if(!getenv("UAGENT_TEST_DETERMINISTIC"))lv_timer_create(runtime_timer,100,NULL);', '}']
    header = '#ifndef UAGENT_CUSTOM_H\n#define UAGENT_CUSTOM_H\n#include "lvgl.h"\n#define UAGENT_RUNTIME_OWNS_SCENE 1\nvoid custom_init(void);\nint uagent_runtime_action(const char*,double);\nvoid uagent_runtime_dump(const char*);\n#endif\n'
    tree = dict(screen_tree)
    tree['runtime_scene'] = {'schema':'uagent.runtime-scene/v1','nodes':receipts,'blockers':blockers,'limitations':limitations,'fonts':len(fonts),'source_controls':len(controls),'bound_controls':len(bound_controls),'timers':len(timers),'ownership':'custom.c owns every page; snapshot is not instantiated'}
    return GenerationBundle(header,'\n'.join(lines)+'\n',units,[f'BLOCKER: {b}' for b in blockers]+[f'PARTIAL: {item}' for item in limitations],tree)


def c_symbol(value: str) -> str:
    result = re.sub(r"[^0-9A-Za-z_]", "_", value)
    result = re.sub(r"_+", "_", result).strip("_").lower() or "widget"
    if result[0].isdigit():
        result = f"w_{result}"
    # Path-based source IDs are commonly longer than a C-friendly identifier.
    # A stable suffix prevents ``Component`` and ``Component#dropdown_1`` from
    # collapsing to the same symbol after truncation.
    if len(result) > 56:
        digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:8]
        return f"{result[:47]}_{digest}"
    return result


def resource_filename(resource_id: str, model: ReactProjectModel) -> str:
    """Return one stable, UIBuilder-safe file name for a discovered resource."""
    resource = model.resources.get(resource_id)
    source_name = resource.path.name if resource else Path(resource_id).name
    suffix = Path(source_name).suffix.lower() or ".png"
    stem = re.sub(r"[^0-9A-Za-z_]+", "_", Path(source_name).stem).strip("_")[:42] or "asset"
    digest = hashlib.sha1(resource_id.encode("utf-8")).hexdigest()[:8]
    return f"uagent_{stem}_{digest}{suffix}"


def _c_string(value: Any) -> str:
    return json.dumps(str(value), ensure_ascii=False)


_LVGL_ICON_SYMBOLS = {
    "Settings": "LV_SYMBOL_SETTINGS", "Languages": "[A]",
    "Wrench": "[tool]", "MoreHorizontal": "...", "Scissors": "[cut]",
    "Home": "LV_SYMBOL_HOME", "ArrowLeft": "LV_SYMBOL_LEFT", "HelpCircle": "[?]",
    "Clock": "[time]", "Wifi": "LV_SYMBOL_WIFI", "WifiOff": "LV_SYMBOL_CLOSE",
    "Lock": "[lock]", "Check": "LV_SYMBOL_OK", "ChevronRight": "LV_SYMBOL_RIGHT",
    "FileText": "LV_SYMBOL_FILE", "Droplet": "[drop]", "Droplets": "[drop]",
    "Sparkles": "*", "RefreshCw": "LV_SYMBOL_REFRESH", "Plus": "LV_SYMBOL_PLUS",
    "Minus": "LV_SYMBOL_MINUS", "X": "LV_SYMBOL_CLOSE", "Menu": "=", "Search": "?",
    "Upload": "^", "Download": "v", "Info": "i",
}


def _icon_text(icon: Any, text: Any) -> str:
    """Build a compile-safe LVGL label expression for a parsed icon.

    Known icons use LVGL's built-in symbol font; unknown names use an ASCII
    bracketed fallback, never a Unicode replacement glyph.  The icon name is
    still preserved in the audit manifest for later project-specific mapping.
    """
    name = str(icon or "").strip()
    caption = _c_string(text if text not in (None, "") else "Button")
    symbol = _LVGL_ICON_SYMBOLS.get(name)
    if symbol and symbol.startswith("LV_SYMBOL_"):
        return f'{symbol} "  " {caption}'
    if symbol:
        return f'{_c_string(symbol)} "  " {caption}'
    return f'{_c_string("[" + (name or "icon") + "]")} "  " {caption}'


def _number(props: dict[str, Any], name: str, default: int) -> int:
    try:
        return int(float(props.get(name, default)))
    except (TypeError, ValueError):
        return default


def _color(value: Any, default: str) -> str:
    value = str(value or default).strip()
    if re.fullmatch(r"#[0-9a-fA-F]{6}", value):
        return f"lv_color_hex(0x{value[1:]})"
    if re.fullmatch(r"0x[0-9a-fA-F]{6}", value):
        return f"lv_color_hex({value})"
    return f"lv_color_hex(0x{default.lstrip('#')})"


def _opa(value: Any, default: int = 255) -> int:
    """Convert CSS/SVG alpha to an LVGL opacity constant."""
    raw = str(value or "").strip()
    match = re.fullmatch(r"rgba?\(\s*\d+\s*,\s*\d+\s*,\s*\d+(?:\s*,\s*([0-9.]+))?\s*\)", raw, re.I)
    if match and match.group(1) is not None:
        try:
            return max(0, min(255, round(float(match.group(1)) * 255)))
        except ValueError:
            return default
    try:
        number = float(raw)
        if number <= 1:
            number *= 255
        return max(0, min(255, round(number)))
    except ValueError:
        return default


def _color_from_raw(value: Any, default: str) -> str:
    """Keep RGB from a literal CSS color while the opacity is handled separately."""
    raw = str(value or "").strip()
    match = re.fullmatch(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)(?:\s*,\s*[0-9.]+)?\s*\)", raw, re.I)
    if match:
        return f"lv_color_hex(0x{int(match.group(1)):02x}{int(match.group(2)):02x}{int(match.group(3)):02x})"
    return _color(raw, default)


def _svg_tag_attributes(svg: str, tag: str) -> list[dict[str, str]]:
    """Read browser-serialized SVG shape attributes without project knowledge."""
    result: list[dict[str, str]] = []
    pattern = re.compile(rf"<{tag}\b([^>]*)>", re.I)
    for match in pattern.finditer(str(svg or "")):
        # Keep both quote styles accepted by browser outerHTML.
        attrs = {key.casefold(): value for key, _quote, value in
                 re.findall(r"([A-Za-z_:][\w:.-]*)\s*=\s*(['\"])(.*?)\2", match.group(1))}
        result.append(attrs)
    return result


def _svg_numbers(value: Any) -> list[float]:
    return [float(item) for item in re.findall(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)", str(value or ""))]


def _svg_gauge_metrics(svg: str) -> dict[str, Any]:
    """Extract progress, angle and tick evidence from one serialized SVG."""
    circles = _svg_tag_attributes(svg, "circle")
    paths = _svg_tag_attributes(svg, "path")
    lines = _svg_tag_attributes(svg, "line")
    dashed = [item for item in [*circles, *paths] if item.get("stroke-dasharray")]
    foreground = next((item for item in dashed
                       if "progress" in str(item.get("class", "")).casefold()), None)
    if foreground is None:
        foreground = next((item for item in dashed if "stroke-dashoffset" in item), None)
    if foreground is None and dashed:
        foreground = dashed[-1]
    roots = _svg_tag_attributes(svg, "svg")
    view_box = _svg_numbers(roots[0].get("viewbox")) if roots else []
    metrics: dict[str, Any] = {"ticks": [], "foreground": foreground or {},
                               "view_box": view_box}
    if foreground:
        dash = _svg_numbers(foreground.get("stroke-dasharray"))
        radius = (_svg_numbers(foreground.get("r")) or [0.0])[0]
        circumference = 2.0 * math.pi * radius if radius > 0 else 0.0
        has_path_length = bool(foreground.get("pathlength"))
        path_length = (_svg_numbers(foreground.get("pathlength")) or [circumference])[0]
        active_length = path_length if has_path_length and path_length > 0 else (dash[0] if dash else circumference)
        try:
            offset = float((_svg_numbers(foreground.get("stroke-dashoffset")) or [0.0])[0])
        except (TypeError, ValueError):
            offset = 0.0
        if has_path_length and dash:
            ratio = dash[0] / active_length if active_length > 0 else 0.0
        else:
            ratio = 1.0 - (offset / active_length) if active_length > 0 else 0.0
        metrics["ratio"] = max(0.0, min(1.0, ratio))
        transform = re.search(r"rotate\(\s*([-+]?\d+(?:\.\d+)?)", foreground.get("transform", ""), re.I)
        metrics["start"] = float(transform.group(1)) if transform else None
        metrics["sweep"] = (active_length / circumference * 360.0
                             if circumference > 0 else None)
        metrics["stroke_width"] = (_svg_numbers(foreground.get("stroke-width")) or [None])[0]
        metrics["stroke"] = foreground.get("stroke")
    for item in lines:
        coords = [_svg_numbers(item.get(key)) for key in ("x1", "y1", "x2", "y2")]
        if all(values for values in coords):
            metrics["ticks"].append({"x1": coords[0][0], "y1": coords[1][0],
                                      "x2": coords[2][0], "y2": coords[3][0],
                                      "stroke": item.get("stroke"),
                                      "stroke_width": (_svg_numbers(item.get("stroke-width")) or [1.0])[0],
                                      "opacity": (_svg_numbers(item.get("opacity")) or [1.0])[0]})
    return metrics


def _style_lines(symbol: str, props: dict[str, Any]) -> list[str]:
    width, height = _number(props, "width", 360), _number(props, "height", 200)
    radius, padding = _number(props, "radius", 16), _number(props, "padding", 12)
    bg = _color(props.get("background", props.get("bg_color")), "18233a")
    return [
        f"    lv_obj_set_size({symbol}, {width}, {height});",
        f"    lv_obj_set_style_radius({symbol}, {radius}, LV_PART_MAIN);",
        f"    lv_obj_set_style_bg_color({symbol}, {bg}, LV_PART_MAIN);",
        "    lv_obj_set_style_bg_opa(" + symbol + ", LV_OPA_COVER, LV_PART_MAIN);",
        f"    lv_obj_set_style_pad_all({symbol}, {padding}, LV_PART_MAIN);",
    ]


def _event_comment(component: Component, model: ReactProjectModel) -> list[str]:
    ids = component.event_ids
    event_map = model.events
    comments = []
    for event_id in ids:
        event = event_map.get(event_id)
        if event:
            handler = re.sub(r"[\r\n]+", " ", event.handler).replace("*/", "* /")
            comments.append(f"    /* React {event.kind}: {handler[:160]} */")
    return comments


def _chart_values(raw: Any) -> list[list[int]]:
    """Accept [[1, 2]], [{value: 1}], or a single numeric list deterministically."""
    if not isinstance(raw, list):
        return [[0, 0, 0, 0, 0]]
    if raw and all(isinstance(x, (int, float)) for x in raw):
        return [[int(x) for x in raw]]
    result: list[list[int]] = []
    for series in raw:
        if isinstance(series, list):
            result.append([int(x) if isinstance(x, (int, float)) else 0 for x in series])
        elif isinstance(series, dict):
            values = series.get("values", series.get("data", []))
            if isinstance(values, list):
                result.append([int(x) if isinstance(x, (int, float)) else 0 for x in values])
    return result or [[0, 0, 0, 0, 0]]


class LVGLRenderer:
    """Render adapter intents to LVGL v8-compatible C and an audit manifest."""

    def render(self, adapted: AdaptedWidget, component: Component, model: ReactProjectModel) -> GeneratedUnit:
        # ``source_id`` may contain a chart-branch suffix, making separate
        # Line/Area/Bar branches within one React component collision-free.
        symbol = c_symbol(adapted.source_id)
        if adapted.adapter == "CarouselAdapter":
            code = self._carousel(symbol, adapted, component, model)
        elif adapted.adapter == "GaugeSceneAdapter":
            code = self._gauge_scene(symbol, adapted, component, model)
        elif adapted.adapter == "GaugeAdapter":
            code = self._gauge(symbol, adapted, component, model)
        elif adapted.adapter == "RechartsAdapter":
            code = self._chart(symbol, adapted, component, model)
        elif adapted.adapter == "TableAdapter":
            code = self._table(symbol, adapted, component, model)
        elif adapted.adapter == "DropdownAdapter":
            code = self._dropdown(symbol, adapted, component, model)
        elif adapted.adapter == "ScrollContainerAdapter":
            code = self._scroll_container(symbol, adapted, component, model)
        elif adapted.adapter == "GenericControlsAdapter":
            code = self._control(symbol, adapted, component, model)
        else:
            raise ValueError(f"No renderer registered for {adapted.adapter}")
        return GeneratedUnit(
            source_id=adapted.source_id, symbol=symbol, adapter=adapted.adapter,
            widget_type=adapted.widget_type, support=adapted.support, c_code=code,
            warnings=list(adapted.warnings), resources=list(adapted.resource_ids),
            events=list(adapted.event_ids), decisions=dict(adapted.properties),
        )

    def _gauge(self, symbol: str, adapted: AdaptedWidget, component: Component, model: ReactProjectModel) -> str:
        """Render an inline SVG/Gauge semantic record as editable LVGL arcs."""
        gauges = adapted.properties.get("gauges") or [{}]
        g = gauges[0] if isinstance(gauges[0], dict) else {}
        # Browser capture is the geometry authority when available. Locate
        # dashed SVG gauges in DOM order and normalize their boxes to the
        # target canvas; source geometry remains the fallback for source-only
        # builds.
        screen_box = adapted.properties.get("screen_box")
        evidence = model.browser_evidence if isinstance(model.browser_evidence, dict) else {}
        nodes: list[dict[str, Any]] = []
        def flatten(items: Any) -> None:
            if not isinstance(items, list): return
            for item in items:
                if not isinstance(item, dict): continue
                nodes.append(item); flatten(item.get("children"))
        for captured in evidence.get("screens", []) if isinstance(evidence.get("screens"), list) else []:
            flatten((captured or {}).get("data", {}).get("nodes", []) if isinstance(captured, dict) else [])
        svg_nodes: list[dict[str, Any]] = []
        svg_metrics: dict[str, Any] = {}
        svg_text = ""
        gauge_svg_text = ""
        for node in nodes:
            tag = str(node.get("tag") or node.get("type") or "").casefold()
            attrs = (json.dumps(node.get("attributes", node.get("attrs", node)), ensure_ascii=False)
                     + " " + str(node.get("svg", ""))).casefold()
            if tag == "svg" and ("dasharray" in attrs or "dashoffset" in attrs or "stroke-dash" in attrs):
                rect = node.get("rect") if isinstance(node.get("rect"), dict) else {}
                if rect and node.get("svg"):
                    # Keep the complete browser node: its outerHTML carries
                    # the actual circles/lines that define geometry and state.
                    svg_nodes.append(node)
        if svg_nodes:
            svg_nodes.sort(key=lambda item: (float(item.get("rect", {}).get("y", 0)),
                                             float(item.get("rect", {}).get("x", 0))))
            ordinal_match = re.search(r"#gauge_(\d+)$", adapted.source_id)
            ordinal = int(ordinal_match.group(1)) - 1 if ordinal_match else 0
            chosen = svg_nodes[min(max(0, ordinal), len(svg_nodes) - 1)]
            chosen_rect = chosen.get("rect", {})
            svg_text = str(chosen.get("svg", ""))
            adapted.properties["svg_node"] = chosen
            svg_metrics = _svg_gauge_metrics(str(chosen.get("svg", "")))
            adapted.properties["svg_metrics"] = svg_metrics
            # A single full-canvas SVG can contain several explicitly named
            # gauge groups. Resolve the instance group before falling back to
            # the root SVG rect, avoiding 1024x600 radial arcs.
            gauge_id = str(adapted.properties.get("gauge_id") or g.get("gauge_id") or "")
            group_svg = ""
            # Source instances are not always named in scanner metadata.
            # Recover ordered gauge groups from their own progress paint,
            # not from project/component names.
            openings = list(re.finditer(r"<g\b[^>]*>", svg_text, re.I))
            starts = [opening.start() for opening in openings
                      if (re.search(r"(?:class|data-openhmi-id|id)=[\"'][^\"']*gauge", opening.group(0), re.I)
                          and re.search(r"class=[\"'][^\"']*progress",
                                        svg_text[opening.start():min(len(svg_text), opening.end() + 900)], re.I))]
            candidates = [(start, svg_text[start:(starts[index + 1] if index + 1 < len(starts) else len(svg_text))])
                          for index, start in enumerate(starts)]
            exact = next((segment for _start, segment in candidates
                          if gauge_id and re.search(
                              rf"<g\b[^>]*(?:data-openhmi-id|id)=[\"']{re.escape(gauge_id)}[\"']", segment[:500], re.I)), None)
            group_svg = exact or ""
            geometry = g.get("geometry") if isinstance(g.get("geometry"), dict) else {}
            if not group_svg and geometry:
                target_radius = float(geometry.get("radius", min(float(geometry.get("width", 0)),
                                                                  float(geometry.get("height", 0))) / 2) or 0)
                target_cx = float(geometry.get("cx", float(geometry.get("x", 0)) + float(geometry.get("width", 0)) / 2))
                scored = []
                for _start, segment in candidates:
                    path = next((item for item in _svg_tag_attributes(segment, "path")
                                 if "progress" in str(item.get("class", "")).casefold()), None)
                    values = _svg_numbers(path.get("d")) if path else []
                    if len(values) >= 9:
                        sx, radius, ex = values[0], values[2], values[-2]
                        scored.append((abs(radius - target_radius) * 10 + abs((sx + ex) / 2 - target_cx), segment))
                if scored:
                    group_svg = min(scored, key=lambda item: item[0])[1]
            if not group_svg and candidates:
                group_svg = candidates[min(max(0, ordinal), len(candidates) - 1)][1]
            if group_svg:
                gauge_svg_text = group_svg
                group_metrics = _svg_gauge_metrics(group_svg)
                group_metrics["view_box"] = svg_metrics.get("view_box", [])
                svg_metrics = group_metrics
                adapted.properties["svg_metrics"] = svg_metrics
                circle = re.search(r"<circle\b[^>]*\bcx=[\"']([0-9.+-]+)[\"'][^>]*\bcy=[\"']([0-9.+-]+)[\"'][^>]*\br=[\"']([0-9.+-]+)[\"']", group_svg, re.I)
                if circle:
                    cx, cy, radius = (float(value) for value in circle.groups())
                    path_radii = [float(value) for value in re.findall(r"\bA\s*([0-9.+-]+)\s+[0-9.+-]+", group_svg, re.I)]
                    # The arc track radius is the materialized gauge box;
                    # the larger dial-surface circle is only background paint.
                    if path_radii:
                        radius = path_radii[0]
                    screen_box = {"x": round(cx - radius), "y": round(cy - radius),
                                  "width": round(radius * 2), "height": round(radius * 2)}
                    adapted.properties["screen_box"] = screen_box
            canvas = {}
            for captured in evidence.get("screens", []) if isinstance(evidence.get("screens"), list) else []:
                data = captured.get("data", {}) if isinstance(captured, dict) else {}
                if isinstance(data.get("canvas"), dict):
                    canvas = data["canvas"]; break
                if isinstance(data.get("root"), dict):
                    canvas = data["root"]; break
            target_w = max(1.0, float(canvas.get("width", 320) or 320))
            target_h = max(1.0, float(canvas.get("height", 240) or 240))
            frames = [n.get("rect", {}) for n in nodes if isinstance(n.get("rect"), dict)
                      and abs(float(n["rect"].get("width", 0)) - target_w) <= 4
                      and abs(float(n["rect"].get("height", 0)) - target_h) <= 4]
            frame = frames[0] if frames else {"x": 0, "y": 0, "width": target_w, "height": target_h}
            fw, fh = max(1.0, float(frame.get("width", target_w))), max(1.0, float(frame.get("height", target_h)))
            if (not isinstance(adapted.properties.get("screen_box"), dict)
                    and not isinstance(g.get("geometry"), dict)):
                adapted.properties["screen_box"] = {
                    "x": round((float(chosen_rect.get("x", 0)) - float(frame.get("x", 0))) * target_w / fw),
                    "y": round((float(chosen_rect.get("y", 0)) - float(frame.get("y", 0))) * target_h / fh),
                    "width": round(float(chosen_rect.get("width", 92)) * target_w / fw),
                    "height": round(float(chosen_rect.get("height", 92)) * target_h / fh)}
        if not isinstance(screen_box, dict):
            geometry = g.get("geometry") if isinstance(g.get("geometry"), dict) else {}
            screen_box = {"x": int(float(geometry.get("x", 0) or 0)), "y": int(float(geometry.get("y", 0) or 0)),
                          "width": int(float(geometry.get("width", 92) or 92)), "height": int(float(geometry.get("height", 92) or 92))}
        box_x, box_y = int(screen_box.get("x", 0)), int(screen_box.get("y", 0))
        box_w, box_h = max(1, int(screen_box.get("width", 92))), max(1, int(screen_box.get("height", 92)))
        readout_candidates = []
        for node in nodes:
            rect = node.get("rect") if isinstance(node.get("rect"), dict) else None
            marker = f"{node.get('className', '')} {node.get('id', '')} {node.get('data-openhmi-id', '')}"
            if (rect and re.search(r"(?:gauge|meter)[-_ ]?(?:value|readout)|(?:^|[-_ ])readout(?:$|[-_ ])",
                                   marker, re.I)):
                center_x = float(rect.get("x", 0)) + float(rect.get("width", 0)) / 2
                center_y = float(rect.get("y", 0)) + float(rect.get("height", 0)) / 2
                if box_x <= center_x <= box_x + box_w and box_y <= center_y <= box_y + box_h:
                    distance = abs(center_x - (box_x + box_w / 2)) + abs(center_y - (box_y + box_h / 2))
                    readout_candidates.append((distance, rect))
        readout_box = min(readout_candidates, key=lambda item: item[0])[1] if readout_candidates else None
        def real_number(key: str, default: float) -> float:
            match = re.search(r"[-+]?\d+(?:\.\d+)?", str(g.get(key, default)))
            return float(match.group(0)) if match else default
        source_min = real_number("min", 0.0)
        source_max = max(real_number("max", 100.0), source_min + 1.0)
        source_value = max(source_min, min(source_max, real_number("value", 0.0)))
        if isinstance(svg_metrics.get("ratio"), (int, float)):
            source_value = source_min + float(svg_metrics["ratio"]) * (source_max - source_min)
        # LVGL's arc range is integral. Scale fractional source ranges while
        # preserving the measured SVG ratio and the displayed value rounding.
        scale = 1000 if any(abs(item - round(item)) > 1e-6
                            for item in (source_min, source_max, source_value)) else 1
        minimum = round(source_min * scale)
        maximum = max(round(source_max * scale), minimum + 1)
        value = max(minimum, min(maximum, round(source_value * scale)))
        arc_start = int(round(float(svg_metrics["start"]))) if isinstance(svg_metrics.get("start"), (int, float)) else 135
        arc_sweep = float(svg_metrics["sweep"]) if isinstance(svg_metrics.get("sweep"), (int, float)) else 270.0
        # Source arc paint records carry the authoritative short-arc angles
        # for side instruments; do not turn them into the radial default.
        angle_paint = next((p for p in (g.get("arc_paints", []) if isinstance(g.get("arc_paints"), list) else [])
                            if isinstance(p, dict) and p.get("start") is not None and p.get("end") is not None), None)
        if angle_paint is not None:
            try:
                # React/SVG polar helpers commonly define 0 degrees at the
                # top (subtracting 90 before sin/cos); LVGL defines 0 at the
                # right. Convert coordinate systems before normalization.
                arc_start = int(round(float(angle_paint["start"]))) - 90
                arc_end = int(round(float(angle_paint["end"]))) - 90
                arc_sweep = float(arc_end - arc_start)
            except (TypeError, ValueError):
                arc_end = int(round(arc_start + arc_sweep))
        else:
            arc_end = int(round(arc_start + arc_sweep))
        # LVGL only normalizes angles greater than 360; negative source SVG
        # angles can make its invalidation/draw path non-terminating. Preserve
        # the source sweep while emitting the equivalent [0, 359] angles.
        arc_start %= 360
        arc_end %= 360
        # Short side gauges often expose their semantic endpoints as nearby
        # labels (E/F, C/H, LOW/HIGH, or numeric extrema).  Use those labels
        # to decide whether increasing values paint from the SVG path's start
        # or from its end; source path order alone is not a value direction.
        reverse_arc = False
        if abs(arc_sweep) < 180:
            side_endpoint_labels: list[tuple[float, float, float]] = []
            other_endpoint_labels: list[tuple[float, float, float]] = []
            low_words = {"E", "C", "L", "LOW", "MIN"}
            high_words = {"F", "H", "HIGH", "MAX"}
            for node in nodes:
                rect = node.get("rect") if isinstance(node.get("rect"), dict) else None
                text = str(node.get("text") or "").strip().upper()
                marker = str(node.get("className") or "")
                if not rect or not text or not re.search(r"side|gauge|meter|label|tick", marker, re.I):
                    continue
                center_x = float(rect.get("x", 0)) + float(rect.get("width", 0)) / 2
                center_y = float(rect.get("y", 0)) + float(rect.get("height", 0)) / 2
                if not (box_x - box_w * .35 <= center_x <= box_x + box_w * 1.35
                        and box_y - box_h * .35 <= center_y <= box_y + box_h * 1.35):
                    continue
                rank = 0.0 if text in low_words else 1.0 if text in high_words else None
                if rank is None:
                    try:
                        rank = float(text)
                    except ValueError:
                        continue
                target = side_endpoint_labels if re.search(r"side", marker, re.I) else other_endpoint_labels
                target.append((rank, center_x, center_y))
            endpoint_labels = side_endpoint_labels if len(side_endpoint_labels) >= 2 else other_endpoint_labels
            if len(endpoint_labels) >= 2:
                low = min(endpoint_labels, key=lambda item: item[0])
                high = max(endpoint_labels, key=lambda item: item[0])
                radius = min(box_w, box_h) / 2
                center_x, center_y = box_x + box_w / 2, box_y + box_h / 2
                start_radians, end_radians = math.radians(arc_start), math.radians(arc_end)
                start_point = (center_x + math.cos(start_radians) * radius,
                               center_y + math.sin(start_radians) * radius)
                end_point = (center_x + math.cos(end_radians) * radius,
                             center_y + math.sin(end_radians) * radius)
                dx, dy = end_point[0] - start_point[0], end_point[1] - start_point[1]
                length_sq = max(1.0, dx * dx + dy * dy)
                projection = lambda item: ((item[1] - start_point[0]) * dx
                                           + (item[2] - start_point[1]) * dy) / length_sq
                reverse_arc = projection(low) > projection(high)
        warn_match = re.search(r"\d+(?:\.\d+)?", str(g.get("warn", "0.7"))); danger_match = re.search(r"\d+(?:\.\d+)?", str(g.get("danger", "0.9")))
        warn_pct = float(warn_match.group(0)) if warn_match else 0.7; danger_pct = float(danger_match.group(0)) if danger_match else 0.9
        label = str(g.get("label") or ""); unit = str(g.get("unit") or "")
        display_expression = str(g.get("display") or "")
        show_value = bool(label.strip() or unit.strip() or display_expression.strip())
        decimals_match = re.search(r"\.toFixed\(\s*(\d+)\s*\)", display_expression)
        display_decimals = min(3, int(decimals_match.group(1))) if decimals_match else 0
        colors = g.get("colors") if isinstance(g.get("colors"), dict) else {}
        track = str(colors.get("track", "#263241")); normal = str(colors.get("normal", "#aeb5ff"))
        if svg_metrics.get("stroke"):
            normal = str(svg_metrics["stroke"])
        # A missing foreground paint must not invent the global green default:
        # inherit the source track/computed stroke conservatively.
        foreground_evidence = next((p for p in (g.get("arc_paints", []) if isinstance(g.get("arc_paints"), list) else [])
                                    if isinstance(p, dict) and p.get("role") == "foreground"
                                    and (p.get("stroke_color") or p.get("stroke"))), None)
        if not foreground_evidence and not svg_metrics.get("stroke"):
            # Resolve CSS class paint evidence from serialized SVG. A
            # gradient is approximated by its middle stop and reported as
            # partial; side-progress explicitly resolves to white.
            if "side-progress" in gauge_svg_text or (not gauge_svg_text and abs(arc_sweep) < 180):
                normal = "#ffffff"
            else:
                gradient = re.search(r"<linearGradient\b[^>]*(?:id|data-openhmi-id)=[\"']progress[\"'][^>]*>([\s\S]*?)</linearGradient>", svg_text, re.I)
                stop_source = gradient.group(1) if gradient else svg_text
                stops = re.findall(r"<stop\b[^>]*(?:stop-color|stopColor)=[\"'](#[0-9a-fA-F]{3,6})[\"']", stop_source, re.I)
                if len(stops) >= 2 and ("gauge-progress" in svg_text or "progress" in svg_text):
                    normal = stops[len(stops) // 2]
                    if len(normal) == 4:
                        normal = "#" + "".join(char * 2 for char in normal[1:])
                    adapted.properties["gradient_approximation"] = "partial: CSS/SVG progress gradient uses middle stop"
                else:
                    normal = str(colors.get("track") or track)
        foreground_width = int(round(float(svg_metrics["stroke_width"]))) if isinstance(svg_metrics.get("stroke_width"), (int, float)) else 7
        glow_layers = adapted.properties.get("glow_layers") if isinstance(adapted.properties.get("glow_layers"), list) else []
        if not glow_layers:
            paints = [p for p in g.get("arc_paints", []) if isinstance(p, dict)]
            glow_layers = [p for p in paints if p.get("role") in {"glow-wide", "glow-soft"}]
        if not any(str(p.get("role")) == "glow-wide" for p in glow_layers):
            glow_layers.insert(0, {"role": "glow-wide", "width": 18, "opacity": 30, "color": normal})
        if not any(str(p.get("role")) == "glow-soft" for p in glow_layers):
            glow_layers.insert(1, {"role": "glow-soft", "width": 12, "opacity": 55, "color": normal})
        glow_names = [f"{symbol}_glow_{index}" for index, _ in enumerate(glow_layers)]
        simulation = adapted.properties.get("simulation") if isinstance(adapted.properties.get("simulation"), dict) else {}
        stepped_simulation = abs(arc_sweep) < 180 and not show_value
        if stepped_simulation:
            simulation_period = max(80, _number(simulation, "compact_period_ms", 280))
            simulation_steps = max(2, _number(simulation, "compact_steps_per_sweep", 8))
        else:
            simulation_period = max(16, _number(simulation, "period_ms", 40))
            simulation_steps = max(20, _number(simulation, "steps_per_sweep", 120))
        simulation_step = max(1, (maximum - minimum) // simulation_steps)
        if stepped_simulation:
            value = max(minimum, min(maximum,
                        minimum + round((value - minimum) / simulation_step) * simulation_step))
        value_label = f"{symbol}_value_label"
        declarations = [f"static lv_obj_t * {symbol};"] + [f"static lv_obj_t * {name};" for name in glow_names]
        if show_value:
            declarations.append(f"static lv_obj_t * {value_label};")
            if readout_box:
                readout_cx = round(float(readout_box.get("x", 0)) + float(readout_box.get("width", 0)) / 2)
                readout_cy = round(float(readout_box.get("y", 0)) + float(readout_box.get("height", 0)) / 2)
                declarations += [
                    f"static void {symbol}_hide_captured_readout(lv_obj_t * root)", "{",
                    "    uint32_t count = lv_obj_get_child_count(root);",
                    "    for(uint32_t index = 0; index < count; index++) {",
                    "        lv_obj_t * child = lv_obj_get_child(root, index);",
                    "        if(lv_obj_check_type(child, &lv_label_class)) {",
                    "            lv_area_t area; lv_obj_get_coords(child, &area);",
                    f"            if(area.x1 <= {readout_cx} && area.x2 >= {readout_cx} && area.y1 <= {readout_cy} && area.y2 >= {readout_cy})",
                    "                lv_obj_add_flag(child, LV_OBJ_FLAG_HIDDEN);",
                    "        }",
                    f"        {symbol}_hide_captured_readout(child);",
                    "    }",
                    "}",
                ]
            declarations += [
                f"static void {symbol}_set_display_value(int value)", "{",
                f"    if(!{value_label}) return;",
                "    char text[24];",
            ]
            if display_decimals and scale > 1:
                divisor = 10 ** display_decimals
                fractional_unit = max(1, scale // divisor)
                declarations += [
                    "    int absolute = value < 0 ? -value : value;",
                    f"    lv_snprintf(text, sizeof(text), \"%s%d.%0{display_decimals}d\", value < 0 ? \"-\" : \"\", absolute / {scale}, (absolute % {scale}) / {fractional_unit});",
                ]
            elif scale > 1:
                declarations.append(f"    lv_snprintf(text, sizeof(text), \"%d\", (value + {scale // 2}) / {scale});")
            else:
                declarations.append("    lv_snprintf(text, sizeof(text), \"%d\", value);")
            declarations += [f"    lv_label_set_text({value_label}, text);", "}"]
        lines = declarations + [
            "#ifndef UAGENT_GAUGE_SIMULATION",
            "#define UAGENT_GAUGE_SIMULATION 1",
            "#endif",
            "/* Compact side gauges advance in discrete levels; primary readouts sweep smoothly. */" if stepped_simulation else "/* Primary gauge simulation uses a smooth sweep. */",
            f"static void {symbol}_simulation_timer(lv_timer_t * timer)", "{",
            "    LV_UNUSED(timer);",
            f"    static int direction = 1;",
            f"    if(!{symbol}) return;",
            f"    int next = lv_arc_get_value({symbol}) + direction * {simulation_step};",
            f"    if(next >= {maximum}) {{ next = {maximum}; direction = -1; }}",
            f"    else if(next <= {minimum}) {{ next = {minimum}; direction = 1; }}",
            f"    lv_arc_set_value({symbol}, next);",
        ]
        lines.extend(f"    if({name}) lv_arc_set_value({name}, next);" for name in glow_names)
        if show_value:
            lines.append(f"    {symbol}_set_display_value(next);")
        lines += [
            f"    float pct = (float)(next - {minimum}) / (float)({maximum - minimum});",
            f"    lv_obj_set_style_arc_color({symbol}, pct >= {danger_pct:.3f}f ? lv_color_hex(0xef4444) : (pct >= {warn_pct:.3f}f ? lv_color_hex(0xf59e0b) : {_color(normal, 'aeb5ff')}), LV_PART_INDICATOR);",
            "}", "",
            f"void uagent_build_{symbol}(lv_obj_t * parent)", "{"]
        for index, layer in enumerate(glow_layers):
            layer = layer if isinstance(layer, dict) else {}
            name = glow_names[index]
            color = _color(layer.get("color"), normal)
            width = _number(layer, "width", 15)
            opacity = _number(layer, "opacity", 45)
            lines += [
                f"    {name} = lv_arc_create(parent);",
                f"    lv_obj_set_size({name}, {box_w}, {box_h});",
                f"    lv_obj_set_pos({name}, {box_x}, {box_y});",
                f"    lv_arc_set_range({name}, {minimum}, {maximum});",
                f"    lv_arc_set_value({name}, {value});",
                f"    lv_arc_set_bg_angles({name}, {arc_start}, {arc_end});",
                f"    lv_arc_set_angles({name}, {arc_start}, {arc_end});",
                *([f"    lv_arc_set_mode({name}, LV_ARC_MODE_REVERSE);"] if reverse_arc else []),
                f"    lv_obj_set_style_arc_width({name}, {width}, LV_PART_MAIN);",
                f"    lv_obj_set_style_arc_width({name}, {width}, LV_PART_INDICATOR);",
                f"    lv_obj_set_style_arc_opa({name}, LV_OPA_TRANSP, LV_PART_MAIN);",
                f"    lv_obj_set_style_arc_color({name}, {color}, LV_PART_INDICATOR);",
                f"    lv_obj_set_style_arc_opa({name}, {opacity}, LV_PART_INDICATOR);",
                f"    lv_obj_set_style_arc_rounded({name}, true, LV_PART_INDICATOR);",
                f"    lv_obj_set_style_bg_opa({name}, LV_OPA_TRANSP, LV_PART_KNOB);",
                f"    lv_obj_clear_flag({name}, LV_OBJ_FLAG_CLICKABLE);",
            ]
        tick_records = svg_metrics.get("ticks") if isinstance(svg_metrics.get("ticks"), list) else []
        tick_records = [item for item in tick_records if isinstance(item, dict)]
        view_box = svg_metrics.get("view_box", [])
        global_svg = (len(view_box) == 4 and
                      (view_box[2] > box_w * 1.5 or view_box[3] > box_h * 1.5))
        if global_svg:
            # Full-canvas SVGs may contain several gauges. Assign only the
            # line segments inside this gauge's geometric bounds, then convert
            # absolute SVG coordinates into the LVGL line's local coordinates.
            pad = max(4, round(min(box_w, box_h) * 0.06))
            tick_records = [item for item in tick_records
                            if box_x - pad <= (float(item["x1"]) + float(item["x2"])) / 2 <= box_x + box_w + pad
                            and box_y - pad <= (float(item["y1"]) + float(item["y2"])) / 2 <= box_y + box_h + pad]
        if gauge_svg_text and "<line" not in gauge_svg_text.lower():
            tick_records = []
        tick_records = tick_records[:21]
        if tick_records:
            tick_points = []
            tick_active = []
            tick_widths = []
            tick_opacities = []
            foreground_stroke = str(svg_metrics.get("stroke") or normal).casefold()
            for tick in tick_records:
                tick_points.append("{{{{{}, {}}}, {{{}, {}}}}}".format(
                    round(float(tick.get("x1", 0)) - (box_x if global_svg else 0)),
                    round(float(tick.get("y1", 0)) - (box_y if global_svg else 0)),
                    round(float(tick.get("x2", 0)) - (box_x if global_svg else 0)),
                    round(float(tick.get("y2", 0)) - (box_y if global_svg else 0))))
                stroke = str(tick.get("stroke") or "").casefold()
                active = bool(stroke and stroke == foreground_stroke) or float(tick.get("opacity", 0)) >= 0.65
                tick_active.append("1" if active else "0")
                tick_widths.append(str(max(1, round(float(tick.get("stroke_width", 1))))))
                tick_opacities.append(str(max(0, min(255, round(float(tick.get("opacity", 1)) * 255)))))
            tick_count = len(tick_records)
            inactive_color = next((str(item.get("stroke")) for item in tick_records
                                   if str(item.get("stroke") or "").casefold() != foreground_stroke
                                   and item.get("stroke")), "#1a4060")
            lines += [f"    static const lv_point_precise_t {symbol}_tick_points[{tick_count}][2] = {{" + ",".join(tick_points) + "};",
                       f"    static const uint8_t {symbol}_tick_active[{tick_count}] = {{" + ",".join(tick_active) + "};",
                       f"    static const uint8_t {symbol}_tick_width[{tick_count}] = {{" + ",".join(tick_widths) + "};",
                       f"    static const uint8_t {symbol}_tick_opa[{tick_count}] = {{" + ",".join(tick_opacities) + "};",
                       f"    for(int tick = 0; tick < {tick_count}; tick++) {{",
                       f"        lv_obj_t * tick_line = lv_line_create(parent); lv_line_set_points(tick_line, {symbol}_tick_points[tick], 2);",
                       f"        lv_obj_set_pos(tick_line, {box_x}, {box_y}); lv_obj_set_style_line_color(tick_line, {symbol}_tick_active[tick] ? {_color(normal, 'aeb5ff')} : {_color(inactive_color, '1a4060')}, LV_PART_MAIN);",
                       f"        lv_obj_set_style_line_width(tick_line, {symbol}_tick_width[tick], LV_PART_MAIN);",
                       f"        lv_obj_set_style_line_opa(tick_line, {symbol}_tick_opa[tick], LV_PART_MAIN);",
                       f"    }}"]
        elif not gauge_svg_text:
            tick_points = []
            outer = min(box_w, box_h) // 2 - 2
            for tick in range(21):
                angle = math.radians(135.0 + (270.0 * tick) / 20.0 - 90.0)
                inner = outer - (12 if tick % 5 == 0 else 7)
                tick_points.append("{{{{{}, {}}}, {{{}, {}}}}}".format(
                    box_w // 2 + round(math.cos(angle) * inner),
                    box_h // 2 + round(math.sin(angle) * inner),
                    box_w // 2 + round(math.cos(angle) * outer),
                    box_h // 2 + round(math.sin(angle) * outer)))
            lines += [f"    static const lv_point_precise_t {symbol}_tick_points[21][2] = {{" + ",".join(tick_points) + "};",
                       f"    for(int tick = 0; tick <= 20; tick++) {{",
                       f"        lv_obj_t * tick_line = lv_line_create(parent); lv_line_set_points(tick_line, {symbol}_tick_points[tick], 2);",
                       f"        lv_obj_set_pos(tick_line, {box_x}, {box_y}); lv_obj_set_style_line_color(tick_line, (tick * {maximum} / 20 <= {value}) ? {_color(normal, 'aeb5ff')} : lv_color_hex(0x1a4060), LV_PART_MAIN);",
                       f"        lv_obj_set_style_line_width(tick_line, (tick % 5) == 0 ? 2 : 1, LV_PART_MAIN);",
                       f"    }}"]
        lines += [
            f"    {symbol} = lv_arc_create(parent);",
            f"    lv_obj_set_size({symbol}, {box_w}, {box_h});",
            f"    lv_obj_set_pos({symbol}, {box_x}, {box_y});",
            f"    lv_arc_set_range({symbol}, {minimum}, {maximum});",
            f"    lv_arc_set_value({symbol}, {value});",
            f"    lv_arc_set_bg_angles({symbol}, {arc_start}, {arc_end});",
            f"    lv_arc_set_angles({symbol}, {arc_start}, {arc_end});",
            *([f"    lv_arc_set_mode({symbol}, LV_ARC_MODE_REVERSE);"] if reverse_arc else []),
            f"    lv_obj_set_style_arc_width({symbol}, {foreground_width}, LV_PART_MAIN);",
            f"    lv_obj_set_style_arc_width({symbol}, {foreground_width}, LV_PART_INDICATOR);",
            f"    lv_obj_set_style_arc_color({symbol}, {_color(track, '263241')}, LV_PART_MAIN);",
            f"    lv_obj_set_style_arc_color({symbol}, {_color(normal, 'aeb5ff')}, LV_PART_INDICATOR);",
            f"    lv_obj_set_style_arc_rounded({symbol}, true, LV_PART_MAIN);",
            f"    lv_obj_set_style_arc_rounded({symbol}, true, LV_PART_INDICATOR);",
            f"    lv_obj_set_style_bg_opa({symbol}, LV_OPA_TRANSP, LV_PART_KNOB);",
            f"    lv_obj_clear_flag({symbol}, LV_OBJ_FLAG_CLICKABLE);",
        ]
        if show_value:
            value_font = 48 if min(box_w, box_h) >= 180 else 32
            value_height = value_font + 14
            if readout_box:
                lines += ["    lv_obj_update_layout(parent);",
                          f"    {symbol}_hide_captured_readout(parent);"]
            lines += [
                f"    {value_label} = lv_label_create(parent);",
                f"    lv_obj_set_size({value_label}, {max(48, box_w // 2)}, {value_height});",
                f"    lv_obj_set_pos({value_label}, {box_x + box_w // 4}, {box_y + box_h // 2 - value_height // 2});",
                f"#if defined(LV_FONT_MONTSERRAT_{value_font}) && LV_FONT_MONTSERRAT_{value_font}",
                f"    lv_obj_set_style_text_font({value_label}, &lv_font_montserrat_{value_font}, LV_PART_MAIN);",
                "#else",
                f"    lv_obj_set_style_text_font({value_label}, &lv_font_montserrat_16, LV_PART_MAIN);",
                "#endif",
                f"    lv_obj_set_style_text_color({value_label}, lv_color_hex(0xffffff), LV_PART_MAIN);",
                f"    lv_obj_set_style_text_align({value_label}, LV_TEXT_ALIGN_CENTER, LV_PART_MAIN);",
                f"    lv_obj_set_style_bg_opa({value_label}, LV_OPA_TRANSP, LV_PART_MAIN);",
                f"    {symbol}_set_display_value({value});",
            ]
        lines += [
            "#if UAGENT_GAUGE_SIMULATION",
            f"    lv_timer_create({symbol}_simulation_timer, {simulation_period}, NULL);",
            "#endif",
            "    /* Disable UAGENT_GAUGE_SIMULATION when a real value binding drives this gauge. */"]
        lines += _event_comment(component, model); lines.append("}")
        return "\n".join(lines) + "\n"

    def _carousel(self, symbol: str, adapted: AdaptedWidget, component: Component, model: ReactProjectModel) -> str:
        props = adapted.properties
        vertical = props.get("orientation") == "vertical"
        flow = "LV_FLEX_FLOW_COLUMN" if vertical else "LV_FLEX_FLOW_ROW"
        direction = "LV_DIR_VER" if vertical else "LV_DIR_HOR"
        snap = {"start": "LV_SCROLL_SNAP_START", "end": "LV_SCROLL_SNAP_END"}.get(props.get("snap"), "LV_SCROLL_SNAP_CENTER")
        count = max(1, _number(props, "slide_count", 1))
        title = props.get("title", "")
        lines = [f"void uagent_build_{symbol}(lv_obj_t * parent)", "{", f"    lv_obj_t * {symbol} = lv_obj_create(parent);"]
        lines += _style_lines(symbol, props)
        lines += [
            f"    lv_obj_set_flex_flow({symbol}, {flow});",
            f"    lv_obj_set_scroll_dir({symbol}, {direction});",
            f"    lv_obj_set_scroll_snap_x({symbol}, {snap});",
            f"    lv_obj_set_scrollbar_mode({symbol}, LV_SCROLLBAR_MODE_OFF);",
        ]
        resources = list(adapted.resource_ids)
        for index in range(count):
            card = f"{symbol}_slide_{index}"
            lines += [
                f"    lv_obj_t * {card} = lv_obj_create({symbol});",
                f"    lv_obj_set_size({card}, {_number(props, 'slide_width', _number(props, 'width', 360) - 24)}, {_number(props, 'slide_height', _number(props, 'height', 200) - 24)});",
                f"    lv_obj_set_style_radius({card}, {_number(props, 'card_radius', 14)}, LV_PART_MAIN);",
                f"    lv_obj_set_style_bg_color({card}, {_color(props.get('card_color'), '243454')}, LV_PART_MAIN);",
                f"    lv_obj_set_style_border_width({card}, 0, LV_PART_MAIN);",
            ]
            if index < len(resources):
                image_name = resource_filename(resources[index], model)
                lines += [
                    f"    lv_obj_t * {card}_image = lv_img_create({card});",
                    f"    lv_img_set_src({card}_image, LVGL_IMAGE_PATH({image_name})); /* {resources[index]} */",
                    f"    lv_obj_center({card}_image);",
                ]
            if title:
                lines += [
                    f"    lv_obj_t * {card}_label = lv_label_create({card});",
                    f"    lv_label_set_text({card}_label, {_c_string(title)});",
                    f"    lv_obj_align({card}_label, LV_ALIGN_BOTTOM_LEFT, 0, 0);",
                ]
        lines += _event_comment(component, model)
        lines.append("}")
        return "\n".join(lines) + "\n"

    def _gauge_scene_legacy(self, symbol: str, adapted: AdaptedWidget, component: Component, model: ReactProjectModel) -> str:
        """Render a fixed SVG instrument scene through one transparent host."""
        scene = adapted.properties.get("scene") if isinstance(adapted.properties.get("scene"), dict) else {}
        canvas = scene.get("canvas", {}) if isinstance(scene.get("canvas"), dict) else {}
        width, height = _number(canvas, "width", 480), _number(canvas, "height", 272)
        gauges = scene.get("gauges", []) if isinstance(scene.get("gauges"), list) else []
        lines = [
            "#include <math.h>", "#include <stdio.h>", "",
            f"static lv_obj_t * {symbol}_host = NULL;",
            f"static int32_t {symbol}_speed = 62;",
            f"static int32_t {symbol}_rpm = 800;",
            f"static int32_t {symbol}_power = 0;",
            f"static int32_t {symbol}_battery = 78;", "",
            "void uagent_gauge_scene_update(void);", "",
            f"static void {symbol}_draw_arc(lv_layer_t * layer, int cx, int cy, int radius, int start, int span, int width, lv_color_t color, lv_opa_t opa)",
            "{", "    if(span <= 0) return;", "    lv_draw_arc_dsc_t dsc;", "    lv_draw_arc_dsc_init(&dsc);",
            "    dsc.center.x = cx; dsc.center.y = cy; dsc.radius = (uint16_t)radius;",
            "    dsc.start_angle = start; dsc.end_angle = start + span; dsc.width = width;",
            "    dsc.color = color; dsc.opa = opa; dsc.rounded = 1;",
            "    if(dsc.end_angle <= 359) lv_draw_arc(layer, &dsc);",
            "    else { dsc.end_angle = 359; lv_draw_arc(layer, &dsc); dsc.start_angle = 0; dsc.end_angle = start + span - 360; if(dsc.end_angle > 0) lv_draw_arc(layer, &dsc); }",
            "}", "",
            f"static void {symbol}_draw_line(lv_layer_t * layer, int x1, int y1, int x2, int y2, int width, lv_color_t color, lv_opa_t opa)",
            "{", "    lv_draw_line_dsc_t dsc; lv_draw_line_dsc_init(&dsc);",
            "    dsc.p1.x = x1; dsc.p1.y = y1; dsc.p2.x = x2; dsc.p2.y = y2;",
            "    dsc.width = width; dsc.color = color; dsc.opa = opa; dsc.round_start = 1; dsc.round_end = 1;",
            "    lv_draw_line(layer, &dsc);", "}", "",
            f"static void {symbol}_draw_label(lv_layer_t * layer, int x, int y, int w, int h, const char * text, const lv_font_t * font, lv_color_t color, lv_opa_t opa)",
            "{", "    lv_draw_label_dsc_t dsc; lv_draw_label_dsc_init(&dsc); dsc.text = text; dsc.font = font; dsc.color = color; dsc.opa = opa; dsc.align = LV_TEXT_ALIGN_CENTER;",
            "    lv_area_t area = {x - w / 2, y - h / 2, x + w / 2, y + h / 2}; lv_draw_label(layer, &dsc, &area);", "}", "",
            f"static void {symbol}_draw_main(lv_event_t * event)", "{",
            "    if(lv_event_get_code(event) != LV_EVENT_DRAW_MAIN) return;",
            "    lv_layer_t * layer = lv_event_get_layer(event); if(!layer) return;",
            f"    lv_draw_rect_dsc_t bg; lv_draw_rect_dsc_init(&bg); bg.bg_color = lv_color_hex(0x040507); bg.bg_opa = LV_OPA_COVER;",
            f"    lv_area_t canvas_area = {{0, 0, {width - 1}, {height - 1}}}; lv_draw_rect(layer, &bg, &canvas_area);",
        ]
        # Source START=-135/SWEEP=270 uses a top-oriented polar helper; the
        # generated ticks retain that exact coordinate convention.  LVGL's
        # draw arc is split at 360 by the helper above.
        for index, gauge in enumerate(gauges):
            if not isinstance(gauge, dict):
                continue
            geometry = gauge.get("geometry", {}) if isinstance(gauge.get("geometry"), dict) else {}
            cx, cy, radius = _number(geometry, "cx", 118 if index == 0 else 362), _number(geometry, "cy", 152), _number(geometry, "radius", 108)
            ticks = gauge.get("ticks", {}) if isinstance(gauge.get("ticks"), dict) else {}
            total, major = _number(ticks, "total", 40), max(1, _number(ticks, "major_every", 5))
            labels = [str(item) for item in ticks.get("labels", [])] if isinstance(ticks.get("labels"), list) else []
            range_info = gauge.get("range", {}) if isinstance(gauge.get("range"), dict) else {}
            maximum = _number(range_info, "max", 200 if index == 0 else 8000)
            binding = str(gauge.get("value_binding") or "speed" if index == 0 else "rpm")
            value_expr = f"{symbol}_rpm" if "rpm" in binding.casefold() else f"{symbol}_speed"
            color = "#BDD8F8" if index == 0 else "#2ECFB8"
            track = next((p for p in gauge.get("paint_layers", []) if isinstance(p, dict) and p.get("role") == "track"), {})
            red_zone = next((p for p in gauge.get("paint_layers", []) if isinstance(p, dict) and p.get("role") == "red-zone"), {})
            wide = next((p for p in gauge.get("paint_layers", []) if isinstance(p, dict) and p.get("role") == "glow-wide"), {})
            soft = next((p for p in gauge.get("paint_layers", []) if isinstance(p, dict) and p.get("role") == "glow-soft"), {})
            foreground = next((p for p in gauge.get("paint_layers", []) if isinstance(p, dict) and p.get("role") == "foreground"), {})
            track_color = _color(track.get("stroke_color"), "263241")
            red_color = _color(red_zone.get("stroke_color"), "ff3b30")
            wide_color = _color(wide.get("stroke_color"), "b4dcff" if index == 0 else "2ecfb8")
            soft_color = _color(soft.get("stroke_color"), color[1:])
            foreground_color = _color(foreground.get("stroke_color"), color[1:])
            opacity = lambda p, default: _number({"value": round(float(p.get("opacity", default)) * 100) if float(p.get("opacity", default)) <= 1 else p.get("opacity", default)}, "value", default) if isinstance(p, dict) else default
            lines += [
                f"    /* {gauge.get('gauge_id', f'gauge_{index + 1}')}: source cx={cx} cy={cy} r={radius}, START=-135 SWEEP=270; paint layers: track, glow-wide, glow-soft, foreground */",
                f"    {symbol}_draw_arc(layer, {cx}, {cy}, {radius}, 135, 270, {_number(gauge, 'track_width', 11)}, {track_color}, {opacity(track, 18)});",
            ]
            if red_zone:
                red = gauge.get("redline") if isinstance(gauge.get("redline"), dict) else {}
                red_tick = _number(red, "tick", 24); red_total = max(1, _number(red, "tick_total", 32))
                lines.append(f"    /* redline: source tick {red_tick}/{red_total} */ {symbol}_draw_arc(layer, {cx}, {cy}, {radius}, 135 + (270 * {red_tick}) / {red_total}, 270 - (270 * {red_tick}) / {red_total}, {_number(gauge, 'track_width', 11)}, {red_color}, {opacity(red_zone, 18)});")
            lines += [
                f"    int {symbol}_span_{index} = (int)((float){value_expr} / {maximum} * 270.0f);",
                f"    lv_color_t {symbol}_dynamic_color_{index} = {('(' + value_expr + ' > 6800 ? lv_color_hex(0xff3b30) : (' + value_expr + ' > 5760 ? lv_color_hex(0xffd60a) : lv_color_hex(0x2ecfb8)))') if index == 1 else foreground_color};",
                "    /* dynamic foreground glow layers: 27/6, 23/10, 19/18, 15/35, 10/255 */",
                f"    {symbol}_draw_arc(layer, {cx}, {cy}, {radius}, 135, {symbol}_span_{index}, 27, {symbol}_dynamic_color_{index}, 6);",
                f"    {symbol}_draw_arc(layer, {cx}, {cy}, {radius}, 135, {symbol}_span_{index}, 23, {symbol}_dynamic_color_{index}, 10);",
                f"    {symbol}_draw_arc(layer, {cx}, {cy}, {radius}, 135, {symbol}_span_{index}, 19, {symbol}_dynamic_color_{index}, 18);",
                f"    {symbol}_draw_arc(layer, {cx}, {cy}, {radius}, 135, {symbol}_span_{index}, 15, {symbol}_dynamic_color_{index}, 35);",
                f"    {symbol}_draw_arc(layer, {cx}, {cy}, {radius}, 135, {symbol}_span_{index}, 10, {symbol}_dynamic_color_{index}, 255);",
                f"    for(int i = 0; i <= {total}; i++) {{ float a = (-135.0f + (270.0f * i) / {total} - 90.0f) * 0.0174532925f; int outer_x = {cx} + (int)roundf(cosf(a) * ({radius} - 3)); int outer_y = {cy} + (int)roundf(sinf(a) * ({radius} - 3)); int inner_r = (i % {major} == 0) ? {radius} - 22 : {radius} - 11; int inner_x = {cx} + (int)roundf(cosf(a) * inner_r); int inner_y = {cy} + (int)roundf(sinf(a) * inner_r); {symbol}_draw_line(layer, outer_x, outer_y, inner_x, inner_y, (i % {major} == 0) ? 2 : 1, (i >= {red_tick if red_zone else total + 1}) ? lv_color_hex(0xff3b30) : lv_color_hex(0xffffff), (i % {major} == 0) ? 130 : 36); }}",
            ]
            if labels:
                cases = " ".join(f"case {i * major}: label = {_c_string(label)}; break;" for i, label in enumerate(labels))
                lines += [f"    for(int i = 0; i <= {total}; i += {major}) {{ const char * label = \"\"; switch(i) {{ {cases} }} float a = (-135.0f + (270.0f * i) / {total} - 90.0f) * 0.0174532925f; {symbol}_draw_label(layer, {cx} + (int)roundf(cosf(a) * ({radius} - 33)), {cy} + (int)roundf(sinf(a) * ({radius} - 33)), 34, 14, label, &lv_font_montserrat_8, lv_color_hex(0xffffff), 82); }}"]
            readout = gauge.get("readout", {}) if isinstance(gauge.get("readout"), dict) else {}
            unit = str(readout.get("unit") or ("KM/H" if index == 0 else "x1000 RPM"))
            value_buffer = f"{symbol}_readout_{index}"
            if "rpm" in binding.casefold():
                value_format = f'"%.1f", (double){value_expr} / 1000.0'
            else:
                value_format = f'"%ld", (long){value_expr}'
            lines += [f"    char {value_buffer}[24]; snprintf({value_buffer}, sizeof({value_buffer}), {value_format});",
                      f"    {symbol}_draw_label(layer, {cx}, {cy - 10}, 86, 42, {value_buffer}, {'&lv_font_montserrat_38' if index == 0 else '&lv_font_montserrat_32'}, lv_color_hex(0xffffff), LV_OPA_COVER);",
                      f"    {symbol}_draw_label(layer, {cx}, {cy + 22}, 92, 18, {_c_string(unit)}, {'&lv_font_montserrat_8' if index == 0 else '&lv_font_montserrat_10'}, lv_color_hex(0xffffff), 96);"]
        lines += [
            f"    {symbol}_draw_line(layer, 228, 124, 228, 180, 1, lv_color_hex(0xffffff), 18);",
            f"    {symbol}_draw_line(layer, 252, 124, 252, 180, 1, lv_color_hex(0xffffff), 18);",
            f"    {symbol}_draw_label(layer, 240, 142, 32, 26, \"P\", &lv_font_montserrat_20, lv_color_hex(0xffffff), 230);",
            f"    {symbol}_draw_label(layer, 240, 165, 48, 14, \"GEAR\", &lv_font_montserrat_8, lv_color_hex(0xffffff), 72);",
            f"    {symbol}_draw_label(layer, 48, 14, 72, 16, \"22 C\", &lv_font_montserrat_12, lv_color_hex(0xffffff), 140);",
            f"    {symbol}_draw_label(layer, 240, 14, 100, 18, \"--:--\", &lv_font_montserrat_14, lv_color_hex(0xffffff), 210);",
            f"    {symbol}_draw_label(layer, 52, 228, 72, 22, \"+0 kW\", &lv_font_montserrat_18, lv_color_hex(0x30d158), 255);",
            f"    {symbol}_draw_label(layer, 52, 245, 92, 14, \"POWER\", &lv_font_montserrat_8, lv_color_hex(0xffffff), 96);",
            f"    {symbol}_draw_label(layer, 240, 228, 80, 22, \"78%\", &lv_font_montserrat_18, lv_color_hex(0xffffff), 220);",
            f"    {symbol}_draw_label(layer, 240, 245, 80, 14, \"BATTERY\", &lv_font_montserrat_8, lv_color_hex(0xffffff), 96);",
            f"    for(int i = 0; i < 10; i++) {symbol}_draw_line(layer, 130 + i * 14, 258, 142 + i * 14, 258, 6, i < ({symbol}_battery / 10) ? lv_color_hex(0x30d158) : lv_color_hex(0x252a31), 220);",
            f"    {symbol}_draw_label(layer, 426, 228, 72, 22, \"287 KM\", &lv_font_montserrat_18, lv_color_hex(0xffffff), 220);",
            f"    {symbol}_draw_label(layer, 426, 245, 96, 14, \"RANGE\", &lv_font_montserrat_8, lv_color_hex(0xffffff), 96);",
            "}", "",
            f"static void {symbol}_timer(lv_timer_t * timer)", "{", "    LV_UNUSED(timer);",
            f"    {symbol}_speed += 1; if({symbol}_speed > 200) {symbol}_speed = 0;",
            f"    {symbol}_rpm += 50; if({symbol}_rpm > 8000) {symbol}_rpm = 800;",
            f"    {symbol}_power = ({symbol}_speed * 85) / 100; {symbol}_battery = 78;",
            f"    uagent_gauge_scene_update();", "}", "",
            f"void uagent_gauge_scene_set_values(int32_t speed, int32_t rpm, int32_t power, int32_t battery)", "{",
            f"    {symbol}_speed = speed < 0 ? 0 : speed > 200 ? 200 : speed; {symbol}_rpm = rpm < 800 ? 800 : rpm > 8000 ? 8000 : rpm;",
            f"    {symbol}_power = power; {symbol}_battery = battery < 0 ? 0 : battery > 100 ? 100 : battery; uagent_gauge_scene_update();",
            "}", "",
            "void uagent_gauge_scene_update(void)", "{", f"    if({symbol}_host) lv_obj_invalidate({symbol}_host);", "}", "",
            f"void uagent_build_{symbol}(lv_obj_t * parent)", "{",
            f"    {symbol}_host = lv_obj_create(parent); lv_obj_set_size({symbol}_host, {width}, {height}); lv_obj_set_pos({symbol}_host, 0, 0);",
            f"    lv_obj_set_style_bg_opa({symbol}_host, LV_OPA_0, LV_PART_MAIN); lv_obj_set_style_border_width({symbol}_host, 0, LV_PART_MAIN); lv_obj_set_style_radius({symbol}_host, 0, LV_PART_MAIN);",
            f"    lv_obj_add_event_cb({symbol}_host, {symbol}_draw_main, LV_EVENT_DRAW_MAIN, NULL);",
            f"    lv_timer_create({symbol}_timer, 80, NULL);",
            "}",
        ]
        return "\n".join(lines) + "\n"

    def _gauge_scene(self, symbol: str, adapted: AdaptedWidget, component: Component, model: ReactProjectModel) -> str:
        """Render one source-backed instrument scene and its status slots."""
        scene = adapted.properties.get("scene") if isinstance(adapted.properties.get("scene"), dict) else {}
        canvas = scene.get("canvas", {}) if isinstance(scene.get("canvas"), dict) else {}
        width, height = _number(canvas, "width", 480), _number(canvas, "height", 272)
        gauges = scene.get("gauges", []) if isinstance(scene.get("gauges"), list) else []
        status_bars = adapted.properties.get("status_bars")
        if not isinstance(status_bars, list):
            status_bars = scene.get("status_bars", []) if isinstance(scene.get("status_bars"), list) else []
        status_bar = next((bar for bar in status_bars if isinstance(bar, dict)), {})
        status_slots = status_bar.get("slots", {}) if isinstance(status_bar.get("slots"), dict) else {}
        temperature = 22
        signal_level = 3
        signal_max = 3
        clock_interval = 1000
        for item in status_slots.get("start", []) if isinstance(status_slots.get("start"), list) else []:
            if isinstance(item, dict) and item.get("type") == "value_with_unit" and isinstance(item.get("value"), (int, float)):
                temperature = int(item["value"])
        for item in status_slots.get("end", []) if isinstance(status_slots.get("end"), list) else []:
            if isinstance(item, dict) and item.get("type") == "signal_bars":
                signal_max = max(1, _number(item, "bar_count", 3))
                signal_level = min(signal_max, max(0, _number(item, "level", signal_max)))
        for item in status_slots.get("center", []) if isinstance(status_slots.get("center"), list) else []:
            if isinstance(item, dict) and item.get("type") == "clock":
                clock_interval = max(1, _number(item, "interval_ms", 1000))

        lines = [
            "#include <math.h>", "#include <stdio.h>", "#include <stdbool.h>", "#include <string.h>", "#include <time.h>", "",
            f"static lv_obj_t * {symbol}_host = NULL;",
            f"static lv_timer_t * {symbol}_scene_timer = NULL;",
            f"static lv_timer_t * {symbol}_clock_timer = NULL;",
            f"static int32_t {symbol}_speed = 62;",
            f"static int32_t {symbol}_rpm = 800;",
            f"static int32_t {symbol}_power = 0;",
            f"static int32_t {symbol}_battery = 78;",
            f"static int32_t {symbol}_temperature = {temperature};",
            f"static int32_t {symbol}_signal_level = {signal_level};",
            f"static int32_t {symbol}_signal_max = {signal_max};",
            f"static int32_t {symbol}_wifi_connected = 1;",
            f"static int32_t {symbol}_time_hour = 20;",
            f"static int32_t {symbol}_time_minute = 31;",
            f"static int32_t {symbol}_time_manual = 0;", "",
            "void uagent_gauge_scene_update(void);", "",
            f"static int {symbol}_norm_angle(int angle)", "{", "    angle %= 360; if(angle < 0) angle += 360; return angle;", "}", "",
            f"static void {symbol}_draw_arc(lv_layer_t * layer, int cx, int cy, int radius, int start, int span, int width, lv_color_t color, lv_opa_t opa)",
            "{", "    if(span <= 0 || radius <= 0 || width <= 0) return;", "    lv_draw_arc_dsc_t dsc; lv_draw_arc_dsc_init(&dsc);",
            f"    dsc.center.x = cx; dsc.center.y = cy; dsc.radius = (uint16_t)radius; dsc.width = (uint16_t)width; dsc.color = color; dsc.opa = opa; dsc.rounded = 1;",
            f"    int first = {symbol}_norm_angle(start);",
            "    if(span >= 360) { dsc.start_angle = first; dsc.end_angle = first + 360; lv_draw_arc(layer, &dsc); return; }",
            "    int last = first + span;",
            "    if(last <= 360) { dsc.start_angle = first; dsc.end_angle = last; lv_draw_arc(layer, &dsc); return; }",
            "    dsc.start_angle = first; dsc.end_angle = 360; lv_draw_arc(layer, &dsc);",
            "    dsc.start_angle = 0; dsc.end_angle = last - 360; lv_draw_arc(layer, &dsc);", "}", "",
            f"static void {symbol}_draw_circle(lv_layer_t * layer, int cx, int cy, int radius, int width, lv_color_t color, lv_opa_t opa)",
            "{", f"    {symbol}_draw_arc(layer, cx, cy, radius, 0, 360, width, color, opa);", "}", "",
            f"static void {symbol}_draw_rect(lv_layer_t * layer, int x1, int y1, int x2, int y2, int radius, int border_width, lv_color_t color, lv_opa_t fill_opa, lv_opa_t border_opa)",
            "{", "    lv_draw_rect_dsc_t dsc; lv_draw_rect_dsc_init(&dsc); dsc.bg_color = color; dsc.bg_opa = fill_opa;",
            "    dsc.border_color = color; dsc.border_opa = border_opa; dsc.border_width = border_width; dsc.radius = radius;",
            "    lv_area_t area = {x1, y1, x2, y2}; lv_draw_rect(layer, &dsc, &area);", "}", "",
            f"static void {symbol}_draw_line(lv_layer_t * layer, int x1, int y1, int x2, int y2, int width, lv_color_t color, lv_opa_t opa)",
            "{", "    lv_draw_line_dsc_t dsc; lv_draw_line_dsc_init(&dsc);",
            "    dsc.p1.x = x1; dsc.p1.y = y1; dsc.p2.x = x2; dsc.p2.y = y2; dsc.width = width;",
            "    dsc.color = color; dsc.opa = opa; dsc.round_start = 1; dsc.round_end = 1; lv_draw_line(layer, &dsc);", "}", "",
            f"static void {symbol}_draw_label(lv_layer_t * layer, int x, int y, int w, int h, const char * text, const lv_font_t * font, lv_color_t color, lv_opa_t opa)",
            "{", "    lv_draw_label_dsc_t dsc; lv_draw_label_dsc_init(&dsc); dsc.text = text; dsc.font = font; dsc.color = color; dsc.opa = opa; dsc.align = LV_TEXT_ALIGN_CENTER;",
            "    lv_area_t area = {x - w / 2, y - h / 2, x + w / 2, y + h / 2}; lv_draw_label(layer, &dsc, &area);", "}", "",
            f"static void {symbol}_draw_status_icon(lv_layer_t * layer, const char * name, int x, int y, int opa)",
            "{", "    lv_color_t white = lv_color_hex(0xffffff);",
            f"    if(strcmp(name, \"user\") == 0) {{ {symbol}_draw_circle(layer, x + 7, y + 5, 3, 1, white, (lv_opa_t)(opa * 28 / 100)); {symbol}_draw_line(layer, x + 2, y + 13, x + 3, y + 10, 1, white, (lv_opa_t)(opa * 28 / 100)); {symbol}_draw_line(layer, x + 3, y + 10, x + 7, y + 8, 1, white, (lv_opa_t)(opa * 28 / 100)); {symbol}_draw_line(layer, x + 7, y + 8, x + 11, y + 10, 1, white, (lv_opa_t)(opa * 28 / 100)); {symbol}_draw_line(layer, x + 11, y + 10, x + 12, y + 13, 1, white, (lv_opa_t)(opa * 28 / 100)); }}",
            f"    else if(strcmp(name, \"lock\") == 0) {{ {symbol}_draw_rect(layer, x + 1, y + 4, x + 15, y + 13, 2, 1, white, LV_OPA_0, (lv_opa_t)(opa * 22 / 100)); {symbol}_draw_arc(layer, x + 8, y + 4, 4, 135, 90, 1, white, (lv_opa_t)(opa * 22 / 100)); {symbol}_draw_circle(layer, x + 8, y + 8, 1, 1, white, (lv_opa_t)(opa * 40 / 100)); }}",
            f"    else if(strcmp(name, \"wifi\") == 0 || strcmp(name, \"wlan\") == 0) {{ {symbol}_draw_circle(layer, x + 7, y + 9, 1, 2, white, (lv_opa_t)(opa * 50 / 100)); {symbol}_draw_arc(layer, x + 7, y + 10, 4, 225, 90, 1, white, (lv_opa_t)(opa * 40 / 100)); {symbol}_draw_arc(layer, x + 7, y + 10, 7, 225, 90, 1, white, (lv_opa_t)(opa * 18 / 100)); }}",
            "}", "",
            f"static void {symbol}_sync_local_time(void)", "{", f"    if({symbol}_time_manual) return;", "#if defined(_WIN32)",
            "    time_t now = time(NULL); struct tm * local = localtime(&now);",
            f"    if(local) {{ {symbol}_time_hour = local->tm_hour; {symbol}_time_minute = local->tm_min; }}",
            "#endif", "}", "",
            f"static void {symbol}_draw_main(lv_event_t * event)", "{",
            "    if(lv_event_get_code(event) != LV_EVENT_DRAW_MAIN) return;",
            "    lv_layer_t * layer = lv_event_get_layer(event); if(!layer) return;",
            f"    lv_draw_rect_dsc_t bg; lv_draw_rect_dsc_init(&bg); bg.bg_color = lv_color_hex(0x040507); bg.bg_opa = LV_OPA_COVER;",
            f"    lv_area_t canvas_area = {{0, 0, {width - 1}, {height - 1}}}; lv_draw_rect(layer, &bg, &canvas_area);",
        ]
        for index, gauge in enumerate(gauges):
            if not isinstance(gauge, dict):
                continue
            geometry = gauge.get("geometry", {}) if isinstance(gauge.get("geometry"), dict) else {}
            default_cx = int(width * (2 * index + 1) / max(1, 2 * len(gauges)))
            default_cy = int(height * 0.56)
            default_radius = max(1, min(int(width / max(1, len(gauges)) * 0.45), int(height * 0.42)))
            cx, cy, radius = _number(geometry, "cx", default_cx), _number(geometry, "cy", default_cy), _number(geometry, "radius", default_radius)
            ticks = gauge.get("ticks", {}) if isinstance(gauge.get("ticks"), dict) else {}
            total, major = _number(ticks, "total", 40), max(1, _number(ticks, "major_every", 5))
            labels = [str(item) for item in ticks.get("labels", [])] if isinstance(ticks.get("labels"), list) else []
            range_info = gauge.get("range", {}) if isinstance(gauge.get("range"), dict) else {}
            maximum = max(1, _number(range_info, "max", 100))
            binding = str(gauge.get("value_binding") or ("speed" if index == 0 else "rpm"))
            value_expr = f"{symbol}_rpm" if "rpm" in binding.casefold() else f"{symbol}_speed"
            color = "#BDD8F8" if index == 0 else "#2ECFB8"
            track = next((p for p in gauge.get("paint_layers", []) if isinstance(p, dict) and p.get("role") == "track"), {})
            red_zone = next((p for p in gauge.get("paint_layers", []) if isinstance(p, dict) and p.get("role") == "red-zone"), {})
            foreground = next((p for p in gauge.get("paint_layers", []) if isinstance(p, dict) and p.get("role") == "foreground"), {})
            track_color = _color(track.get("stroke_color"), "263241")
            red_color = _color(red_zone.get("stroke_color"), "ff3b30")
            foreground_color = _color(foreground.get("stroke_color"), color[1:])
            def opacity(paint: dict[str, Any], default: int) -> int:
                if not isinstance(paint, dict):
                    return default
                try:
                    raw = float(paint.get("opacity", default))
                    return max(0, min(255, round(raw * 255 if raw <= 1 else raw)))
                except (TypeError, ValueError):
                    return default
            red = gauge.get("redline") if isinstance(gauge.get("redline"), dict) else {}
            red_tick, red_total = _number(red, "tick", total + 1), max(1, _number(red, "tick_total", total))
            lines += [
                f"    /* {gauge.get('gauge_id', f'gauge_{index + 1}')}: source geometry cx={cx} cy={cy} r={radius} */",
                f"    {symbol}_draw_arc(layer, {cx}, {cy}, {radius}, 135, 270, {_number(gauge, 'track_width', 11)}, {track_color}, {opacity(track, 18)});",
            ]
            if red_zone:
                lines.append(f"    /* redline: source tick {red_tick}/{red_total} */ {symbol}_draw_arc(layer, {cx}, {cy}, {radius}, 135 + (270 * {red_tick}) / {red_total}, 270 - (270 * {red_tick}) / {red_total}, {_number(gauge, 'track_width', 11)}, {red_color}, {opacity(red_zone, 18)});")
            dynamic_color = f"({value_expr} > 6800 ? lv_color_hex(0xff3b30) : ({value_expr} > 5760 ? lv_color_hex(0xffd60a) : lv_color_hex(0x2ecfb8)))" if "rpm" in binding.casefold() else foreground_color
            lines += [
                f"    int {symbol}_span_{index} = (int)((float){value_expr} / {maximum} * 270.0f);",
                f"    lv_color_t {symbol}_dynamic_color_{index} = {dynamic_color};",
                "    /* glow-wide and glow-soft: source Gaussian blur is represented by synchronized geometry-only passes. */",
                "    /* dynamic foreground glow layers: 27/6, 23/10, 19/18, 15/35, 10/255 */",
                f"    {symbol}_draw_arc(layer, {cx}, {cy}, {radius}, 135, {symbol}_span_{index}, 27, {symbol}_dynamic_color_{index}, 6);",
                f"    {symbol}_draw_arc(layer, {cx}, {cy}, {radius}, 135, {symbol}_span_{index}, 23, {symbol}_dynamic_color_{index}, 10);",
                f"    {symbol}_draw_arc(layer, {cx}, {cy}, {radius}, 135, {symbol}_span_{index}, 19, {symbol}_dynamic_color_{index}, 18);",
                f"    {symbol}_draw_arc(layer, {cx}, {cy}, {radius}, 135, {symbol}_span_{index}, 15, {symbol}_dynamic_color_{index}, 35);",
                f"    {symbol}_draw_arc(layer, {cx}, {cy}, {radius}, 135, {symbol}_span_{index}, 10, {symbol}_dynamic_color_{index}, 255);",
                f"    for(int i = 0; i <= {total}; i++) {{ float a = (-135.0f + (270.0f * i) / {total} - 90.0f) * 0.0174532925f; int outer_x = {cx} + (int)roundf(cosf(a) * ({radius} - 3)); int outer_y = {cy} + (int)roundf(sinf(a) * ({radius} - 3)); int inner_r = (i % {major} == 0) ? {radius} - 22 : {radius} - 11; int inner_x = {cx} + (int)roundf(cosf(a) * inner_r); int inner_y = {cy} + (int)roundf(sinf(a) * inner_r); {symbol}_draw_line(layer, outer_x, outer_y, inner_x, inner_y, (i % {major} == 0) ? 2 : 1, (i >= {red_tick}) ? lv_color_hex(0xff3b30) : lv_color_hex(0xffffff), (i % {major} == 0) ? 130 : 36); }}",
            ]
            if labels:
                cases = " ".join(f"case {i * major}: label = {_c_string(label)}; break;" for i, label in enumerate(labels))
                lines.append(f"    for(int i = 0; i <= {total}; i += {major}) {{ const char * label = \"\"; switch(i) {{ {cases} }} float a = (-135.0f + (270.0f * i) / {total} - 90.0f) * 0.0174532925f; {symbol}_draw_label(layer, {cx} + (int)roundf(cosf(a) * ({radius} - 33)), {cy} + (int)roundf(sinf(a) * ({radius} - 33)), 34, 14, label, &lv_font_montserrat_8, lv_color_hex(0xffffff), 82); }}")
            readout = gauge.get("readout", {}) if isinstance(gauge.get("readout"), dict) else {}
            unit = str(readout.get("unit") or ("KM/H" if index == 0 else "x1000 RPM"))
            value_buffer = f"{symbol}_readout_{index}"
            value_format = f'"%.1f", (double){value_expr} / 1000.0' if "rpm" in binding.casefold() else f'"%ld", (long){value_expr}'
            lines += [
                f"    char {value_buffer}[24]; snprintf({value_buffer}, sizeof({value_buffer}), {value_format});",
                f"    {symbol}_draw_label(layer, {cx}, {cy - 10}, 86, 42, {value_buffer}, {'&lv_font_montserrat_38' if index == 0 else '&lv_font_montserrat_32'}, lv_color_hex(0xffffff), LV_OPA_COVER);",
                f"    {symbol}_draw_label(layer, {cx}, {cy + 22}, 92, 18, {_c_string(unit)}, {'&lv_font_montserrat_8' if index == 0 else '&lv_font_montserrat_10'}, lv_color_hex(0xffffff), 96);",
            ]
        lines += [
            f"    {symbol}_draw_line(layer, 228, 124, 228, 180, 1, lv_color_hex(0xffffff), 18);",
            f"    {symbol}_draw_line(layer, 252, 124, 252, 180, 1, lv_color_hex(0xffffff), 18);",
            f"    {symbol}_draw_label(layer, 240, 142, 32, 26, \"P\", &lv_font_montserrat_20, lv_color_hex(0xffffff), 230);",
            f"    {symbol}_draw_label(layer, 240, 165, 48, 14, \"GEAR\", &lv_font_montserrat_8, lv_color_hex(0xffffff), 72);",
        ]
        if status_bar:
            frame = status_bar.get("frame", {}) if isinstance(status_bar.get("frame"), dict) else {}
            fx, fy = _number(frame, "x", 0), _number(frame, "y", 0)
            fw, fh = _number(frame, "width", width), _number(frame, "height", 32)
            layout = status_bar.get("layout", {}) if isinstance(status_bar.get("layout"), dict) else {}
            padding = list(layout.get("padding", [0, 14, 0, 14])) if isinstance(layout.get("padding"), list) else [0, 14, 0, 14]
            while len(padding) < 4:
                padding.append(padding[-1] if padding else 0)
            pad_top, pad_right, _pad_bottom, pad_left = [int(float(value or 0)) for value in padding[:4]]
            center_y = fy + max(1, fh) // 2
            slot_gaps = layout.get("slot_gaps", {}) if isinstance(layout.get("slot_gaps"), dict) else {}
            start_gap, end_gap = _number(slot_gaps, "start", 10), _number(slot_gaps, "end", 8)
            start_items = status_slots.get("start", []) if isinstance(status_slots.get("start"), list) else []
            center_items = status_slots.get("center", []) if isinstance(status_slots.get("center"), list) else []
            end_items = status_slots.get("end", []) if isinstance(status_slots.get("end"), list) else []
            def item_width(item: dict[str, Any]) -> int:
                kind = str(item.get("type", ""))
                if kind in {"vector_icon", "connectivity_indicator"}:
                    return max(1, _number(item, "width", 14))
                if kind == "signal_bars":
                    count = max(1, _number(item, "bar_count", 3))
                    bar_width = max(1, _number(item, "width", 3))
                    return count * bar_width + max(0, count - 1) * max(0, _number(item, "gap", 2))
                if kind == "value_with_unit":
                    return 32
                if kind == "clock":
                    return 72
                return 16
            def item_height(item: dict[str, Any]) -> int:
                kind = str(item.get("type", ""))
                if kind == "signal_bars":
                    return max(1, _number(item, "height_base", 3) + _number(item, "bar_count", 3) * _number(item, "height_step", 3))
                return max(1, _number(item, "height", 14))
            def emit_status_item(item: dict[str, Any], x: int, y: int) -> list[str]:
                kind = str(item.get("type", ""))
                if str(item.get("support", "custom")) == "fallback":
                    return [f"    /* status fallback retained in audit: {kind} */"]
                if kind == "vector_icon":
                    return [f"    {symbol}_draw_status_icon(layer, {_c_string(item.get('name', 'vector'))}, {x}, {y}, 255); /* source SVG vector icon */"]
                if kind == "connectivity_indicator":
                    return [f"    {symbol}_draw_status_icon(layer, \"wifi\", {x}, {y}, {symbol}_wifi_connected ? 255 : 54); /* source Wi-Fi paths */"]
                if kind == "value_with_unit":
                    value_style = item.get("value_style") if isinstance(item.get("value_style"), dict) else {}
                    unit_style = item.get("unit_style") if isinstance(item.get("unit_style"), dict) else {}
                    unit = str(item.get("unit") or "")
                    return [
                        f"    char {symbol}_temperature_text[16]; snprintf({symbol}_temperature_text, sizeof({symbol}_temperature_text), \"%ld\", (long){symbol}_temperature);",
                        f"    {symbol}_draw_label(layer, {x + 8}, {y + 7}, 18, 14, {symbol}_temperature_text, &lv_font_montserrat_12, {_color_from_raw(value_style.get('color'), 'ffffff')}, {_opa(value_style.get('color'), 140)});",
                        f"    {symbol}_draw_label(layer, {x + 24}, {y + 8}, 18, 12, {_c_string(unit)}, &lv_font_montserrat_8, {_color_from_raw(unit_style.get('color'), 'ffffff')}, {_opa(unit_style.get('color'), 76)});",
                    ]
                if kind == "clock":
                    style = item.get("style") if isinstance(item.get("style"), dict) else {}
                    return [
                        f"    char {symbol}_clock_text[8]; snprintf({symbol}_clock_text, sizeof({symbol}_clock_text), \"%02ld:%02ld\", (long){symbol}_time_hour, (long){symbol}_time_minute);",
                        f"    {symbol}_draw_label(layer, {x}, {y}, 72, 18, {symbol}_clock_text, &lv_font_montserrat_14, {_color_from_raw(style.get('color'), 'ffffff')}, {_opa(style.get('color'), 210)});",
                    ]
                if kind == "signal_bars":
                    count = max(1, _number(item, "bar_count", 3))
                    bar_width = max(1, _number(item, "width", 3))
                    gap = max(0, _number(item, "gap", 2))
                    base = max(1, _number(item, "height_base", 3))
                    step = max(1, _number(item, "height_step", 3))
                    baseline = y + item_height(item) - 1
                    return [f"    for(int i = 1; i <= {count}; i++) {{ int bar_h = {base} + i * {step}; {symbol}_draw_rect(layer, {x} + (i - 1) * ({bar_width} + {gap}), {baseline} - bar_h, {x} + (i - 1) * ({bar_width} + {gap}) + {bar_width} - 1, {baseline}, 1, 0, lv_color_hex(0xffffff), i <= {symbol}_signal_level ? 140 : 36, 0); }}"]
                return [f"    /* status item retained in audit: {kind} */"]
            lines.append(f"    /* status bar: source edge={status_bar.get('edge', 'top')} frame={fx},{fy},{fw},{fh}; bounded background */")
            lines.append(f"    {symbol}_draw_rect(layer, {fx}, {fy}, {fx + fw - 1}, {fy + fh - 1}, 0, 0, lv_color_hex(0x040507), 242, 0);")
            cursor = fx + pad_left
            for item in start_items:
                if not isinstance(item, dict):
                    continue
                lines += emit_status_item(item, cursor, fy + pad_top + max(0, (fh - item_height(item)) // 2))
                cursor += item_width(item) + start_gap
            for item in center_items:
                if isinstance(item, dict):
                    lines += emit_status_item(item, fx + fw // 2, center_y)
            concrete_end = [item for item in end_items if isinstance(item, dict)]
            end_width = sum(item_width(item) for item in concrete_end) + max(0, len(concrete_end) - 1) * end_gap
            cursor = fx + fw - pad_right - end_width
            for item in concrete_end:
                lines += emit_status_item(item, cursor, fy + max(0, (fh - item_height(item)) // 2))
                cursor += item_width(item) + end_gap
        lines += [
            f"    {symbol}_draw_label(layer, 52, 228, 72, 22, \"+0 kW\", &lv_font_montserrat_18, lv_color_hex(0x30d158), 255);",
            f"    {symbol}_draw_label(layer, 52, 245, 92, 14, \"POWER\", &lv_font_montserrat_8, lv_color_hex(0xffffff), 96);",
            f"    {symbol}_draw_label(layer, 240, 228, 80, 22, \"78%\", &lv_font_montserrat_18, lv_color_hex(0xffffff), 220);",
            f"    {symbol}_draw_label(layer, 240, 245, 80, 14, \"BATTERY\", &lv_font_montserrat_8, lv_color_hex(0xffffff), 96);",
            f"    for(int i = 0; i < 10; i++) {symbol}_draw_line(layer, 130 + i * 14, 258, 142 + i * 14, 258, 6, i < ({symbol}_battery / 10) ? lv_color_hex(0x30d158) : lv_color_hex(0x252a31), 220);",
            f"    {symbol}_draw_label(layer, 426, 228, 72, 22, \"287 KM\", &lv_font_montserrat_18, lv_color_hex(0xffffff), 220);",
            f"    {symbol}_draw_label(layer, 426, 245, 96, 14, \"RANGE\", &lv_font_montserrat_8, lv_color_hex(0xffffff), 96);",
            "}", "",
            f"static void {symbol}_clock_tick(lv_timer_t * timer)", "{", "    LV_UNUSED(timer);", f"    {symbol}_sync_local_time(); uagent_gauge_scene_update();", "}", "",
            f"static void {symbol}_scene_tick(lv_timer_t * timer)", "{", "    LV_UNUSED(timer);",
            f"    {symbol}_speed += 1; if({symbol}_speed > 200) {symbol}_speed = 0; {symbol}_rpm += 50; if({symbol}_rpm > 8000) {symbol}_rpm = 800; {symbol}_power = ({symbol}_speed * 85) / 100; {symbol}_battery = 78; uagent_gauge_scene_update();", "}", "",
            f"void uagent_gauge_scene_set_values(int32_t speed, int32_t rpm, int32_t power, int32_t battery)", "{",
            f"    {symbol}_speed = speed < 0 ? 0 : speed > 200 ? 200 : speed; {symbol}_rpm = rpm < 800 ? 800 : rpm > 8000 ? 8000 : rpm; {symbol}_power = power; {symbol}_battery = battery < 0 ? 0 : battery > 100 ? 100 : battery; uagent_gauge_scene_update();", "}", "",
            "void uagent_status_set_time(int hour, int minute)", "{",
            f"    {symbol}_time_hour = hour < 0 ? 0 : hour > 23 ? 23 : hour; {symbol}_time_minute = minute < 0 ? 0 : minute > 59 ? 59 : minute; {symbol}_time_manual = 1; uagent_gauge_scene_update();", "}", "",
            "void uagent_status_set_temperature(int value)", "{", f"    {symbol}_temperature = value; uagent_gauge_scene_update();", "}", "",
            "void uagent_status_set_signal(int level)", "{", f"    {symbol}_signal_level = level < 0 ? 0 : level > {symbol}_signal_max ? {symbol}_signal_max : level; uagent_gauge_scene_update();", "}", "",
            "void uagent_status_set_wifi(bool connected)", "{", f"    {symbol}_wifi_connected = connected ? 1 : 0; uagent_gauge_scene_update();", "}", "",
            "void uagent_gauge_scene_update(void)", "{", f"    if({symbol}_host) lv_obj_invalidate({symbol}_host);", "}", "",
            f"void uagent_build_{symbol}(lv_obj_t * parent)", "{", f"    if({symbol}_host) return;",
            f"    {symbol}_host = lv_obj_create(parent); lv_obj_set_size({symbol}_host, {width}, {height}); lv_obj_set_pos({symbol}_host, 0, 0);",
            f"    lv_obj_set_style_bg_opa({symbol}_host, LV_OPA_0, LV_PART_MAIN); lv_obj_set_style_border_width({symbol}_host, 0, LV_PART_MAIN); lv_obj_set_style_radius({symbol}_host, 0, LV_PART_MAIN);",
            f"    lv_obj_add_event_cb({symbol}_host, {symbol}_draw_main, LV_EVENT_DRAW_MAIN, NULL);",
            f"    if(!{symbol}_scene_timer) {symbol}_scene_timer = lv_timer_create({symbol}_scene_tick, 80, NULL);",
            f"    {symbol}_sync_local_time(); if(!{symbol}_clock_timer) {symbol}_clock_timer = lv_timer_create({symbol}_clock_tick, {clock_interval}, NULL);", "}",
        ]
        return "\n".join(lines) + "\n"

    def _chart(self, symbol: str, adapted: AdaptedWidget, component: Component, model: ReactProjectModel) -> str:
        chart_type = adapted.properties["chart_type"]
        if chart_type == "pie":
            return self._pie(symbol, adapted, component, model)
        props = adapted.properties
        values = _chart_values(props.get("series", props.get("data")))
        point_count = max(len(points) for points in values)
        min_value = min((point for series in values for point in series), default=0)
        max_value = max((point for series in values for point in series), default=100)
        if min_value == max_value:
            max_value += 1
        chart_kind = "LV_CHART_TYPE_BAR" if chart_type == "bar" else "LV_CHART_TYPE_LINE"
        lines = [f"void uagent_build_{symbol}(lv_obj_t * parent)", "{", f"    lv_obj_t * {symbol} = lv_chart_create(parent);"]
        lines += _style_lines(symbol, props)
        lines += [
            f"    lv_chart_set_type({symbol}, {chart_kind});",
            f"    lv_chart_set_point_count({symbol}, {point_count});",
            f"    lv_chart_set_range({symbol}, LV_CHART_AXIS_PRIMARY_Y, {min_value}, {max_value});",
            f"    lv_chart_set_div_line_count({symbol}, 5, 5);",
        ]
        colors = ["35b5ff", "7c5cff", "31d0aa", "ffb74d"]
        for index, series in enumerate(values):
            series_name = f"{symbol}_series_{index}"
            lines.append(f"    lv_chart_series_t * {series_name} = lv_chart_add_series({symbol}, lv_color_hex(0x{colors[index % len(colors)]}), LV_CHART_AXIS_PRIMARY_Y);")
            for value in series:
                lines.append(f"    lv_chart_set_next_value({symbol}, {series_name}, {value});")
        if chart_type == "area":
            lines.append("    /* Area fill intentionally omitted: see audit warning. */")
        lines += _event_comment(component, model)
        lines.append("}")
        return "\n".join(lines) + "\n"

    def _pie(self, symbol: str, adapted: AdaptedWidget, component: Component, model: ReactProjectModel) -> str:
        props = adapted.properties
        values = _chart_values(props.get("series", props.get("data")))[0]
        positive = [max(0, item) for item in values]
        total = sum(positive) or 1
        lines = [f"void uagent_build_{symbol}(lv_obj_t * parent)", "{"]
        start = 0
        colors = ["35b5ff", "7c5cff", "31d0aa", "ffb74d"]
        for index, value in enumerate(positive):
            end = start + round(value * 360 / total)
            arc = f"{symbol}_slice_{index}"
            lines += [
                f"    lv_obj_t * {arc} = lv_arc_create(parent);",
                f"    lv_obj_set_size({arc}, {_number(props, 'width', 220)}, {_number(props, 'height', 220)});",
                f"    lv_arc_set_bg_angles({arc}, {start}, {end});",
                f"    lv_arc_set_angles({arc}, {start}, {end});",
                f"    lv_obj_set_style_arc_color({arc}, lv_color_hex(0x{colors[index % len(colors)]}), LV_PART_INDICATOR);",
                f"    lv_obj_set_style_arc_width({arc}, {_number(props, 'arc_width', 28)}, LV_PART_INDICATOR);",
                f"    lv_obj_remove_style({arc}, NULL, LV_PART_KNOB);",
                f"    lv_obj_center({arc});",
            ]
            start = end
        lines += _event_comment(component, model)
        lines.append("}")
        return "\n".join(lines) + "\n"

    def _table(self, symbol: str, adapted: AdaptedWidget, component: Component, model: ReactProjectModel) -> str:
        props = adapted.properties
        table = props["table"]
        headers = [str(value) for value in table["headers"]]
        columns = max(1, _number(table, "column_count", len(headers)))
        rows = max(1, _number(table, "row_count", 1))
        template = [str(value) for value in table.get("row_template", [])]
        width = _number(props, "width", max(360, columns * 116))
        column_width = max(72, width // columns)
        lines = [f"void uagent_build_{symbol}(lv_obj_t * parent)", "{", f"    lv_obj_t * {symbol} = lv_table_create(parent);"]
        lines += _style_lines(symbol, props)
        lines += [
            f"    lv_table_set_col_cnt({symbol}, {columns});",
            f"    lv_table_set_row_cnt({symbol}, {rows + 1}); /* header + source rows */",
            f"    lv_obj_set_scroll_dir({symbol}, LV_DIR_HOR | LV_DIR_VER);",
            f"    lv_obj_set_scrollbar_mode({symbol}, LV_SCROLLBAR_MODE_AUTO);",
            f"    lv_obj_set_style_bg_color({symbol}, {_color(props.get('cell_color'), '18233a')}, LV_PART_ITEMS);",
            f"    lv_obj_set_style_bg_color({symbol}, {_color(props.get('header_color'), '263552')}, LV_PART_ITEMS); /* header row is kept editable */",
        ]
        for index in range(columns):
            lines.append(f"    lv_table_set_col_width({symbol}, {index}, {column_width});")
            label = headers[index] if index < len(headers) else f"Column {index + 1}"
            lines.append(f"    lv_table_set_cell_value({symbol}, 0, {index}, {_c_string(label)});")
        for row in range(1, rows + 1):
            for column in range(columns):
                value = template[column] if column < len(template) else "…"
                lines.append(f"    lv_table_set_cell_value({symbol}, {row}, {column}, {_c_string(value)});")
        lines += _event_comment(component, model)
        lines.append("}")
        return "\n".join(lines) + "\n"

    def _dropdown(self, symbol: str, adapted: AdaptedWidget, component: Component, model: ReactProjectModel) -> str:
        props = adapted.properties
        dropdown = props["dropdown"]
        options = "\n".join(str(value).replace("\n", " ") for value in dropdown["options"])
        selected = max(0, _number(dropdown, "selected_index", 0))
        lines = [f"void uagent_build_{symbol}(lv_obj_t * parent)", "{", f"    lv_obj_t * {symbol} = lv_dropdown_create(parent);"]
        lines += _style_lines(symbol, props)
        lines += [
            f"    lv_dropdown_set_options({symbol}, {_c_string(options)});",
            f"    lv_dropdown_set_selected({symbol}, {selected});",
            f"    lv_obj_set_style_border_width({symbol}, 1, LV_PART_MAIN);",
            f"    lv_obj_set_style_border_color({symbol}, {_color(props.get('border_color'), '3b4d6d')}, LV_PART_MAIN);",
            f"    lv_obj_set_style_text_color({symbol}, lv_color_hex(0xf5f7ff), LV_PART_MAIN);",
        ]
        lines += _event_comment(component, model)
        lines.append("}")
        return "\n".join(lines) + "\n"

    def _scroll_container(self, symbol: str, adapted: AdaptedWidget, component: Component, model: ReactProjectModel) -> str:
        props = adapted.properties
        scroll = props["scroll"]
        vertical = scroll.get("axis") != "x"
        direction = "LV_DIR_VER" if vertical else "LV_DIR_HOR"
        flow = "LV_FLEX_FLOW_COLUMN" if vertical else "LV_FLEX_FLOW_ROW"
        lines = [f"void uagent_build_{symbol}(lv_obj_t * parent)", "{", f"    lv_obj_t * {symbol} = lv_obj_create(parent);"]
        lines += _style_lines(symbol, props)
        lines += [
            f"    lv_obj_set_flex_flow({symbol}, {flow});",
            f"    lv_obj_set_scroll_dir({symbol}, {direction});",
            f"    lv_obj_set_scrollbar_mode({symbol}, LV_SCROLLBAR_MODE_AUTO);",
            f"    lv_obj_set_style_pad_right({symbol}, {_number(props, 'scrollbar_gap', 8)}, LV_PART_MAIN);",
        ]
        lines += _event_comment(component, model)
        lines.append("}")
        return "\n".join(lines) + "\n"

    def _control(self, symbol: str, adapted: AdaptedWidget, component: Component, model: ReactProjectModel) -> str:
        """Render one scanner-proven JSX control to its native LVGL peer.

        This is intentionally a collection of small, direct mappings rather
        than a visual heuristic.  Every constructor in this function has a
        corresponding ``control.kind`` record in the audit manifest.
        """
        props = dict(adapted.properties)
        control = dict(props.get("control", {}))
        kind = str(control.get("kind", ""))
        defaults = {
            "button": (160, 44), "label": (220, 36), "image": (160, 120),
            "switch": (72, 40), "checkbox": (220, 38), "radio": (220, 38),
            "radio_group": (280, 48), "slider": (240, 24), "input": (260, 44),
            "list": (320, 200), "tabs": (360, 240), "select": (240, 44),
        }
        width, height = defaults.get(kind, (240, 60))
        props.setdefault("width", width)
        props.setdefault("height", height)
        props.setdefault("background", "18233a")
        disabled = str(control.get("disabled", "")).lower() == "true" or control.get("disabled") is True
        checked = str(control.get("checked", "")).lower() == "true" or control.get("checked") is True
        lines = [f"void uagent_build_{symbol}(lv_obj_t * parent)", "{"]

        if kind == "button":
            lines += [f"    lv_obj_t * {symbol} = lv_btn_create(parent);"] + _style_lines(symbol, props)
            lines += [
                f"    lv_obj_t * {symbol}_label = lv_label_create({symbol});",
                f"    lv_label_set_text({symbol}_label, {_icon_text(control.get('icon'), control.get('text', 'Button')) if control.get('icon') else _c_string(control.get('text', 'Button'))});",
                f"    lv_obj_center({symbol}_label);",
            ]
        elif kind == "label":
            lines += [f"    lv_obj_t * {symbol} = lv_label_create(parent);"]
            lines += [
                f"    lv_label_set_text({symbol}, {_c_string(control.get('text', ''))});",
                f"    lv_obj_set_width({symbol}, {_number(props, 'width', width)});",
                f"    lv_label_set_long_mode({symbol}, LV_LABEL_LONG_WRAP);",
            ]
        elif kind == "image":
            resource_id = str(control["resource_id"])
            lines += [f"    lv_obj_t * {symbol} = lv_img_create(parent);"]
            lines += [
                f"    lv_img_set_src({symbol}, LVGL_IMAGE_PATH({resource_filename(resource_id, model)})); /* {resource_id} */",
                f"    lv_obj_set_size({symbol}, {_number(props, 'width', width)}, {_number(props, 'height', height)});",
            ]
        elif kind == "switch":
            lines += [f"    lv_obj_t * {symbol} = lv_switch_create(parent);"] + _style_lines(symbol, props)
            if checked:
                lines.append(f"    lv_obj_add_state({symbol}, LV_STATE_CHECKED);")
        elif kind in {"checkbox", "radio"}:
            lines += [f"    lv_obj_t * {symbol} = lv_checkbox_create(parent);"] + _style_lines(symbol, props)
            text = control.get("text") or control.get("value") or ("Radio" if kind == "radio" else "Checkbox")
            lines.append(f"    lv_checkbox_set_text({symbol}, {_c_string(text)});")
            if checked:
                lines.append(f"    lv_obj_add_state({symbol}, LV_STATE_CHECKED);")
        elif kind == "radio_group":
            options = [str(value) for value in control.get("options", [])]
            map_name = f"{symbol}_map"
            map_values = ", ".join(_c_string(value) for value in options + [""])
            lines += [
                f"    static const char * {map_name}[] = {{{map_values}}};",
                f"    lv_obj_t * {symbol} = lv_btnmatrix_create(parent);",
            ] + _style_lines(symbol, props) + [
                f"    lv_btnmatrix_set_map({symbol}, {map_name});",
                f"    lv_btnmatrix_set_one_checked({symbol}, true);",
            ]
            selected = str(control.get("value", ""))
            if selected in options:
                lines.append(f"    lv_btnmatrix_set_btn_ctrl({symbol}, {options.index(selected)}, LV_BTNMATRIX_CTRL_CHECKED);")
        elif kind == "slider":
            minimum = _number(control, "min", 0)
            maximum = _number(control, "max", 100)
            value = _number(control, "value", minimum)
            if maximum <= minimum:
                maximum = minimum + 100
            value = max(minimum, min(maximum, value))
            lines += [f"    lv_obj_t * {symbol} = lv_slider_create(parent);"] + _style_lines(symbol, props) + [
                f"    lv_slider_set_range({symbol}, {minimum}, {maximum});",
                f"    lv_slider_set_value({symbol}, {value}, LV_ANIM_OFF);",
            ]
        elif kind == "input":
            lines += [f"    lv_obj_t * {symbol} = lv_textarea_create(parent);"] + _style_lines(symbol, props)
            placeholder = control.get("placeholder")
            value = control.get("value")
            if placeholder:
                lines.append(f"    lv_textarea_set_placeholder_text({symbol}, {_c_string(placeholder)});")
            if isinstance(value, (str, int, float)) and str(value) and not re.fullmatch(r"[A-Za-z_$][\\w$]*(?:\\.[\\w$]+)*", str(value)):
                lines.append(f"    lv_textarea_set_text({symbol}, {_c_string(value)});")
        elif kind == "list":
            lines += [f"    lv_obj_t * {symbol} = lv_list_create(parent);"] + _style_lines(symbol, props)
            for index, value in enumerate(control.get("items", [])):
                lines.append(f"    lv_list_add_btn({symbol}, NULL, {_c_string(value)}); /* item {index + 1} */")
        elif kind == "tabs":
            lines += [f"    lv_obj_t * {symbol} = lv_tabview_create(parent, LV_DIR_TOP, 42);"] + _style_lines(symbol, props)
            for index, value in enumerate(control.get("tabs", [])):
                tab = f"{symbol}_tab_{index}"
                lines += [
                    f"    lv_obj_t * {tab} = lv_tabview_add_tab({symbol}, {_c_string(value)});",
                    f"    lv_obj_set_style_pad_all({tab}, 8, LV_PART_MAIN);",
                ]
        elif kind == "select":
            options = "\\n".join(str(value).replace("\\n", " ") for value in control.get("options", []))
            selected = str(control.get("value", ""))
            source_options = [str(value) for value in control.get("options", [])]
            selected_index = source_options.index(selected) if selected in source_options else 0
            lines += [f"    lv_obj_t * {symbol} = lv_dropdown_create(parent);"] + _style_lines(symbol, props) + [
                f"    lv_dropdown_set_options({symbol}, {_c_string(options)});",
                f"    lv_dropdown_set_selected({symbol}, {selected_index});",
            ]
        else:
            # Defensive guard: adapter and renderer must never silently create
            # a generic object for an unsupported control kind.
            lines.append("    /* Unsupported scanner control; no LVGL widget emitted. */")

        if disabled and kind not in {"label", "image"}:
            lines.append(f"    lv_obj_add_state({symbol}, LV_STATE_DISABLED);")
        for event_name in control.get("events", []):
            lines.append(f"    /* JSX {event_name} is present on this exact control; bind application callback in AiBuilder custom code. */")
        lines.append("}")
        return "\n".join(lines) + "\n"


def bundle(units: Iterable[GeneratedUnit], screen_tree: dict[str, Any] | None = None) -> GenerationBundle:
    units = list(units)
    scene_units = [unit for unit in units if unit.adapter == "GaugeSceneAdapter"
                   or unit.decisions.get("render_operation") == "custom-draw-scene"]
    # GaugeAdapter units carrying source geometry/paint layers are custom
    # draw overlays.  Snapshot owns ordinary widgets, but these overlays must
    # still mount even when the snapshot already has children.
    # Every GaugeAdapter unit owns one logical gauge group, including
    # instances whose geometry comes from a named browser SVG group.
    gauge_custom_units = [unit for unit in units if unit.adapter == "GaugeAdapter"]
    custom_units = scene_units + [unit for unit in gauge_custom_units if unit not in scene_units]
    declarations = "\n".join(f"void uagent_build_{unit.symbol}(lv_obj_t * parent);" for unit in units)
    scene_api = ("\nvoid uagent_gauge_scene_set_values(int32_t speed, int32_t rpm, int32_t power, int32_t battery);"
                 "\nvoid uagent_gauge_scene_update(void);"
                 "\nvoid uagent_status_set_time(int hour, int minute);"
                 "\nvoid uagent_status_set_temperature(int value);"
                 "\nvoid uagent_status_set_signal(int level);"
                 "\nvoid uagent_status_set_wifi(bool connected);") if scene_units else ""
    header = "#ifndef UAGENT_CUSTOM_H\n#define UAGENT_CUSTOM_H\n\n#include <stdbool.h>\n#include \"lvgl.h\"\n\n" + declarations + scene_api + "\n\n#endif\n"
    source = "#include \"custom.h\"\n"
    # Embedded LVGL builds commonly enable only a small subset of the
    # built-in Montserrat fonts (often just 16).  Keep generated custom-draw
    # scenes portable: an unavailable size is mapped to the guaranteed 16px
    # face at preprocessing time, while Windows/SDL builds keep their exact
    # requested size whenever it is enabled in lv_conf.h.
    source += "\n/* UAgent font compatibility: fall back to Montserrat 16 on embedded builds. */\n"
    for _font_size in (8, 10, 12, 14, 18, 20, 32, 38, 48):
        source += (
            f"#if !defined(LV_FONT_MONTSERRAT_{_font_size}) || !LV_FONT_MONTSERRAT_{_font_size}\n"
            f"#define lv_font_montserrat_{_font_size} lv_font_montserrat_16\n"
            "#endif\n"
        )
    source += "\n"
    screens = list((screen_tree or {}).get("screens", []))
    if len(screens) > 1:
        # UIBuilder generates the individual screen structs from the snapshot,
        # but its default ui_init.c only loads the first screen. Include the
        # generated object registry and materialize every remaining screen so
        # generic multi-tab projects are actually navigable in the simulator.
        source += "#include \"../ui_objects.h\"\n#include <string.h>\n\n"
    source += "\n" + "\n".join(unit.c_code for unit in units)
    source += "\nvoid uagent_build_screen(lv_obj_t * parent)\n{\n"
    source += "    /* Composition entry generated from the audited screen tree. */\n"
    source += "    static lv_obj_t * uagent_materialized_parent = NULL;\n"
    source += "    if (uagent_materialized_parent == parent) return;\n"
    source += "    uagent_materialized_parent = parent;\n"
    source += "    /* Composite scenes are always mounted; the snapshot contains only transparent anchors. */\n"
    for unit in scene_units:
        source += f"    uagent_build_{unit.symbol}(parent);\n"
    # Snapshot widgets and custom draw units have distinct ownership. A
    # populated snapshot parent must not suppress source-backed arcs, while
    # ordinary-only bundles retain the legacy snapshot child guard.
    if not custom_units:
        source += "    if (lv_obj_get_child_count(parent) > 0) return; /* ordinary widgets already materialized by .snapshot */\n"
    else:
        for unit in custom_units:
            source += f"    uagent_build_{unit.symbol}(parent);\n"
    source += "}\n"
    if len(screens) > 1:
        source += "\nvoid uagent_bind_all_tabs(void);\n"
    source += "\nvoid custom_init(void)\n{\n"
    source += "    /* UIBuilder calls custom_init() after loading the default screen. */\n"
    source += "    uagent_build_screen(lv_scr_act());\n"
    if len(screens) > 1:
        source += "    /* Materialize all non-default screens and bind semantic Tab labels. */\n"
        for screen in screens[1:]:
            sid = re.sub(r"[^A-Za-z0-9_]", "_", str(screen.get("id", "")))
            if sid:
                source += f"    {sid}_create(&ui_manager);\n"
        source += "    uagent_bind_all_tabs();\n"
    source += "}\n"
    if len(screens) > 1:
        source += r'''

static int uagent_label_equals(lv_obj_t * label, const char * expected)
{
    if(!lv_obj_check_type(label, &lv_label_class)) return 0;
    const char * actual = lv_label_get_text(label);
    if(!actual || !expected) return 0;
    while(*actual == ' ' || *actual == '\n' || *actual == '\r' || *actual == '\t') actual++;
    while(*expected == ' ' || *expected == '\n' || *expected == '\r' || *expected == '\t') expected++;
    size_t a_len = strlen(actual), e_len = strlen(expected);
    while(a_len && (actual[a_len - 1] == ' ' || actual[a_len - 1] == '\n' || actual[a_len - 1] == '\r' || actual[a_len - 1] == '\t')) a_len--;
    while(e_len && (expected[e_len - 1] == ' ' || expected[e_len - 1] == '\n' || expected[e_len - 1] == '\r' || expected[e_len - 1] == '\t')) e_len--;
    if(a_len != e_len) return 0;
    for(size_t i = 0; i < a_len; i++) {
        char a = actual[i], e = expected[i];
        if(a >= 'a' && a <= 'z') a = (char)(a - 'a' + 'A');
        if(e >= 'a' && e <= 'z') e = (char)(e - 'a' + 'A');
        if(a != e) return 0;
    }
    return 1;
}

static void uagent_tab_event(lv_event_t * event)
{
    if(lv_event_get_code(event) == LV_EVENT_CLICKED) {
        lv_obj_t * target = (lv_obj_t *)lv_event_get_user_data(event);
        if(target) lv_scr_load(target);
    }
}

static void uagent_bind_tab_tree(lv_obj_t * root, const char * label, lv_obj_t * target)
{
    if(!root) return;
    uint32_t count = lv_obj_get_child_count(root);
    for(uint32_t i = 0; i < count; i++) {
        lv_obj_t * child = lv_obj_get_child(root, i);
        uint32_t nested = lv_obj_get_child_count(child);
        for(uint32_t j = 0; j < nested; j++) {
            lv_obj_t * text = lv_obj_get_child(child, j);
            if(uagent_label_equals(text, label)) {
                lv_obj_add_event_cb(child, uagent_tab_event, LV_EVENT_CLICKED, target);
                break;
            }
        }
        uagent_bind_tab_tree(child, label, target);
    }
}

static void uagent_bind_image_nav_tree(lv_obj_t * root, uint32_t wanted, lv_obj_t * target, uint32_t * seen)
{
    if(!root || !seen) return;
    lv_coord_t root_h = lv_obj_get_height(root);
    lv_coord_t root_w = lv_obj_get_width(root);
    uint32_t count = lv_obj_get_child_count(root);
    for(uint32_t i = 0; i < count; i++) {
        lv_obj_t * child = lv_obj_get_child(root, i);
        if(lv_obj_check_type(child, &lv_img_class)
           && root_h > 0 && lv_obj_get_y(child) >= (root_h * 3) / 4
           && lv_obj_get_width(child) < root_w / 2) {
            if(*seen == wanted) {
                lv_obj_add_flag(child, LV_OBJ_FLAG_CLICKABLE);
                lv_obj_add_event_cb(child, uagent_tab_event, LV_EVENT_CLICKED, target);
                return;
            }
            (*seen)++;
        }
        uagent_bind_image_nav_tree(child, wanted, target, seen);
    }
}

void uagent_bind_all_tabs(void)
{
'''
        for source_screen in screens:
            source_id = re.sub(r"[^A-Za-z0-9_]", "_", str(source_screen.get("id", "")))
            if not source_id:
                continue
            for target_screen in screens:
                target_id = re.sub(r"[^A-Za-z0-9_]", "_", str(target_screen.get("id", "")))
                target_label = str(target_screen.get("name") or target_screen.get("route") or "")
                if target_id and target_label:
                    source += f'    uagent_bind_tab_tree({source_id}_get(&ui_manager)->obj, "{_c_string(target_label)[1:-1]}", {target_id}_get(&ui_manager)->obj);\n'
            for target_index, target_screen in enumerate(screens):
                target_id = re.sub(r"[^A-Za-z0-9_]", "_", str(target_screen.get("id", "")))
                if target_id:
                    source += f'    {{ uint32_t seen = 0; uagent_bind_image_nav_tree({source_id}_get(&ui_manager)->obj, {target_index}, {target_id}_get(&ui_manager)->obj, &seen); }}\n'
        source += "}\n"
    header = header.replace(
        "\n\n#endif",
        "\nvoid uagent_build_screen(lv_obj_t * parent);\nvoid custom_init(void);\n\n#endif",
    )
    warnings = [warning for unit in units for warning in unit.warnings]
    return GenerationBundle(header, source, units, warnings, screen_tree or {})
