"""Тред D — сырые пробы 4 кандидатов A-Parser'а, БЕЗ парсинга (CLAUDE.md: "live тесты
A-Parser форматов").

Печатает ПОЛНЫЙ JSON-конверт (success/data/resultString как есть) для каждого парсера на
каждом домене — чтобы увидеть реальный формат ДО того, как писать код разбора. Проект уже
дважды обжёгся на слепом угадывании формата ответа (whois-возраст, дата дропа cctld — см.
CLAUDE.md, «Текущее состояние 2026-07-13»). Ничего не пишет в БД, не трогает воронку
скоринга, не решает капчу — read-only network probe, безопасно гонять сколько угодно раз
(кроме SE::Yandex/Google — те через прокси, вежливость как у остальных SERP-вызовов).

Кандидаты (аудит CLAUDE.md, Тред D):
  SE::Google::SafeBrowsing — «домен зафлагован Google?» (M1 cleanliness)
  Rank::Archive            — присутствие в archive.org (M1 history, дополняет Wayback)
  SecurityTrails::Domain   — DNS-история (M1 history)
  SE::Yandex               — SERP-фолбэк к SearXNG (M1 indexed_echo), запрос `site:domain`

Запуск (на боксе или с машины с доступом к A-Parser :9091 и реальным APARSER_API_KEY в .env):
    PYTHONPATH=backend python backend/scripts/aparser_probe_threadd.py [domain ...]
Без аргументов — пробует example.com (заведомо старый чистый домен) и один явно молодой/
непоказательный вами домен стоит передать явно, напр. свежий из /domains/pool.
"""
import sys

from app.integrations.aparser import AParserClient

# (parser, query_template) — query_template.format(domain=...) если запрос ≠ голый домен
CANDIDATES = [
    ("SE::Google::SafeBrowsing", "{domain}"),
    ("Rank::Archive", "{domain}"),
    ("SecurityTrails::Domain", "{domain}"),
    ("SE::Yandex", "site:{domain}"),
    ("SE::Google::TrustCheck", "{domain}"),
    ("SE::Google::Compromised", "{domain}"),
    ("Check::BackLink", "{domain}"),
    ("Cloudflare::Radar", "{domain}"),
]


def probe(client: AParserClient, parser: str, query: str) -> None:
    try:
        res = client._call("oneRequest", {"query": query, "parser": parser,
                                          "configPreset": "default", "preset": "default"})
        print(f"  [{parser}] query={query!r}")
        print(f"    raw: {res!r}")
    except Exception as e:  # noqa: BLE001 — это диагностика, показываем ЛЮБОЙ сбой как есть
        print(f"  [{parser}] query={query!r} -> ОШИБКА: {type(e).__name__}: {e}")


# --- Тред D, продолжение (2026-07-17): indexed_echo фолбэк + закрытие ---------------
# SecurityTrails/SE::Yandex. См. docs/superpowers/specs/
# 2026-07-17-threadd-serp-fallback-design.md. Тоже read-only, тоже безопасно гонять
# сколько угодно (SE::Google/Yandex — через прокси, та же вежливость, что у остальных
# SERP-вызовов в этом файле).

# Заведомо не индексирован (мусорный тестовый домен) / реальный живой домен / заведомо
# индексированный контрольный домен — тот же принцип трёх точек, что у SafeBrowsing/
# Archive проб (см. CANDIDATES выше).
SITE_QUERY_DOMAINS = ["dswjcndwijnwld23234212djf.ru", "zudpopo.ru", "wikipedia.org"]


def probe_site_query(client: AParserClient, parser: str, preset: str, domain: str) -> None:
    query = f"site:{domain}"
    try:
        res = client._call("oneRequest", {"query": query, "parser": parser,
                                          "configPreset": "default", "preset": preset})
        print(f"  [{parser} preset={preset}] query={query!r}")
        print(f"    raw: {res!r}")
    except Exception as e:  # noqa: BLE001 — диагностика, показываем любой сбой как есть
        print(f"  [{parser} preset={preset}] query={query!r} -> ОШИБКА: {type(e).__name__}: {e}")


def probe_preset(client: AParserClient, parser: str, preset: str) -> None:
    try:
        res = client._call("getParserPreset", {"parser": parser, "preset": preset})
        print(f"  [getParserPreset {parser}/{preset}]")
        print(f"    raw: {res!r}")
    except Exception as e:  # noqa: BLE001
        print(f"  [getParserPreset {parser}/{preset}] -> ОШИБКА: {type(e).__name__}: {e}")


def probe_serp_urls_prod(client: AParserClient, domain: str) -> None:
    """Тот же ПРОДОВЫЙ путь, что вызовет ветка А: публичный serp_urls() с его реальным
    парсингом (строки resultString, начинающиеся с 'http'). Сырой конверт из
    probe_site_query показывает транспорт; этот вызов показывает, что увидит scoring.py —
    без зазора на интерпретацию «а распарсится ли»."""
    try:
        urls = client.serp_urls(f"site:{domain}", limit=5)
        print(f"  [serp_urls] site:{domain} -> {len(urls)} URL: {urls!r}")
    except Exception as e:  # noqa: BLE001
        print(f"  [serp_urls] site:{domain} -> ОШИБКА: {type(e).__name__}: {e}")


def probe_continuation() -> None:
    client = AParserClient()

    print("=== SE::Google site: (кандидат в фолбэк для indexed_echo) ===")
    # 2 прохода: в пробе 2026-07-16 TrustCheck/Compromised давали 0–50% успеха —
    # ОДИН сэмпл на домен не отличает «надёжно» от «через раз». 6 сэмплов отличают.
    for attempt in (1, 2):
        print(f"--- попытка {attempt} (сырой конверт) ---")
        for d in SITE_QUERY_DOMAINS:
            probe_site_query(client, "SE::Google", "default", d)
    print("--- продовый путь: serp_urls(), парсинг как в ветке А ---")
    for d in SITE_QUERY_DOMAINS:
        probe_serp_urls_prod(client, d)

    print()
    print("=== SecurityTrails::Domain — конфиг пресета: нужен свой API-ключ и задан ли он? ===")
    probe_preset(client, "SecurityTrails::Domain", "default")

    print()
    print("=== SE::Yandex — default сначала (ожидаем повтор ReadTimeout из пробы 2026-07-16) ===")
    for d in SITE_QUERY_DOMAINS[:2]:
        probe_site_query(client, "SE::Yandex", "default", d)
    print("--- пробуем непрокси-пресет по аналогии с Rank::Archive/no_proxy ---")
    probe_preset(client, "SE::Yandex", "no_proxy")   # мог не существовать — ошибка ожидаема
    for d in SITE_QUERY_DOMAINS[:2]:
        probe_site_query(client, "SE::Yandex", "no_proxy", d)


def main() -> int:
    args = sys.argv[1:]
    if args and args[0] == "--only-continuation":
        # старый свип CANDIDATES на 8 парсеров содержит 6 подтверждённо мёртвых
        # (100% ReadTimeout / recaptcha, проба 2026-07-16) — гонять их заново значит
        # платить минуты таймаутов за уже известный ответ. Этот флаг гоняет ТОЛЬКО
        # continuation-блок.
        probe_continuation()
        return 0
    domains = args or ["example.com"]
    client = AParserClient()
    for d in domains:
        print(f"=== {d} ===")
        for parser, tmpl in CANDIDATES:
            probe(client, parser, tmpl.format(domain=d))
        print()
    probe_continuation()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
