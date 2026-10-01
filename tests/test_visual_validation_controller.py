from pathlib import Path

from core.model_provider import ModelSettings, PROVIDER_PRESETS, test_model_connection as probe_model_connection
import pytest

from tools.cross_validate import _json_summary, cross_validate


class _Response:
    status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return {"id": "resp-test", "model": "vision-test", "output_text": "UAGENT_OK", "output": []}


class _ModelsResponse(_Response):
    def json(self):
        return {"data": [{"id": "vision-test"}, {"id": "text-only"}]}


def test_provider_presets_include_deepseek_and_xai():
    assert PROVIDER_PRESETS["deepseek"]["base_url"] == "https://api.deepseek.com"
    assert PROVIDER_PRESETS["xai"]["base_url"] == "https://api.x.ai/v1"
    assert PROVIDER_PRESETS["zhipu"]["api_style"] == "chat_completions"
    assert PROVIDER_PRESETS["zhipu"]["model"] == "glm-4v-flash"


def test_zhipu_model_hints_are_offered_when_remote_listing_omits_them(monkeypatch):
    monkeypatch.setattr("core.model_provider.httpx.get", lambda *args, **kwargs: _ModelsResponse())
    from core.model_provider import list_available_models
    result = list_available_models(ModelSettings(provider="zhipu", model="glm-4v-flash"), "secret")
    assert result["model_discovery"] == "advisory"
    assert "glm-4.6v-flash" in result["models"]


def test_connection_probe_uses_responses_and_image(monkeypatch):
    captured = {}

    def post(url, **kwargs):
        captured.update(url=url, **kwargs)
        return _Response()

    def get(url, **kwargs):
        captured["models_url"] = url
        captured["models_headers"] = kwargs["headers"]
        return _ModelsResponse()

    monkeypatch.setattr("core.model_provider.httpx.post", post)
    monkeypatch.setattr("core.model_provider.httpx.get", get)
    result = probe_model_connection(
        ModelSettings(enabled=True, base_url="https://example.test/v1",
                      model="vision-test", provider="custom"),
        "secret", require_vision=True,
    )
    assert result["ok"] is True
    assert captured["models_url"] == "https://example.test/v1/models"
    assert result["models"] == ["text-only", "vision-test"]
    assert captured["url"] == "https://example.test/v1/responses"
    content = captured["json"]["input"][0]["content"]
    assert any(item["type"] == "input_image" for item in content)
    assert captured["headers"]["Authorization"] == "Bearer secret"


def test_zhipu_probe_uses_chat_completions_and_accepts_reasoning_content(monkeypatch):
    captured = {}

    class _ChatResponse(_Response):
        def json(self):
            return {"id": "chat-test", "model": "glm-4.7-flash",
                    "choices": [{"message": {"content": "", "reasoning_content": "UAGENT_OK"}}]}

    def post(url, **kwargs):
        captured.update(url=url, **kwargs)
        return _ChatResponse()

    monkeypatch.setattr("core.model_provider.httpx.post", post)
    monkeypatch.setattr("core.model_provider.httpx.get", lambda *args, **kwargs: type("Models", (), {
        "raise_for_status": lambda self: None,
        "json": lambda self: {"data": [{"id": "glm-4.7-flash"}]},
    })())
    result = probe_model_connection(
        ModelSettings(enabled=True, base_url="https://open.bigmodel.cn/api/paas/v4",
                      model="glm-4.7-flash", provider="zhipu"),
        "secret", require_vision=True,
    )
    assert result["ok"] is True
    assert captured["url"] == "https://open.bigmodel.cn/api/paas/v4/chat/completions"
    content = captured["json"]["messages"][0]["content"]
    assert any(item["type"] == "image_url" for item in content)


def test_connection_rejects_model_not_authorized_by_station(monkeypatch):
    monkeypatch.setattr("core.model_provider.httpx.get", lambda *args, **kwargs: _ModelsResponse())
    result = probe_model_connection(
        ModelSettings(base_url="https://station.example/v1", model="unknown", provider="station"),
        "session-token", require_vision=True,
    )
    assert result["status"] == "model_unavailable"
    assert result["ok"] is False


def test_summary_exports_only_after_deterministic_gate(tmp_path):
    validation = {"status": "passed", "completed": True, "errors": [], "stages": [],
                  "deterministic_gate": {"passed": True, "confidence": .91, "threshold": .90}}
    summary = _json_summary(Path("source"), None, tmp_path, {"status": "browser-interaction", "screens": [{}]},
                            validation, [], [{"round": 1, "passed": True}])
    assert summary["export_ready"] is True
    assert summary["validation"]["deterministic_gate"]["confidence"] == .91


def test_visual_gate_cannot_be_lowered_below_ninety_percent(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    with pytest.raises(ValueError, match="fixed at 0.90"):
        cross_validate(source, tmp_path / "output", confidence_threshold=.85)
