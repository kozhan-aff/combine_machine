"""RDAP (бутстрап IANA, 404 = свободен, сбой IANA -> статичная карта) и Web Risk (ключ в
заголовке). Без сети: HTTP подменяется на инстансе."""
import json
import logging
import pathlib
from datetime import datetime, timezone

import httpx
import pytest

from app.integrations.rdap import NoRdap, RdapClient, _iso
from app.integrations.webrisk import WebRiskClient

FX = pathlib.Path(__file__).parent / "fixtures" / "v2"


def _router(routes, calls):
    """routes: {подстрока URL: payload | int-статус}."""
    def request(method, url, **kw):
        calls.append(url)
        req = httpx.Request(method, url)
        for key, val in routes.items():
            if key in url:
                if isinstance(val, int):
                    raise httpx.HTTPStatusError(str(val), request=req,
                                                response=httpx.Response(val, request=req))
                return httpx.Response(200, json=val, request=req)
        raise AssertionError(f"неожиданный URL {url}")
    return request


def _iana():
    return json.loads((FX / "iana_dns.json").read_text(encoding="utf-8"))


def _pending():
    return json.loads((FX / "rdap_pending_delete.json").read_text(encoding="utf-8"))


def test_rdap_pending_delete_keeps_original_registration(monkeypatch, real_rdap_bootstrap):
    c, calls = RdapClient(), []
    monkeypatch.setattr(c, "request", _router({
        "data.iana.org": _iana(),
        "rdap.verisign.com/com/v1/domain/pharmaindustrie.com": _pending()}, calls))
    r = c.lookup("pharmaindustrie.com")
    assert r["exists"] is True and "pending delete" in r["status"]
    assert r["registered_at"] == datetime(1998, 7, 29, 4, 0, tzinfo=timezone.utc)
    c.lookup("pharmaindustrie.com")
    assert sum("data.iana.org" in u for u in calls) == 1        # бутстрап — один раз на клиент


def test_rdap_404_means_free_and_missing_zone_raises(monkeypatch, real_rdap_bootstrap):
    c = RdapClient()
    monkeypatch.setattr(c, "request", _router({
        "data.iana.org": _iana(),
        "/domain/free-name.com": 404}, []))
    assert c.lookup("free-name.com") == {"exists": False, "status": [], "registered_at": None}
    assert c.has_rdap("x.co.uk") is True and c.has_rdap("x.mx") is False
    with pytest.raises(NoRdap):
        c.lookup("x.mx")


def test_rdap_5xx_propagates(monkeypatch, real_rdap_bootstrap):
    c = RdapClient()
    monkeypatch.setattr(c, "request", _router({
        "data.iana.org": _iana(),
        "/domain/x.com": 503}, []))
    with pytest.raises(httpx.HTTPStatusError):
        c.lookup("x.com")


def test_rdap_iana_down_falls_back_once_per_client(monkeypatch, real_rdap_bootstrap, caplog):
    # IANA 503: ОДИН запрос в IANA и один warning на жизнь клиента, дальше статичная карта —
    # не шторм ретраев под общим локом на каждый домен волны
    c, calls = RdapClient(), []
    monkeypatch.setattr(c, "request", _router({
        "data.iana.org": 503,
        "rdap.verisign.com/com/v1/domain/pharmaindustrie.com": _pending()}, calls))
    with caplog.at_level(logging.WARNING, logger="app.integrations.rdap"):
        for _ in range(5):
            assert c.lookup("pharmaindustrie.com")["exists"] is True    # .com — через _FALLBACK
        assert c.has_rdap("x.mx") is False
        with pytest.raises(NoRdap):
            c.lookup("x.mx")                                             # .mx нет и в карте
    assert sum("data.iana.org" in u for u in calls) == 1
    assert len([r for r in caplog.records if r.name == "app.integrations.rdap"]) == 1


def test_rdap_ping_asks_iana_directly(monkeypatch):
    # /diag не зеленеет на статичной карте: пинг ходит в IANA сам, мимо запомненного бутстрапа
    c, calls = RdapClient(), []
    monkeypatch.setattr(c, "request", _router({"data.iana.org": 503}, calls))
    with pytest.raises(httpx.HTTPStatusError):
        c.ping()
    monkeypatch.setattr(c, "request", _router({"data.iana.org": _iana()}, calls))
    assert c.ping() is True
    assert sum("data.iana.org" in u for u in calls) == 2


def test_iso_pads_fraction_and_assumes_utc_for_naive():
    # живой CentralNic (.xyz): '.0Z' — fromisoformat на Python 3.10 берёт дробь только из 3 или 6 цифр
    assert _iso("2014-03-20T12:59:17.0Z") == datetime(2014, 3, 20, 12, 59, 17, tzinfo=timezone.utc)
    assert _iso("2014-03-20T12:59:17.1234567Z") == datetime(2014, 3, 20, 12, 59, 17, 123456,
                                                           tzinfo=timezone.utc)
    naive = _iso("2014-03-20T12:59:17")                    # без смещения -> UTC, не наивная дата
    assert naive.tzinfo is not None and naive == datetime(2014, 3, 20, 12, 59, 17, tzinfo=timezone.utc)
    assert _iso("1998-07-29T04:00:00+02:00") == datetime(1998, 7, 29, 2, 0, tzinfo=timezone.utc)
    assert _iso(None) is None and _iso("") is None and _iso("not a date") is None


def test_default_harness_has_no_rdap_and_no_paid_keys():
    # autouse _no_paid_keys (conftest): боевые ключи из .env не видны, бутстрап IANA не зовётся,
    # ни у одной зоны нет RDAP — сети нет даже у теста, который забыл подменить клиент
    from app.config import settings
    assert settings.AHREFS_API_KEY == settings.WEBRISK_API_KEY == settings.SPAMHAUS_DQS_KEY == ""
    c = RdapClient()
    assert c.has_rdap("x.com") is False
    with pytest.raises(NoRdap):
        c.lookup("x.com")
    assert WebRiskClient().configured is False


# Web Risk: JSON ниже — ПО ДОКУМЕНТАЦИИ (uris:search), живьём не снят — ключа нет. Живые ответы
# (тестовая malware-страница Google и example.com) снимаются в Задаче 17 (находка R2-3).
def test_webrisk_key_in_header_not_in_url(monkeypatch):
    c, seen = WebRiskClient(api_key="SECRET"), {}

    def request(method, url, **kw):
        seen.update(kw, url=url)
        return httpx.Response(200, json={}, request=httpx.Request(method, url))
    monkeypatch.setattr(c, "request", request)
    assert c.threats("example.com") == []
    assert seen["headers"]["X-Goog-Api-Key"] == "SECRET"
    assert "SECRET" not in seen["url"] and all("SECRET" not in str(v) for _, v in seen["params"])
    assert ("uri", "http://example.com/") in seen["params"]


def test_webrisk_threat_and_configured(monkeypatch):
    c = WebRiskClient(api_key="k")
    monkeypatch.setattr(c, "request", lambda m, u, **kw: httpx.Response(
        200, json={"threat": {"threatTypes": ["MALWARE"], "expireTime": "2026-10-02T00:00:00Z"}},
        request=httpx.Request(m, u)))
    assert c.threats("bad.com") == ["MALWARE"]
    assert WebRiskClient(api_key="").configured is False


def test_webrisk_unknown_shape_raises_not_clean(monkeypatch):
    # находка R2-3: формат не снят живьём. Незнакомый ответ -> исключение (W3 пишет `webrisk:`,
    # домен «вслепую», вне пакета), а не [] — тихое «чисто» пустило бы угрозу в пакет
    c = WebRiskClient(api_key="k")
    for payload in (["MALWARE"], "ok", {"threat": ["MALWARE"]}, {"threat": "MALWARE"},
                    {"threat": {"types": ["MALWARE"]}}):
        monkeypatch.setattr(c, "request", lambda m, u, _p=payload, **kw: httpx.Response(
            200, json=_p, request=httpx.Request(m, u)))
        with pytest.raises(ValueError):
            c.threats("x.com")


def test_webrisk_key_is_scrubbed_on_diag(monkeypatch):
    from app.config import settings
    from app.services import diagnostics
    monkeypatch.setattr(settings, "WEBRISK_API_KEY", "WR-SECRET-123")
    assert diagnostics._scrub("403 for uris:search?key=WR-SECRET-123") == "403 for uris:search?key=***"
