import sys
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import server.app as server_app
from core.model_provider import ModelSettings, analyze_video_frames


def test_disabled_video_model_never_needs_a_key():
    assert analyze_video_frames(ModelSettings(enabled=False), []) == {"status": "disabled"}


def test_public_model_settings_do_not_persist_station_token(tmp_path, monkeypatch):
    settings_path = tmp_path / "settings.json"
    monkeypatch.setattr(server_app, "SETTINGS_PATH", settings_path)
    monkeypatch.setattr(server_app, "SERVICE_ACCESS_TOKEN", None)
    monkeypatch.setattr(server_app, "list_available_models",
                        lambda settings, token: {"ok": True, "models": ["vision-model"]})
    client = TestClient(server_app.app)

    response = client.post("/api/settings", json={
        "enabled": True,
        "agent_enabled": True,
        "deployment": "station",
        "provider": "station",
        "base_url": "https://station.example/v1",
        "model": "vision-model",
        "station_token": "temporary-session-token",
    })
    assert response.status_code == 200
    assert "temporary-session-token" not in settings_path.read_text(encoding="utf-8")
    assert client.get("/api/settings").json()["station_token_configured"] is True


def test_desktop_rejects_direct_provider_api_keys(tmp_path, monkeypatch):
    monkeypatch.setattr(server_app, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.delenv("UAGENT_INTERNAL_BYOK", raising=False)
    client = TestClient(server_app.app)
    response = client.post("/api/settings", json={
        "enabled": True, "agent_enabled": True, "deployment": "station",
        "base_url": "https://api.openai.com/v1", "model": "gpt-5.6-terra",
        "station_token": "must-not-be-used-as-provider-key",
    })
    assert response.status_code == 400
    assert "禁止直连" in str(response.json())


def test_internal_byok_lists_key_authorized_models_without_persisting_key(tmp_path, monkeypatch):
    settings_path = tmp_path / "settings.json"
    monkeypatch.setenv("UAGENT_INTERNAL_BYOK", "1")
    monkeypatch.setattr(server_app, "SETTINGS_PATH", settings_path)
    monkeypatch.setattr(server_app, "SERVICE_ACCESS_TOKEN", None)
    monkeypatch.setattr(server_app, "list_available_models",
                        lambda settings, token: {"ok": True, "models": ["grok-vision", "grok-fast"]})
    client = TestClient(server_app.app)
    models = client.post("/api/settings/models", json={
        "deployment": "internal_byok", "provider": "xai",
        "base_url": "https://api.x.ai/v1", "api_key": "internal-secret",
    })
    assert models.status_code == 200
    assert models.json()["models"] == ["grok-vision", "grok-fast"]
    saved = client.post("/api/settings", json={
        "enabled": True, "agent_enabled": True, "deployment": "internal_byok",
        "provider": "xai", "base_url": "https://api.x.ai/v1",
        "model": "grok-vision", "api_key": "internal-secret",
    })
    assert saved.status_code == 200
    assert saved.json()["api_key_configured"] is True
    assert "internal-secret" not in settings_path.read_text(encoding="utf-8")
    public = client.get("/api/settings").json()
    assert public["allow_internal_byok"] is True
    assert public["deployment"] == "internal_byok"
