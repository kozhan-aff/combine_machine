"""Проба опубликованного сайта по HTTP — транспорт для проверки публикации (M5). Только GET."""
import httpx


def fetch(url: str, timeout: float = 15.0) -> tuple[int, str]:
    """GET url -> (HTTP-статус, тело). Редиректы (http->https, www) идём; TLS проверяем."""
    r = httpx.get(url, timeout=timeout, follow_redirects=True,
                  headers={"User-Agent": "Mozilla/5.0 (publish-verify)"})
    return r.status_code, r.text
