"""Read-only checks performed before React/Figma Make generation."""
from __future__ import annotations

import json
from pathlib import Path

from core.pipeline import analyze
from core.browser_layout import _vite_runtime_works
from core.capability import (capability_candidates, high_confidence_blockers,
                              plan_capabilities, source_browser_plan_counts)
from core.planning_agent import run_planning_agent
from generator.parser_registry import detect_parser
from generator.screen_ir import build_screen_ir, reachable_components


def inspect_source(source_dir: str | Path) -> dict[str, object]:
    source = Path(source_dir).resolve()
    blockers: list[str] = []
    warnings: list[str] = []
    if not source.is_dir():
        return {"status": "blocked", "source": str(source), "blockers": ["源码目录不存在"], "warnings": []}
    manifest = source / "package.json"
    if not manifest.is_file():
        blockers.append("缺少 package.json，无法启动 React/Vite 浏览器采集")
    else:
        try:
            package = json.loads(manifest.read_text(encoding="utf-8"))
            scripts = package.get("scripts", {}) if isinstance(package, dict) else {}
            if not isinstance(scripts, dict) or not ({"dev", "start"} & set(scripts)):
                warnings.append("package.json 没有 dev/start 脚本，将尝试直接启动本地 Vite")
        except (OSError, ValueError):
            blockers.append("package.json 不是有效 JSON")
    entries = [path for path in (source / "src").glob("main.*")] if (source / "src").is_dir() else []
    if not entries:
        warnings.append("未找到 src/main.*，入口将由扫描器推断")
    model = analyze(source)
    reachable = reachable_components(model)
    # Reuse a previously verified browser capture when one is available. The
    # preflight endpoint remains read-only and does not launch a full browser
    # session on every button press.
    cached_browser = None
    for evidence_file in (source / "browser-layout-v2.json", source / "browser-layout.json"):
        if not evidence_file.is_file():
            continue
        try:
            candidate = json.loads(evidence_file.read_text(encoding="utf-8"))
            if isinstance(candidate, dict) and isinstance(candidate.get("screens"), list):
                cached_browser = candidate
                break
        except (OSError, ValueError, TypeError):
            continue
    if cached_browser is not None:
        plan_capabilities(model, cached_browser)
        run_planning_agent(model, browser_evidence=cached_browser)
    else:
        # ``analyze`` already ran this pass; keep the explicit branch so the
        # report contract remains clear if a caller supplies a custom model.
        run_planning_agent(model)
    screen_ir = build_screen_ir(model, reachable)
    controls: dict[str, int] = {}
    scroll_components = 0
    for component_id in reachable:
        component = model.components[component_id]
        if component.props.get("scroll"):
            scroll_components += 1
        for control in component.props.get("controls", []):
            if isinstance(control, dict):
                kind = str(control.get("kind") or "unknown")
                controls[kind] = controls.get(kind, 0) + 1
    lockfile = next((name for name in ("pnpm-lock.yaml", "yarn.lock", "package-lock.json")
                     if (source / name).is_file()), None)
    vite_package = source / "node_modules" / "vite" / "bin" / "vite.js"
    runtime_ready = _vite_runtime_works(vite_package, timeout=8)
    if not runtime_ready:
        warnings.append("Vite 运行时探针未通过，生成时将按锁文件自动修复依赖")
    if not reachable:
        blockers.append("未找到可达的 React 组件")
    capabilities = sorted({
        *("gauge" for _ in range(1) if controls.get("gauge")),
        *("scroll" for _ in range(1) if scroll_components),
        *(kind for kind in controls if kind != "unknown"),
    })
    navigation = [{
        "id": edge.id,
        "trigger": edge.trigger,
        "action": edge.action,
        "target": edge.target_route,
        "target_component": edge.target_component,
        "confidence": edge.confidence,
        "status": edge.status,
        "evidence": edge.evidence,
    } for edge in model.navigation]
    navigation_components = sorted({component.id.rsplit(":", 1)[-1] for component in model.components.values()})
    if navigation:
        capabilities.append("navigation")
        capabilities = sorted(set(capabilities))
    unresolved_navigation = sum(1 for edge in model.navigation if not edge.target_component)
    if unresolved_navigation:
        warnings.append(f"发现 {unresolved_navigation} 个导航目标尚未映射到源码页面，将只审计、不自动生成跳转")
    counts = source_browser_plan_counts(model)
    counts["source"]["Screen"] = len(screen_ir) or 1
    counts["plan"]["Screen"] = len(screen_ir) or 1
    if cached_browser is None:
        warnings.append("浏览器证据尚未采集；source/browser/plan 对照中的 browser 计数暂为空")
    capability_blockers = high_confidence_blockers(model)
    blockers.extend(capability_blockers)
    agent_planning = model.agent_planning or {}
    blockers.extend(agent_planning.get("blockers", []) if isinstance(agent_planning, dict) else [])
    source_browser_plan = {}
    for key in ("Screen", "Gauge", "Arc", "Navigation", "Scroll", "VisualEffect"):
        source_count = counts["source"].get(key, 0)
        browser_count = counts["browser"].get(key)
        plan_count = counts["plan"].get(key, 0)
        item_blockers = []
        if isinstance(source_count, int) and isinstance(plan_count, int) and source_count > plan_count:
            item_blockers.append(f"{key} source={source_count} > plan={plan_count}")
        if isinstance(browser_count, int) and isinstance(plan_count, int) and browser_count > plan_count:
            item_blockers.append(f"{key} browser={browser_count} > plan={plan_count}")
        source_browser_plan[key] = {
            "source": source_count, "browser": browser_count, "plan": plan_count,
            "blockers": item_blockers,
        }
        blockers.extend(item_blockers)
    capabilities.extend(["Screen", "Gauge", "Arc", "VisualEffect"])
    capabilities = sorted(set(capabilities))
    return {
        "schema": "uagent.preflight/v1",
        "status": "blocked" if blockers else "ready",
        "source": str(source),
        "parser": detect_parser(source).parser_id,
        "package_manager": "pnpm" if lockfile == "pnpm-lock.yaml" else "yarn" if lockfile == "yarn.lock" else "npm",
        "runtime_ready": runtime_ready,
        "components": {"scanned": len(model.components), "reachable": len(reachable)},
        "resources": len(model.resources),
        "events": len(model.events),
        "navigation": {
            "detected": len(navigation),
            "confirmed": sum(1 for item in navigation if item["status"] == "confirmed"),
            "needs_verification": sum(1 for item in navigation if item["status"] != "confirmed"),
            "items": navigation,
            "component_candidates": navigation_components,
        },
        "controls": controls,
        "capabilities": capabilities,
        "scroll_components": scroll_components,
        "source_browser_plan": source_browser_plan,
        "capability_plan": {
            "decisions": len(model.capability_plan),
            "compound": sum(1 for item in model.capability_plan.values() if item.support == "compound"),
            "fallback": sum(1 for item in model.capability_plan.values() if item.support == "fallback"),
            "unsupported": sum(1 for item in model.capability_plan.values() if item.support == "unsupported"),
            "blockers": capability_blockers,
        },
        "capability_candidates": capability_candidates(model),
        "agent_planning": agent_planning,
        "blockers": blockers,
        "warnings": warnings,
    }
