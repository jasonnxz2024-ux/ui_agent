"""High-level API: React intermediate model -> auditable AiBuilder custom code."""
from __future__ import annotations

import json
import os
from collections import Counter
from shutil import copy2
from pathlib import Path

from adapters.lvgl import adapt_components
from core.model import ReactProjectModel
from core.capability import capability_plan_dict, plan_capabilities
from core.browser_layout import browser_background_paint
from core.planning_agent import run_planning_agent
from core.reactive_contract import extract_reactive_contract, source_control_bindings, source_view_bindings, resolve_actions
from core.state_rules import compile_state_rules

from .lvgl import LVGLRenderer, bundle, resource_filename, runtime_scene_bundle
from .model import GenerationBundle
from .project import write_project_scaffold
from .screen_ir import build_screen_ir, reachable_components


def _merge_runtime_effect_decisions(planning: dict, runtime: dict, captures: list, contract: dict) -> None:
    """Reconcile browser effects only with classified, source-bound paint nodes.

    This adds instance evidence to the earlier source plan. It never clears
    blockers merely because a renderer emitted C or a screenshot looks similar.
    """
    source_ids={f'{node["file"]}:{node["start"]}' for node in contract.get('jsx',[])}
    receipts={(r['screen'],r['node']):r for r in runtime.get('nodes',[])}
    decisions=[dict(d) for d in planning.get('decisions',[])]
    resolved=[]
    for decision in decisions:
        props=decision.get('properties',{})
        browser_origin=str(decision.get('node_id','')).startswith('browser:') or str(props.get('source_id','')).startswith(('browser:','dom:'))
        if decision.get('kind')!='VisualEffect' or decision.get('support')!='unsupported' or not browser_origin:continue
        index=props.get('screen_index');bounds=props.get('bounds')
        if not isinstance(index,int) or not 0<=index<len(captures) or not bounds or len(bounds)!=4:continue
        scene=captures[index].get('data',{}).get('runtime_scene',{})
        candidates=[]
        for node in scene.get('nodes',[]):
            identity=node.get('attrs',{}).get('data-uagent-source')
            receipt=receipts.get((index,node['id']))
            if identity not in source_ids or not receipt or receipt['support'] in {'unsupported','fallback'}:continue
            if any(str(b).startswith((f'{index}:{node["id"]}:',identity+':')) for b in runtime.get('blockers',[])):continue
            rect=node['rect']
            if any(abs(float(rect[k])-float(value))>.05 for k,value in zip(('x','y','width','height'),bounds)):continue
            keys={'filter':['filter'],'gradient':['backgroundImage'],'shadow':['boxShadow','textShadow']}.get(props.get('kind'),[])
            if props.get('value') not in [styles.get(key) for styles in (node['style'],node.get('sourceStyle',{})) for key in keys]:continue
            candidates.append((node,receipt,identity))
        if len(candidates)!=1:continue
        node,receipt,identity=candidates[0]
        partial=any(str(item).startswith(f'{index}:{node["id"]}:') for item in runtime.get('limitations',[]))
        decision.update(support='partial' if partial else 'custom',source_id=identity,
                        reason='Source-bound runtime paint recipe: '+receipt['reason'],blockers=[],
                        evidence=[*decision.get('evidence',[]),'source:'+identity,f'runtime-scene:{index}:{node["id"]}'])
        resolved.append(decision['node_id'])
    if not resolved:return
    counts={key:dict(value) for key,value in planning.get('counts',{}).items()}
    planning['pre_runtime_counts']=planning.get('counts',{})
    counts.setdefault('VisualEffect',{})['planned']=counts.get('VisualEffect',{}).get('planned',0)+len(resolved)
    remaining={blocker for d in decisions for blocker in d.get('blockers',[])}
    blocks=[]
    for blocker in planning.get('blockers',[]):
        if blocker=='Runtime capture contains a VisualEffect with no source-backed effect node.' and blocker not in remaining:continue
        if blocker.startswith('Completeness blocker: VisualEffect ') and counts['VisualEffect']['planned']>=counts['VisualEffect'].get('browser',0):continue
        blocks.append(blocker)
    planning.update(decisions=decisions,counts=counts,blockers=blocks,status='blocked' if blocks else 'ready',runtime_effect_resolutions=resolved)
    supports=Counter(d['support'] for d in decisions)
    planning['summary']={**planning.get('summary',{}),**{key:supports[key] for key in ('native','custom','partial','fallback','unsupported')},'blocker_count':len(blocks)}


def build_screen_tree(model: ReactProjectModel, reachable: list[str]) -> dict[str, object]:
    """Build a stable composition manifest from the reachable component graph."""
    selected = set(reachable)
    nodes = []
    for component_id in reachable:
        component = model.components[component_id]
        children = [child for child in model.component_graph.get(component_id, []) if child in selected]
        nodes.append({
            "id": component_id,
            "name": component.id.rsplit(":", 1)[-1],
            "kind": component.kind,
            "source_file": component.source_file,
            "children": children,
            "adapted": bool(adapt_components(component, model)),
        })
    screens = build_screen_ir(model, reachable)
    return {
        "schema": "uagent.screen-tree/v1",
        "screens": [
            {
                "id": screen.id,
                "name": screen.name,
                "route": screen.route,
                "root": screen.root_component,
                "shared": list(screen.shared_components),
                "roots": [*screen.shared_components, screen.root_component],
            }
            for screen in screens
        ],
        "nodes": nodes,
        "layout_policy": "preserve-source-evidence; unknown positions remain partial",
        "capability_plan": capability_plan_dict(model),
        "agent_planning": model.agent_planning,
        "render_plan": {
            "schema": model.render_plan.schema,
            "items": [item.__dict__ for item in model.render_plan.items],
            "blockers": list(model.render_plan.blockers),
        },
    }


def compile_lvgl(model: ReactProjectModel) -> GenerationBundle:
    """Compile all supported components without project-name-specific paths."""
    plan_capabilities(model)
    # Planning is complete before adapters run. It classifies evidence and
    # completeness only; the renderer remains the sole producer of C.
    run_planning_agent(model)
    renderer = LVGLRenderer()
    units = []
    skipped = []
    reachable = reachable_components(model)
    for component_id in reachable:
        component = model.components[component_id]
        adapted_units = adapt_components(component, model)
        for adapted in adapted_units:
            units.append(renderer.render(adapted, component, model))
        if not adapted_units and component.kind != "component":
            skipped.append(f"已识别但尚无生成适配器：{component.id} ({component.kind})")
    tree = build_screen_tree(model, reachable)
    if any(s.get('data', {}).get('runtime_scene') for s in (model.browser_evidence or {}).get('screens', [])):
        contract = extract_reactive_contract(model.source_dir, [model.components[c].source_file for c in reachable])
        contract['list_capacity']=int(os.environ.get('UAGENT_LIST_CAPACITY','64'))
        contract['state_rules'] = compile_state_rules(contract, contract['list_capacity'])
        contract['blockers'].extend(contract['state_rules']['blockers'])
        # A timeout was previously absent from the interval-only contract.
        # Preserve it as a requirement until event-owned scheduling is lowered.
        for task in contract.get('tasks', []):
            if task['kind'] == 'timeout':
                contract['blockers'].append(f'{task["id"]}: timeout requires event/effect-owned task scheduling and cancellation')
        contract['controls'] = source_control_bindings(contract)
        contract['views'] = source_view_bindings(contract)
        for timer in contract.get('timers', []):
            try:
                if not timer.get('effectOwned'):raise ValueError('Event-created interval requires lifecycle scheduling; it must not start at application launch')
                timer['actions'] = resolve_actions(timer['callback'], contract)
            except ValueError as exc:
                timer['blocker'] = str(exc)
        tree['reactive_contract'] = contract
    output = runtime_scene_bundle(model, tree) or bundle(units, tree)
    if not units:
        output.warnings.append("没有可生成的 LVGL 组件；请检查语义扫描结果")
    if not output.screen_tree.get('runtime_scene'):
        output.warnings.extend(skipped)
    output.capability_plan = capability_plan_dict(model)
    output.agent_planning = dict(model.agent_planning)
    runtime = output.screen_tree.get('runtime_scene')
    if runtime:
        _merge_runtime_effect_decisions(output.agent_planning,runtime,(model.browser_evidence or {}).get('screens',[]),tree.get('reactive_contract',{}))
        output.agent_planning['summary'] = {**output.agent_planning.get('summary', {}), 'runtime_scene': {
            'nodes': len(runtime['nodes']), 'source_controls': runtime['source_controls'],
            'bound_controls': runtime['bound_controls'], 'timers': runtime['timers'],
            'limitations': runtime.get('limitations', []),
            'visual_validation': 'requires external browser/SDL comparison',
        }}
        output.agent_planning['summary']['state_rules'] = tree.get('reactive_contract', {}).get('state_rules', {}).get('summary', {})
        output.agent_planning['blockers'] = [*output.agent_planning.get('blockers', []), *runtime['blockers']]
        if runtime['blockers']:
            output.agent_planning['status'] = 'blocked'
        output.screen_tree['agent_planning'] = output.agent_planning
    output.warnings.extend(f"BLOCKER: {item}" for item in model.render_plan.blockers)
    if len(reachable) < len(model.components):
        output.warnings.append(
            f"已按入口组件图过滤未使用声明：参与转换 {len(reachable)} / 扫描 {len(model.components)}"
        )
    return output


def write_aibuilder_custom(
    bundle_output: GenerationBundle,
    destination: str | Path,
    model: ReactProjectModel | None = None,
    include_runtime: bool = False,
) -> dict[str, Path]:
    """Write generated code, audit data, and all referenced local assets."""
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    paths = {
        "header": destination / "custom.h",
        "source": destination / "custom.c",
        "audit": destination / "uagent-audit.json",
        "screen_tree": destination / "screen-tree.json",
    }
    paths["header"].write_text(bundle_output.custom_h, encoding="utf-8", newline="\n")
    paths["source"].write_text(bundle_output.custom_c, encoding="utf-8", newline="\n")
    if model is not None:
        if destination.name.lower() == "custom" and destination.parent.name.lower() == "ui_builder":
            project_root = destination.parent.parent
        else:
            project_root = destination
        paths.update(write_project_scaffold(project_root, model, include_runtime=include_runtime))
        # Scaffold capture may be newer than the caller's capture. Recompile
        # after it so custom.c and snapshot consume one browser-evidence set.
        bundle_output = compile_lvgl(model)
        paths["header"].write_text(bundle_output.custom_h, encoding="utf-8", newline="\n")
        paths["source"].write_text(bundle_output.custom_c, encoding="utf-8", newline="\n")
        # write_project_scaffold may have obtained fresh browser geometry and
        # therefore refreshed the local fallback decisions. Persist that exact
        # RenderPlan in the audit/tree files written below.
        bundle_output.capability_plan = capability_plan_dict(model)
        bundle_output.screen_tree["capability_plan"] = bundle_output.capability_plan
        bundle_output.screen_tree["render_plan"] = {
            "schema": model.render_plan.schema,
            "items": [item.__dict__ for item in model.render_plan.items],
            "blockers": list(model.render_plan.blockers),
        }
        layout_path = project_root / "openhmi" / "browser-layout.json"
        try:
            layout = json.loads(layout_path.read_text(encoding="utf-8"))
            captures = layout.get("screens", []) if isinstance(layout, dict) else []
            paints = []
            for screen in captures:
                if not isinstance(screen, dict) or not isinstance(screen.get("data"), dict):
                    continue
                paint = browser_background_paint(screen["data"])
                if paint:
                    paints.append({
                        "support": "native" if paint.get("gradient_type") == "linear" else "partial",
                        "strategy": "lvgl-bg-grad" if paint.get("gradient_type") == "linear" else "bounded-radial-layers",
                        "gradient_type": paint.get("gradient_type"), "value": paint.get("value"),
                        "bounds": paint.get("bounds"), "stops": paint.get("stops", []),
                    })
            if paints:
                bundle_output.screen_tree["background_effects"] = paints
        except (OSError, ValueError, TypeError):
            pass
        image_dir = project_root / "resources" / "image"
        image_dir.mkdir(parents=True, exist_ok=True)
        referenced = {resource_id for unit in bundle_output.units for resource_id in unit.resources}
        for resource_id in sorted(referenced):
            resource = model.resources.get(resource_id)
            if resource is None or not resource.path.is_file():
                bundle_output.warnings.append(f"资源不存在，未复制：{resource_id}")
                continue
            copy2(resource.path, image_dir / resource_filename(resource_id, model))
        paths["images"] = image_dir
    paths["audit"].write_text(json.dumps(bundle_output.audit_dict(), ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    paths["screen_tree"].write_text(json.dumps(bundle_output.screen_tree, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    return paths
