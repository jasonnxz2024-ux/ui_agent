"""Optional model and constrained Agent calls; secrets stay process-local."""
from dataclasses import dataclass, replace
from pathlib import Path
import base64
import json
import os
import re

import httpx


AGENT_STRATEGIES = {"native", "compound", "fallback", "unsupported"}
AGENT_ADAPTERS = {
    "GaugeAdapter", "VisualEffectAdapter", "GenericControlsAdapter",
    "ScreenCompositionAdapter", "RechartsAdapter", "TableAdapter",
    "DropdownAdapter", "ScrollContainerAdapter", "SourceComponent",
}
AGENT_SUGGESTION_KEYS = {
    "target_id", "source_id", "semantic", "target_component", "strategy",
    "adapter", "confidence", "reason", "evidence_ids", "fallback_bounds",
}


@dataclass
class ModelSettings:
    enabled: bool = False
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-5.6-terra"
    api_key_env: str = "OPENHMI_STATION_TOKEN"
    # ``None`` preserves the old setting file semantics: enabling the visual
    # model also enables constrained Agent calls unless explicitly separated.
    agent_enabled: bool | None = None
    provider: str = "openai"
    deployment: str = "station"

    @property
    def agent_is_enabled(self) -> bool:
        return self.enabled if self.agent_enabled is None else bool(self.agent_enabled)

    def api_key(self) -> str | None:
        return os.environ.get(self.api_key_env)


def load_settings(path: Path) -> ModelSettings:
    if not path.is_file():
        return ModelSettings()
    data = json.loads(path.read_text(encoding="utf-8"))
    return ModelSettings(**{key: data[key] for key in ModelSettings.__dataclass_fields__ if key in data})


def save_public_settings(settings: ModelSettings, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "enabled": settings.enabled,
        "base_url": settings.base_url,
        "model": settings.model,
        "api_key_env": settings.api_key_env,
        "agent_enabled": settings.agent_is_enabled,
        "provider": settings.provider,
        "deployment": settings.deployment,
    }, ensure_ascii=False, indent=2), encoding="utf-8")


PROVIDER_PRESETS: dict[str, dict[str, str]] = {
    "openai": {"base_url": "https://api.openai.com/v1", "model": "gpt-5.6-terra", "api_style": "responses"},
    "deepseek": {"base_url": "https://api.deepseek.com", "model": "deepseek-flash", "api_style": "responses"},
    "xai": {"base_url": "https://api.x.ai/v1", "model": "grok-4.6", "api_style": "responses"},
    "zhipu": {"base_url": "https://open.bigmodel.cn/api/paas/v4", "model": "glm-4v-flash", "api_style": "chat_completions"},
    "custom": {"base_url": "", "model": "", "api_style": "responses"},
}

# A provider's ``/models`` response is useful discovery data, not necessarily
# a complete entitlement list.  In particular, Zhipu may omit vision models
# that are still accepted by its Chat Completions endpoint.  These are merely
# selectable candidates; the image probe remains the authority.
PROVIDER_MODEL_HINTS: dict[str, tuple[str, ...]] = {
    "zhipu": ("glm-4.6v-flash", "glm-4v-flash", "glm-4v-plus"),
}


def _api_style(settings: ModelSettings) -> str:
    return PROVIDER_PRESETS.get(settings.provider, {}).get("api_style", "responses")


def _error_detail(exc: httpx.HTTPError) -> tuple[int | None, str]:
    response = getattr(exc, "response", None)
    if response is None:
        return None, str(exc)[:300]
    try:
        payload = response.json()
        error = payload.get("error", {}) if isinstance(payload, dict) else {}
        detail = error.get("message") or payload.get("message") or ""
    except (ValueError, TypeError, AttributeError):
        detail = response.text
    return response.status_code, str(detail)[:300]


def list_available_models(settings: ModelSettings, access_token: str | None = None) -> dict:
    """List models authorized by OpenHmi Station for this session.

    In production ``base_url`` points to Station, whose server owns provider
    API keys. The desktop receives only model ids and never provider secrets.
    """
    token = access_token or settings.api_key()
    if not token:
        return {"status": "missing_access_token", "ok": False, "models": []}
    try:
        response = httpx.get(settings.base_url.rstrip("/") + "/models",
                             headers={"Authorization": f"Bearer {token}"}, timeout=30)
        response.raise_for_status()
        body = response.json()
        raw = body.get("data", body.get("models", [])) if isinstance(body, dict) else []
        models = []
        for item in raw if isinstance(raw, list) else []:
            model_id = item.get("id") if isinstance(item, dict) else item
            if model_id and str(model_id) not in models:
                models.append(str(model_id))
        for candidate in PROVIDER_MODEL_HINTS.get(settings.provider, ()):
            if candidate not in models:
                models.append(candidate)
        models.sort()
        return {"status": "connected", "ok": True, "models": models,
                "selected_model_available": settings.model in models,
                "model_discovery": "advisory" if settings.provider in PROVIDER_MODEL_HINTS else "authoritative"}
    except httpx.HTTPError as exc:
        status, detail = _error_detail(exc)
        if settings.provider == "zhipu" and settings.model:
            return {"status": "model_list_unavailable", "ok": True, "models": [settings.model],
                    "selected_model_available": True, "model_source": "configured"}
        return {"status": "error", "ok": False, "models": [],
                "http_status": status, "message": detail}
    except (OSError, ValueError) as exc:
        return {"status": "error", "ok": False, "models": [], "message": str(exc)[:300]}


def _post_model_content(settings: ModelSettings, key: str, content: list[dict], *,
                        max_output_tokens: int, timeout: int) -> dict:
    headers = {"Authorization": f"Bearer {key}"}
    if _api_style(settings) == "chat_completions":
        chat_content = []
        for item in content:
            if item["type"] == "input_text":
                chat_content.append({"type": "text", "text": item["text"]})
            elif item["type"] == "input_image":
                chat_content.append({"type": "image_url", "image_url": {"url": item["image_url"]}})
        response = httpx.post(settings.base_url.rstrip("/") + "/chat/completions", headers=headers,
                              json={"model": settings.model, "max_tokens": max_output_tokens,
                                    "messages": [{"role": "user", "content": chat_content}]}, timeout=timeout)
    else:
        response = httpx.post(settings.base_url.rstrip("/") + "/responses", headers=headers,
                              json={"model": settings.model, "store": False,
                                    "max_output_tokens": max_output_tokens,
                                    "input": [{"role": "user", "content": content}]}, timeout=timeout)
    response.raise_for_status()
    return response.json()


def test_model_connection(settings: ModelSettings, api_key: str | None = None,
                          *, require_vision: bool = True) -> dict:
    """Validate credentials, model selection and optional image input.

    The test uses the same Responses endpoint and payload shape as visual
    regression, so a successful result is stronger than a plain HTTP probe.
    Secrets and response bodies are never persisted.
    """
    key = api_key or settings.api_key()
    if not key:
        return {"status": "missing_access_token", "ok": False, "vision": False}
    available = list_available_models(settings, key)
    if not available.get("ok"):
        return {**available, "vision": False}
    # Station owns the authorization list, so retain its strict allow-list.
    # Direct vendor model discovery is advisory: a real image request decides
    # whether a manually entered or hinted model is usable.
    if settings.deployment == "station" and settings.model not in available.get("models", []):
        return {"status": "model_unavailable", "ok": False, "vision": False,
                "model": settings.model, "models": available.get("models", [])}
    content: list[dict[str, str]] = [{
        "type": "input_text",
        "text": "Connection test. Reply with exactly UAGENT_OK.",
    }]
    if require_vision:
        # Small valid RGB PNG. It proves the same input_image contract used by
        # Browser/SDL comparison; some providers reject minimal alpha fixtures.
        pixel = "iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAIAAACQkWg2AAAAF0lEQVR4nGOQ6/lAEmIY1TCq4cOw1QAAJc2aEFi7X6sAAAAASUVORK5CYII="
        content.append({"type": "input_image", "image_url": f"data:image/png;base64,{pixel}", "detail": "low"})
    try:
        body = _post_model_content(settings, key, content, max_output_tokens=32, timeout=30)
        reply = _output_text(body)
        if not reply:
            return {"status": "empty_model_reply", "ok": False, "vision": False,
                    "provider": settings.provider, "model": settings.model}
        return {"status": "connected", "ok": True, "vision": require_vision,
                "provider": settings.provider, "model": body.get("model", settings.model),
                "response_id": body.get("id"), "models": available.get("models", [])}
    except httpx.HTTPError as exc:
        status, detail = _error_detail(exc)
        return {"status": "error", "ok": False, "vision": False,
                "http_status": status, "message": detail}
    except (OSError, ValueError) as exc:
        return {"status": "error", "ok": False, "vision": False, "message": str(exc)[:300]}


def _output_text(body: dict) -> str:
    choices = body.get("choices")
    if isinstance(choices, list) and choices:
        message = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
        if isinstance(message, dict):
            return str(message.get("content") or message.get("reasoning_content") or "")
    output_text = body.get("output_text")
    if output_text:
        return str(output_text)
    pieces = []
    for item in body.get("output", []):
        for block in item.get("content", []):
            if block.get("type") == "output_text":
                pieces.append(str(block.get("text", "")))
    return "\n".join(pieces)


def _clean_json(text: str) -> str:
    return re.sub(r"^```(?:json)?\s*|\s*```$", "", text or "", flags=re.I | re.S).strip()


def _post_responses(settings: ModelSettings, key: str, prompt: dict,
                    *, max_output_tokens: int = 1600) -> dict:
    """Make a provider-style request while preserving the validated output contract."""
    return _post_model_content(settings, key, [{"type": "input_text", "text": json.dumps(prompt, ensure_ascii=False)}],
                               max_output_tokens=max_output_tokens, timeout=120)


def validate_agent_suggestions(decoded: object, candidates: list[dict]) -> tuple[list[dict], list[str]]:
    """Validate and sanitize Agent JSON against the current candidate set.

    This is deliberately strict.  Unknown keys, source-code-shaped fields,
    unknown target ids, strategies, adapters, and evidence are rejected so an
    Agent response can never become an unreviewed renderer instruction.
    """
    if not isinstance(decoded, dict):
        return [], ["Agent response must be a JSON object"]
    allowed_root = {"schema", "suggestions"}
    unknown_root = sorted(set(decoded) - allowed_root)
    if unknown_root:
        return [], [f"Agent response has unknown keys: {', '.join(unknown_root)}"]
    raw_items = decoded.get("suggestions")
    if not isinstance(raw_items, list):
        return [], ["Agent response suggestions must be a list"]
    by_id = {str(item.get("target_id")): item for item in candidates if isinstance(item, dict)}
    accepted: list[dict] = []
    errors: list[str] = []
    for index, raw in enumerate(raw_items):
        if not isinstance(raw, dict):
            errors.append(f"suggestions[{index}] is not an object")
            continue
        unknown = sorted(set(raw) - AGENT_SUGGESTION_KEYS)
        if unknown:
            errors.append(f"suggestions[{index}] has unknown keys: {', '.join(unknown)}")
            continue
        target_id = str(raw.get("target_id", ""))
        if not target_id or target_id not in by_id:
            errors.append(f"suggestions[{index}] target_id is not an unresolved candidate")
            continue
        candidate = by_id[target_id]
        candidate_source = candidate.get("source_id")
        if raw.get("source_id") is not None and str(raw.get("source_id")) != str(candidate_source):
            errors.append(f"suggestions[{index}] source_id is not source-backed")
            continue
        strategy = str(raw.get("strategy", "")).casefold()
        if strategy not in AGENT_STRATEGIES:
            errors.append(f"suggestions[{index}] strategy is not whitelisted")
            continue
        adapter = str(raw.get("adapter") or candidate.get("adapter") or "")
        if adapter not in AGENT_ADAPTERS:
            errors.append(f"suggestions[{index}] adapter is not whitelisted")
            continue
        evidence = raw.get("evidence_ids", candidate.get("evidence_ids", []))
        if not isinstance(evidence, list) or any(str(item) not in {str(v) for v in candidate.get("evidence_ids", [])} for item in evidence):
            errors.append(f"suggestions[{index}] evidence_ids are not source-backed")
            continue
        try:
            confidence = float(raw.get("confidence", 0))
        except (TypeError, ValueError):
            errors.append(f"suggestions[{index}] confidence is invalid")
            continue
        if not 0 <= confidence <= 1:
            errors.append(f"suggestions[{index}] confidence must be between 0 and 1")
            continue
        fallback_bounds = raw.get("fallback_bounds")
        if fallback_bounds is not None and (not isinstance(fallback_bounds, list) or len(fallback_bounds) != 4):
            errors.append(f"suggestions[{index}] fallback_bounds must be a four-number list")
            continue
        if fallback_bounds is not None:
            try:
                fallback_bounds = [float(value) for value in fallback_bounds]
            except (TypeError, ValueError):
                errors.append(f"suggestions[{index}] fallback_bounds must contain numbers")
                continue
            if fallback_bounds[2] <= 0 or fallback_bounds[3] <= 0:
                errors.append(f"suggestions[{index}] fallback_bounds must have positive size")
                continue
        accepted.append({
            "target_id": target_id,
            "source_id": str(raw.get("source_id") or candidate.get("source_id") or target_id),
            "semantic": str(raw.get("semantic") or "")[:120],
            "target_component": (str(raw["target_component"]) if raw.get("target_component") is not None else None),
            "strategy": strategy,
            "adapter": adapter,
            "confidence": confidence,
            "reason": str(raw.get("reason") or "")[:300],
            "evidence_ids": [str(item) for item in evidence],
            "fallback_bounds": fallback_bounds,
        })
    return accepted, errors


def analyze_capability_candidates(
    settings: ModelSettings,
    candidates: list[dict],
    api_key: str | None = None,
    *,
    browser_verified: bool = True,
) -> dict:
    """Ask the optional Agent to classify only unresolved/low-confidence items."""
    if not settings.agent_is_enabled:
        return {"status": "disabled", "suggestions": [], "calls": 0}
    if not browser_verified:
        return {"status": "awaiting_browser_verification", "suggestions": [], "calls": 0}
    key = api_key or settings.api_key()
    if not key:
        return {"status": "missing_api_key", "suggestions": [], "calls": 0}
    pending = [item for item in candidates if isinstance(item, dict) and
               (str(item.get("support", "")) == "unsupported" or
                float(item.get("confidence", 0) or 0) < .8)]
    if not pending:
        return {"status": "not_needed", "suggestions": [], "calls": 0}
    prompt = {
        "task": "Classify unresolved React/Figma Make evidence using only existing targets.",
        "schema": {"suggestions": [{
            "target_id": "existing candidate id", "semantic": "short label",
            "strategy": "native|compound|fallback|unsupported",
            "adapter": "existing whitelisted adapter", "confidence": "0..1",
            "reason": "evidence-backed reason", "evidence_ids": ["existing evidence id"],
        }]},
        "rules": [
            "Return JSON only; no markdown or extra top-level keys.",
            "Use only the supplied target_id and evidence_ids.",
            "Do not output C/C++, source code, callbacks, patches, new components, or paths.",
            "Do not make a fallback decision without browser-verified local bounds.",
            "Acceptance remains mandatory and this response is advisory only.",
        ],
        "candidates": pending,
    }
    try:
        body = _post_responses(settings, key, prompt, max_output_tokens=1600)
    except (httpx.HTTPError, OSError, ValueError) as exc:
        return {"status": "error", "suggestions": [], "calls": 1, "message": str(exc)[:300]}
    try:
        decoded = json.loads(_clean_json(_output_text(body)))
    except json.JSONDecodeError:
        return {"status": "invalid_response", "suggestions": [], "calls": 1}
    suggestions, errors = validate_agent_suggestions(decoded, pending)
    if errors:
        return {"status": "invalid_response", "suggestions": suggestions, "errors": errors,
                "calls": 1, "model": body.get("model", settings.model), "usage": body.get("usage")}
    return {"status": "completed", "suggestions": suggestions, "calls": 1,
            "model": body.get("model", settings.model), "usage": body.get("usage"),
            "verified": browser_verified}


def analyze_visual_regression(settings: ModelSettings, report: dict,
                              candidates: list[dict], api_key: str | None = None) -> dict:
    """Use paired evidence to refine only existing low-confidence decisions.

    The model cannot emit source, patches or arbitrary rendering parameters.
    Its output passes through the same strict capability validator used by
    preflight, keeping iterative repair generic and auditable.
    """
    if not settings.agent_is_enabled:
        return {"status": "disabled", "suggestions": [], "calls": 0}
    key = api_key or settings.api_key()
    if not key:
        return {"status": "missing_api_key", "suggestions": [], "calls": 0}
    pending = [item for item in candidates if isinstance(item, dict) and
               (str(item.get("support", "")) == "unsupported" or
                float(item.get("confidence", 0) or 0) < .8)]
    if not pending:
        return {"status": "not_needed", "suggestions": [], "calls": 0}
    validation_root = Path(str(report.get("report_file", ""))).parent
    references = sorted((validation_root / "reference").glob("*.png"))[:4]
    simulators = sorted((validation_root / "simulator").glob("*.png"))[:4]
    pairs = list(zip(references, simulators))
    if not pairs:
        return {"status": "missing_evidence", "suggestions": [], "calls": 0}
    prompt = {
        "task": "Compare paired Browser reference and SDL output images, then refine only supplied capability decisions.",
        "schema": {"suggestions": [{"target_id": "existing id", "source_id": "existing source id",
                    "strategy": "native|compound|fallback|unsupported", "adapter": "existing adapter",
                    "confidence": "0..1", "reason": "visual evidence", "evidence_ids": ["existing id"]}]},
        "rules": ["Return JSON only.", "Never output code, patches, file paths or project-specific rules.",
                  "Use only supplied ids, adapters and evidence ids.",
                  "Fallback requires browser-backed bounds; otherwise keep unsupported."],
        "deterministic_gate": report.get("deterministic_gate"),
        "failed_stages": [stage for stage in report.get("stages", []) if not stage.get("passed")],
        "candidates": pending,
    }
    content: list[dict] = [{"type": "input_text", "text": json.dumps(prompt, ensure_ascii=False)}]
    for index, (reference, simulator) in enumerate(pairs):
        for label, path in (("browser", reference), ("sdl", simulator)):
            encoded = base64.b64encode(path.read_bytes()).decode("ascii")
            content.append({"type": "input_text", "text": f"pair {index + 1} {label}"})
            content.append({"type": "input_image", "image_url": f"data:image/png;base64,{encoded}", "detail": "low"})
    try:
        body = _post_model_content(settings, key, content, max_output_tokens=1600, timeout=120)
        decoded = json.loads(_clean_json(_output_text(body)))
    except json.JSONDecodeError:
        return {"status": "invalid_response", "suggestions": [], "calls": 1}
    except (httpx.HTTPError, OSError, ValueError) as exc:
        return {"status": "error", "suggestions": [], "calls": 1, "message": str(exc)[:300]}
    suggestions, errors = validate_agent_suggestions(decoded, pending)
    return {"status": "completed" if not errors else "invalid_response",
            "suggestions": suggestions, "errors": errors, "calls": 1,
            "model": body.get("model", settings.model), "usage": body.get("usage")}


def apply_agent_suggestions(model, suggestions: list[dict], browser_evidence: dict | None = None,
                            *, browser_verified: bool = False) -> dict:
    """Adopt validated suggestions only after explicit browser verification."""
    if not browser_verified or not isinstance(browser_evidence, dict) or browser_evidence.get("status") != "browser-interaction":
        return {"status": "awaiting_browser_verification", "applied": 0, "suggestions": []}
    from core.capability import plan_capabilities
    from core.model import RenderPlan, RenderPlanItem

    # Start from the current browser-backed plan so only decisions that are
    # present in this task can be considered for adoption.
    plan_capabilities(model, browser_evidence)

    candidates = [{"target_id": key, "adapter": value.adapter,
                   "confidence": value.confidence, "support": value.support,
                   "evidence_ids": value.evidence_ids, "source_id": value.source_id}
                  for key, value in model.capability_plan.items()
                  if value.support == "unsupported" or value.confidence < .8]
    valid, errors = validate_agent_suggestions({"suggestions": suggestions}, candidates)
    if errors:
        return {"status": "invalid_response", "applied": 0, "suggestions": [], "errors": errors}
    applied: list[str] = []
    for item in valid:
        current = model.capability_plan.get(item["target_id"])
        if current is None:
            continue
        # The Agent can choose a strategy, but bounds remain those already
        # obtained from the browser; arbitrary raster regions are not adopted.
        if item["strategy"] == "fallback" and not current.fallback_bounds:
            continue
        updated = replace(current, support=item["strategy"], strategy=item["strategy"],
                          adapter=item["adapter"], confidence=item["confidence"],
                          reason=item["reason"] or current.reason,
                          evidence_ids=item["evidence_ids"] or current.evidence_ids)
        model.capability_plan[item["target_id"]] = updated
        applied.append(item["target_id"])
    # Rebuild operation items while retaining source evidence and any newly
    # adopted decisions; no generated source is accepted from the Agent.
    render_plan = RenderPlan()
    for decision in model.capability_plan.values():
        operation = {"native": "native", "compound": "compound",
                     "fallback": "local-screenshot", "unsupported": "blocker"}[decision.support]
        if decision.properties.get("effect_kind") == "glow" and decision.support == "compound":
            operation = "arc-glow-layers"
        render_plan.items.append(RenderPlanItem(
            id=decision.target_id, source_id=decision.source_id or decision.target_id,
            support=decision.support, adapter=decision.adapter, operation=operation,
            evidence_ids=list(decision.evidence_ids), fallback_bounds=decision.fallback_bounds,
            properties={**decision.properties, "reason": decision.reason, "confidence": decision.confidence},
        ))
        if decision.support == "unsupported" and decision.confidence >= .85:
            render_plan.blockers.append(f"{decision.target_id}: {decision.reason}")
    model.render_plan = render_plan
    return {"status": "completed", "applied": len(applied), "suggestions": applied}


def analyze_video_frames(
    settings: ModelSettings,
    frame_paths: list[str],
    api_key: str | None = None,
) -> dict:
    """Ask a vision model for interaction evidence; never invent code changes."""
    key = api_key or settings.api_key()
    if not settings.enabled:
        return {"status": "disabled"}
    if not key:
        return {"status": "missing_api_key"}
    content: list[dict] = [{
        "type": "input_text",
        "text": (
            "These ordered frames are visual evidence for a React/Figma Make UI. "
            "Return JSON only with keys transitions, gestures, animations, controls, "
            "layout_changes, uncertainties. Do not infer behavior not visible in frames. "
            "Each item must include frame indices and confidence from 0 to 1."
        ),
    }]
    for frame_path in frame_paths[:8]:
        path = Path(frame_path)
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
        content.append({"type": "input_image", "image_url": f"data:{mime};base64,{encoded}", "detail": "low"})
    payload = {
        "model": settings.model,
        "store": False,
        "max_output_tokens": 2000,
        "input": [{"role": "user", "content": content}],
    }
    endpoint = settings.base_url.rstrip("/") + "/responses"
    response = httpx.post(endpoint, headers={"Authorization": f"Bearer {key}"}, json=payload, timeout=120)
    response.raise_for_status()
    body = response.json()
    output_text = body.get("output_text")
    if not output_text:
        pieces = []
        for item in body.get("output", []):
            for block in item.get("content", []):
                if block.get("type") == "output_text":
                    pieces.append(block.get("text", ""))
        output_text = "\n".join(pieces)
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", output_text or "", flags=re.I | re.S)
    try:
        analysis = json.loads(cleaned)
    except json.JSONDecodeError:
        analysis = {"raw": output_text, "uncertainties": ["模型未返回可解析 JSON"]}
    return {
        "status": "completed",
        "model": body.get("model", settings.model),
        "response_id": body.get("id"),
        "usage": body.get("usage"),
        "analysis": analysis,
    }


def analyze_navigation_candidates(
    settings: ModelSettings,
    navigation: dict,
    component_names: list[str],
    api_key: str | None = None,
) -> dict:
    """Resolve ambiguous navigation as constrained JSON; never emit or edit code."""
    key = api_key or settings.api_key()
    if not settings.enabled:
        return {"status": "disabled", "suggestions": []}
    if not key:
        return {"status": "missing_api_key", "suggestions": []}
    pending = [item for item in navigation.get("items", []) if item.get("status") != "confirmed"]
    if not pending:
        return {"status": "not_needed", "suggestions": []}
    prompt = {
        "task": "Map navigation targets to existing React page components using only supplied evidence.",
        "rules": [
            "Return JSON only: {suggestions:[{navigation_id,target_component,confidence,reason}]}",
            "target_component must exactly equal one candidate or be null",
            "Do not generate source code, C code, callbacks, or new component names",
            "Use null and low confidence when evidence is insufficient",
        ],
        "component_candidates": component_names,
        "navigation_candidates": pending,
    }
    payload = {
        "model": settings.model, "store": False, "max_output_tokens": 1200,
        "input": [{"role": "user", "content": [{"type": "input_text", "text": json.dumps(prompt, ensure_ascii=False)}]}],
    }
    response = httpx.post(settings.base_url.rstrip("/") + "/responses",
                          headers={"Authorization": f"Bearer {key}"}, json=payload, timeout=120)
    response.raise_for_status()
    body = response.json()
    output_text = body.get("output_text") or "\n".join(
        block.get("text", "") for item in body.get("output", [])
        for block in item.get("content", []) if block.get("type") == "output_text"
    )
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", output_text, flags=re.I | re.S)
    try:
        decoded = json.loads(cleaned)
    except json.JSONDecodeError:
        return {"status": "invalid_response", "suggestions": []}
    known = set(component_names)
    pending_ids = {str(item.get("id")) for item in pending}
    suggestions = []
    for item in decoded.get("suggestions", []) if isinstance(decoded, dict) else []:
        target = item.get("target_component")
        nav_id = str(item.get("navigation_id", ""))
        if nav_id not in pending_ids or (target is not None and target not in known):
            continue
        suggestions.append({
            "navigation_id": nav_id, "target_component": target,
            "confidence": max(0.0, min(1.0, float(item.get("confidence", 0)))),
            "reason": str(item.get("reason", ""))[:300],
        })
    return {"status": "completed", "model": body.get("model", settings.model),
            "suggestions": suggestions, "usage": body.get("usage")}
