"""Диагностика интеграций для панели — пингует всё, что нужно конвейеру, и
возвращает статус (ok/fail/skip + latency + ошибка). Параллельно, с таймаутом,
чтобы страница не висела на медленном пинге (Wayback/RKN).

Чисто транспортная проверка: каждый клиент уже умеет ping(). Здесь только оркестрация.
"""
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutTimeout

from app.config import settings

PING_TIMEOUT = 20.0  # сек на один пинг; Wayback стабильно ~15с — даём запас, чтобы не мигал

# Поля настроек, чьё значение — секрет: если оно утекло в текст исключения (напр. httpx
# кладёт полный URL с ?api_key=... в HTTPStatusError.str()), затираем ДО показа на /diag.
# /diag доступен по Basic-auth в LAN, но сырой api_key в «деталях ошибки» — это утечка
# ключа любому, кто откроет/залогирует страницу (в отличие от GITHUB_TOKEN, который
# deploy.py уже скрабит везде). Список — все credential-поля Settings.
_SECRET_FIELDS = (
    "AHREFS_API_KEY", "CHECKTRUST_API_KEY", "DATAFORSEO_LOGIN", "DATAFORSEO_PASSWORD",
    "SERPAPI_KEY", "YANDEX_WORDSTAT_TOKEN", "BACKORDER_LOGIN", "BACKORDER_PASSWORD",
    "OPTIMIZATOR_API_KEY", "REGRU_PASSWORD", "CLOUDFLARE_API_TOKEN", "AAPANEL_API_KEY",
    "LLM_API_KEY", "APARSER_API_KEY", "GITHUB_TOKEN", "PANEL_PASS", "SPAMHAUS_DQS_KEY",
)


def _scrub(s: str) -> str:
    """Затереть любое непустое значение секрета из настроек, встретившееся в тексте."""
    for name in _SECRET_FIELDS:
        val = getattr(settings, name, "")
        if val and isinstance(val, str) and val in s:
            s = s.replace(val, "***")
    return s


def _db_ping() -> bool:
    from sqlalchemy import text
    from app.db import SessionLocal
    with SessionLocal() as db:
        return db.execute(text("SELECT 1")).scalar() == 1


def _spec():
    """Список проверок: (key, label, role, need_cred, module, critical, factory→ping).

    role — роль в конвейере (для UI). need_cred — значение настройки, без которой
    пинг бессмысленен (пусто → skip). Ленивый импорт клиентов внутри лямбд, чтобы
    отсутствие опц. зависимостей не роняло всю страницу.
    """
    return [
        ("cloudflare", "Cloudflare", "M3 · зоны/DNS", settings.CLOUDFLARE_API_TOKEN, "M3", False,
         lambda: __import__("app.integrations.cloudflare", fromlist=["x"]).CloudflareClient().ping()),
        ("aapanel", "aaPanel", "M3 · vhost/файлы", settings.AAPANEL_API_KEY, "M3", False,
         lambda: __import__("app.integrations.aapanel", fromlist=["x"]).AaPanelClient().ping()),
        ("llm", "LiteLLM", "M4 · контент", settings.LLM_BASE_URL, "M4", True,
         lambda: __import__("app.integrations.llm", fromlist=["x"]).LlmClient().ping()),
        ("searxng", "SearXNG", "M1/M5 · SERP/индекс", settings.SEARXNG_URL, "M5", False,
         lambda: __import__("app.integrations.searxng", fromlist=["x"]).SearxngClient().ping()),
        ("backorder", "Backorder", "M1 · discovery", "1", "M1", True,  # публичный фид, кред не нужен
         lambda: __import__("app.integrations.backorder", fromlist=["x"]).BackorderClient().ping()),
        ("optimizator", "Optimizator", "M2 · выкуп (свободные чистые)", settings.OPTIMIZATOR_API_KEY, "M1", False,
         lambda: __import__("app.integrations.optimizator", fromlist=["x"]).OptimizatorClient().ping()),
        ("wayback", "Wayback", "M1 · история", "1", "M1", True,
         lambda: __import__("app.integrations.wayback", fromlist=["x"]).WaybackClient().ping()),
        ("rkn", "РКН (antizapret)", "M1 · блок-лист", settings.RKN_SOURCE_URL, "M1", True,
         lambda: __import__("app.integrations.rkn", fromlist=["x"]).RknClient().ping()),
        ("aparser", "A-Parser", "M1 · whois/лейн + fetch", settings.APARSER_API_KEY, "M1", True,
         lambda: __import__("app.integrations.aparser", fromlist=["x"]).AParserClient().ping()),
        ("blacklist", "Spamhaus/SURBL", "M1 · спам-лист", "1", "M1", False,
         lambda: __import__("app.integrations.blacklist", fromlist=["x"]).BlacklistClient().ping()),
        ("db", "PostgreSQL", "БД конвейера", settings.DATABASE_URL, "инфра", True, _db_ping),
    ]


def _run_one(key, label, role, need_cred, module, critical, fn) -> dict:
    base = {"key": key, "label": label, "role": role, "module": module, "critical": critical}
    if not need_cred:
        return {**base, "status": "skip", "ms": None, "error": "нет кредов в .env"}
    t0 = time.monotonic()
    try:
        ok = bool(fn())
        return {**base, "status": "ok" if ok else "fail",
                "ms": int((time.monotonic() - t0) * 1000), "error": None}
    except Exception as e:  # noqa: BLE001 — любой сбой интеграции = красный, не 500
        return {**base, "status": "fail",
                "ms": int((time.monotonic() - t0) * 1000),
                "error": _scrub(f"{type(e).__name__}: {e}")[:200]}


def run_diagnostics(specs=None) -> list[dict]:
    """Пингует все проверки параллельно. Возвращает результаты в исходном порядке.

    Не используем `with`-контекст: его shutdown(wait=True) заблокировал бы ответ до
    завершения зависшего пинга. Собираем результаты с per-future таймаутом (итого
    ≤ PING_TIMEOUT, т.к. пинги идут параллельно), затем shutdown(wait=False) — зависший
    поток дотикает в фоне (у клиентов свои httpx-таймауты) и ответ не ждёт его.
    """
    specs = specs if specs is not None else _spec()
    results: dict[int, dict] = {}
    ex = ThreadPoolExecutor(max_workers=len(specs) or 1)
    try:
        futs = {ex.submit(_run_one, k, lbl, role, cred, mod, crit, fn): i
                for i, (k, lbl, role, cred, mod, crit, fn) in enumerate(specs)}
        for fut, i in futs.items():
            k, lbl, role, cred, mod, crit, fn = specs[i]
            try:
                results[i] = fut.result(timeout=PING_TIMEOUT)
            except FutTimeout:
                results[i] = {"key": k, "label": lbl, "role": role, "module": mod,
                              "critical": crit, "status": "fail",
                              "ms": int(PING_TIMEOUT * 1000), "error": f"timeout > {PING_TIMEOUT:.0f}s"}
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    return [results[i] for i in range(len(specs))]


if __name__ == "__main__":  # self-check: ok/fail/skip/timeout ветки без сети
    specs = [
        ("a", "A", "role", "1", "M1", True, lambda: True),
        ("b", "B", "role", "1", "M1", True, lambda: False),
        ("c", "C", "role", "", "M1", False, lambda: True),                        # skip: нет кред
        ("d", "D", "role", "1", "M1", True, lambda: (_ for _ in ()).throw(RuntimeError("boom"))),
        ("e", "E", "role", "1", "M1", True, lambda: time.sleep(2)),               # timeout
    ]
    PING_TIMEOUT = 0.5  # module-level rebind (this block runs at module scope)
    out = {r["key"]: r["status"] for r in run_diagnostics(specs)}
    assert out == {"a": "ok", "b": "fail", "c": "skip", "d": "fail", "e": "fail"}, out
    print("diagnostics self-check ok:", out)
