import json

import pytest

from drift_agent.ai import foundry_client as module
from drift_agent.ai.foundry_client import (
    FakeFoundryClient,
    FoundryUnavailable,
    HttpFoundryClient,
    ModelError,
    build_system_prompt,
    build_user_message,
    parse_analysis,
)
from drift_agent.models import AnalysisSource, RiskLevel
from drift_agent.settings import FoundrySettings

VALID = {
    "summary": "tags changed",
    "probable_cause": "manual edit",
    "risk_recommendation": "low",
    "proposed_change": "",
    "confidence": 0.8,
    "manual_review_reason": None,
}


def settings(**overrides) -> FoundrySettings:
    base = {
        "endpoint": "https://example.services.ai.azure.com",
        "deployment": "gpt-4o",
        "api_key": "placeholder-key",
    }
    base.update(overrides)
    return FoundrySettings(**base)


def test_system_prompt_states_the_model_is_not_the_detector():
    prompt = build_system_prompt("SKILL BODY")
    assert "NOT the" in prompt
    assert "SKILL BODY" in prompt
    assert "never" in prompt.lower()


def test_user_message_redacts_credentials_in_repository_context():
    message = build_user_message(
        {"resources": []}, 'password = "hunter2hunter2"\nclient_secret: abcdef123456'
    )
    assert "hunter2hunter2" not in message
    assert "abcdef123456" not in message
    assert "[redacted]" in message


def test_parse_analysis_accepts_plain_and_fenced_json():
    plain = parse_analysis(json.dumps(VALID), deployment="gpt-4o", usage={"prompt_tokens": 7})
    assert plain.risk_recommendation is RiskLevel.LOW
    assert plain.source is AnalysisSource.MODEL
    assert plain.prompt_tokens == 7
    fenced = parse_analysis("```json\n" + json.dumps(VALID) + "\n```")
    assert fenced.summary == "tags changed"


def test_parse_analysis_rejects_bad_output():
    for content in ("not json", "[]", json.dumps({"confidence": "high"})):
        with pytest.raises(ModelError):
            parse_analysis(content)


def test_unconfigured_client_is_unavailable():
    with pytest.raises(FoundryUnavailable):
        HttpFoundryClient(FoundrySettings()).analyze(
            drift_facts={}, skill="", repository_context=""
        )


def test_missing_credential_is_unavailable():
    client = HttpFoundryClient(settings(api_key=None), bearer_provider=lambda: None)
    with pytest.raises(FoundryUnavailable):
        client.analyze(drift_facts={}, skill="", repository_context="")


def test_http_client_parses_structured_response(monkeypatch):
    captured = {}

    def fake_request(method, url, *, headers=None, payload=None, timeout=30, accepted=(200,)):
        captured.update(url=url, headers=headers, payload=payload)
        return {
            "choices": [{"message": {"content": json.dumps(VALID)}}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 4},
        }

    monkeypatch.setattr(module, "request_json", fake_request)
    analysis = HttpFoundryClient(settings()).analyze(
        drift_facts={"resources": [{"address": "a"}]}, skill="skill", repository_context="ctx"
    )

    assert analysis.summary == "tags changed"
    assert analysis.prompt_tokens == 11
    assert analysis.completion_tokens == 4
    assert captured["headers"]["api-key"] == "placeholder-key"
    assert "openai/deployments/gpt-4o/chat/completions" in captured["url"]
    assert captured["payload"]["temperature"] == 0
    assert captured["payload"]["response_format"] == {"type": "json_object"}


def test_http_failure_does_not_leak_response_body(monkeypatch):
    from drift_agent.http import HttpError

    def fake_request(*_args, **_kwargs):
        raise HttpError(401, "http_error")

    monkeypatch.setattr(module, "request_json", fake_request)
    with pytest.raises(ModelError) as caught:
        HttpFoundryClient(settings()).analyze(drift_facts={}, skill="", repository_context="")
    assert caught.value.reason == "foundry_request_failed"
    assert caught.value.status == 401


def test_fake_client_records_calls():
    client = FakeFoundryClient(VALID)
    result = client.analyze(drift_facts={"a": 1}, skill="s", repository_context="c")
    assert result.summary == "tags changed"
    assert len(client.calls) == 1
    assert client.calls[0]["drift_facts"] == {"a": 1}
