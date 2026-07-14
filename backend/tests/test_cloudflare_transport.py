"""Транспорт CF: account-aware read-методы, пагинация, verify по token_kind (Задача 3, P0).

Подмена `request` делается на ИНСТАНСЕ клиента (не на классе) — рубильник сети в
conftest режет httpx-транспорты, а не наш собственный `request`, так что подмена
безопасна и герметична сама по себе.
"""
import httpx
import pytest

from app.integrations.cloudflare import CloudflareClient


def _resp(payload, status=200):
    return httpx.Response(status, json=payload, request=httpx.Request("GET", "http://x"))


def test_envelope_success_false_raises_on_http_200():
    c = CloudflareClient.with_token("tok")
    c.request = lambda *a, **k: _resp({"success": False, "errors": [{"message": "bad"}], "result": None})
    with pytest.raises(Exception):
        c.list_accounts_paginated()


def test_pagination_collects_all_pages_over_50():
    c = CloudflareClient.with_token("tok")
    calls = {"n": 0}

    def fake(method, url, **kw):
        calls["n"] += 1
        page = kw["params"]["page"]
        total_pages = 2
        results = [{"id": f"z{page}-{i}"} for i in range(50 if page == 1 else 5)]
        return _resp({"success": True, "result": results,
                      "result_info": {"page": page, "total_pages": total_pages}})
    c.request = fake
    zones = c.list_zones_paginated("acct1")
    assert len(zones) == 55 and calls["n"] == 2


def test_user_vs_account_verify_endpoint():
    seen = {}
    c = CloudflareClient.with_token("tok")

    def fake(method, url, **kw):
        seen["url"] = url
        return _resp({"success": True, "result": {"status": "active"}})
    c.request = fake
    c.verify_token("user")
    assert seen["url"].endswith("/user/tokens/verify")
    c.verify_token("account", "acctHEX")
    assert seen["url"].endswith("/accounts/acctHEX/tokens/verify")


def test_find_zone_in_account_filters_by_account():
    c = CloudflareClient.with_token("tok")

    def fake(method, url, **kw):
        assert kw["params"]["account.id"] == "acctHEX"
        return _resp({"success": True, "result": [{"id": "z1", "name": "a.ru", "status": "active",
                                                   "account": {"id": "acctHEX"}}],
                      "result_info": {"page": 1, "total_pages": 1}})
    c.request = fake
    z = c.find_zone_in_account("a.ru", "acctHEX")
    assert z["id"] == "z1"


def test_empty_list_is_not_error():
    c = CloudflareClient.with_token("tok")
    c.request = lambda *a, **k: _resp({"success": True, "result": [],
                                       "result_info": {"page": 1, "total_pages": 1}})
    assert c.list_zones_paginated("acctHEX") == []


def test_token_never_in_repr_or_headers_leak():
    c = CloudflareClient.with_token("SECRET-TOKEN")
    assert "SECRET-TOKEN" not in repr(c.__dict__.get("account_id", ""))
    assert c._headers()["Authorization"] == "Bearer SECRET-TOKEN"  # только в заголовке, нигде ещё
