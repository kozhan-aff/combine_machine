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


def main() -> int:
    domains = sys.argv[1:] or ["example.com"]
    client = AParserClient()
    for d in domains:
        print(f"=== {d} ===")
        for parser, tmpl in CANDIDATES:
            probe(client, parser, tmpl.format(domain=d))
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
