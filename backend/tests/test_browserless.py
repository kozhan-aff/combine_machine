"""Browserless: /screenshot (PNG), /function (JSON), ping; ошибки — BrowserlessError, не падение."""
import json
import httpx
import pytest
from app.config import settings
from app.integrations.browserless import BrowserlessClient, BrowserlessError


@pytest.fixture
def mk(monkeypatch):
    monkeypatch.setattr(settings, "BROWSERLESS_URL", "http://bl:3000")
    monkeypatch.setattr(settings, "BROWSERLESS_TOKEN", "tok")

    def _mk(handler):
        c = BrowserlessClient()
        c._client = httpx.Client(transport=httpx.MockTransport(handler))
        return c
    return _mk


def test_screenshot_posts_url_viewport_and_returns_png(mk):
    seen = {}
    def h(req):
        seen["url"], seen["body"] = str(req.url), json.loads(req.content)
        return httpx.Response(200, content=b"\x89PNG...", headers={"content-type": "image/png"})
    png = mk(h).screenshot("https://ex.com/", width=1366, height=768)
    assert png.startswith(b"\x89PNG") and seen["url"] == "http://bl:3000/screenshot?token=tok"
    assert seen["body"]["url"] == "https://ex.com/" and seen["body"]["viewport"] == {"width": 1366, "height": 768}
    assert seen["body"]["options"] == {"fullPage": True, "type": "png"}


def test_screenshot_html_posts_html_instead_of_url(mk):
    seen = {}
    def h(req):
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, content=b"\x89PNG", headers={"content-type": "image/png"})
    mk(h).screenshot_html("<p>x</p>", width=390, height=800)
    assert seen["body"]["html"] == "<p>x</p>" and "url" not in seen["body"]
    assert seen["body"]["viewport"] == {"width": 390, "height": 800}


def test_screenshot_non_png_or_error_raises(mk):
    with pytest.raises(BrowserlessError):
        mk(lambda r: httpx.Response(500, text="boom")).screenshot("https://ex.com/")
    with pytest.raises(BrowserlessError):
        mk(lambda r: httpx.Response(200, text="<html>", headers={"content-type": "text/html"})).screenshot("https://ex.com/")


def test_screenshot_transport_failure_is_browserless_error_without_token_leak(mk):
    def h(req):
        raise httpx.ConnectError("refused", request=req)
    with pytest.raises(BrowserlessError) as ei:
        mk(h).screenshot("https://ex.com/")
    assert "tok" not in str(ei.value)


def test_overflow_runs_function_and_parses_json(mk):
    # живая форма (Задача 1): /function отдаёт конверт {data, type}
    def h(req):
        assert req.url.path == "/function" and "scrollWidth" in req.content.decode()
        return httpx.Response(200, json={"data": {"scrollWidth": 900, "innerWidth": 390}, "type": "application/json"})
    assert mk(h).overflow("<div style='width:900px'>x</div>", 390) == {"scrollWidth": 900, "innerWidth": 390}


def test_overflow_accepts_bare_object(mk):
    r = mk(lambda r: httpx.Response(200, json={"scrollWidth": 900, "innerWidth": 390})).overflow("<p>x</p>", 390)
    assert r == {"scrollWidth": 900, "innerWidth": 390}


def test_overflow_errors_raise(mk):
    with pytest.raises(BrowserlessError):
        mk(lambda r: httpx.Response(500, text="boom")).overflow("<p>x</p>", 390)
    with pytest.raises(BrowserlessError):
        mk(lambda r: httpx.Response(200, text="not json")).overflow("<p>x</p>", 390)


def test_ping(mk):
    assert mk(lambda r: httpx.Response(200, json={"Browser": "Chrome"})).ping() is True
    assert mk(lambda r: httpx.Response(503)).ping() is False

    def boom(req):
        raise httpx.ConnectError("refused", request=req)
    assert mk(boom).ping() is False
