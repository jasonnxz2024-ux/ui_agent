"""Local API consumed by the modern UAgent frontend."""
from pathlib import Path
from uuid import uuid4
import mimetypes
import os
import sys
import httpx
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from core.pipeline import analyze, write_report
from core.preflight import inspect_source
from core.video import analyze_video, write_evidence
from core.model_provider import (ModelSettings, analyze_capability_candidates,
                                 analyze_navigation_candidates, analyze_video_frames,
                                 apply_agent_suggestions, load_settings, save_public_settings,
                                 test_model_connection, list_available_models, PROVIDER_PRESETS)
from generator.compiler import compile_lvgl, write_aibuilder_custom
from core.browser_layout import capture_layout
from tools.cross_validate import cross_validate

app = FastAPI(title="UAgent", version="0.1.0")
mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("text/css", ".css")
SOURCE_ROOT = Path(__file__).resolve().parents[1]
BUNDLE_ROOT = Path(getattr(sys, "_MEIPASS", SOURCE_ROOT))
if getattr(sys, "frozen", False):
    default_state_root = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "UAgent"
else:
    default_state_root = SOURCE_ROOT
STATE_ROOT = Path(os.environ.get("UAGENT_DATA_ROOT", str(default_state_root)))
WORKSPACES = STATE_ROOT / "workspaces"
TASKS = {}
SETTINGS_PATH = STATE_ROOT / "uagent.settings.json"
SERVICE_ACCESS_TOKEN: str | None = None


def _internal_byok_enabled() -> bool:
    return os.environ.get("UAGENT_INTERNAL_BYOK") == "1" or (BUNDLE_ROOT / "internal-byok.enabled").is_file()


def _api_only_build() -> bool:
    """The internal release contains the BYOK marker and deliberately omits Station mode."""
    return (BUNDLE_ROOT / "internal-byok.enabled").is_file()


def _direct_provider_forbidden(base_url: str) -> bool:
    """Block provider API endpoints in distributed desktop mode."""
    if _internal_byok_enabled():
        return False
    host = base_url.casefold()
    return any(value in host for value in ("api.openai.com", "api.deepseek.com", "api.x.ai"))


class SourceRequest(BaseModel):
    source_dir: str


@app.post("/api/preflight")
def preflight_source(request: SourceRequest):
    """Report generation readiness without writing an output project."""
    report = inspect_source(request.source_dir)
    settings = load_settings(SETTINGS_PATH)
    browser_verified = False
    source = Path(request.source_dir).resolve()
    for evidence_file in (source / "browser-layout-v2.json", source / "browser-layout.json"):
        if not evidence_file.is_file():
            continue
        try:
            import json
            cached = json.loads(evidence_file.read_text(encoding="utf-8"))
            browser_verified = cached.get("status") == "browser-interaction"
            if browser_verified:
                break
        except (OSError, ValueError, TypeError):
            continue
    capability_items = list(report.get("capability_candidates", []))
    secure_model_route = ((settings.deployment == "station" and not _direct_provider_forbidden(settings.base_url)) or
                          (settings.deployment == "internal_byok" and _internal_byok_enabled()))
    if capability_items and secure_model_route:
        try:
            report["agent"] = analyze_capability_candidates(
                settings, capability_items, SERVICE_ACCESS_TOKEN, browser_verified=browser_verified
            )
        except Exception as exc:
            report["agent"] = {"status": "failed", "suggestions": [], "calls": 0,
                                "error": str(exc)[:300]}
    elif not secure_model_route:
        report["agent"] = {"status": "station_required", "suggestions": [], "calls": 0}
    else:
        report["agent"] = {"status": "not_needed", "suggestions": [], "calls": 0}
    navigation = report.get("navigation", {})
    if secure_model_route and isinstance(navigation, dict) and navigation.get("needs_verification"):
        try:
            navigation["agent"] = analyze_navigation_candidates(
                settings, navigation, list(navigation.get("component_candidates", [])), SERVICE_ACCESS_TOKEN
            )
        except Exception as exc:
            navigation["agent"] = {"status": "failed", "suggestions": [], "error": str(exc)}
    return report


class GenerateRequest(BaseModel):
    task_id: str
    output_dir: str | None = None


class SettingsRequest(BaseModel):
    enabled: bool = False
    agent_enabled: bool | None = None
    base_url: str = ""
    model: str = ""
    station_token: str | None = None
    api_key: str | None = None
    provider: str = "station"
    deployment: str = "station"


@app.get("/api/health")
def health():
    return {"ok": True, "service": "uagent"}


@app.get("/api/settings")
def get_settings():
    settings = load_settings(SETTINGS_PATH)
    safe_station = settings.deployment == "station" and not _direct_provider_forbidden(settings.base_url)
    safe_internal = settings.deployment == "internal_byok" and _internal_byok_enabled()
    return {
        "enabled": settings.enabled,
        "agent_enabled": settings.agent_is_enabled,
        "agent_status": "enabled" if settings.agent_is_enabled else "disabled",
        "base_url": settings.base_url if safe_station or safe_internal else "",
        "model": settings.model if safe_station or safe_internal else "",
        "provider": settings.provider,
        "deployment": settings.deployment if safe_station or safe_internal else "station",
        "api_only": _api_only_build(),
        "allow_internal_byok": _internal_byok_enabled(),
        "provider_presets": PROVIDER_PRESETS if _internal_byok_enabled() else {},
        "station_token_configured": bool(SERVICE_ACCESS_TOKEN or (settings.api_key() if safe_station else None)),
        "api_key_configured": bool(SERVICE_ACCESS_TOKEN) if safe_internal else False,
    }


@app.post("/api/settings")
def update_settings(request: SettingsRequest):
    global SERVICE_ACCESS_TOKEN
    internal = request.deployment == "internal_byok"
    if _api_only_build() and not internal:
        raise HTTPException(400, "此内部构建只支持厂商 API Key 直连")
    if internal and not _internal_byok_enabled():
        raise HTTPException(403, "当前构建未启用内部 BYOK 模式")
    if not internal and request.deployment != "station":
        raise HTTPException(400, "未知模型部署模式")
    if not internal and _direct_provider_forbidden(request.base_url):
        raise HTTPException(400, "客户端禁止直连模型厂商；请填写 OpenHmi Station 地址")
    if internal and request.provider not in PROVIDER_PRESETS:
        raise HTTPException(400, "未知模型服务商")
    settings = ModelSettings(enabled=request.enabled, base_url=request.base_url.strip(),
                             model=request.model.strip(), agent_enabled=request.agent_enabled,
                             provider=request.provider if internal else "station",
                             deployment=request.deployment)
    token = (request.api_key if internal else request.station_token) or SERVICE_ACCESS_TOKEN or settings.api_key()
    available = list_available_models(settings, token)
    strict_model_list = request.deployment == "station"
    if not available.get("ok") or (strict_model_list and request.model not in available.get("models", [])):
        raise HTTPException(400, {"message": "所选模型不在 Station 授权列表中", "models": available})
    save_public_settings(settings, SETTINGS_PATH)
    supplied = request.api_key if internal else request.station_token
    if supplied:
        SERVICE_ACCESS_TOKEN = supplied.strip()
    return {"ok": True, "enabled": settings.enabled,
            "agent_enabled": settings.agent_is_enabled,
            "agent_status": "enabled" if settings.agent_is_enabled else "disabled",
            "station_token_configured": bool(SERVICE_ACCESS_TOKEN or settings.api_key()) if not internal else False,
            "api_key_configured": bool(SERVICE_ACCESS_TOKEN) if internal else False}


class SettingsTestRequest(BaseModel):
    provider: str = "station"
    base_url: str
    model: str
    station_token: str | None = None
    api_key: str | None = None
    deployment: str = "station"
    require_vision: bool = True


@app.post("/api/settings/test")
def test_settings(request: SettingsTestRequest):
    """Test Station auth, authorized model selection and image input."""
    internal = request.deployment == "internal_byok"
    if _api_only_build() and not internal:
        raise HTTPException(400, "此内部构建只支持厂商 API Key 直连")
    if internal and not _internal_byok_enabled():
        raise HTTPException(403, "当前构建未启用内部 BYOK 模式")
    if not internal and _direct_provider_forbidden(request.base_url):
        raise HTTPException(400, "客户端禁止直连模型厂商；请填写 OpenHmi Station 地址")
    settings = ModelSettings(enabled=True, base_url=request.base_url.strip(),
                             model=request.model.strip(), agent_enabled=True,
                             provider=request.provider if internal else "station", deployment=request.deployment)
    token = (request.api_key if internal else request.station_token) or SERVICE_ACCESS_TOKEN
    result = test_model_connection(settings, token,
                                   require_vision=request.require_vision)
    if not result.get("ok"):
        raise HTTPException(400, result)
    return result


class ModelsRequest(BaseModel):
    base_url: str
    station_token: str | None = None
    api_key: str | None = None
    provider: str = "station"
    deployment: str = "station"


@app.post("/api/settings/models")
def available_models(request: ModelsRequest):
    """Return only models authorized by Station for the current user."""
    internal = request.deployment == "internal_byok"
    if _api_only_build() and not internal:
        raise HTTPException(400, "此内部构建只支持厂商 API Key 直连")
    if internal and not _internal_byok_enabled():
        raise HTTPException(403, "当前构建未启用内部 BYOK 模式")
    if not internal and _direct_provider_forbidden(request.base_url):
        raise HTTPException(400, "客户端禁止直连模型厂商；请填写 OpenHmi Station 地址")
    provider = request.provider if internal else "station"
    settings = ModelSettings(base_url=request.base_url.strip(), provider=provider,
                             model=PROVIDER_PRESETS.get(provider, {}).get("model", ""),
                             deployment=request.deployment)
    token = (request.api_key if internal else request.station_token) or SERVICE_ACCESS_TOKEN
    result = list_available_models(settings, token)
    if not result.get("ok"):
        raise HTTPException(400, result)
    return result


class VisualValidationRequest(BaseModel):
    source_dir: str
    output_dir: str
    simulator_exe: str | None = None
    max_rounds: int = 5


@app.post("/api/validate/visual")
def visual_validation(request: VisualValidationRequest):
    """Run persistent Browser/SDL comparison with a fixed 90% gate."""
    settings = load_settings(SETTINGS_PATH)
    if not ((settings.deployment == "station" and not _direct_provider_forbidden(settings.base_url)) or
            (settings.deployment == "internal_byok" and _internal_byok_enabled())):
        raise HTTPException(409, "视觉增强验证仅允许通过 OpenHmi Station")
    key = SERVICE_ACCESS_TOKEN or settings.api_key()
    if not settings.enabled or not settings.agent_is_enabled or not key:
        raise HTTPException(409, "视觉增强验证需要启用功能并配置 OpenHmi Station 会话")
    connection = test_model_connection(settings, key, require_vision=True)
    if not connection.get("ok"):
        raise HTTPException(400, {"message": "模型连接测试失败", "connection": connection})
    output = Path(request.output_dir).expanduser().resolve()
    simulator = request.simulator_exe
    if not simulator:
        candidate = output / "simulator" / "build" / "main.exe"
        simulator = str(candidate) if candidate.is_file() else None
    try:
        return cross_validate(request.source_dir, output, simulator_exe=simulator,
                              settings=settings, api_key=key,
                              max_rounds=request.max_rounds, visible_browser=True,
                              confidence_threshold=.90)
    except (OSError, RuntimeError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc


class AgentApplyRequest(BaseModel):
    task_id: str
    suggestions: list[dict]
    browser_verified: bool = False


@app.post("/api/agent/apply")
def apply_agent(request: AgentApplyRequest):
    """Adopt advisory Agent decisions only after a verified browser capture."""
    model = TASKS.get(request.task_id)
    if model is None:
        raise HTTPException(404, "分析任务不存在或服务已重启，请重新分析源码")
    result = apply_agent_suggestions(
        model, request.suggestions, model.browser_evidence,
        browser_verified=request.browser_verified,
    )
    if result.get("status") == "awaiting_browser_verification":
        raise HTTPException(409, result)
    return result


@app.get("/api/dialog/source")
def choose_source_directory():
    """Open a native Windows directory picker for the local desktop workflow."""
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        selected = filedialog.askdirectory(title="选择 React / Figma Make 源码目录")
        root.destroy()
    except Exception as exc:  # pragma: no cover - depends on desktop session
        raise HTTPException(503, f"无法打开本机目录选择器：{exc}") from exc
    return {"path": selected}


@app.get("/api/dialog/output")
def choose_output_directory():
    """Open a native picker for the generated project destination."""
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk(); root.withdraw(); root.attributes("-topmost", True)
        selected = filedialog.askdirectory(title="选择 AiBuilder 工程输出目录")
        root.destroy()
    except Exception as exc:  # pragma: no cover
        raise HTTPException(503, f"无法打开本机目录选择器：{exc}") from exc
    return {"path": selected}


@app.get("/api/history")
def history():
    """Return clickable local generation history without exposing file contents."""
    items = []
    if WORKSPACES.is_dir():
        for task_dir in sorted(WORKSPACES.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            report = task_dir / "analysis.json"
            project = next(task_dir.glob("aibuilder/*.aicpro"), None)
            if report.is_file() or project:
                items.append({"task_id": task_dir.name, "path": str(project or task_dir), "project": project.name if project else ""})
    return {"items": items[:50]}


@app.post("/api/analyze")
def analyze_source(request: SourceRequest):
    source = Path(request.source_dir)
    if not source.is_dir():
        raise HTTPException(400, "源码目录不存在")
    preflight = inspect_source(source)
    if preflight.get("status") == "blocked":
        raise HTTPException(400, {"message": "源码前置检测未通过", "preflight": preflight})
    task_id = uuid4().hex
    task_dir = WORKSPACES / task_id
    model = analyze(source)
    TASKS[task_id] = model
    report = write_report(model, task_dir / "analysis.json")
    return {
        "task_id": task_id,
        "report": str(report),
        "preflight": preflight,
        "summary": {
            "resources": [{"id": item.id, "kind": item.kind, "path": str(item.path)} for item in model.resources.values()],
            "components": [{"id": item.id, "kind": item.kind, "source_file": item.source_file} for item in model.components.values()],
            "events": [{"id": item.id, "kind": item.kind, "handler": item.handler} for item in model.events.values()],
            "warnings": model.warnings,
        },
    }


@app.post("/api/video")
async def video(file: UploadFile = File(...), task_id: str | None = Form(None)):
    task_id = task_id or uuid4().hex
    task_dir = WORKSPACES / task_id / "video"
    upload_dir = task_dir / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    safe_name = Path(file.filename or "evidence.mp4").name
    video_path = upload_dir / safe_name
    with video_path.open("wb") as stream:
        while chunk := await file.read(1024 * 1024):
            stream.write(chunk)
    try:
        evidence = analyze_video(video_path, task_dir / "frames")
    except (OSError, RuntimeError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    report = write_evidence(evidence, task_dir / "evidence.json")
    settings = load_settings(SETTINGS_PATH)
    model_analysis = {"status": "disabled"}
    if settings.enabled and ((settings.deployment == "station" and not _direct_provider_forbidden(settings.base_url)) or
                             (settings.deployment == "internal_byok" and _internal_byok_enabled())):
        try:
            model_analysis = analyze_video_frames(settings, evidence.keyframes, SERVICE_ACCESS_TOKEN)
        except (httpx.HTTPError, OSError, ValueError) as exc:
            model_analysis = {"status": "error", "message": str(exc)}
    if task_id in TASKS:
        TASKS[task_id].video_evidence = {
            "report": str(report), "frames": evidence.keyframes,
            "duration": evidence.duration, "fps": evidence.fps,
            "model_analysis": model_analysis,
        }
    return {"task_id": task_id, "report": str(report), "frames": len(evidence.keyframes), "model_analysis": model_analysis}


@app.post("/api/layout")
def capture_source_layout(request: SourceRequest):
    """Capture computed browser geometry before conversion.

    This is deliberately separate from static analysis so the UI can show
    whether coordinates came from a real browser or from the fallback scanner.
    """
    source = Path(request.source_dir)
    if not source.is_dir():
        raise HTTPException(400, "源码目录不存在")
    try:
        evidence = capture_layout(source)
    except Exception as exc:
        raise HTTPException(400, f"浏览器布局采集失败：{exc}") from exc
    if evidence is None:
        return {"status": "static-fallback", "reason": "项目缺少可运行的 Vite/node_modules 或 dist/index.html"}
    return {"status": "browser", "viewport": evidence["data"]["viewport"],
            "root": evidence["data"]["root"], "nodes": len(evidence["data"]["nodes"]),
            "evidence": evidence}


@app.post("/api/generate")
def generate(request: GenerateRequest):
    model = TASKS.get(request.task_id)
    if model is None:
        raise HTTPException(404, "分析任务不存在或服务已重启，请重新分析源码")
    if request.output_dir:
        selected = Path(request.output_dir).expanduser()
        # UI asks for the project root, while the writer accepts the custom
        # source directory.  Normalize here so resources never escape into a
        # drive-level ``D:\resources`` folder.
        destination = selected if selected.name.lower() == "custom" and selected.parent.name.lower() == "ui_builder" else selected / "ui_builder" / "custom"
    else:
        destination = WORKSPACES / request.task_id / "aibuilder" / "ui_builder" / "custom"
    bundle = compile_lvgl(model)
    paths = write_aibuilder_custom(bundle, destination, model, include_runtime=True)
    validation = {}
    if paths.get("validation_report") and paths["validation_report"].is_file():
        import json
        validation = json.loads(paths["validation_report"].read_text(encoding="utf-8"))
    return {
        "task_id": request.task_id,
        "output": {key: str(value) for key, value in paths.items()},
        "units": len(bundle.units),
        "warnings": bundle.warnings,
        "layout_source": "browser" if paths.get("browser_layout") and '"status": "static-fallback"' not in paths["browser_layout"].read_text(encoding="utf-8") else "static-fallback",
        "validation_status": validation.get("status", "failed"),
        "completed": bool(validation.get("completed", False)),
        "validation_errors": validation.get("errors", ["验收报告缺失"]),
    }


FRONTEND_DIST = BUNDLE_ROOT / "frontend" / "dist"
if FRONTEND_DIST.is_dir():
    app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="frontend")
