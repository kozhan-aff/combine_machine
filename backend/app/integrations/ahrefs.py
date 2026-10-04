"""Ahrefs API v3 — транспорт. Base https://api.ahrefs.com/v3, Bearer AHREFS_API_KEY.

Живые замеры 2026-10-01 (docs/v2/research/metrics-history.md):
  · public/domain-rating-free — 0 units, до 1000 целей. ЛИЦЕНЗИЯ: DR показывать только с подписью
    «Domain Rating by Ahrefs» и ссылкой на ahrefs.com; «систематический» массовый сбор запрещён —
    поэтому discovery зовёт его ОДИН раз на домен и только для новых доменов;
  · batch-analysis — 25 units за строку (6 полей + index + url), до 100 целей;
  · site-explorer/anchors — ~9 units/строку;
  · subscription-info/limits-and-usage — бесплатно.
Платные методы (batch, anchors, metrics_history) ходят через `_request_once` — ОДНА попытка, без
ретрая BaseClient: таймаут после того, как Ahrefs принял запрос, — уже списанные units, и ретрай
списал бы их трижды. Сбой — исключение; вызывающий (W4/W6) пишет его в errors, домен — следующим
прогоном. Бесплатные dr_free/units_left ретраятся как обычно.
MCP-коннектор Ahrefs в чате Claude — НЕ это: приложению нужен свой ключ.
"""
from datetime import date, timedelta

from app.config import settings
from app.integrations.base import BaseClient

BATCH_FIELDS = ("domain_rating", "refdomains", "refdomains_dofollow", "refips_subnets",
                "backlinks", "org_traffic")


def _host(target) -> str:
    """'https://Example.com/' | 'пример.рф/' -> 'example.com' | 'xn--e1afmkfd.xn--p1ai'.

    Ahrefs возвращает target в нижнем регистре, со слэшем на конце и IDN — в Юникоде, хотя спрошен
    punycode (живой ответ 2026-10-01). Ключ — канон-форма ASCII, как домен хранится у нас; имя,
    которое IDNA не кодирует, остаётся как есть (тогда оно просто не совпадёт со спрошенным).
    """
    s = str(target or "").strip().lower()
    for p in ("https://", "http://"):
        if s.startswith(p):
            s = s[len(p):]
    s = s.rstrip("/")
    try:
        return s.encode("idna").decode("ascii")
    except UnicodeError:
        return s


class AhrefsClient(BaseClient):
    def __init__(self, api_key: str | None = None):
        super().__init__("https://api.ahrefs.com/v3", timeout=60.0)
        self.api_key = settings.AHREFS_API_KEY if api_key is None else api_key

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"}

    def dr_free(self, domains: list[str]) -> dict[str, float]:
        """{домен: DR} для ≤1000 доменов. Домена нет в ответе — DR неизвестен (вызывающий решает).

        Ключ — `_host` спрошенного имени. Строку о домене, которого не спрашивали, не берём: DR не
        приписывается чужому имени, а спрошенный домен честно остаётся «DR неизвестен».
        """
        if not domains:
            return {}
        if len(domains) > 1000:
            raise ValueError("dr_free: не больше 1000 доменов за запрос")
        r = self.request("POST", f"{self.base_url}/public/domain-rating-free",
                         headers=self._headers(), json={"targets": list(domains)})
        asked = {_host(d) for d in domains}
        out = {}
        for t in (r.json().get("domain_rating") or {}).get("targets") or []:
            name, dr = _host(t.get("target")), t.get("domain_rating")
            if name in asked and dr is not None:
                out[name] = float(dr)
        return out

    def batch(self, domains: list[str]) -> dict[str, dict]:
        """{домен: {поля BATCH_FIELDS}} для ≤100 доменов.

        Сопоставление — ТОЛЬКО по `index` строки ответа (0-based, порядок `targets` запроса). Не по
        `url`: живой ответ 2026-10-01 вернул IDN в Юникоде (`пример.рф/` на отправленный punycode) —
        по `url` терялись бы все IDN. И не по позиции строки: метрики, приписанные соседу, — это
        одобренный спам-домен с чужим DR. Строка без `index` или с индексом вне диапазона
        пропускается — домен остаётся без метрик, вызывающий решает. `url` в `select` оставлен:
        так снят живой образец и замерена цена (25 units за строку).
        """
        if not domains:
            return {}
        if len(domains) > 100:
            raise ValueError("batch: не больше 100 доменов за запрос")
        body = {"select": ["index", "url", *BATCH_FIELDS],
                "targets": [{"url": d, "mode": "subdomains", "protocol": "both"} for d in domains]}
        r = self._request_once("POST", f"{self.base_url}/batch-analysis/batch-analysis",
                               headers=self._headers(), json=body)
        out = {}
        for row in r.json().get("targets") or []:
            i = row.get("index")
            if isinstance(i, int) and not isinstance(i, bool) and 0 <= i < len(domains):
                out[domains[i]] = {k: row.get(k) for k in BATCH_FIELDS}
        return out

    def anchors(self, domain: str, limit: int = 50) -> list[dict]:
        """Анкоры по убыванию refdomains, вся история (all_time) — спам-волна после дропа видна и
        в уже потерянных ссылках."""
        r = self._request_once("GET", f"{self.base_url}/site-explorer/anchors",
                               headers=self._headers(),
                               params={"target": domain, "mode": "subdomains", "limit": limit,
                                       "order_by": "refdomains:desc", "history": "all_time",
                                       "select": "anchor,refdomains,is_spam,first_seen"})
        return r.json().get("anchors") or []

    def metrics_history(self, domain: str, years: int = 5, today: date | None = None) -> list[dict]:
        """Помесячный органический трафик за `years` лет: [{"date", "org_traffic"}].

        Формат снят по документации, НЕ живьём (живой образец — Задача 17). Поэтому ответ без
        списка `metrics` — ValueError (W6 пишет `deep_history:ValueError`), а не []: тихий пустой
        список читался бы как «трафика не было» по ответу, который мы не поняли.
        """
        start = (today or date.today()) - timedelta(days=365 * years)
        r = self._request_once("GET", f"{self.base_url}/site-explorer/metrics-history",
                               headers=self._headers(),
                               params={"target": domain, "mode": "subdomains",
                                       "date_from": start.isoformat(), "history_grouping": "monthly",
                                       "select": "date,org_traffic"})
        data = r.json()
        rows = data.get("metrics") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            raise ValueError("metrics_history: в ответе нет списка 'metrics'")
        return rows

    def units_left(self) -> int | None:
        """Остаток units в месяце (запрос бесплатный). None — лимит не сообщён."""
        r = self.request("GET", f"{self.base_url}/subscription-info/limits-and-usage",
                         headers=self._headers())
        lu = r.json().get("limits_and_usage") or {}
        lim, used = lu.get("units_limit_workspace"), lu.get("units_usage_workspace")
        return None if lim is None or used is None else int(lim) - int(used)

    def ping(self) -> bool:
        return self.units_left() is not None
