import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.model_provider import (
    ModelSettings,
    analyze_capability_candidates,
    save_public_settings,
    validate_agent_suggestions,
)
from core.capability import plan_capabilities
from core.model import Component, ReactProjectModel
from core.pipeline import analyze
from core.planning_agent import merge_runtime_evidence, run_planning_agent
from generator.compiler import compile_lvgl, write_aibuilder_custom


class _Response:
    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        return None

    def json(self):
        return self.body


def test_agent_is_disabled_without_a_call():
    result = analyze_capability_candidates(
        ModelSettings(agent_enabled=False),
        [{"target_id": "candidate", "support": "unsupported", "confidence": .2}],
    )
    assert result == {"status": "disabled", "suggestions": [], "calls": 0}


def test_agent_response_is_whitelisted_and_only_targets_candidates(monkeypatch):
    candidate = {
        "target_id": "effect:unknown", "source_id": "effect:unknown",
        "support": "unsupported", "confidence": .4,
        "adapter": "VisualEffectAdapter", "evidence_ids": ["evidence:1"],
    }
    payload = {"output_text": json.dumps({"suggestions": [{
        "target_id": "effect:unknown", "strategy": "compound",
        "adapter": "VisualEffectAdapter", "confidence": .87,
        "reason": "browser evidence links the existing effect",
        "evidence_ids": ["evidence:1"],
    }]})}
    monkeypatch.setattr("core.model_provider.httpx.post", lambda *args, **kwargs: _Response(payload))
    result = analyze_capability_candidates(
        ModelSettings(agent_enabled=True), [candidate], "temporary-key",
    )
    assert result["status"] == "completed"
    assert result["suggestions"][0]["target_id"] == "effect:unknown"
    assert "temporary-key" not in json.dumps(result)

    accepted, errors = validate_agent_suggestions(
        {"suggestions": [{"target_id": "not-a-candidate", "strategy": "native",
                           "adapter": "GaugeAdapter", "confidence": .9}]},
        [candidate],
    )
    assert not accepted
    assert errors


def test_public_settings_never_persist_the_key(tmp_path):
    path = tmp_path / "settings.json"
    save_public_settings(ModelSettings(enabled=True, agent_enabled=True), path)
    assert "temporary-key" not in path.read_text(encoding="utf-8")
    assert json.loads(path.read_text(encoding="utf-8"))["agent_enabled"] is True


def _write_app(root: Path, body: str) -> None:
    source = root / "src"
    source.mkdir(parents=True, exist_ok=True)
    (source / "main.tsx").write_text(
        "import App from './App'; createRoot(document.body).render(<App />);",
        encoding="utf-8",
    )
    (source / "App.tsx").write_text(body, encoding="utf-8")


def test_local_planning_degrades_to_source_without_browser_evidence(tmp_path):
    _write_app(tmp_path, "export default function App() { return <main><span>Ready</span></main>; }")
    model = analyze(tmp_path)
    report = model.agent_planning
    assert report["enabled"] is True
    assert report["evidence_mode"] == "source-fallback"
    assert report["counts"]["Screen"]["browser"] is None
    assert report["decisions"]


def test_host_overflow_is_not_internal_scroll():
    model = ReactProjectModel(Path("."))
    evidence = {
        "status": "browser-interaction",
        "screens": [{"data": {
            "viewport": {"width": 1024, "height": 600},
            "overflow": {"hasOverflow": True, "hasOverflowY": True},
            "scroll": {"clientWidth": 1024, "clientHeight": 600,
                        "scrollWidth": 1024, "scrollHeight": 1200},
            "nodes": [
                {"tag": "html", "style": {"overflow": "auto"}},
                {"tag": "body", "style": {"overflowY": "scroll"}},
                {"tag": "main", "style": {"overflow": "auto"}},
            ],
        }}],
    }
    merged = merge_runtime_evidence(model, evidence)
    assert merged["counts"]["Scroll"] == 0
    assert merged["host_overflow"][0]["y"] is True
    assert merged["host_overflow"][0]["ignored_for_internal_scroll"] is True


def test_gaussian_blur_merge_is_a_custom_glow_recipe(tmp_path):
    _write_app(tmp_path, """
      export default function App() {
        return <svg><defs><filter id="glow">
          <feGaussianBlur stdDeviation="4" />
          <feMerge><feMergeNode in="SourceGraphic" /></feMerge>
        </filter></defs><circle cx="20" cy="20" r="8" filter="url(#glow)" /></svg>;
      }
    """)
    model = analyze(tmp_path)
    recipes = [item for item in model.agent_planning["decisions"]
               if item["kind"] == "VisualEffect" and "feGaussianBlur" in item["reason"]]
    assert recipes and recipes[0]["support"] == "custom"
    assert not [item for item in recipes if item["support"] == "unsupported"]


def test_unknown_filter_primitive_is_a_blocker(tmp_path):
    _write_app(tmp_path, """
      export default function App() {
        return <svg><defs><filter id="mystery">
          <feColorMatrix values="1 0 0 0 0 0 1 0 0 0 0 0 1 0 0 0 0 0 1 0" />
        </filter></defs><circle filter="url(#mystery)" /></svg>;
      }
    """)
    report = analyze(tmp_path).agent_planning
    assert any(item["support"] == "unsupported" and item["kind"] == "VisualEffect"
               for item in report["decisions"])
    assert any("unknown primitive" in blocker.lower() for blocker in report["blockers"])


def test_missing_runtime_screen_is_a_completeness_blocker(tmp_path):
    model = ReactProjectModel(tmp_path)
    app = Component("src/App.tsx:App", "component", "src/App.tsx")
    model.components[app.id] = app
    model.entry_components.append(app.id)
    browser = {"status": "browser-interaction", "screens": [
        {"data": {"nodes": []}}, {"data": {"nodes": []}},
    ]}
    plan_capabilities(model, browser)
    report = run_planning_agent(model, browser_evidence=browser)
    assert any("Screen browser=2" in blocker for blocker in report["blockers"])


def test_audit_contains_agent_planning_summary(tmp_path):
    _write_app(tmp_path, "export default function App() { return <main><button>Go</button></main>; }")
    bundle = compile_lvgl(analyze(tmp_path))
    written = write_aibuilder_custom(bundle, tmp_path / "custom")
    payload = json.loads(written["audit"].read_text(encoding="utf-8"))
    assert payload["agent_planning"]["schema"] == "uagent.agent-planning/v1"
    assert "blockers" in payload["agent_planning"]
