"""End-to-end source analysis entry point for UAgent."""
from dataclasses import asdict
from pathlib import Path
import json
from .scanner import scan
from .model import ReactProjectModel
from .capability import plan_capabilities
from .planning_agent import load_browser_evidence, run_planning_agent


def analyze(source_dir: str | Path, *, planning_enabled: bool = True) -> ReactProjectModel:
    source = Path(source_dir)
    model = scan(source)
    # A cached browser receipt is optional.  When present it is fed into both
    # the capability pass and the local planning agent; otherwise the planner
    # explicitly records source-only fallback evidence.
    browser_evidence = load_browser_evidence(source)
    # Keep source analysis and target planning as separate passes while
    # returning the historical model object from this public API.
    plan_capabilities(model, browser_evidence)
    run_planning_agent(model, browser_evidence=browser_evidence, enabled=planning_enabled)
    return model


def write_report(model: ReactProjectModel, output: str | Path) -> Path:
    target = Path(output)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = asdict(model)
    payload["source_dir"] = str(model.source_dir)
    for item in payload["resources"].values():
        item["path"] = str(item["path"])
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Analyze a React/Figma Make source tree")
    parser.add_argument("source")
    parser.add_argument("-o", "--output", default="uagent-analysis.json")
    args = parser.parse_args()
    report = write_report(analyze(args.source), args.output)
    print(report)
