"""Extract a bounded React state contract with the project's TypeScript parser.

This module emits semantic data only. Unsupported JavaScript stays explicit;
it is never executed by the compiler and no target C appears here.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any


_EXTRACT = r"""
const fs=require('fs'),path=require('path');
const root=process.argv[1], ts=require(process.argv[3]||path.join(root,'node_modules/typescript/lib/typescript.js'));
const files=JSON.parse(process.argv[2]);
const output={schema:'uagent.reactive-contract/v1',states:[],functions:{},constants:{},jsx:[],timers:[],aliases:{},blockers:[]};
const K=ts.SyntaxKind;
function expr(n) {
 if(!n) return null;
 if(ts.isParenthesizedExpression(n)||ts.isAsExpression(n)||ts.isNonNullExpression(n)) return expr(n.expression);
 if(ts.isNumericLiteral(n)) return ['literal',Number(n.text)];
 if(ts.isStringLiteral(n)||ts.isNoSubstitutionTemplateLiteral(n)) return ['literal',n.text];
 if(n.kind===K.TrueKeyword||n.kind===K.FalseKeyword) return ['literal',n.kind===K.TrueKeyword];
 if(n.kind===K.NullKeyword) return ['literal',null];
 if(ts.isIdentifier(n)) return ['id',n.text];
 if(ts.isPropertyAccessExpression(n)) return ['member',expr(n.expression),['literal',n.name.text]];
 if(ts.isElementAccessExpression(n)) return ['member',expr(n.expression),expr(n.argumentExpression)];
 if(ts.isBinaryExpression(n)) return ['binary',n.operatorToken.getText(),expr(n.left),expr(n.right)];
 if(ts.isPrefixUnaryExpression(n)) return ['unary',ts.tokenToString(n.operator),expr(n.operand)];
 if(ts.isConditionalExpression(n)) return ['conditional',expr(n.condition),expr(n.whenTrue),expr(n.whenFalse)];
 if(ts.isCallExpression(n)) return ['call',expr(n.expression),n.arguments.map(expr)];
 if(ts.isNewExpression(n)) return ['new',expr(n.expression),(n.arguments||[]).map(expr)];
 if(ts.isObjectLiteralExpression(n)) return ['object',n.properties.map(p=>ts.isSpreadAssignment(p)?['spread',expr(p.expression)]:[ts.isComputedPropertyName(p.name)?expr(p.name.expression):['literal',p.name.text],expr(p.initializer||p.name)])];
 if(ts.isArrayLiteralExpression(n)) return ['array',n.elements.map(expr)];
 if(ts.isArrowFunction(n)||ts.isFunctionExpression(n)) return fn(n);
 if(ts.isTemplateExpression(n)) return ['template',n.head.text,n.templateSpans.map(s=>[expr(s.expression),s.literal.text])];
 return ['unsupported',n.getText()];
}
function parameters(n) {return n.parameters.map(p=>({name:ts.isIdentifier(p.name)?p.name.text:null,fields:ts.isObjectBindingPattern(p.name)?p.name.elements.map(e=>({name:e.name.text,property:e.propertyName?.text||e.name.text,default:expr(e.initializer)})):null}));}
function statement(n) {
 if(ts.isReturnStatement(n)) return ['return',expr(n.expression)];
 if(ts.isExpressionStatement(n)) return ['expression',expr(n.expression)];
 if(ts.isVariableStatement(n)) return ['variables',n.declarationList.declarations.flatMap(d=>ts.isIdentifier(d.name)?[[d.name.text,expr(d.initializer)]]:ts.isArrayBindingPattern(d.name)&&!(ts.isCallExpression(d.initializer)&&d.initializer.expression.getText()==='useState')?d.name.elements.flatMap((e,i)=>ts.isBindingElement(e)?[[e.name.text,['member',expr(d.initializer),['literal',i]]]]:[]):[])];
 if(ts.isIfStatement(n)) return ['if',expr(n.expression),statement(n.thenStatement),n.elseStatement?statement(n.elseStatement):null];
 if(ts.isBlock(n)) return ['block',n.statements.map(statement)];
 return ['unsupported',n.getText()];
}
function fn(n) {return ['function',parameters(n),ts.isBlock(n.body)?n.body.statements.map(statement):[['return',expr(n.body)]]];}
function walk(n,owner,file) {
 if(ts.isFunctionDeclaration(n)&&n.name) {owner=n.name.text; output.functions[owner]=fn(n);}
 if(ts.isVariableDeclaration(n)&&n.initializer) {
  const init=n.initializer;
  if(ts.isArrayBindingPattern(n.name)&&ts.isCallExpression(init)&&init.expression.getText()==='useState') {
   output.states.push({name:n.name.elements[0].name.text,setter:n.name.elements[1]?.name?.text||null,owner,initial:expr(init.arguments[0]),file,start:n.pos});
  }
  if(ts.isIdentifier(n.name)) {
   let f=init;
   if(ts.isCallExpression(f)&&f.expression.getText()==='useCallback') f=f.arguments[0];
   if(ts.isArrowFunction(f)||ts.isFunctionExpression(f)) output.functions[n.name.text]=fn(f);
   else if(owner===null) output.constants[n.name.text]=expr(init);
  }
 }
 if(ts.isCallExpression(n)&&n.expression.getText()==='setInterval') {
  let block=n.parent;while(block&&!ts.isBlock(block))block=block.parent;
  const callback=n.arguments[0],immediate=ts.isIdentifier(callback)&&!!block?.statements.some(s=>s.end<n.pos&&ts.isExpressionStatement(s)&&ts.isCallExpression(s.expression)&&s.expression.expression.getText()===callback.text);
  const persistent={};
  for(const s of block?.statements||[])if(s.end<n.pos&&ts.isVariableStatement(s)&&(s.declarationList.flags&ts.NodeFlags.Let))for(const d of s.declarationList.declarations)if(ts.isIdentifier(d.name)&&d.initializer){
   const name='effect_'+block.pos+'_'+d.name.text;persistent[d.name.text]=['id',name];
   if(!output.states.some(s=>s.name===name))output.states.push({name,setter:null,owner,initial:expr(d.initializer),file,start:d.pos,internal:true});
  }
  const replace=value=>Array.isArray(value)?(value[0]==='id'&&persistent[value[1]]?persistent[value[1]]:value.map(replace)):value;
  output.timers.push({owner,callback:replace(expr(callback)),period:expr(n.arguments[1]),immediate,file,start:n.pos});
 }
 if(ts.isJsxOpeningElement(n)||ts.isJsxSelfClosingElement(n)) {
  const attrs={}; for(const a of n.attributes.properties) if(ts.isJsxAttribute(a)) {
   attrs[a.name.text]=!a.initializer?['literal',true]:ts.isJsxExpression(a.initializer)?expr(a.initializer.expression):expr(a.initializer);
   if(attrs[a.name.text]?.[0]==='id') (output.aliases[a.name.text]??=[]).push(attrs[a.name.text][1]);
  }
  const children=ts.isJsxOpeningElement(n)&&ts.isJsxElement(n.parent)?n.parent.children.filter(c=>ts.isJsxText(c)||ts.isJsxExpression(c)).map(c=>ts.isJsxText(c)?['literal',c.text.trim()]:expr(c.expression)).filter(Boolean):[];
  const loops=[];for(let p=n.parent;p;p=p.parent) if(ts.isArrowFunction(p)&&ts.isCallExpression(p.parent)&&ts.isPropertyAccessExpression(p.parent.expression)&&p.parent.expression.name.text==='map') loops.push({items:expr(p.parent.expression.expression),parameters:parameters(p)});
  const guards=[];for(let p=n.parent;p;p=p.parent)if(ts.isBinaryExpression(p)&&p.operatorToken.kind===K.AmpersandAmpersandToken&&n.pos>=p.right.pos&&n.end<=p.right.end)guards.push(expr(p.left));
  output.jsx.push({tag:n.tagName.getText(),attrs,children,owner,file,start:n.pos,end:ts.isJsxOpeningElement(n)&&ts.isJsxElement(n.parent)?n.parent.end:n.end,loops,guards});
 }
 ts.forEachChild(n,c=>walk(c,owner,file));
}
for(const file of files) {const source=ts.createSourceFile(file,fs.readFileSync(path.join(root,file),'utf8'),ts.ScriptTarget.Latest,true,ts.ScriptKind.TSX);walk(source,null,file);}
for(const key of Object.keys(output.aliases)) output.aliases[key]=[...new Set(output.aliases[key])];
process.stdout.write(JSON.stringify(output));
"""


def extract_reactive_contract(source_dir: Path, source_files: list[str], *, parser_module: Path | None = None) -> dict[str, Any]:
    parser = parser_module or source_dir / 'node_modules/typescript/lib/typescript.js'
    node = shutil.which('node')
    if not node or not parser.is_file():
        return {'schema': 'uagent.reactive-contract/v1', 'blockers': ['Local TypeScript parser unavailable; reactive logic not compiled']}
    result = subprocess.run([node, '-e', _EXTRACT, str(source_dir), json.dumps(sorted(set(source_files))),str(parser)],
                            capture_output=True, text=True, encoding='utf-8', timeout=20)
    if result.returncode:
        return {'schema': 'uagent.reactive-contract/v1', 'blockers': [result.stderr[-2000:]]}
    contract=json.loads(result.stdout)
    names=[state['name'] for state in contract.get('states',[])]
    if len(set(names))!=len(names):contract['blockers'].append('Ambiguous duplicate state names across component/effect scopes')
    return contract


def instrument_browser_source(source_dir: Path, files: list[str], parser: Path) -> None:
    """Annotate an explicitly isolated browser copy, leaving compiler input intact."""
    script=r'''
const fs=require('fs'),path=require('path'),ts=require(process.argv[1]),root=process.argv[2];
for(const file of JSON.parse(process.argv[3])){
 const name=path.join(root,file),text=fs.readFileSync(name,'utf8'),tree=ts.createSourceFile(file,text,ts.ScriptTarget.Latest,true,ts.ScriptKind.TSX),edits=[];
 function visit(n){
  if((ts.isJsxOpeningElement(n)||ts.isJsxSelfClosingElement(n))&&/^[a-z]/.test(n.tagName.getText()))edits.push([n.attributes.end,` data-uagent-source=${JSON.stringify(file+':'+n.pos)}`]);
  if(ts.isVariableStatement(n))for(const d of n.declarationList.declarations)if(ts.isArrayBindingPattern(d.name)&&d.initializer&&ts.isCallExpression(d.initializer)&&d.initializer.expression.getText()==='useState'){
   const state=d.name.elements[0].name.text;edits.push([n.end,`;globalThis.__uagentState??={};globalThis.__uagentState[${JSON.stringify(state)}]=${state};`]);
  }
  ts.forEachChild(n,visit);
 }
 visit(tree);let result=text;for(const [at,insert]of edits.sort((a,b)=>b[0]-a[0]))result=result.slice(0,at)+insert+result.slice(at);fs.writeFileSync(name,result);
}
'''
    subprocess.run([shutil.which('node') or 'node','-e',script,str(parser),str(source_dir),json.dumps(files)],check=True,capture_output=True,text=True,timeout=20)


def substitute(expr: Any, env: dict[str, Any]) -> Any:
    if not isinstance(expr, list) or not expr:
        return expr
    if expr[0] == 'id' and expr[1] in env:
        return env[expr[1]]
    return [substitute(value, env) for value in expr]


def literal(expr: Any, constants: dict[str, Any] | None = None) -> Any:
    if not isinstance(expr, list):
        return None
    if expr[0] == 'literal':
        return expr[1]
    if expr[0] == 'id' and constants and expr[1] in constants:
        return literal(constants[expr[1]], constants)
    if expr[0] == 'object':
        return {literal(k, constants): literal(v, constants) for k, v in expr[1] if k != 'spread'}
    if expr[0] == 'array':
        return [literal(v, constants) for v in expr[1]]
    return None


def resolve_actions(callback: Any, contract: dict[str, Any], event_value: Any = None) -> list[dict[str, Any]]:
    """Resolve supported setters through source functions and JSX prop aliases."""
    functions = contract.get('functions', {})
    setters = {s['setter']: s for s in contract.get('states', []) if s.get('setter')}
    persistent = {s['name'] for s in contract.get('states', []) if s.get('internal')}
    actions: list[dict[str, Any]] = []
    event_value = event_value or ['event']

    def expression(value: Any, env: dict[str, Any]) -> Any:
        value = substitute(value, env)
        if isinstance(value, list) and value and value[0] == 'member':
            if value[1] == ['member', ['event-object'], ['literal', 'target']] and literal(value[2]) in {'value', 'checked'}:
                return event_value
        if isinstance(value, list) and value and value[0] == 'call' and value[1] == ['id', 'Number']:
            return expression(value[2][0], env)
        return value

    def invoke(value: Any, args: list[Any], env: dict[str, Any], depth: int = 0, guard: Any = None) -> None:
        if depth > 12:
            raise ValueError('Recursive event handler is unsupported')
        if value and value[0] == 'id':
            name = value[1]
            if name in setters:
                state = setters[name]
                assigned = expression(args[0], env)
                if assigned and assigned[0] == 'function':
                    state_env = dict(env)
                    for parameter in assigned[1]:
                        state_env[parameter['name']] = ['id', state['name']]
                    returned = next((s[1] for s in assigned[2] if s[0] == 'return'), None)
                    assigned = expression(returned, state_env)
                if assigned and assigned[0] == 'object':
                    for key, item in assigned[1]:
                        if key == 'spread':
                            continue
                        field = literal(expression(key, env))
                        if not isinstance(field, str):
                            raise ValueError('Unresolved computed state key')
                        actions.append({'state':state['name'],'field':field,'value':expression(item,env),**({'guard':guard} if guard else {})})
                else:
                    actions.append({'state':state['name'],'field':None,'value':assigned,**({'guard':guard} if guard else {})})
                return
            aliases = contract.get('aliases', {}).get(name, [])
            if name not in functions and len(aliases) == 1:
                name = aliases[0]
            value = functions.get(name)
        if not value or value[0] != 'function':
            raise ValueError('Unresolved event callback')
        local = dict(env)
        for i, p in enumerate(value[1]):
            if p['name']:
                local[p['name']] = args[i] if i < len(args) else event_value
        execute(value[2], local, depth, guard)

    def execute(statements: list[Any], local: dict[str,Any], depth: int, guard: Any) -> None:
        for statement in statements:
            if statement[0] in {'expression','return'} and statement[1] and statement[1][0] == 'call':
                call = statement[1]
                invoke(call[1], [expression(a,local) for a in call[2]], local, depth+1, guard)
            elif statement[0]=='expression' and statement[1] and statement[1][0]=='binary' and statement[1][1] in {'=','+=','-=','*='}:
                _,operator,target,right=statement[1]
                if target[0]!='id' or target[1] not in persistent:
                    raise ValueError('Unsupported mutation outside a captured effect variable')
                if guard:raise ValueError('Conditional mutation of an effect variable requires a control-flow merge')
                previous=local.get(target[1],['state',target[1]])
                assigned=expression(right,local) if operator=='=' else ['binary',operator[0],previous,expression(right,local)]
                actions.append({'state':target[1],'field':None,'value':assigned,**({'guard':guard} if guard else {})})
                local[target[1]]=assigned
            elif statement[0] == 'variables':
                for name, value in statement[1]:
                    local[name] = expression(value, local)
            elif statement[0]=='block':
                execute(statement[1],local,depth,guard)
            elif statement[0]=='if':
                condition=expression(statement[1],local)
                yes=['binary','&&',guard,condition] if guard else condition
                no=['binary','&&',guard,['unary','!',condition]] if guard else ['unary','!',condition]
                execute([statement[2]],dict(local),depth,yes)
                if statement[3]:execute([statement[3]],dict(local),depth,no)
            elif statement[0] != 'return':
                raise ValueError('Unsupported event statement')

    # JSX onChange may receive an event object (native input) or a boolean
    # (component callback). Preserve both cases without inventing a callback.
    if callback and callback[0] == 'function':
        names = [p['name'] for p in callback[1]]
        body_text = json.dumps(callback)
        argument = ['event-object'] if any(f'"{name}"' in body_text and '"target"' in body_text for name in names) else event_value
    else:
        argument = event_value
    invoke(callback, [argument], {})
    combined: dict[tuple[str,str|None],dict[str,Any]] = {}
    for action in actions:
        key=(action['state'],action['field'])
        guard=action.pop('guard',None)
        if guard:
            previous=combined[key]['value'] if key in combined else (['id',key[0]] if key[1] is None else ['member',['id',key[0]],['literal',key[1]]])
            action['value']=['conditional',guard,action['value'],previous]
        combined[key]=action
    return list(combined.values())


def source_control_bindings(contract: dict[str, Any]) -> list[dict[str, Any]]:
    bindings = []
    for jsx in contract.get('jsx', []):
        attrs = jsx['attrs']
        key = literal(attrs.get('aria-label')) or literal(attrs.get('id'))
        if not key:
            key = ''.join(str(literal(c) or '') for c in jsx['children'])
        callback = attrs.get('onChange') or attrs.get('onClick')
        if not key or not callback:
            continue
        item = {'identity':key,'source':{'file':jsx['file'],'offset':jsx['start']},'actions':[]}
        try:
            item['actions'] = resolve_actions(callback, contract)
        except ValueError as exc:
            item['blocker'] = str(exc)
        bindings.append(item)
    return bindings


def _static_text(expr: Any) -> str | None:
    if not expr:
        return None
    if expr[0] == 'literal':
        return str(expr[1])
    if expr[0] == 'template':
        value = expr[1]
        for field, tail in expr[2]:
            part = _static_text(field)
            if part is None:
                return None
            value += part + tail
        return value
    return None


def source_view_bindings(contract: dict[str, Any]) -> list[dict[str, Any]]:
    """Instantiate source expression bindings for labelled component instances."""
    jsx = contract.get('jsx', [])
    functions = contract.get('functions', {})
    views = []
    state_names={s['name'] for s in contract.get('states',[])}
    def dynamic(value):
        return isinstance(value,list) and (len(value)==2 and value[0]=='id' and value[1] in state_names or any(dynamic(v) for v in value))
    for node in jsx:
        definition=functions.get(node['owner'])
        if not definition or definition[1] or not node['tag'][0].islower():continue
        env={}
        for statement in definition[2]:
            if statement[0]=='variables':
                for name,value in statement[1]:env[name]=substitute(value,env)
        child={'tag':node['tag'],'attrs':{k:substitute(v,env) for k,v in node['attrs'].items()},'texts':[substitute(v,env) for v in node['children']],
               'guards':[substitute(v,env) for v in node.get('guards',[])]}
        if dynamic(child['texts']) or any(dynamic(v) for v in child['attrs'].values()):
            views.append({'source_id':f'{node["file"]}:{node["start"]}','identity':f'{node["file"]}:{node["start"]}','children':[child]})
    for usage in jsx:
        definition = functions.get(usage['tag'])
        if not definition:
            continue
        env = {}
        for parameter in definition[1]:
            for field in parameter.get('fields') or []:
                env[field['name']] = usage['attrs'].get(field['property'], field['default'])
        for statement in definition[2]:
            if statement[0] == 'variables':
                for name, value in statement[1]:
                    env[name] = substitute(value, env)
        roots = [n for n in jsx if n['owner'] == usage['tag'] and 'aria-label' in n['attrs'] and 'role' in n['attrs']]
        for root in roots:
            identity = _static_text(substitute(root['attrs']['aria-label'], env))
            if not identity:
                continue
            children = [n for n in jsx if n['owner'] == root['owner'] and root['start'] < n['start'] < root['end']]
            views.append({'identity':identity,'role':literal(root['attrs']['role']),
                          'children':[{'tag':n['tag'],'attrs':{k:substitute(v,env) for k,v in n['attrs'].items()},'texts':[substitute(v,env) for v in n['children']]} for n in children]})
    for root in jsx:
        if not root.get('loops') or 'role' not in root['attrs'] or 'aria-label' not in root['attrs']:
            continue
        loop = root['loops'][0]
        items = literal(loop['items'])
        if not isinstance(items, list):
            continue
        for item in items:
            env = {}
            for parameter in loop['parameters']:
                for field in parameter.get('fields') or []:
                    env[field['name']] = ['literal',item.get(field['property'])]
            identity = _static_text(substitute(root['attrs']['aria-label'],env))
            if identity:
                children = [n for n in jsx if n['owner'] == root['owner'] and root['start'] < n['start'] < root['end']]
                views.append({'identity':identity,'role':literal(root['attrs']['role']),
                              'children':[{'tag':n['tag'],'attrs':{k:substitute(v,env) for k,v in n['attrs'].items()},'texts':[substitute(v,env) for v in n['children']]} for n in children]})
    return views
