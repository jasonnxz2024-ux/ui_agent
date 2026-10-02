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
const output={schema:'uagent.reactive-contract/v1',states:[],functions:{},constants:{},jsx:[],timers:[],aliases:{},navigation:[],blockers:[],locals:{},effects:[],subscriptions:[],gestures:[]};
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
 if(ts.isSpreadElement(n)) return ['spread',expr(n.expression)];
 if(ts.isArrowFunction(n)||ts.isFunctionExpression(n)) return fn(n);
 if(ts.isTemplateExpression(n)) return ['template',n.head.text,n.templateSpans.map(s=>[expr(s.expression),s.literal.text])];
 return ['unsupported',n.getText()];
}
function parameters(n) {return n.parameters.map(p=>({name:ts.isIdentifier(p.name)?p.name.text:null,fields:ts.isObjectBindingPattern(p.name)?p.name.elements.map(e=>({name:e.name.text,property:e.propertyName?.text||e.name.text,default:expr(e.initializer)})):ts.isArrayBindingPattern(p.name)?p.name.elements.map((e,i)=>({name:e.name?.text,property:i})):null}));}
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
 if(ts.isCallExpression(n)&&ts.isPropertyAccessExpression(n.expression)&&n.expression.name.text==='addEventListener') {
  output.blockers.push(`${file}:${n.pos}: event listener ${n.arguments[0]?.getText()||'unknown'} has no compiled subscription/lifecycle recipe`);
  output.subscriptions.push({owner,target:n.expression.expression.getText(),event:expr(n.arguments[0]),callback:expr(n.arguments[1]),file,start:n.pos});
 }
 if(ts.isFunctionDeclaration(n)&&n.name) {owner=n.name.text; output.functions[owner]=fn(n);}
 if(ts.isSwitchStatement(n)&&ts.isIdentifier(n.expression)){
  const cases=n.caseBlock.clauses.map(c=>{const r=c.statements.find(s=>ts.isReturnStatement(s));const e=r?.expression;return {value:ts.isCaseClause(c)?expr(c.expression):null,component:e&&ts.isJsxSelfClosingElement(e)?e.tagName.getText():e&&ts.isJsxElement(e)?e.openingElement.tagName.getText():null};});
  if(cases.length&&cases.every(c=>c.component))output.navigation.push({state:n.expression.text,cases});
 }
 if(ts.isVariableDeclaration(n)&&n.initializer) {
  const init=n.initializer;
  if(ts.isIdentifier(n.name)&&/^[A-Z]/.test(n.name.text)&&(ts.isArrowFunction(init)||ts.isFunctionExpression(init)))owner=n.name.text;
  if(ts.isArrayBindingPattern(n.name)&&ts.isCallExpression(init)&&init.expression.getText()==='useState') {
   const binding=n.name.elements[0],setter=n.name.elements[1];
   if(binding&&ts.isBindingElement(binding)&&ts.isArrayBindingPattern(binding.name)&&binding.name.elements.every(e=>ts.isBindingElement(e)&&ts.isIdentifier(e.name))){
    const name='tuple_'+n.pos;output.states.push({name,setter:setter?.name?.text||null,owner,initial:expr(init.arguments[0]),file,start:n.pos});
    binding.name.elements.forEach((e,i)=>output.constants[e.name.text]=['member',['id',name],['literal',i]]);
   }else if(!binding||!ts.isBindingElement(binding)||!ts.isIdentifier(binding.name)||(setter&&(!ts.isBindingElement(setter)||!ts.isIdentifier(setter.name)))) {
    output.blockers.push(`${file}:${n.pos}: nested or omitted useState binding requires an explicit state destructuring recipe`);
   }else output.states.push({name:binding.name.text,setter:setter?.name?.text||null,owner,initial:expr(init.arguments[0]),file,start:n.pos});
  }
  if(ts.isIdentifier(n.name)) {
   if(owner)(output.locals[owner]??={})[n.name.text]=expr(init);
   let f=init;
   if(ts.isCallExpression(f)&&f.expression.getText()==='useCallback') f=f.arguments[0];
   if(ts.isArrowFunction(f)||ts.isFunctionExpression(f)) output.functions[n.name.text]=fn(f);
   else if(owner===null) output.constants[n.name.text]=expr(init);
  }
 }
 if(ts.isCallExpression(n)&&n.expression.getText()==='useEffect')output.effects.push({owner,callback:expr(n.arguments[0]),dependencies:expr(n.arguments[1]),file,start:n.pos});
 if(ts.isCallExpression(n)&&n.expression.getText()==='setInterval') {
  let block=n.parent;while(block&&!ts.isBlock(block))block=block.parent;
  const callback=n.arguments[0],immediate=ts.isIdentifier(callback)&&!!block?.statements.some(s=>s.end<n.pos&&ts.isExpressionStatement(s)&&ts.isCallExpression(s.expression)&&s.expression.expression.getText()===callback.text);
  const persistent={};
  for(const s of block?.statements||[])if(s.end<n.pos&&ts.isVariableStatement(s)&&(s.declarationList.flags&ts.NodeFlags.Let))for(const d of s.declarationList.declarations)if(ts.isIdentifier(d.name)&&d.initializer){
   const name='effect_'+block.pos+'_'+d.name.text;persistent[d.name.text]=['id',name];
   if(!output.states.some(s=>s.name===name))output.states.push({name,setter:null,owner,initial:expr(d.initializer),file,start:d.pos,internal:true});
  }
  const replace=value=>Array.isArray(value)?(value[0]==='id'&&persistent[value[1]]?persistent[value[1]]:value.map(replace)):value;
  let effectOwned=false;for(let p=n.parent;p;p=p.parent)if(ts.isCallExpression(p)&&p.expression.getText()==='useEffect'){effectOwned=true;break;}
  output.timers.push({owner,callback:replace(expr(callback)),period:expr(n.arguments[1]),immediate,effectOwned,file,start:n.pos});
 }
 if(ts.isJsxOpeningElement(n)||ts.isJsxSelfClosingElement(n)) {
  const attrs={}; for(const a of n.attributes.properties) if(ts.isJsxAttribute(a)) {
   attrs[a.name.text]=!a.initializer?['literal',true]:ts.isJsxExpression(a.initializer)?expr(a.initializer.expression):expr(a.initializer);
   if(/^on(?:Drag|Pointer|Mouse|Touch|Wheel|Key)/.test(a.name.text))output.blockers.push(`${file}:${a.pos}: ${a.name.text} requires a pointer/gesture event recipe`);
   if(attrs[a.name.text]?.[0]==='id') (output.aliases[a.name.text]??=[]).push(attrs[a.name.text][1]);
  }
  const children=ts.isJsxOpeningElement(n)&&ts.isJsxElement(n.parent)?n.parent.children.filter(c=>ts.isJsxText(c)||ts.isJsxExpression(c)).map(c=>ts.isJsxText(c)?['literal',c.text.trim()]:expr(c.expression)).filter(Boolean):[];
  const loops=[];for(let p=n.parent;p;p=p.parent) if(ts.isArrowFunction(p)&&ts.isCallExpression(p.parent)&&ts.isPropertyAccessExpression(p.parent.expression)&&p.parent.expression.name.text==='map') loops.push({items:expr(p.parent.expression.expression),parameters:parameters(p)});
  const guards=[];for(let p=n.parent;p;p=p.parent)if(ts.isBinaryExpression(p)&&p.operatorToken.kind===K.AmpersandAmpersandToken&&n.pos>=p.right.pos&&n.end<=p.right.end)guards.push(expr(p.left));
  for(let p=n.parent;p;p=p.parent)if(ts.isConditionalExpression(p)){
   if(n.pos>=p.whenTrue.pos&&n.end<=p.whenTrue.end)guards.push(expr(p.condition));
   else if(n.pos>=p.whenFalse.pos&&n.end<=p.whenFalse.end)guards.push(['unary','!',expr(p.condition)]);
  }
  if(attrs.onDragEnd)output.gestures.push({owner,source_id:file+':'+n.pos,callback:attrs.onDragEnd,loops,key:attrs.key,attrs,file,start:n.pos});
  output.jsx.push({tag:n.tagName.getText(),attrs,children,owner,file,start:n.pos,end:ts.isJsxOpeningElement(n)&&ts.isJsxElement(n.parent)?n.parent.end:n.end,loops,guards});
 }
 ts.forEachChild(n,c=>walk(c,owner,file));
}
// Preserve lexical declarations independently of the legacy flat lowering.
// The latter remains gated until the target can execute instance semantics.
output.scopes=[];output.types={};output.tasks=[];
function typeExpr(n){
 if(!n)return null;
 if(ts.isParenthesizedTypeNode(n))return typeExpr(n.type);
 if(ts.isArrayTypeNode(n))return {kind:'list',element:typeExpr(n.elementType)};
 if(ts.isUnionTypeNode(n))return {kind:'union',members:n.types.map(typeExpr)};
 if(ts.isLiteralTypeNode(n)){
  const value=expr(n.literal);
  if(value?.[0]==='literal')return {kind:'literal',value:value[1]};
  if(value?.[0]==='unary'&&value[1]==='-'&&value[2]?.[0]==='literal'&&typeof value[2][1]==='number')return {kind:'literal',value:-value[2][1]};
  return {kind:'unknown',source:n.getText()};
 }
 if(ts.isTypeLiteralNode(n))return recordType(n.members);
 if(ts.isTypeReferenceNode(n)){
  const name=n.typeName.getText();
  if(name==='Array'||name==='ReadonlyArray')return {kind:'list',element:typeExpr(n.typeArguments?.[0])};
  if(name==='File')return {kind:'host-file'};
  return {kind:'reference',name};
 }
 return {kind:({[K.StringKeyword]:'string',[K.NumberKeyword]:'number',[K.BooleanKeyword]:'boolean',[K.NullKeyword]:'null'})[n.kind]||'unknown',source:n.getText()};
}
function recordType(members){
 if(members.some(m=>!ts.isPropertySignature(m)||!m.name||ts.isComputedPropertyName(m.name)))return {kind:'unknown',source:'non-field type member'};
 return {kind:'record',fields:Object.fromEntries(members.map(m=>[m.name.text,{...typeExpr(m.type),optional:!!m.questionToken}]))};
}
function scopedWalk(n,scope,file){
 if(ts.isInterfaceDeclaration(n))output.types[file+'#'+n.name.text]=n.heritageClauses?.length?{kind:'unknown',source:'interface inheritance'}:recordType(n.members);
 if(ts.isTypeAliasDeclaration(n))output.types[file+'#'+n.name.text]=typeExpr(n.type);
 const f=ts.isFunctionDeclaration(n)?n:ts.isVariableDeclaration(n)&&n.initializer&&(ts.isArrowFunction(n.initializer)||ts.isFunctionExpression(n.initializer))?n.initializer:null;
 if(f&&n.name&&ts.isIdentifier(n.name)){
  const name=n.name.text;
  scope={id:file+':'+n.pos,name,file,start:n.pos,parent:scope?.id||null,kind:/^[A-Z]/.test(name)?'component':'function',definition:fn(f),states:[],events:[],loops:[],calls:[]};
  output.scopes.push(scope);
 }
 if(scope&&ts.isVariableDeclaration(n)&&ts.isArrayBindingPattern(n.name)&&n.initializer&&ts.isCallExpression(n.initializer)&&n.initializer.expression.getText()==='useState'){
  const binding=n.name.elements[0],setter=n.name.elements[1];
  scope.states.push({name:binding?.name?.getText(),setter:setter?.name?.getText(),initial:expr(n.initializer.arguments[0]),declaredType:typeExpr(n.initializer.typeArguments?.[0]),source_id:file+':'+n.pos});
 }
 if(scope&&ts.isCallExpression(n)){
  const callee=n.expression.getText();
  scope.calls.push({callee,arguments:n.arguments.map(expr),source_id:file+':'+n.pos});
  if(['setTimeout','setInterval'].includes(callee)){
   let effect=null;for(let p=n.parent;p;p=p.parent)if(ts.isCallExpression(p)&&p.expression.getText()==='useEffect'){effect=file+':'+p.pos;break;}
   output.tasks.push({id:file+':'+n.pos,scope:scope.id,kind:callee==='setTimeout'?'timeout':'interval',trigger:effect?'effect':'call',effect,delay:expr(n.arguments[1]),callback:expr(n.arguments[0])});
  }
 }
 if(scope&&(ts.isJsxOpeningElement(n)||ts.isJsxSelfClosingElement(n))){
  const source_id=file+':'+n.pos,attrs={};
  for(const a of n.attributes.properties)if(ts.isJsxAttribute(a)){attrs[a.name.text]=!a.initializer?['literal',true]:ts.isJsxExpression(a.initializer)?expr(a.initializer.expression):expr(a.initializer);if(/^on[A-Z]/.test(a.name.text))scope.events.push({source_id,event:a.name.text,callback:attrs[a.name.text],tag:n.tagName.getText()});}
  for(let p=n.parent;p;p=p.parent)if(ts.isArrowFunction(p)&&ts.isCallExpression(p.parent)&&ts.isPropertyAccessExpression(p.parent.expression)&&p.parent.expression.name.text==='map'){
   // Only the root of each repeated subtree owns the reconciliation key.
   let body=p.body;while(ts.isParenthesizedExpression(body))body=body.expression;
   const element=ts.isJsxOpeningElement(n)?n.parent:n;
   if(body===element)scope.loops.push({source_id,items:expr(p.parent.expression.expression),key:attrs.key||null,parameters:parameters(p)});
  }
 }
 ts.forEachChild(n,c=>scopedWalk(c,scope,file));
}
for(const file of files) {const source=ts.createSourceFile(file,fs.readFileSync(path.join(root,file),'utf8'),ts.ScriptTarget.Latest,true,ts.ScriptKind.TSX);walk(source,null,file);scopedWalk(source,null,file);}
for(const key of Object.keys(output.aliases)) output.aliases[key]=[...new Set(output.aliases[key])];
process.stdout.write(JSON.stringify(output));
"""


def extract_reactive_contract(source_dir: Path, source_files: list[str], *, parser_module: Path | None = None) -> dict[str, Any]:
    parser = parser_module or source_dir / 'node_modules/typescript/lib/typescript.js'
    node = shutil.which('node')
    if not node or not parser.is_file():
        return {'schema': 'uagent.reactive-contract/v1', 'blockers': ['Local TypeScript parser unavailable; reactive logic not compiled']}
    result = subprocess.run([node, '-e', _EXTRACT, str(source_dir), json.dumps(sorted({f.replace('\\','/') for f in source_files})),str(parser)],
                            capture_output=True, text=True, encoding='utf-8', timeout=20)
    if result.returncode:
        return {'schema': 'uagent.reactive-contract/v1', 'blockers': [result.stderr[-2000:]]}
    contract=json.loads(result.stdout)
    names=[state['name'] for state in contract.get('states',[])]
    if len(set(names))!=len(names):contract['blockers'].append('Ambiguous duplicate state names across component/effect scopes')
    functions = {}
    for scope in contract.get('scopes', []):
        functions.setdefault(scope['name'], []).append(scope['id'])
    for name, declarations in functions.items():
        if len(declarations) > 1:
            contract['blockers'].append(f'Ambiguous duplicate function {name}: lexical lowering required ({", ".join(declarations)})')
    return contract


def instrument_browser_source(source_dir: Path, files: list[str], parser: Path) -> None:
    """Annotate an explicitly isolated browser copy, leaving compiler input intact."""
    script=r'''
const fs=require('fs'),path=require('path'),ts=require(process.argv[1]),root=process.argv[2];
const fileList=JSON.parse(process.argv[3]);
const instanceMode=fileList.some(file=>/addEventListener\s*\(\s*["'](?:mousemove|pointermove)["']|onDragEnd\s*=/.test(fs.readFileSync(path.join(root,file),'utf8')));
for(const file of fileList){
 const name=path.join(root,file),text=fs.readFileSync(name,'utf8'),tree=ts.createSourceFile(file,text,ts.ScriptTarget.Latest,true,ts.ScriptKind.TSX),edits=[];
 function visit(n,component=null,hasCallsite=false){
  if((ts.isImportDeclaration(n)||ts.isExportDeclaration(n))&&n.moduleSpecifier&&ts.isStringLiteral(n.moduleSpecifier)){
   const spec=n.moduleSpecifier.text,normalized=spec.replace(/^(@[^/]+\/[^@]+|[^@/]+)@\d[^/]*$/,'$1');
   if(normalized!==spec)edits.push([n.moduleSpecifier.getStart(),JSON.stringify(normalized),n.moduleSpecifier.end]);
  }
  const componentName=ts.isFunctionDeclaration(n)?n.name?.text:ts.isVariableDeclaration(n)&&n.initializer&&(ts.isArrowFunction(n.initializer)||ts.isFunctionExpression(n.initializer))?n.name.getText():null;
  const body=ts.isFunctionDeclaration(n)?n.body:componentName?n.initializer.body:null;
  if(componentName&&/^[A-Z]/.test(componentName)&&body&&ts.isBlock(body)){
   const fn=ts.isFunctionDeclaration(n)?n:n.initializer;
   if(fn.parameters[0]&&ts.isObjectBindingPattern(fn.parameters[0].name)){
    hasCallsite=true;edits.push([fn.parameters[0].name.getStart()+1,'"data-uagent-callsite":__uaCallsite,']);
   }
  }
  if(instanceMode&&componentName&&/^[A-Z]/.test(componentName)&&body&&ts.isBlock(body)){
   component=componentName;const fn=ts.isFunctionDeclaration(n)?n:n.initializer;
   const fields=fn.parameters.flatMap(p=>ts.isObjectBindingPattern(p.name)?p.name.elements.map(e=>e.name.getText()):ts.isIdentifier(p.name)?[p.name.text]:[]);
   edits.push([body.getStart()+1,`const __uaInstance=__uaUseId();globalThis.__uagentInstances??={};globalThis.__uagentInstances[__uaInstance]={owner:${JSON.stringify(component)},props:__uaProps({${fields.join(',')}})};`]);
  }
  if((ts.isJsxOpeningElement(n)||ts.isJsxSelfClosingElement(n))&&/^[a-z]/.test(n.tagName.getText())){
   const ref=n.attributes.properties.find(a=>ts.isJsxAttribute(a)&&a.name.text==='ref');
   const key=n.attributes.properties.find(a=>ts.isJsxAttribute(a)&&a.name.text==='key');
   edits.push([n.attributes.end,` data-uagent-source=${JSON.stringify(file+':'+n.pos)}`+(hasCallsite?' data-uagent-callsite={__uaCallsite}':'')+(instanceMode&&component?' data-uagent-instance={__uaInstance}':'')+(instanceMode&&ref?.initializer&&ts.isJsxExpression(ref.initializer)?` data-uagent-ref=${JSON.stringify(ref.initializer.expression.getText())}`:'')+(instanceMode&&key?.initializer&&ts.isJsxExpression(key.initializer)?` data-uagent-key={${key.initializer.expression.getText()}}`:'')]);
  }
  if((ts.isJsxOpeningElement(n)||ts.isJsxSelfClosingElement(n))&&/^[A-Z]/.test(n.tagName.getText()))edits.push([n.attributes.end,` data-uagent-callsite=${JSON.stringify(file+':'+n.pos)}`]);
  if(ts.isVariableStatement(n))for(const d of n.declarationList.declarations)if(ts.isArrayBindingPattern(d.name)&&d.initializer&&ts.isCallExpression(d.initializer)&&d.initializer.expression.getText()==='useState'){
   const binding=d.name.elements[0]?.name;
   if(binding&&ts.isIdentifier(binding)){const state=binding.text;edits.push([n.end,`;globalThis.__uagentState??={};globalThis.__uagentState[${instanceMode&&component?'__uaInstance+":"+':''}${JSON.stringify(state)}]=${state};`]);}
   else if(binding&&ts.isArrayBindingPattern(binding)&&binding.elements.every(e=>ts.isBindingElement(e)&&ts.isIdentifier(e.name))){const state='tuple_'+d.pos;edits.push([n.end,`;globalThis.__uagentState??={};globalThis.__uagentState[${instanceMode&&component?'__uaInstance+":"+':''}${JSON.stringify(state)}]=[${binding.elements.map(e=>e.name.text).join(',')}];`]);}
  }
  ts.forEachChild(n,c=>visit(c,component,hasCallsite));
 }
 visit(tree);let result=text;for(const [at,insert,end]of edits.sort((a,b)=>b[0]-a[0]))result=result.slice(0,at)+insert+result.slice(end??at);if(instanceMode)result='import {useId as __uaUseId} from "react";\nfunction __uaProps(v,d=0){if(v==null||typeof v!=="object")return typeof v==="function"?{__callback:true}:v;if(d>8||v.$$typeof)return {__unsupported:"opaque React prop"};return Array.isArray(v)?v.map(x=>__uaProps(x,d+1)):Object.fromEntries(Object.entries(v).map(([k,x])=>[k,__uaProps(x,d+1)]));}\n'+result;result='import {flushSync as __uaFlushSync} from "react-dom";globalThis.__uagentFlush=__uaFlushSync;\n'+result;fs.writeFileSync(name,result);
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
    local_serial = 0
    event_value = event_value or ['event']

    def queued_value(state: dict[str, Any]) -> Any:
        """Functional updates consume the queue, direct reads use render state."""
        def contains_call(value):
            return isinstance(value, list) and bool(value) and (value[0] == 'call' or any(contains_call(v) for v in value))
        previous = ['id', state['name']]
        initial = literal(state['initial'])
        fields = {name: ['member', previous, ['literal', name]] for name in initial} if isinstance(initial, dict) else None
        changed = False
        for action in actions:
            if action['state'] != state['name']:
                continue
            if contains_call(action['value']):
                raise ValueError('Queued call result requires a captured temporary; repeated evaluation is unsafe')
            if action['field'] is not None:
                if fields is None or action['field'] not in fields:
                    raise ValueError('Queued record update has no declared field')
                field = action['field']
                fields[field] = (['conditional', action['guard'], action['value'], fields[field]]
                                 if action.get('guard') else action['value'])
                changed = True
                continue
            previous = (['conditional', action['guard'], action['value'], previous]
                        if action.get('guard') else action['value'])
        return ['object', [[['literal', name], value] for name, value in fields.items()]] if changed else previous

    def expression(value: Any, env: dict[str, Any]) -> Any:
        value = substitute(value, env)
        def fold_member(item):
            if not isinstance(item, list):
                return item
            item = [fold_member(v) for v in item]
            if item and item[0] == 'member' and item[1][0] == 'object':
                key = literal(item[2])
                for field, field_value in reversed(item[1][1]):
                    if field != 'spread' and literal(field) == key:
                        return field_value
            return item
        value = fold_member(value)
        if isinstance(value, list) and value and value[0] == 'member':
            if value[1] == ['member', ['event-object'], ['literal', 'target']] and literal(value[2]) in {'value', 'checked'}:
                return event_value
        if isinstance(value, list) and value and value[0] == 'call' and value[1] == ['id', 'Number']:
            return expression(value[2][0], env)
        return value

    def invoke(value: Any, args: list[Any], env: dict[str, Any], depth: int = 0, guard: Any = None) -> None:
        nonlocal local_serial
        if depth > 12:
            raise ValueError('Recursive event handler is unsupported')
        if value and value[0] == 'id':
            name = value[1]
            if name in setters:
                state = setters[name]
                assigned = expression(args[0], env)
                bindings=[]
                if assigned and assigned[0] == 'function':
                    if guard and any(s[0]=='variables' for s in assigned[2]):raise ValueError('Guarded updater locals require conditional scheduling')
                    state_env = dict(env)
                    for parameter in assigned[1]:
                        state_env[parameter['name']] = queued_value(state)
                    returned=None
                    for statement in assigned[2]:
                        if statement[0]=='variables':
                            for name,initial in statement[1]:
                                symbol=f'updater_{local_serial}';local_serial+=1
                                bindings.append({'name':symbol,'value':expression(initial,state_env)})
                                state_env[name]=['local',symbol]
                        elif statement[0]=='return':
                            returned=expression(statement[1],state_env);break
                        else:raise ValueError('Updater control flow requires a compiled return recipe')
                    if returned is None:raise ValueError('Updater has no return value')
                    assigned=returned
                if assigned and assigned[0] == 'object':
                    for key, item in assigned[1]:
                        if key == 'spread':
                            continue
                        field = literal(expression(key, env))
                        if not isinstance(field, str):
                            raise ValueError('Unresolved computed state key')
                        actions.append({'state':state['name'],'field':field,'value':expression(item,env),**({'bindings':bindings} if bindings else {}),**({'guard':guard} if guard else {})})
                else:
                    actions.append({'state':state['name'],'field':None,'value':assigned,**({'bindings':bindings} if bindings else {}),**({'guard':guard} if guard else {})})
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
                callee=call[1]
                if callee[0]=='member' and literal(callee[2])=='pop' and callee[1][0]=='id' and callee[1][1] in local:
                    name=callee[1][1];array=local[name]
                    if not (array and array[0]=='array' and len(array[1])==1 and array[1][0][0]=='spread'):
                        raise ValueError('Only pop on a local copied array is supported')
                    local[name]=['array-drop',array[1][0][1]]
                    continue
                invoke(call[1], [expression(a,local) for a in call[2]], local, depth+1, guard)
            elif statement[0] in {'expression','return'} and statement[1] and statement[1][:2]==['binary','&&']:
                condition=expression(statement[1][2],local)
                execute([['expression',statement[1][3]]],local,depth,['binary','&&',guard,condition] if guard else condition)
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
    definition=functions.get(callback[1]) if callback and callback[0]=='id' else callback
    if definition and definition[0] == 'function':
        names = [p['name'] for p in definition[1]]
        body_text = json.dumps(definition)
        argument = ['event-object'] if any(f'"{name}"' in body_text and '"target"' in body_text for name in names) else event_value
    else:
        argument = event_value
    invoke(callback, [argument], {})
    combined: dict[tuple[str,str|None],dict[str,Any]] = {}
    for action in actions:
        key=(action['state'],action['field'])
        previous_bindings = combined.get(key, {}).get('bindings', [])
        if previous_bindings:
            action['bindings'] = [*previous_bindings, *action.get('bindings', [])]
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
        if not callback or not jsx['tag'][0].islower() and not key:
            continue
        if not key and any(attrs.get(a,[None])[0]=='id' for a in ('id','aria-label')) and any(j['tag']==jsx.get('owner') and (literal(j['attrs'].get('id')) or literal(j['attrs'].get('aria-label'))) for j in contract.get('jsx',[])):
            continue  # Labelled component use-sites already bind this template control.
        env={}
        for statement in contract.get('functions',{}).get(jsx.get('owner'),[None,None,[]])[2]:
            if statement[0]=='variables':
                for name,value in statement[1]:env[name]=substitute(value,env)
        environments=[env]
        for loop in reversed(jsx.get('loops',[])):
            expanded=[]
            for local in environments:
                values=literal(substitute(loop['items'],local),contract.get('constants'))
                if not isinstance(values,list):continue
                for index,value in enumerate(values):
                    scope=dict(local)
                    for pi,p in enumerate(loop['parameters']):
                        if p['name']:scope[p['name']]=['literal',value if pi==0 else index]
                    expanded.append(scope)
            environments=expanded
        def fold(v):
            if not isinstance(v,list):return v
            v=[fold(x) for x in v]
            if v and v[0]=='member' and v[1][0]=='literal':
                target,field=v[1][1],literal(v[2])
                if isinstance(target,dict) and field in target:return ['literal',target[field]]
            return v
        if not environments:
            bindings.append({'identity':key or f'{jsx["file"]}:{jsx["start"]}','actions':[],'blocker':'Control iteration has no bounded static items'})
        for ordinal,local in enumerate(environments):
            source_id=f'{jsx["file"]}:{jsx["start"]}'
            item = {'identity':key or f'source:{source_id}#{ordinal}','source_id':source_id,'ordinal':ordinal,'owner':jsx.get('owner'),
                    'source':{'file':jsx['file'],'offset':jsx['start']},'actions':[]}
            try:
                event=['event-text'] if literal(attrs.get('type')) in {'date','time','text'} else None
                item['actions'] = resolve_actions(fold(substitute(callback,local)), contract,event)
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
        if not roots and any(p.get('fields') for p in definition[1]):
            from core.pointer_model import reduce_expression
            view_env={k:(['literal',None] if v is None else ['literal',True] if v[0]=='unsupported' and str(v[1]).lstrip().startswith('<') else v) for k,v in env.items()}
            for node in jsx:
                if node['owner']!=usage['tag'] or not node['tag'][0].islower():continue
                guards=[reduce_expression(substitute(g,view_env),{}) for g in node.get('guards',[])]
                if any(g[0]=='literal' and not g[1] for g in guards):continue
                child={'tag':node['tag'],'attrs':{k:reduce_expression(substitute(v,view_env),{}) for k,v in node['attrs'].items()},
                       'texts':[reduce_expression(substitute(v,view_env),{}) for v in node['children']],
                       'guards':[g for g in guards if g!=['literal',True]]}
                if dynamic(child['texts']) or any(dynamic(v) for v in child['attrs'].values()):
                    identity=f"{node['file']}:{node['start']}"
                    views.append({'source_id':identity,'callsite':f"{usage['file']}:{usage['start']}",
                                  'identity':identity,'children':[child]})
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
