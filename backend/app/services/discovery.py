"""M1a — Domain discovery. See BUILD_SPEC.md §7.

Pull candidates from the backorder public feed (no auth) and upsert into `domains`
with status='discovered'. Feed `links` (donor count) rides straight into referring_domains
as a free RD signal. Transport lives in integrations; this is the business logic.
"""
import logging
import re
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_DOMAIN_RE = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)+$")   # проверяем punycode-форму (ASCII)


def _parse_deadline(val) -> datetime | None:
    """backorder delete_date -> datetime UTC. Формат выверить на живом фиде (спек §J);
    парсим устойчиво: ISO-дата/дату-время, иначе None."""
    s = str(val or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d.%m.%Y"):
        try:
            return datetime.strptime(s[:19], fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def canonical_domain(raw) -> str | None:
    """Единая канон-форма домена для ВСЕХ источников: lower, без www./точки, IDN→punycode.
    None если не домен (мусор, e-mail, пустое, недопустимые метки)."""
    s = (raw or "").strip().lower().rstrip(".")
    if s.startswith("www."):
        s = s[4:]
    if not s or len(s) > 253 or "@" in s or " " in s:
        return None
    try:
        puny = s.encode("idna").decode("ascii")
    except (UnicodeError, ValueError):
        return None                       # пустая метка, >63, недопустимый символ
    return puny if _DOMAIN_RE.match(puny) else None


def normalize_row(row: dict) -> dict | None:
    """Одна строка фида backorder -> нормализованный кандидат (или None если мусор).
    backorder — bid-лейн из источника; тянем дедлайн/visitors/tic (раньше выбрасывались)."""
    domain = canonical_domain(row.get("domainname"))
    if domain is None:
        return None

    def _int(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return None
    return {"domain": domain, "source": "backorder", "referring_domains": _int(row.get("links")) or 0,
            "lane": "bid", "acquire_deadline": _parse_deadline(row.get("delete_date")),
            "visitors": _int(row.get("visitors")), "tic": _int(row.get("yandex_tic"))}


def _sources():
    from app.integrations.backorder import BackorderClient
    from app.integrations.cctld import CctldClient
    from app.integrations.regru_drops import RegruDropsClient
    from app.integrations.sweb_drops import SwebDropsClient
    return {"backorder": BackorderClient, "cctld": CctldClient,
            "reg_ru": RegruDropsClient, "sweb": SwebDropsClient}


def _collect(enabled: dict, on_progress=None) -> list[dict]:
    """Собрать строки со всех включённых источников. Сбой одного источника не топит остальные.

    on_progress(done, total, current) — чтобы бар не висел в 0/0 во время опроса источников:
    репортим «собираю: <источник>» перед каждым (total=1, discovery не по-доменный)."""
    rows: list[dict] = []
    for name, Client in _sources().items():
        if not enabled.get(name):
            continue
        if on_progress:
            on_progress(0, 1, f"собираю: {name}")
        try:
            if name == "backorder":                         # даёт RD + фид-флаги
                for r in Client().list_dropping():
                    nr = normalize_row(r)
                    if nr:
                        nr["feed_flags"] = {k: bool(r.get(k)) for k in ("rkn", "judicial", "block")}
                        rows.append(nr)
            else:
                rows.extend(Client().list_dropping())
        except Exception as e:  # noqa: BLE001 — один источник упал, остальные идут
            logger.warning("discovery source %s failed: %s", name, e)
            continue
    return rows


def run_discovery(on_progress=None) -> int:
    """Собрать включённые источники, дедуп по domain (выигрывает бо́льший RD), upsert новых.
    on_progress(done, total, current) — discovery не по-доменный: во время сбора репортит
    «собираю: <источник>» по каждому, в конце (1, 1, "собрано N")."""
    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError
    from app.db import SessionLocal
    from app.models.domain import Domain
    from app.services.settings import get_settings

    rows = _collect(get_settings()["sources_enabled"], on_progress)
    best: dict[str, dict] = {}
    for r in rows:
        d = canonical_domain(r.get("domain"))      # единый ключ: сырые источники тоже канонятся
        if not d:
            continue
        r["domain"] = d
        cur = best.get(d)
        if cur is None or (r.get("referring_domains") or 0) > (cur.get("referring_domains") or 0):
            best[d] = r
    candidates = best
    if not candidates:
        # ноль кандидатов (фид пуст / источники выключены) — тоже честный терминал:
        # репортим 0/0 «нет кандидатов»; JS считает джоб завершённым по running=False
        # (терминальный контракт в services/jobs.py), done/total — только отображение.
        if on_progress:
            on_progress(0, 0, "нет кандидатов")
        return 0

    def _insert(db) -> int:
        existing = set(db.execute(
            select(Domain.domain).where(Domain.domain.in_(candidates))
        ).scalars().all())
        fresh = [n for n in candidates if n not in existing]
        db.add_all(Domain(
            domain=candidates[n]["domain"], source=candidates[n]["source"],
            referring_domains=candidates[n].get("referring_domains"),
            feed_flags=candidates[n].get("feed_flags"),
            lane=candidates[n].get("lane"),
            acquire_deadline=candidates[n].get("acquire_deadline"),
            visitors=candidates[n].get("visitors"), tic=candidates[n].get("tic"),
            acquire_price=(__import__("app.services.pricing", fromlist=["x"]).cached_backorder_price()
                           if candidates[n].get("source") == "backorder" else None),
        ) for n in fresh)
        db.commit()
        return len(fresh)

    with SessionLocal() as db:
        try:
            n = _insert(db)
        except IntegrityError:
            # гонка: параллельный запуск вставил часть кандидатов между нашим SELECT и COMMIT
            # (unique на domain). Откатываемся, перечитываем existing и досыпаем остаток —
            # одной повторной попытки достаточно (перечитанный existing уже включает их вставки).
            db.rollback()
            n = _insert(db)
    if on_progress:
        on_progress(1, 1, f"собрано {n}")
    return n


if __name__ == "__main__":  # pure normalize self-check (no network)
    nr = normalize_row({"domainname": "Example.COM.", "links": "12"})
    assert nr["domain"] == "example.com" and nr["referring_domains"] == 12 and nr["lane"] == "bid"
    assert normalize_row({"domainname": "пример.рф", "links": 3})["domain"] == "xn--e1afmkfd.xn--p1ai"
    assert normalize_row({"domainname": "under_score.ru", "links": 1}) is None
    assert normalize_row({"domainname": "", "links": 5}) is None
    assert canonical_domain("www.a.ru") == "a.ru" and canonical_domain("x@y.ru") is None
    print("discovery normalize_row ok")
