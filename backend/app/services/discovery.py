"""M1a — Domain discovery. See BUILD_SPEC.md §7.

Pull candidates from the backorder public feed (no auth) and upsert into `domains`
with status='discovered'. Feed `links` (donor count) rides straight into referring_domains
as a free RD signal. Transport lives in integrations; this is the business logic.
"""
import logging
import re

logger = logging.getLogger(__name__)

_DOMAIN_RE = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)+$")


def normalize_row(row: dict) -> dict | None:
    """One feed row -> {domain, source, referring_domains} or None if junk."""
    domain = (row.get("domainname") or "").strip().lower().rstrip(".")
    if not domain or len(domain) > 253 or not _DOMAIN_RE.match(domain):
        return None
    try:
        links = int(row.get("links") or 0)
    except (TypeError, ValueError):
        links = 0
    return {"domain": domain, "source": "backorder", "referring_domains": links}


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
        d = r.get("domain")
        if not d:
            continue
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
        db.add_all(Domain(domain=candidates[n]["domain"], source=candidates[n]["source"],
                          referring_domains=candidates[n].get("referring_domains"),
                          feed_flags=candidates[n].get("feed_flags")) for n in fresh)
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
    assert normalize_row({"domainname": "Example.COM.", "links": "12"}) == {
        "domain": "example.com", "source": "backorder", "referring_domains": 12}
    assert normalize_row({"domainname": "under_score.ru", "links": 1}) is None  # junk char
    assert normalize_row({"domainname": "", "links": 5}) is None
    assert normalize_row({"domainname": "sub.dropzone.ru"})["referring_domains"] == 0
    print("discovery normalize_row ok")
