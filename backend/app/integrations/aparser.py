"""A-Parser client — whois / SERP / keywords / coarse-DR. Transport only.

See docs/api/aparser.md. Local box :9091. Request: POST /API {password, action, data}.
Useful parsers: Net::Whois (M2 free-check), SE::Google/Yandex (M1/M4), WordStat (M4).
"""
import re
from datetime import datetime, timezone

from app.config import settings
from app.integrations.base import BaseClient

# .ru/.рф/gTLD сырой whois (фолбэк): 'created: 2010.11.15' / 'Creation Date: 2004-03-15T...'
_RE_RU = re.compile(r"created:\s*(\d{4})\.(\d{2})\.(\d{2})", re.I)
_RE_GTLD = re.compile(r"creation date:\s*(\d{4})-(\d{2})-(\d{2})", re.I)
# свёртка A-Parser Net::Whois (основной формат, снят вживую 2026-07-07):
#   '<domain> - registered: 0|1, expire: ..., creation: DD.MM.YYYY|none'
_RE_SVERTKA_REG = re.compile(r"registered:\s*([01])\b", re.I)
_RE_SVERTKA_CREATION = re.compile(r"creation:\s*(\d{2})\.(\d{2})\.(\d{4})", re.I)

# Rank::Ahrefs resultString (Result format "$query: $rating, $bl, $domains\n"), живьём
# проверено 2026-07-08: 'wikipedia.org: 97, 3421649284, 4000871' / 'lev777.casino: none, 476, 268'
# rating может быть буквально строкой 'none' — Ahrefs не всем доменам присваивает DR,
# даже когда backlinks/referring-domains у них реальные (не 0) — отдельный null-check.
_RE_AHREFS = re.compile(
    r"^\s*\S+:\s*(?P<rating>none|\d+)\s*,\s*(?P<bl>\d+)\s*,\s*(?P<domains>\d+)\s*$",
    re.I | re.M,
)

# SE::Google::SafeBrowsing resultString: '<domain>: 0|1\n' (1 = Google-флаг вредонос/фишинг),
# живьём проверено 2026-07-16 на обоих тестовых доменах (оба вернули 0).
_RE_SAFEBROWSING = re.compile(r":\s*([01])\s*$")

# Rank::Archive resultString (preset no_proxy — default сломан через прокси, см. дизайн-документ):
# '<domain>: <first|none> - <last|none> (<times|none> times)\n', даты dd.mm.yyyy.
# Живьём проверено 2026-07-16: google.com (19936104), wikipedia.org (393880),
# yandex.ru (506844), zudpopo.ru (19, 2023-2026).
_RE_ARCHIVE = re.compile(
    r":\s*(?P<first>none|\d{2}\.\d{2}\.\d{4})\s*-\s*(?P<last>none|\d{2}\.\d{2}\.\d{4})"
    r"\s*\((?P<times>none|\d+)\s*times\)",
    re.I,
)


def _parse_safebrowsing(text: str) -> bool | None:
    """True = зафлагован Google, False = чист, None = формат не распознан (вызывающий
    код трактует как «не проверено», НЕ как «чисто» — см. scoring._funnel)."""
    m = _RE_SAFEBROWSING.search(text or "")
    return None if not m else m.group(1) == "1"


def _parse_archive(text: str) -> dict:
    """times=0 -> вызывающий код вправе пропустить дорогой Wayback-фетч; times=None ->
    формат не распознан/сбой, фолбэк на реальный Wayback как раньше."""
    m = _RE_ARCHIVE.search(text or "")
    if not m:
        return {"times": None, "first": None, "last": None}
    times = m.group("times")
    return {
        "times": None if times.lower() == "none" else int(times),
        "first": None if m.group("first").lower() == "none" else m.group("first"),
        "last": None if m.group("last").lower() == "none" else m.group("last"),
    }


def _parse_ahrefs(text: str) -> dict:
    """resultString '<domain>: <rating|none>, <bl>, <domains>' -> dr/backlinks/
    referring_domains. Немэтч (разметка A-Parser/Ahrefs изменилась) -> все None, не
    исключение — вызывающий код (scoring._funnel) уже привык к None-сигналам."""
    m = _RE_AHREFS.search(text or "")
    if not m:
        return {"dr": None, "backlinks": None, "referring_domains": None}
    rating = m.group("rating")
    return {
        "dr": None if rating.lower() == "none" else int(rating),
        "backlinks": int(m.group("bl")),
        "referring_domains": int(m.group("domains")),
    }


def _parse_whois_created(text: str) -> datetime | None:
    """Дата регистрации из whois-ответа. Свёртка A-Parser (DD.MM.YYYY) или сырой whois
    (.ru YYYY.MM.DD / gTLD YYYY-MM-DD). Самая ранняя найденная, UTC. None если нет."""
    found = []
    for dy, mo, y in _RE_SVERTKA_CREATION.findall(text or ""):           # DD.MM.YYYY
        try:
            found.append(datetime(int(y), int(mo), int(dy), tzinfo=timezone.utc))
        except ValueError:
            pass
    for rx in (_RE_RU, _RE_GTLD):                                        # YYYY.MM.DD / YYYY-MM-DD
        for y, mo, dy in rx.findall(text or ""):
            try:
                found.append(datetime(int(y), int(mo), int(dy), tzinfo=timezone.utc))
            except ValueError:
                pass
    return min(found) if found else None


# маркеры сырого whois (фолбэк, если пресет отдаёт не свёртку)
_FREE_MARKERS = ("no entries found", "not found", "no match", "no object found",
                 "available for registration", "нет данных", "not registered")
_REG_MARKERS = ("nserver", "registrar", "person:", "org:", "paid-till", "domain:")


def _parse_whois_available(text: str) -> bool | None:
    """True — свободен, False — занят, None — не определить.
    Свёртка A-Parser 'registered: 0|1' приоритетна; иначе — маркеры сырого whois."""
    low = (text or "").lower()
    m = _RE_SVERTKA_REG.search(low)
    if m:
        return m.group(1) == "0"                     # 0 = свободен, 1 = занят
    if any(w in low for w in _FREE_MARKERS):
        return True
    if _RE_RU.search(low) or _RE_GTLD.search(low) or any(w in low for w in _REG_MARKERS):
        return False
    return None


class AParserClient(BaseClient):
    def __init__(self):
        super().__init__(settings.APARSER_URL)
        self.password = settings.APARSER_API_KEY

    def _call(self, action: str, data: dict | None = None) -> dict:
        """Один вызов /API. Отказ A-Parser -> RuntimeError (см. ниже) — глотать его нельзя.

        A-Parser отвечает **HTTP 200 даже на отказ**: сбой живёт в КОНВЕРТЕ, а не в статусе
        ({"success":0,"msg":"Auth failed"} — docs/api/aparser.md). `raise_for_status` такого
        не видит, и раньше тело возвращалось наверх как обычный результат: `resultString`
        пуст -> whois честно докладывал «ничего не разобрал» ({available: None, created: None})
        -> возраст None -> гейт `too_young` **не применялся вовсе**, а `errors` оставался пуст,
        так что и метка «оценён вслепую» молчала. Машина не отличала «домену 16 лет» от
        «я не смог спросить» (аудит F6).

        Проверяем ТОЛЬКО конверт, НЕ содержимое. `success:1` с пустым `resultString` — законный
        ответ «ничего не нашлось» (домена нет в whois, SERP пуст, Ahrefs не знает домена), и
        объявить ошибкой ЕГО значило бы обменять одну немоту на другую: воронка стала бы
        считать сбоем каждый ненайденный домен. Ошибка — это когда A-Parser сказал «нет»,
        а не когда он сказал «пусто».
        """
        body: dict = {"password": self.password, "action": action}
        if data is not None:
            body["data"] = data
        r = self.request("POST", f"{self.base_url}/API", json=body)
        res = r.json()
        if not isinstance(res, dict) or res.get("success") != 1:
            # тело без `success` — это не «пустой результат», а НЕ ТОТ ответ (редирект на
            # логин, прокси, сменившийся API): принять его молча — снова выдавать незнание
            # за знание. `msg` — то, что A-Parser сам сказал о причине; несём его наверх,
            # иначе /diag краснеет без объяснения.
            msg = (res.get("msg") if isinstance(res, dict) else None) \
                or f"неожиданный ответ: {str(res)[:120]}"
            raise RuntimeError(f"A-Parser {action}: {msg}")
        return res

    def info(self) -> dict:
        """Version + installed parsers list."""
        return self._call("info")

    def ping(self) -> bool:
        return self._call("ping").get("data") == "pong"

    @staticmethod
    def _result_string(res: dict) -> str:
        """oneRequest envelope -> data.resultString ('' если формат иной)."""
        data = res.get("data")
        if isinstance(data, dict):
            return data.get("resultString") or ""
        return ""

    def serp_urls(self, query: str, limit: int = 10) -> list[str]:
        """Топ органической выдачи по ключу (SE::Google), URL в порядке ранга, деду́п.

        A-Parser ходит через ротируемые прокси — пробивает антибот там, где сырой GET
        падает. resultString — URL по строкам. См. docs/api/aparser.md."""
        res = self._call("oneRequest", {"query": query, "parser": "SE::Google",
                                        "configPreset": "default", "preset": "default"})
        seen: set[str] = set()
        out: list[str] = []
        for ln in self._result_string(res).splitlines():
            u = ln.strip()
            if u.startswith("http") and u not in seen:
                seen.add(u)
                out.append(u)
        return out[:limit]

    def fetch_html(self, url: str) -> str | None:
        """Скачать страницу по URL (Net::HTTP через прокси). Возвращает HTML или None (с логом статуса).

        resultString = 'СТАТУС\\nзаголовки\\n\\nHTML' — режем по первой пустой строке,
        проверяем 200 (в т.ч. форму 'HTTP/1.1 200 OK'). JS не рендерит (сырой GET), но
        для структуры H2/H3 хватает. Не-200/пустой ответ — не тихий None, а warning в лог
        со статусом (иначе первый прод-сбой источника недебажим)."""
        import logging
        res = self._call("oneRequest", {"query": url, "parser": "Net::HTTP",
                                        "configPreset": "default", "preset": "default"})
        head, sep, body = self._result_string(res).partition("\n\n")
        head_norm = head.replace("\r\n", "\n").strip()
        first_line = head_norm.splitlines()[0] if head_norm else ""
        ok = first_line.startswith("200") or (first_line.upper().startswith("HTTP/") and " 200" in first_line)
        if not sep or not ok:
            logging.getLogger(__name__).warning("fetch_html %s: не-200 (%r)", url, first_line[:80])
            return None
        return body or None

    def whois_probe(self, domain: str) -> dict:
        """Один Net::Whois-вызов -> доступность + дата регистрации.
        available: True свободен / False занят / None не определить. created: дата или None.
        Сетевой сбой И отказ A-Parser (конверт success:0, см. _call) пробрасываются — ловит
        вызывающий код (_funnel -> sig["errors"] -> метка «вслепую» + запрет авто-approve).
        None-ы здесь означают ТОЛЬКО «A-Parser ответил, но разобрать нечего», а не «не спросили»."""
        res = self._call("oneRequest", {"query": domain, "parser": "Net::Whois",
                                        "configPreset": "default", "preset": "default"})
        text = self._result_string(res)
        return {"available": _parse_whois_available(text), "created": _parse_whois_created(text)}

    def whois_created(self, domain: str) -> datetime | None:
        """Дата регистрации (обёртка над whois_probe для обратной совместимости)."""
        return self.whois_probe(domain)["created"]

    def ahrefs_probe(self, domain: str) -> dict:
        """Rank::Ahrefs через капча-решатель (пресет RuCapcha, живьём проверено
        2026-07-08 — см. docs/superpowers/specs/2026-07-08-ahrefs-dr-design.md).
        Дорогой вызов (платная капча) — вызывающий код решает, КОГДА его делать
        (scoring.py _funnel: только для доменов без RD из фида, T3-выжившие, под
        runtime-бюджетом max_ahrefs_per_run). Сетевой сбой И отказ A-Parser (конверт success:0,
        напр. не решилась капча) пробрасываются — ловит вызывающий код (как whois_probe).
        Ретрая здесь нет: _call поднимает RuntimeError УЖЕ ПОСЛЕ request(), вне его ретрай-обёртки,
        иначе один отказ конверта стоил бы трёх платных решений капчи."""
        res = self._call("oneRequest", {
            "query": domain,
            "parser": "Rank::Ahrefs",
            "preset": "default",
            "configPreset": "default",
            "options": [
                {"name": "Use proxy", "value": True},
                {"name": "Proxy Checker", "value": settings.APARSER_PROXY_CHECKER},
                {"name": "Result format", "value": "$query: $rating, $bl, $domains\n"},
                {"name": "Util::Turnstile preset", "value": "RuCapcha"},
            ],
        })
        return _parse_ahrefs(self._result_string(res))

    def safebrowsing_check(self, domain: str) -> bool | None:
        """SE::Google::SafeBrowsing — не SERP-скрейпинг, прямой lookup, recaptcha не
        задевает (в отличие от TrustCheck/Compromised, живьём проверено 2026-07-16)."""
        res = self._call("oneRequest", {
            "query": domain, "parser": "SE::Google::SafeBrowsing",
            "configPreset": "default", "preset": "default",
        })
        return _parse_safebrowsing(self._result_string(res))

    def archive_probe(self, domain: str) -> dict:
        """Rank::Archive, ОБЯЗАТЕЛЬНО preset=no_proxy — default бьётся в archive.org
        через прокси-пул и получает 502 на каждую попытку (живьём подтверждено
        2026-07-16: getParserPreset показал useproxy=1 у default, логи oneRequest —
        502 Bad Gateway на всех прокси). no_proxy живьём подтверждён рабочим."""
        res = self._call("oneRequest", {
            "query": domain, "parser": "Rank::Archive",
            "configPreset": "default", "preset": "no_proxy",
        })
        return _parse_archive(self._result_string(res))


if __name__ == "__main__":  # pure whois-parse self-check (no network)
    assert _parse_whois_available("x.ru - registered: 1, expire: none, creation: 01.02.2020") is False
    assert _parse_whois_available("x.ru - registered: 0, expire: none, creation: none") is True
    assert _parse_whois_created("x.ru - registered: 1, expire: none, creation: 01.02.2020").year == 2020
    assert _parse_whois_created("x.ru - registered: 0, expire: none, creation: none") is None
    assert _parse_whois_available("No entries found") is True                      # фолбэк
    assert _parse_whois_available("nserver: ns1.x.ru") is False                    # фолбэк
    assert _parse_whois_available("мусор без маркеров") is None
    print("aparser whois-parse ok")
