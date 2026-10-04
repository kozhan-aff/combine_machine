"""W5: строгий разбор ответа LLM и клиент классификации. Мягкий сигнал — непригодный ответ = None,
не исключение."""
import httpx
import pytest

from app.integrations.llm import LlmClassifyClient
from app.services import history_llm

GOOD = ('Вот ответ: {"snapshots":[{"year":2015,"lang":"es","topic":"travel blog","parked":false},'
        '{"year":2019,"lang":"en","topic":"vpn reviews","parked":false},'
        '{"year":2022,"lang":"en","topic":"domain for sale","parked":true}],'
        '"topic_summary":"VPN and privacy reviews","vpn_adjacent":0.9} — готово')
TOPIC = '"topic_summary": "vpn blog"'


def test_parse_answer_extracts_json_from_prose_and_picks_latest_live_lang():
    r = history_llm.parse_answer(GOOD)
    assert r == {"market_lang": "en", "topic": "VPN and privacy reviews",
                 "topical_relevance": 0.9, "parked_share": 0.33}


def test_parse_answer_clamps_and_rejects_garbage():
    """4.2: `vpn_adjacent` — только конечное число (не строка, не bool, не NaN/Infinity), кламп 0..1."""
    assert history_llm.parse_answer('{"vpn_adjacent": 7, "snapshots": [], %s}' % TOPIC)["topical_relevance"] == 1.0
    assert history_llm.parse_answer('{"vpn_adjacent": -2, %s}' % TOPIC)["topical_relevance"] == 0.0
    assert history_llm.parse_answer("не JSON вовсе") is None
    assert history_llm.parse_answer('[1, 2]') is None
    for bad in ('"много"', '"0.5"', 'true', 'NaN', 'Infinity', 'null'):
        assert history_llm.parse_answer('{"vpn_adjacent": %s, %s}' % (bad, TOPIC)) is None, bad
    r = history_llm.parse_answer('{"vpn_adjacent": 0.2, "snapshots": [{"year": 2020, "lang": "Spanish"}], %s}' % TOPIC)
    assert r["market_lang"] is None


def test_parse_answer_is_strict_about_parked_snapshots_and_topic():
    """4.2: парковка — только литерал true; `snapshots` не списком — снимков нет; без
    `topic_summary` ответ непригоден («тема не определена»)."""
    r = history_llm.parse_answer('{"snapshots": [{"year": 2021, "lang": "de", "parked": "false"}], '
                                 '"topic_summary": "Reisen", "vpn_adjacent": 0.1}')
    assert r["market_lang"] == "de" and r["parked_share"] == 0.0
    r = history_llm.parse_answer('{"snapshots": 5, "topic_summary": "vpn", "vpn_adjacent": 0.5}')
    assert r["market_lang"] is None and r["parked_share"] is None and r["topic"] == "vpn"
    assert history_llm.parse_answer('{"snapshots": [], "vpn_adjacent": 0.5}') is None
    assert history_llm.parse_answer('{"snapshots": [], "topic_summary": "  ", "vpn_adjacent": 0.5}') is None


def test_classify_topics_truncates_and_asks_deterministically():
    seen = {}

    class LLM:
        def complete(self, system, prompt, **kw):
            seen.update(prompt=prompt, **kw)
            return GOOD
    texts = [{"timestamp": "20190101000000", "text": "x" * 5000}] * 7
    r = history_llm.classify_topics("a.com", texts, LLM())
    assert r["market_lang"] == "en" and seen["temperature"] == 0
    assert "model" not in seen                     # модель выбирает клиент (LLM_CLASSIFY_MODEL)
    assert seen["prompt"].count("--- снимок") == 5 and "x" * 2001 not in seen["prompt"]
    assert history_llm.classify_topics("a.com", [], LLM()) is None


def test_classify_client_one_attempt_short_timeout_and_model_fallback(monkeypatch):
    """3.3: зависший LiteLLM не держит слот волны истории ~6 минут (120 с × 3 попытки): у клиента
    классификации таймаут 30 с и ОДНА попытка. Р6: модель — LLM_CLASSIFY_MODEL, пусто -> LLM_MODEL."""
    from app.config import settings
    monkeypatch.setattr(settings, "LLM_CLASSIFY_MODEL", "")
    c = LlmClassifyClient()
    assert c.model == settings.LLM_MODEL and c._client.timeout.read == 30.0
    calls = []

    def down(method, url, **kw):
        calls.append(url)
        raise httpx.ConnectError("down")
    monkeypatch.setattr(c._client, "request", down)
    with pytest.raises(httpx.ConnectError):
        c.complete("s", "p")
    assert len(calls) == 1                          # без ретраев BaseClient
    monkeypatch.setattr(settings, "LLM_CLASSIFY_MODEL", "ollama/qwen2.5")
    assert LlmClassifyClient().model == "ollama/qwen2.5"
