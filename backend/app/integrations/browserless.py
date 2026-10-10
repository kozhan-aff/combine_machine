"""Browserless (бокс :3000, контейнер `browserless`): скриншоты страниц конкурентов и макета, проверка
горизонтального переполнения. Только транспорт (спека 2026-10-10 §4.3, §7.4). Формы запросов и ответов —
по docs/v2/research/research-live-formats-2026-10.md (живая сверка)."""
import json

import httpx

from app.config import settings
from app.integrations.base import BaseClient


class BrowserlessError(RuntimeError):
    pass


class BrowserlessClient(BaseClient):
    POOLED = True

    def __init__(self, timeout: float = 60.0):
        super().__init__(settings.BROWSERLESS_URL, timeout=timeout)
        self.token = settings.BROWSERLESS_TOKEN

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}" + (f"?token={self.token}" if self.token else "")

    def _post(self, what: str, path: str, **kwargs) -> httpx.Response:
        """POST без ретрая (скриншот тяжёлый, не идемпотентный по ресурсам). Любой сбой — BrowserlessError;
        текст ошибки собираем сами: str(исключения httpx) содержит URL, а в нём токен."""
        try:
            r = self.request("POST", self._url(path), retry=False, **kwargs)
        except httpx.HTTPStatusError as e:
            raise BrowserlessError(f"{what}: HTTP {e.response.status_code} {e.response.text[:120]}") from None
        except httpx.HTTPError as e:
            raise BrowserlessError(f"{what}: {type(e).__name__}") from None
        if r.status_code != 200:
            raise BrowserlessError(f"{what}: HTTP {r.status_code} {r.text[:120]}")
        return r

    def _png(self, body: dict) -> bytes:
        r = self._post("screenshot", "/screenshot", json=body)
        if not r.headers.get("content-type", "").startswith("image/"):
            raise BrowserlessError(f"screenshot: не картинка ({r.headers.get('content-type', '')}) {r.text[:120]}")
        return r.content

    def screenshot(self, url: str, *, width: int = 1366, height: int = 768, full_page: bool = True) -> bytes:
        return self._png({"url": url, "options": {"fullPage": full_page, "type": "png"},
                          "viewport": {"width": width, "height": height}})

    def screenshot_html(self, html: str, *, width: int = 1366, height: int = 768) -> bytes:
        return self._png({"html": html, "options": {"fullPage": True, "type": "png"},
                          "viewport": {"width": width, "height": height}})

    def overflow(self, html: str, width: int) -> dict:
        code = ("export default async function ({ page }) {"
                f" await page.setViewport({{width: {int(width)}, height: 800}});"
                f" await page.setContent({json.dumps(html)});"
                " const w = await page.evaluate(() => [document.documentElement.scrollWidth, window.innerWidth]);"
                " return { data: { scrollWidth: w[0], innerWidth: w[1] }, type: 'application/json' }; }")
        r = self._post("function", "/function", content=code.encode(),
                       headers={"Content-Type": "application/javascript"})
        try:
            d = r.json()
            d = d.get("data", d)      # живой /function отдаёт конверт {data, type}; голый объект тоже принимаем
            return {"scrollWidth": int(d["scrollWidth"]), "innerWidth": int(d["innerWidth"])}
        except (ValueError, KeyError, TypeError, AttributeError):
            raise BrowserlessError("function: неожиданный ответ") from None

    def ping(self) -> bool:
        try:
            return self.request("GET", self._url("/json/version"), retry=False).status_code == 200
        except Exception:  # noqa: BLE001 — ping не бросает
            return False
