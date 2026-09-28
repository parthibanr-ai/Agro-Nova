"""Vendor order OpenAI -> Anthropic -> Gemini: first to answer wins, photos are passed on, failures fall through."""

import httpx
import pytest
from google import genai
from google.genai import errors, types

from app.core.config import get_settings
from app.services import gemini_client

OPENAI_OK = {"choices": [{"message": {"content": '{"from": "openai"}'}}]}
ANTHROPIC_OK = {"content": [{"type": "text", "text": 'Here you go:\n```json\n{"from": "anthropic"}\n```'}]}


class _Models:
    def __init__(self, code):
        self.code, self.calls = code, 0

    def generate_content(self, model, contents, config):
        self.calls += 1
        if self.code is None:
            return type("R", (), {"text": '{"from": "gemini"}'})()
        raise errors.APIError(self.code, {"error": {"message": "x", "status": "X"}})


@pytest.fixture
def setup(monkeypatch):
    def make(*, openai=OPENAI_OK, anthropic=ANTHROPIC_OK, gemini=None, keys=("openai", "anthropic", "gemini"),
             order="openai,anthropic,gemini"):
        """openai / anthropic: a JSON body to return, or an int HTTP status to fail with. gemini: None = works."""
        gemini_client.reset_state()
        s = get_settings()
        monkeypatch.setattr(s, "openai_api_key", "o-key" if "openai" in keys else None)
        monkeypatch.setattr(s, "anthropic_api_key", "a-key" if "anthropic" in keys else None)
        monkeypatch.setattr(s, "gemini_api_key", "g-key" if "gemini" in keys else None)
        monkeypatch.setattr(s, "llm_order", order)
        monkeypatch.setattr(gemini_client.time, "sleep", lambda x: None)
        models = _Models(gemini)
        monkeypatch.setattr(genai, "Client", lambda **kw: type("C", (), {"models": models})())
        sent = []
        replies = {"api.openai.com": openai, "api.anthropic.com": anthropic}

        def fake_post(url, headers, json, timeout):
            host = url.split("/")[2]
            sent.append((host, headers, json))
            reply = replies[host]
            req = httpx.Request("POST", url)
            if isinstance(reply, int):
                return httpx.Response(reply, json={"error": "x"}, request=req)
            return httpx.Response(200, json=reply, request=req)

        monkeypatch.setattr(httpx, "post", fake_post)
        return models, sent

    yield make
    gemini_client.reset_state()


def hosts(sent):
    return [h for h, _, _ in sent]


def test_openai_is_tried_first_and_gemini_is_untouched_when_it_answers(setup):
    models, sent = setup()
    assert gemini_client.generate_json(["hello"], 0.3) == {"from": "openai"}
    assert hosts(sent) == ["api.openai.com"] and models.calls == 0
    _, headers, body = sent[0]
    assert headers["Authorization"] == "Bearer o-key"
    assert body["response_format"] == {"type": "json_object"} and body["temperature"] == 0.3
    assert body["messages"][0]["content"] == [{"type": "text", "text": "hello"}]


def test_anthropic_is_next_when_openai_fails(setup):
    models, sent = setup(openai=503)
    assert gemini_client.generate_json(["hello"], 0.3) == {"from": "anthropic"}, "JSON is cut out of a fenced reply"
    assert hosts(sent) == ["api.openai.com", "api.anthropic.com"] and models.calls == 0
    _, headers, body = sent[1]
    assert headers["x-api-key"] == "a-key" and headers["anthropic-version"]
    assert body["max_tokens"] > 0 and body["messages"][0]["content"] == [{"type": "text", "text": "hello"}]


def test_gemini_is_last_when_both_others_fail(setup):
    models, sent = setup(openai=429, anthropic=500)
    assert gemini_client.generate_json(["x"], 0.1) == {"from": "gemini"}
    assert hosts(sent) == ["api.openai.com", "api.anthropic.com"] and models.calls == 1


def test_a_photo_goes_to_each_vendor_in_its_own_format(setup):
    _, sent = setup(openai=503)
    photo = types.Part.from_bytes(data=b"\xff\xd8jpeg", mime_type="image/jpeg")
    gemini_client.generate_json([photo, "what is this"], 0.2)
    openai_content = sent[0][2]["messages"][0]["content"]
    assert openai_content[0]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    anthropic_content = sent[1][2]["messages"][0]["content"]
    assert anthropic_content[0]["type"] == "image" and anthropic_content[0]["source"]["media_type"] == "image/jpeg"
    assert anthropic_content[1] == {"type": "text", "text": "what is this"}


def test_a_vendor_without_a_key_is_skipped(setup):
    models, sent = setup(keys=("anthropic",))
    assert gemini_client.generate_json(["x"], 0.1) == {"from": "anthropic"}
    assert hosts(sent) == ["api.anthropic.com"] and models.calls == 0


def test_with_only_a_gemini_key_behaviour_is_unchanged(setup):
    models, sent = setup(keys=("gemini",))
    assert gemini_client.generate_json(["x"], 0.1) == {"from": "gemini"}
    assert sent == [] and models.calls == 1


def test_the_order_is_configurable(setup):
    models, sent = setup(order="gemini,openai")
    assert gemini_client.generate_json(["x"], 0.1) == {"from": "gemini"}
    assert sent == []


def test_when_everything_fails_the_gemini_error_is_raised(setup):
    models, _ = setup(openai=503, anthropic=503, gemini=503)
    with pytest.raises(errors.APIError):
        gemini_client.generate_json(["x"], 0.1)


def test_a_vendor_that_keeps_failing_is_paused_then_skipped(setup):
    _, sent = setup(openai=503)
    for _ in range(gemini_client.BREAKER_THRESHOLD + 2):
        gemini_client.generate_json(["x"], 0.1)
    assert hosts(sent).count("api.openai.com") == gemini_client.BREAKER_THRESHOLD
    assert gemini_client.circuit_states()["openai"] is True


def test_quota_exhaustion_on_gemini_moves_on_without_retrying(setup):
    models, _ = setup(keys=("gemini",), gemini=429)
    with pytest.raises(errors.APIError):
        gemini_client.generate_json(["x"], 0.1)
    assert models.calls == 3, "one attempt per Gemini model (three configured), not three per model"


def test_a_reply_without_json_counts_as_a_failure_and_falls_through(setup):
    _, sent = setup(openai={"choices": [{"message": {"content": "not json"}}]})
    assert gemini_client.generate_json(["x"], 0.1) == {"from": "anthropic"}
