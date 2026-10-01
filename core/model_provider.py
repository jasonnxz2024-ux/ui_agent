"""Compatibility shims for the retired remote model integration.

Conversion planning and verification are local and deterministic.  These
functions remain import-compatible with older callers but never read secrets,
contact a provider, or apply model-generated changes.
"""
from dataclasses import dataclass
import json
from pathlib import Path


@dataclass
class ModelSettings:
    enabled: bool = False
    base_url: str = ""
    model: str = ""
    api_key_env: str = ""
    agent_enabled: bool | None = False
    provider: str = "disabled"
    deployment: str = "local"

    @property
    def agent_is_enabled(self) -> bool:
        return False

    def api_key(self) -> None:
        return None


PROVIDER_PRESETS: dict[str, dict[str, str]] = {}


def load_settings(path: Path) -> ModelSettings:
    return ModelSettings()


def save_public_settings(settings: ModelSettings, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"enabled": False, "agent_enabled": False,
                                "deployment": "local"}, indent=2), encoding="utf-8")


def _disabled() -> dict:
    return {"status": "disabled", "reason": "Remote model APIs were removed; use local planning.",
            "suggestions": [], "calls": 0}


def list_available_models(settings: ModelSettings, access_token: str | None = None) -> dict:
    return {**_disabled(), "ok": False, "models": []}


def test_model_connection(settings: ModelSettings, api_key: str | None = None,
                          *, require_vision: bool = True) -> dict:
    return {**_disabled(), "ok": False, "vision": False}


def validate_agent_suggestions(decoded: object, candidates: list[dict]) -> tuple[list[dict], list[str]]:
    return [], ["Remote model suggestions are disabled."]


def analyze_capability_candidates(settings: ModelSettings, candidates: list[dict],
                                  api_key: str | None = None, *,
                                  browser_verified: bool = True) -> dict:
    return _disabled()


def analyze_visual_regression(settings: ModelSettings, report: dict,
                              candidates: list[dict], api_key: str | None = None) -> dict:
    return _disabled()


def analyze_video_frames(settings: ModelSettings, frames: list[dict],
                         api_key: str | None = None) -> dict:
    return _disabled()


def analyze_navigation_candidates(settings: ModelSettings, navigation: dict,
                                  candidates: list[dict], api_key: str | None = None) -> dict:
    return _disabled()


def apply_agent_suggestions(model, suggestions: list[dict], browser_evidence: dict | None = None,
                            *, browser_verified: bool = False) -> dict:
    return {**_disabled(), "applied": False}
