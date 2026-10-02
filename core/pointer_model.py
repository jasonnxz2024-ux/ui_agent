"""Target-independent lowering of bounded instance-local pointer handlers.

Only source expressions are lowered. Browser data supplies instance identities,
resolved primitive props and layout references, never replacement callbacks.
"""
from __future__ import annotations

import copy
import math
import operator
import re

from core.reactive_contract import literal


def value_ast(value):
    if isinstance(value,dict):return ['object',[[['literal',k],value_ast(v)] for k,v in value.items()]]
    if isinstance(value,list):return ['array',[value_ast(v) for v in value]]
    return ['literal',value]


def reduce_expression(value, env, depth=0):
    if depth>80:raise ValueError('Recursive pointer expression')
    if not isinstance(value,list) or not value:return value
    kind=value[0]
    if kind=='id':
        replacement=env.get(value[1])
        return reduce_expression(replacement,env,depth+1) if replacement is not None and replacement!=value else value
    if kind=='literal' or kind in {'pointer','layout'}:return value
    if kind=='object':return ['object',[[reduce_expression(k,env,depth+1),reduce_expression(v,env,depth+1)] for k,v in value[1]]]
    if kind=='array':return ['array',[reduce_expression(v,env,depth+1) for v in value[1]]]
    if kind=='member':
        base=reduce_expression(value[1],env,depth+1);key=reduce_expression(value[2],env,depth+1)
        if base[0]=='object':
            found=next((v for k,v in base[1] if literal(k)==literal(key)),None)
            if found is None:raise ValueError('Unknown pointer object field '+str(literal(key)))
            return found
        if base[0]=='array' and isinstance(literal(key),int):return base[1][literal(key)]
        return ['member',base,key]
    if kind=='call':
        callee=reduce_expression(value[1],env,depth+1);args=[reduce_expression(v,env,depth+1) for v in value[2]]
        if callee[0]=='member' and callee[1][0]=='ref' and literal(callee[2])=='getBoundingClientRect':
            return ['object',[[['literal',k],['layout',callee[1][1],k]] for k in ('left','top','width','height')]]
        if callee[0]=='member' and callee[1]==['id','Math'] and all(a[0]=='literal' for a in args):
            functions={'min':min,'max':max,'sqrt':math.sqrt,'pow':pow,'abs':abs,'sin':math.sin,'cos':math.cos}
            if literal(callee[2]) in functions:return ['literal',functions[literal(callee[2])](*(a[1] for a in args))]
        return ['call',callee,args]
    if kind=='ref':return value
    if kind=='binary':
        left=reduce_expression(value[2],env,depth+1);right=reduce_expression(value[3],env,depth+1)
        if value[1] in {'&&','||'} and left[0]=='literal':
            return right if bool(left[1])==(value[1]=='&&') else left
        operations={'+':operator.add,'-':operator.sub,'*':operator.mul,'/':operator.truediv,'<':operator.lt,'>':operator.gt,'<=':operator.le,'>=':operator.ge,'===':operator.eq,'!==':operator.ne}
        if left[0]==right[0]=='literal' and value[1] in operations:return ['literal',operations[value[1]](left[1],right[1])]
        return ['binary',value[1],left,right]
    if kind=='conditional':
        condition=reduce_expression(value[1],env,depth+1)
        if condition[0]=='literal':return reduce_expression(value[2] if condition[1] else value[3],env,depth+1)
        return ['conditional',condition,reduce_expression(value[2],env,depth+1),reduce_expression(value[3],env,depth+1)]
    if kind=='unary':
        child=reduce_expression(value[2],env,depth+1)
        if value[1]=='!' and child[0] in {'ref','literal'}:return ['literal',False if child[0]=='ref' else not child[1]]
        if value[1] in {'+','-'} and child[0]=='literal' and type(child[1]) in {int,float}:return ['literal',child[1] if value[1]=='+' else -child[1]]
        return ['unary',value[1],child]
    if kind=='template':return ['template',value[1],[[reduce_expression(v,env,depth+1),s] for v,s in value[2]]]
    return value


def lower_handler(function, env, setters, functions=None):
    """Preserve local assignment, branch merge and early-return semantics."""
    actions=[]
    def run(statements, local, guard):
        if not statements:return
        statement,*rest=statements;kind=statement[0]
        if kind=='variables':
            for name,value in statement[1]:local[name]=reduce_expression(value,local)
            return run(rest,local,guard)
        if kind=='block':return run(statement[1]+rest,local,guard)
        if kind=='if':
            condition=reduce_expression(statement[1],local)
            yes=[statement[2]];no=[statement[3]] if statement[3] else []
            if condition[0]=='literal':return run((yes if condition[1] else no)+rest,local,guard)
            run(yes+rest,dict(local),['binary','&&',guard,condition])
            run(no+rest,dict(local),['binary','&&',guard,['unary','!',condition]])
            return
        if kind=='return' and statement[1] is None:return
        if kind in {'expression','return'}:
            expression=statement[1]
            if expression[0]=='binary' and expression[1] in {'=','+=','-=','*='}:
                _,op,target,right=expression
                if target[0]!='id' or target[1] not in local:raise ValueError('Pointer mutation is not local')
                value=reduce_expression(right,local)
                local[target[1]]=value if op=='=' else ['binary',op[0],local[target[1]],value]
            elif expression[0]=='call' and expression[1][0]=='id' and expression[1][1] in setters:
                state=setters[expression[1][1]];assigned=reduce_expression(expression[2][0],local)
                if assigned[0]=='function':
                    update_env=dict(local)
                    update_env[assigned[1][0]['name']]=['id',state]
                    returned=next((s[1] for s in assigned[2] if s[0]=='return'),None)
                    assigned=reduce_expression(returned,update_env)
                if assigned[0]=='object':
                    for key,value in assigned[1]:actions.append({'state':state,'field':literal(key),'value':value,'guard':guard})
                else:actions.append({'state':state,'field':None,'value':assigned,'guard':guard})
            elif expression[0]=='call' and expression[1][0]=='id' and expression[1][1] in (functions or {}):
                child=functions[expression[1][1]];scope=dict(local)
                for parameter,arg in zip(child[1],expression[2]):
                    if parameter['name']:scope[parameter['name']]=reduce_expression(arg,local)
                run(child[2],scope,guard)
            else:raise ValueError('Unsupported pointer callback statement')
            if kind=='return':return
            return run(rest,local,guard)
        raise ValueError('Unsupported pointer callback control flow')
    run(function[2],dict(env),['literal',True])
    merged={}
    for action in actions:
        key=(action['state'],action['field']);previous=merged.get(key,{}).get('value',['id',key[0]] if key[1] is None else ['member',['id',key[0]],['literal',key[1]]])
        guard=action.pop('guard')
        action['value']=action['value'] if guard==['literal',True] else ['conditional',guard,action['value'],previous]
        merged[key]=action
    return list(merged.values())


def expand_pointer_instances(contract, scenes):
    def ast_nodes(value):
        if not isinstance(value,list):return
        if value and isinstance(value[0],str):yield value
        for child in value:yield from ast_nodes(child)

    subscriptions=contract.get('subscriptions',[])
    gestures=contract.get('gestures',[])
    if not subscriptions and not gestures:return contract
    out=copy.deepcopy(contract);out.update(states=[],controls=[],views=[],pointers=[],translations=[],keyed_lists=[])
    # Instance lowering must never erase events handled by a different binding pass.
    # Until those controls are rebound to instance identities, keep them explicit.
    for jsx in contract.get('jsx',[]):
        for event in jsx.get('attrs',{}):
            if re.fullmatch(r'on[A-Z].*',event) and event!='onDragEnd':
                out.setdefault('blockers',[]).append(f"{jsx['file']}:{jsx['start']}: {event} requires an instance-scoped control binding")
    if len(scenes)!=1:raise ValueError('Instance pointer lifecycle currently requires one mounted screen')
    scene=scenes[0];instances=scene.get('instances') or {}
    if not instances:raise ValueError('Instance capture is missing; recapture the instrumented source')
    nodes=scene['nodes'];handled=set()
    out['layout_offsets']={str(n['id']):{k:n['rect'][field]-math.floor(n['rect'][field]+.5) for k,field in [('left','x'),('top','y'),('width','width'),('height','height')]} for n in nodes}
    all_states={s['name']:[identity+':'+s['name'] for identity,i in instances.items() if i['owner']==s['owner']] for s in contract['states']}
    global_names={name:['id',keys[0]] for name,keys in all_states.items() if len(keys)==1}
    all_setters={s['setter']:global_names[s['name']][1] for s in contract['states'] if s.get('setter') and s['name'] in global_names}
    for identity,instance in instances.items():
        owner=instance['owner'];props=instance.get('props',{});env={k:value_ast(v) for k,v in props.items()}
        if len(env)==1 and isinstance(props.get('props'),dict):env['props']=value_ast(props['props'])
        for usage in contract['jsx']:
            if usage['tag']==owner:
                for key,value in usage['attrs'].items():
                    if value and value[0]=='id' and value[1] in global_names:env[key]=global_names[value[1]]
        local_nodes=[n for n in nodes if n['attrs'].get('data-uagent-instance')==identity]
        local_states=[s for s in contract['states'] if s['owner']==owner]
        setters={**all_setters,**{s['setter']:identity+':'+s['name'] for s in local_states if s.get('setter')}}
        for state in local_states:env[state['name']]=['id',identity+':'+state['name']]
        for name,value in contract.get('locals',{}).get(owner,{}).items():
            if value and value[0]=='call' and value[1]==['id','useRef']:
                matches=[n for n in local_nodes if n['attrs'].get('data-uagent-ref')==name]
                if len(matches)!=1:raise ValueError('Layout ref must identify exactly one mounted instance node')
                env[name]=['object',[[['literal','current'],['ref',matches[0]['id']]]]]
            elif value and value[0] in {'literal','object','conditional','binary'}:
                env[name]=reduce_expression(value,env)
        initial={identity+':'+s['name']:literal(reduce_expression(s['initial'],env)) for s in local_states}
        for effect in contract.get('effects',[]):
            if effect['owner']!=owner:continue
            callback=effect['callback']
            adds=[n for n in ast_nodes(callback) if n[0]=='call' and n[1][0]=='member' and literal(n[1][2])=='addEventListener']
            if adds:
                cleanups=[r[1] for r in callback[2] if r[0]=='return' and r[1] and r[1][0]=='function']
                removes=[n for cleanup in cleanups for n in ast_nodes(cleanup) if n[0]=='call' and n[1][0]=='member' and literal(n[1][2])=='removeEventListener']
                if any(not any(add[1][1]==remove[1][1] and add[2]==remove[2] for remove in removes) for add in adds):raise ValueError('Pointer subscription lacks matching cleanup')
                continue
            initial_env={**env,**{keys[0]:next((s['initial'] for s in contract['states'] if s['name']==name),None) for name,keys in all_states.items() if len(keys)==1}}
            for action in lower_handler(callback,initial_env,setters,contract['functions']):
                if action['field'] is None:initial[action['state']]=literal(reduce_expression(action['value'],initial_env))
                else:initial[action['state']][action['field']]=literal(reduce_expression(action['value'],initial_env))
        for state in local_states:
            key=identity+':'+state['name'];out['states'].append({**state,'name':key,'initial':value_ast(initial[key])})
        for subscription in subscriptions:
            if subscription['owner']!=owner:continue
            if subscription['target']!='window' or literal(subscription['event']) not in {'mousemove','pointermove'}:raise ValueError('Unsupported pointer event target/type')
            callback=subscription['callback'];function=contract['functions'].get(callback[1]) if callback[0]=='id' else callback
            if not function or function[0]!='function':raise ValueError('Unresolved pointer handler')
            pointer_env=dict(env);parameter=function[1][0]['name']
            pointer_env[parameter]=['object',[[['literal','clientX'],['pointer','x']],[['literal','clientY'],['pointer','y']]]]
            out['pointers'].append({'instance':identity,'actions':lower_handler(function,pointer_env,setters)})
            handled.add(f"{subscription['file']}:{subscription['start']}: event listener")
        for gesture in gestures:
            if gesture['owner']!=owner:continue
            if len(gesture['loops'])!=1:raise ValueError('Gesture list must have one bounded keyed loop')
            loop=gesture['loops'][0];items=reduce_expression(loop['items'],env)
            if items[0]!='id' or items[1] not in initial:raise ValueError('Gesture list must be instance-local state')
            order=initial[items[1]]
            if not isinstance(order,list) or not 0<len(order)<=64 or not all(type(v) is int for v in order) or len(set(order))!=len(order):raise ValueError('Keyed permutation must start with unique bounded numeric indices')
            roots=[n for n in local_nodes if n['attrs'].get('data-uagent-source')==gesture['source_id']]
            if len(roots)!=len(order) or len({n['attrs'].get('data-uagent-key') for n in roots})!=len(order):raise ValueError('Keyed item identities are incomplete or duplicated')
            function=gesture['callback'];gesture_env=dict(env)
            for field in function[1][1]['fields'] or []:
                gesture_env[field['name']]=['object',[[['literal',axis],['gesture',field['property'],axis]] for axis in ('x','y')]]
            actions=lower_handler(function,gesture_env,setters,contract['functions'])
            if any(a['state']!=items[1] for a in actions):raise ValueError('Gesture action changes state outside its keyed list')
            out['keyed_lists'].append({'state':items[1],'source':gesture['source_id'],'order':order,'nodes':[n['id'] for n in roots],'actions':actions})
            # Only this source handler has now been accounted for.
            marker=f"{gesture['file']}:"
            out['blockers']=[b for b in out['blockers'] if not (b.startswith(marker) and 'onDragEnd requires' in b)]
        for jsx in contract.get('jsx',[]):
            if jsx['owner']!=owner:continue
            style=jsx['attrs'].get('style')
            if not style or style[0]!='object':continue
            transform=next((v for k,v in style[1] if literal(k)=='transform'),None)
            if not transform:continue
            transform=reduce_expression(transform,env)
            if transform[0]!='template':raise ValueError('Unsupported pointer transform')
            template=transform[1];values=[]
            for expression,suffix in transform[2]:template+='@'+str(len(values))+'@'+suffix;values.append(expression)
            groups=re.findall(r'translate\(([^)]+)\)',template)
            if not groups or re.sub(r'translate\([^)]+\)','',template).strip():raise ValueError('Only translate transforms are supported for pointer instances')
            matches=[n for n in local_nodes if n['attrs'].get('data-uagent-source')==f"{jsx['file']}:{jsx['start']}"]
            if len(matches)!=1:raise ValueError('Dynamic transform source node was not uniquely captured')
            node=matches[0];axes=[['literal',0],['literal',0]]
            for group in groups:
                parts=group.split(',')
                if len(parts)!=2:raise ValueError('translate requires two explicit axes')
                for axis,part in enumerate(parts):
                    part=part.strip();found=re.fullmatch(r'@(\d+)@px',part)
                    if found:term=values[int(found[1])]
                    elif part.endswith('%'):term=['literal',float(part[:-1])*node['rect'][('width','height')[axis]]/100]
                    elif part.endswith('px'):term=['literal',float(part[:-2])]
                    else:raise ValueError('Unsupported translate unit')
                    axes[axis]=['binary','+',axes[axis],term]
            initial_env={k:value_ast(v) for k,v in initial.items()}
            origin=[literal(reduce_expression(axis,initial_env)) for axis in axes]
            if any(v is None for v in origin):raise ValueError('Transform initial value is not source-resolved')
            out['translations'].append({'node':node['id'],'axes':axes,'origin':origin})
    out['blockers']=[b for b in out.get('blockers',[]) if not any(b.startswith(prefix) for prefix in handled)]
    return out
