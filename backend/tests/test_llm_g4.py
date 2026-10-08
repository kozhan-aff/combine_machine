"""G4: LLM-классификатор W5 (S2-09/S6-10/S2-01): reasoning_effort=none для ollama, пустой content при
thinking -> явная ошибка, явный фолбэк модели и видимая причина при 403/429, один повтор ReadTimeout."""
import httpx
import pytest

from app.config import settings
from app.integrations.llm import LlmClassifyClient, LlmClient, LlmEmptyContent
from app.services import history_llm


class _R:
    def __init__(self, code=200, body=None):
        self.code, self.body = code, body

    def json(self):
        return self.body

    def raise_for_status(self):
        if self.code >= 400:
            req = httpx.Request("POST", "http://llm/v1/chat/completions")
            raise httpx.HTTPStatusError("e", request=req,
                                        response=httpx.Response(self.code, json=self.body, request=req))


def _ok(text, **extra):
    return _R(200, {"choices": [{"message": {"content": text, **extra}}]})


def _client(monkeypatch, outcomes, model="", fallback=""):
    monkeypatch.setattr(settings, "LLM_CLASSIFY_MODEL", model)
    monkeypatch.setattr(settings, "LLM_CLASSIFY_FALLBACK_MODEL", fallback)
    c = LlmClassifyClient()
    seen, it = [], iter(outcomes)

    def once(method, url, **kw):
        seen.append(kw["json"])
        o = next(it)
        if isinstance(o, Exception):
            raise o
        o.raise_for_status()
        return o
    c._request_once = once
    return c, seen


def test_ollama_gets_reasoning_effort_none_and_max_tokens(monkeypatch):
    c, seen = _client(monkeypatch, [_ok("{}")], model="ollama/qwen3.5:9b")
    c.complete("s", "p", max_tokens=800)
    assert seen[0]["reasoning_effort"] == "none" and seen[0]["max_tokens"] == 800


def test_non_ollama_model_gets_no_reasoning_param(monkeypatch):
    c, seen = _client(monkeypatch, [_ok("{}")], model="mistral-small")
    c.complete("s", "p")
    assert "reasoning_effort" not in seen[0]


def test_classify_topics_sends_max_tokens():
    got = {}

    class L:
        def complete(self, system, prompt, **kw):
            got.update(kw)
            return "{}"
    history_llm.classify_topics("a.com", [{"timestamp": "2019", "text": "x"}], L())
    assert got["max_tokens"] >= 400


def test_empty_content_with_reasoning_is_explicit_error(monkeypatch):
    c = LlmClient()
    monkeypatch.setattr(c, "request", lambda *a, **k: _ok("", reasoning_content="думаю..."))
    with pytest.raises(LlmEmptyContent, match="reasoning"):
        c.complete("s", "p")
    monkeypatch.setattr(c, "request", lambda *a, **k: _ok(""))          # просто пусто — прежний контракт
    assert c.complete("s", "p") == ""


def test_403_falls_back_to_second_model(monkeypatch):
    c, seen = _client(monkeypatch, [_R(403, {"error": {"message": "tier_not_allowed"}}), _ok("ответ")],
                      model="mistral", fallback="mistral-small")
    assert c.complete("s", "p") == "ответ"
    assert [b["model"] for b in seen] == ["mistral", "mistral-small"]


def test_403_without_fallback_raises_visible_error(monkeypatch):
    c, _ = _client(monkeypatch, [_R(403, {"error": {"message": "tier_not_allowed"}})], model="mistral")
    with pytest.raises(RuntimeError) as e:
        c.complete("s", "p")
    assert "mistral" in str(e.value) and "403" in str(e.value) and "tier_not_allowed" in str(e.value)


def test_both_models_down_lists_both_reasons(monkeypatch):
    c, _ = _client(monkeypatch, [_R(403, {}), _R(429, {})], model="mistral", fallback="mistral-small")
    with pytest.raises(RuntimeError) as e:
        c.complete("s", "p")
    assert "403" in str(e.value) and "429" in str(e.value)


def test_400_is_not_swallowed_by_fallback(monkeypatch):
    c, seen = _client(monkeypatch, [_R(400, {})], model="mistral", fallback="mistral-small")
    with pytest.raises(httpx.HTTPStatusError):
        c.complete("s", "p")
    assert len(seen) == 1


def test_local_model_read_timeout_retried_once(monkeypatch):
    c, seen = _client(monkeypatch, [httpx.ReadTimeout("t"), _ok("ok")], model="ollama/q")
    assert c.complete("s", "p") == "ok" and len(seen) == 2


def test_remote_model_read_timeout_not_retried(monkeypatch):
    c, seen = _client(monkeypatch, [httpx.ReadTimeout("t"), _ok("ok")], model="mistral")
    with pytest.raises(RuntimeError, match="таймаут"):
        c.complete("s", "p")
    assert len(seen) == 1


def test_thinking_only_answer_falls_back(monkeypatch):
    c, seen = _client(monkeypatch, [_ok("", reasoning_content="..."), _ok("json")],
                      model="ollama/q", fallback="mistral-small")
    assert c.complete("s", "p") == "json" and len(seen) == 2


# --- W2a: think:false для ollama/* ---

def _plain(monkeypatch, model, think=False):
    monkeypatch.setattr(settings, "LLM_THINK", think)
    c = LlmClient()
    seen = []

    def once(method, url, **kw):
        seen.append(kw["json"])
        return _ok("ok")
    c.request = once
    c._request_once = once
    return c, seen


def test_think_false_sent_for_ollama(monkeypatch):
    c, seen = _plain(monkeypatch, "x")
    c.complete("s", "p", model="ollama/qwen3.5:9b-q8_0")
    assert seen[0]["think"] is False


def test_no_think_for_mistral(monkeypatch):
    c, seen = _plain(monkeypatch, "x")
    c.complete("s", "p", model="mistral")
    assert "think" not in seen[0]


def test_llm_think_setting_disables_override(monkeypatch):
    c, seen = _plain(monkeypatch, "x", think=True)
    c.complete("s", "p", model="ollama/qwen3.5:9b-q8_0")
    assert "think" not in seen[0]


def test_probe_uses_think_false(monkeypatch):
    c, seen = _plain(monkeypatch, "x")
    c.model = "ollama/qwen3.5:9b-q8_0"
    c._request_once = lambda m, u, **kw: (seen.append(kw["json"]), _ok("ok"))[1]
    assert c.probe() is True and seen[0]["think"] is False


def test_classify_client_ollama_gets_think_false(monkeypatch):
    c, seen = _client(monkeypatch, [_ok("{}")], model="ollama/qwen3.5:9b-q8_0")
    c.complete("s", "p")
    assert seen[0]["think"] is False and seen[0]["reasoning_effort"] == "none"


def test_empty_content_with_reasoning_is_clear_error(monkeypatch):
    c, _ = _plain(monkeypatch, "x")
    c.request = lambda *a, **kw: _ok("", reasoning_content="думаю")
    with pytest.raises(LlmEmptyContent):
        c.complete("s", "p", model="ollama/q")


# --- переопределение с экрана ключей приходит строкой ---

@pytest.mark.parametrize("raw,expect_think", [("false", False), ("False", False), ("true", True)])
def test_think_override_string_from_panel(monkeypatch, key_overrides, raw, expect_think):
    api_keys = key_overrides
    # без _plain: он выставляет settings.LLM_THINK напрямую и заглушил бы переопределение;
    # monkeypatch.undo() тут нельзя — он откатил бы и autouse-рубильники сети/ключей
    c = LlmClient()
    seen = []

    def once(method, url, **kw):
        seen.append(kw["json"])
        return _ok("ok")
    c.request = once
    c._request_once = once
    monkeypatch.setattr(api_keys, "_snapshot", lambda: {"LLM_THINK": raw})
    c.complete("s", "p", model="ollama/qwen3.5:9b-q8_0")
    assert ("think" in seen[0]) is (not expect_think)


def test_think_false_sent_for_hf_model(monkeypatch):
    c, seen = _plain(monkeypatch, "x")
    c.complete("s", "p", model="hf.co/unsloth/Qwen3.8-27B-GGUF:Q3_K_M")
    assert seen[0]["think"] is False
