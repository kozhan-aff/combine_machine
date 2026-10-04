"""Ahrefs API v3: разбор живых ответов (фикстуры 2026-10-01), форма запросов, без сети."""
import json
import pathlib
from datetime import date

import httpx
import pytest

from app.integrations.ahrefs import AhrefsClient, BATCH_FIELDS

FX = pathlib.Path(__file__).parent / "fixtures" / "v2"


def _fx(name):
    return json.loads((FX / name).read_text(encoding="utf-8"))


def _fake(payload, calls):
    """Подмена HTTP на инстансе. Бесплатные dr_free/units_left идут через `request` (с ретраем),
    платные batch/anchors/metrics_history — через `_request_once` (одна попытка): подменяется тот
    метод, которым ходит проверяемый вызов."""
    def request(method, url, **kw):
        calls.append({"method": method, "url": url, **kw})
        return httpx.Response(200, json=payload, request=httpx.Request(method, url))
    return request


def test_dr_free_keys_by_punycode_strips_slash_and_takes_only_asked(monkeypatch):
    # живой ответ 2026-10-01: слэш на конце, IDN в Юникоде (`пример.рф/`), хотя спрошен punycode.
    # Без IDNA в `_host` каждый IDN-домен молча ушёл бы в «DR недоступен».
    c, calls = AhrefsClient(api_key="k"), []
    monkeypatch.setattr(c, "request", _fake(_fx("ahrefs_dr_free.json"), calls))
    out = c.dr_free(["xn--e1afmkfd.xn--p1ai", "pharmaindustrie.com"])
    assert out == {"xn--e1afmkfd.xn--p1ai": 10.0, "pharmaindustrie.com": 0.0}  # nordvpn.com не спрашивали
    assert calls[0]["method"] == "POST" and calls[0]["url"].endswith("/public/domain-rating-free")
    assert calls[0]["json"] == {"targets": ["xn--e1afmkfd.xn--p1ai", "pharmaindustrie.com"]}
    assert calls[0]["headers"]["Authorization"] == "Bearer k"


def test_dr_free_empty_makes_no_call_and_rejects_over_1000(monkeypatch):
    c, calls = AhrefsClient(api_key="k"), []
    monkeypatch.setattr(c, "request", _fake({}, calls))
    assert c.dr_free([]) == {} and calls == []
    with pytest.raises(ValueError):
        c.dr_free([f"d{i}.com" for i in range(1001)])


def test_batch_maps_by_index_not_by_url_or_position(monkeypatch):
    c, calls = AhrefsClient(api_key="k"), []
    rows = list(reversed(_fx("ahrefs_batch.json")["targets"]))     # строки пришли в обратном порядке
    monkeypatch.setattr(c, "_request_once", _fake({"targets": rows}, calls))
    out = c.batch(["pharmaindustrie.com", "nordvpn.com", "xn--e1afmkfd.xn--p1ai", "missing.com"])
    assert out["pharmaindustrie.com"]["refdomains"] == 719
    assert out["nordvpn.com"]["refips_subnets"] == 12145
    assert "missing.com" not in out                                 # строки index=3 нет — метрик нет
    assert set(out["nordvpn.com"]) == set(BATCH_FIELDS)
    body = calls[0]["json"]
    assert body["select"][:2] == ["index", "url"] and set(BATCH_FIELDS) <= set(body["select"])
    assert body["targets"][0] == {"url": "pharmaindustrie.com", "mode": "subdomains", "protocol": "both"}


def test_batch_idn_gets_metrics_by_index_though_url_is_unicode(monkeypatch):
    # живой факт 2026-10-01: punycode в запросе, Юникод в ответе — по `url` IDN потерялся бы
    c = AhrefsClient(api_key="k")
    monkeypatch.setattr(c, "_request_once", _fake(_fx("ahrefs_batch.json"), []))
    out = c.batch(["pharmaindustrie.com", "nordvpn.com", "xn--e1afmkfd.xn--p1ai"])
    assert out["xn--e1afmkfd.xn--p1ai"]["refdomains"] == 575
    assert out["xn--e1afmkfd.xn--p1ai"]["domain_rating"] == 10.0
    assert "пример.рф" not in out


def test_batch_skips_rows_without_index_or_out_of_range(monkeypatch):
    c = AhrefsClient(api_key="k")
    row = _fx("ahrefs_batch.json")["targets"][1]
    no_index = {k: v for k, v in row.items() if k != "index"}
    monkeypatch.setattr(c, "_request_once", _fake(
        {"targets": [no_index, {**row, "index": 2}, {**row, "index": -1}]}, []))
    assert c.batch(["nordvpn.com", "other.com"]) == {}              # по позиции и по url не гадаем


def test_anchors_and_metrics_history_request_shape(monkeypatch):
    c, calls = AhrefsClient(api_key="k"), []
    monkeypatch.setattr(c, "_request_once", _fake(_fx("ahrefs_anchors_spam.json"), calls))
    assert c.anchors("pharmaindustrie.com")[1]["refdomains"] == 278
    p = calls[0]["params"]
    assert calls[0]["url"].endswith("/site-explorer/anchors")
    assert p["target"] == "pharmaindustrie.com" and p["limit"] == 50 and p["order_by"] == "refdomains:desc"
    monkeypatch.setattr(c, "_request_once", _fake(_fx("ahrefs_metrics_history.json"), calls))
    hist = c.metrics_history("x.com", years=5, today=date(2026, 10, 1))
    assert hist[1]["org_traffic"] == 1850
    assert calls[-1]["params"]["date_from"] == "2021-10-02"
    assert calls[-1]["url"].endswith("/site-explorer/metrics-history")


def test_metrics_history_without_metrics_list_raises(monkeypatch):
    # находка R2-21: формат metrics-history снят по документации, не живьём (живой образец —
    # Задача 17). Ответ без списка `metrics` — незнакомая форма: исключение (W6 запишет
    # `deep_history:ValueError`), а не тихий [] — «трафика не было» по ответу, который не поняли.
    c = AhrefsClient(api_key="k")
    for payload in ({}, {"error": "unexpected"}, {"metrics": None}, [{"date": "2026-01-01"}]):
        monkeypatch.setattr(c, "_request_once", _fake(payload, []))
        with pytest.raises(ValueError):
            c.metrics_history("x.com")
    monkeypatch.setattr(c, "_request_once", _fake({"metrics": []}, []))
    assert c.metrics_history("x.com") == []                     # пустая история — законный ответ


@pytest.mark.parametrize("paid", [
    lambda c: c.batch(["a.com"]),
    lambda c: c.anchors("a.com"),
    lambda c: c.metrics_history("a.com"),
], ids=["batch", "anchors", "metrics_history"])
def test_paid_call_is_sent_once_on_timeout(monkeypatch, paid):
    # находка R2-6: таймаут после того, как Ahrefs принял запрос, — уже списанные units; ретрай
    # tenacity списал бы их трижды. Подменяется httpx-клиент ПОД ретраем: через `request` этот
    # же таймаут ушёл бы 3 раза.
    c, calls = AhrefsClient(api_key="k"), []

    def timeout(method, url, **kw):
        calls.append(url)
        raise httpx.ReadTimeout("timed out", request=httpx.Request(method, url))
    monkeypatch.setattr(c._client, "request", timeout)
    with pytest.raises(httpx.ReadTimeout):
        paid(c)
    assert len(calls) == 1


def test_units_left_and_ping(monkeypatch):
    c = AhrefsClient(api_key="k")
    monkeypatch.setattr(c, "request", _fake(_fx("ahrefs_limits.json"), []))
    assert c.units_left() == 2000000 - 84961
    assert c.ping() is True
