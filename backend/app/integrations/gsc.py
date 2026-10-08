"""Google Search Console — URL Inspection API (M5, проверка индексации). ТОЛЬКО транспорт.

Авторизация — service account (GSC_SERVICE_ACCOUNT_JSON): JWT-bearer подписываем сами
(google.auth.crypt, без сети) и меняем на токен у oauth2.googleapis.com через наш httpx —
поэтому весь обмен идёт через BaseClient и мок-транспорт тестов, а не через свой HTTP-стек
google-auth. Адрес обмена токена ЗАШИТ (token_uri из JSON игнорируем): подписанный JWT уходит
только Google, что бы ни лежало в ключе, вставленном в панели.

Формат ответа — по документации URL Inspection (живой образец не снят: ключа сервис-аккаунта нет;
первый живой ответ -> фикстура -> правка парсера, инвариант «не гадать форматы»):
  POST {GSC_API_URL}/v1/urlInspection/index:inspect {inspectionUrl, siteUrl}
  -> inspectionResult.indexStatusResult {verdict: PASS|PARTIAL|FAIL|NEUTRAL|VERDICT_UNSPECIFIED,
     coverageState: "Submitted and indexed" | "Crawled - currently not indexed" | ..., lastCrawlTime}

Квота Google: 2000 запросов/сут и 600/мин на свойство. Здесь — только транспорт и честные исключения;
суточный счёт и фолбэк на SearXNG живут в services/publish.
"""
import json
import time

import httpx

from app.config import settings
from app.integrations.base import BaseClient
from app.log_scrub import install as _install_log_scrub

_install_log_scrub()

SCOPE = "https://www.googleapis.com/auth/webmasters.readonly"
TOKEN_URL = "https://oauth2.googleapis.com/token"      # зашит намеренно, см. докстринг
DAILY_QUOTA = 2000                                      # URL Inspection: запросов/сут на свойство


class GscError(Exception):
    """Search Console не ответил по существу (сеть, 5xx, мусор в ответе)."""


class GscNotConfigured(GscError):
    """Нет или битый GSC_SERVICE_ACCOUNT_JSON."""


class GscNoAccess(GscError):
    """403/404: аккаунт не добавлен в свойство этого сайта (или свойства нет). Сайт GSC «не видит»."""


class GscQuota(GscError):
    """429: исчерпана квота (суточная или поминутная). Дальше сегодня не спрашиваем."""


def _service_info() -> dict | None:
    raw = (settings.GSC_SERVICE_ACCOUNT_JSON or "").strip()
    if not raw:
        return None
    try:
        info = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    if not isinstance(info, dict) or not info.get("client_email") or not info.get("private_key"):
        return None
    return info


def configured() -> bool:
    """Есть ли пригодный ключ сервис-аккаунта (не ходит в сеть)."""
    return _service_info() is not None


class GscClient(BaseClient):
    def __init__(self):
        super().__init__(settings.GSC_API_URL or "https://searchconsole.googleapis.com", timeout=30.0)
        self._token: str | None = None
        self._token_exp = 0.0
        self._property: dict[str, str] = {}       # домен -> свойство, на котором уже сработало

    # ── токен ────────────────────────────────────────────────────────────────────────────────
    def _assertion(self, info: dict) -> str:
        from google.auth import crypt, jwt
        now = int(time.time())
        signer = crypt.RSASigner.from_service_account_info(info)
        claims = {"iss": info["client_email"], "scope": SCOPE, "aud": TOKEN_URL,
                  "iat": now, "exp": now + 3000}
        raw = jwt.encode(signer, claims)
        return raw.decode() if isinstance(raw, bytes) else raw

    def _access_token(self) -> str:
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        info = _service_info()
        if info is None:
            raise GscNotConfigured("GSC_SERVICE_ACCOUNT_JSON пуст или не похож на ключ сервис-аккаунта")
        try:
            assertion = self._assertion(info)
        except Exception as e:  # noqa: BLE001 — битый private_key: понятная причина вместо трейса
            raise GscNotConfigured(f"ключ сервис-аккаунта не подписывает ({type(e).__name__})") from e
        try:
            r = self.request("POST", TOKEN_URL, retry=True, data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion})
            tok = r.json().get("access_token")
        except httpx.HTTPStatusError as e:
            raise GscError(f"обмен токена: HTTP {e.response.status_code}") from e
        except (httpx.HTTPError, ValueError) as e:
            raise GscError(f"обмен токена: {type(e).__name__}") from e
        if not tok:
            raise GscError("обмен токена: в ответе нет access_token")
        self._token = tok
        self._token_exp = time.time() + 3000
        return tok

    # ── вызовы ───────────────────────────────────────────────────────────────────────────────
    def _inspect_property(self, site_url: str, url: str) -> dict:
        headers = {"Authorization": f"Bearer {self._access_token()}"}
        try:
            r = self.request("POST", f"{self.base_url}/v1/urlInspection/index:inspect", headers=headers,
                             json={"inspectionUrl": url, "siteUrl": site_url})
        except httpx.HTTPStatusError as e:
            code = e.response.status_code
            if code == 429:
                raise GscQuota("квота URL Inspection исчерпана (HTTP 429)") from e
            if code in (403, 404):
                raise GscNoAccess(f"нет доступа к свойству {site_url} (HTTP {code})") from e
            if code == 401:
                self._token = None                  # протух/отозван: следующий вызов обменяет заново
            raise GscError(f"URL Inspection: HTTP {code}") from e
        except httpx.HTTPError as e:
            raise GscError(f"URL Inspection: {type(e).__name__}") from e
        try:
            body = r.json()
        except ValueError as e:
            raise GscError("URL Inspection: ответ не JSON") from e
        return body if isinstance(body, dict) else {}

    def inspect(self, domain: str, url: str) -> dict:
        """-> {indexed: bool|None, verdict, coverage_state, last_crawl, property}.

        `indexed`: True — verdict PASS; False — Google ответил и страницы в индексе нет (NEUTRAL/FAIL:
        «Crawled/Discovered - currently not indexed», «URL is unknown to Google», noindex и т. п.);
        None — ответ без индексного вердикта (PARTIAL/UNSPECIFIED/нет блока): не знаем.
        Свойство: сначала доменное `sc-domain:`, при 403 — префикс `https://домен/`; сработавшее
        запоминается на жизнь клиента."""
        d = (domain or "").strip().lower()
        props = [self._property[d]] if d in self._property else [f"sc-domain:{d}", f"https://{d}/"]
        last: GscNoAccess | None = None
        for prop in props:
            try:
                body = self._inspect_property(prop, url)
            except GscNoAccess as e:
                last = e
                continue
            self._property[d] = prop
            res = (body.get("inspectionResult") or {}).get("indexStatusResult")
            if not isinstance(res, dict):
                return {"indexed": None, "verdict": None, "coverage_state": None,
                        "last_crawl": None, "property": prop}
            verdict = res.get("verdict")
            indexed = True if verdict == "PASS" else False if verdict in ("NEUTRAL", "FAIL") else None
            return {"indexed": indexed, "verdict": verdict, "coverage_state": res.get("coverageState"),
                    "last_crawl": res.get("lastCrawlTime"), "property": prop}
        raise last or GscNoAccess("свойство не найдено")
