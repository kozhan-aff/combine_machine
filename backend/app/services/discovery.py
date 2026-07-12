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

    def _pos(v):                    # сентинелы фида (-1 = «нет данных») -> None
        n = _int(v)
        return n if n is not None and n >= 0 else None

    def _price(v):
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None
    return {"domain": domain, "source": "backorder", "referring_domains": _int(row.get("links")) or 0,
            "lane": "bid", "acquire_deadline": _parse_deadline(row.get("delete_date")),
            "visitors": _pos(row.get("visitors")), "tic": _pos(row.get("yandex_tic")),
            "price": _price(row.get("price"))}


def _sources():
    from app.integrations.backorder import BackorderClient
    from app.integrations.cctld import CctldClient
    from app.integrations.regru_drops import RegruDropsClient
    from app.integrations.sweb_drops import SwebDropsClient
    return {"backorder": BackorderClient, "cctld": CctldClient,
            "reg_ru": RegruDropsClient, "sweb": SwebDropsClient}


_SOURCE_RU = {"backorder": "backorder", "cctld": "cctld", "reg_ru": "reg.ru", "sweb": "sweb"}


def _collect(enabled: dict) -> list[dict]:
    """Собрать строки со всех включённых источников. Сбой одного источника не топит остальные.

    Стадию репортим ПЕРЕД походом в источник: сбор идёт секунды, и оператор должен видеть,
    кого именно сейчас опрашиваем (jobs.report вне track — no-op, юнит-тесты не ломаются).
    """
    from app.services import jobs
    rows: list[dict] = []
    for name, Client in _sources().items():
        if not enabled.get(name):
            continue
        jobs.report("discovery", stage=name, current=f"собираю: {_SOURCE_RU.get(name, name)}")
        before = len(rows)
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
        got = len(rows) - before
        logger.info("discovery source %s: %d строк", name, got)
        if got == 0:
            # тихое пусто источника невидимо в логах (I6) — теперь явный warning
            logger.warning("discovery source %s дал 0 строк (пусто/сломана разметка?)", name)
    return rows


def run_discovery() -> int:
    """Собрать включённые источники, дедуп по domain (выигрывает бо́льший RD), upsert новых +
    обогащение уже известных discovered-строк. Прогресс — сам, через jobs.track: тогда его
    видно и когда discovery зовёт оркестратор из воркера, а не панель кнопкой."""
    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError
    from app.db import SessionLocal
    from app.models.domain import Domain
    from app.services import jobs
    from app.services.settings import get_settings

    enabled = get_settings()["sources_enabled"]
    stages = ([{"key": k, "label": _SOURCE_RU[k]} for k in _SOURCE_RU if enabled.get(k)]
              + [{"key": "dedup", "label": "дедуп"}, {"key": "save", "label": "запись"}])
    with jobs.track("discovery", stages=stages):
        rows = _collect(enabled)
        jobs.report("discovery", stage="dedup")
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
            jobs.report("discovery", done=0, total=0, current="", message="нет кандидатов")
            return 0
        jobs.report("discovery", stage="save")

        def _insert(db) -> int:
            existing = {d.domain: d for d in db.execute(
                select(Domain).where(Domain.domain.in_(candidates))
            ).scalars().all()}
            fresh = [n for n in candidates if n not in existing]
            # обогащение уже известных, но ещё НЕ обработанных (discovered) строк (I5): дозаполняем
            # NULL-поля и повышаем RD, если повторная встреча принесла больше данных (например,
            # домен сперва увиден на "сыром" реестре, потом — на backorder с RD/lane/дедлайном).
            # Статус/reject_reason не трогаем — re-run не откатывает уже отсканированные домены.
            for name, d in existing.items():
                if d.status != "discovered":
                    continue
                c = candidates[name]
                new_rd = c.get("referring_domains") or 0
                if new_rd > (d.referring_domains or 0):
                    d.referring_domains = new_rd
                for attr in ("lane", "acquire_deadline", "feed_flags", "visitors", "tic"):
                    if getattr(d, attr, None) is None and c.get(attr) is not None:
                        setattr(d, attr, c.get(attr))
            db.add_all(Domain(
                domain=candidates[n]["domain"], source=candidates[n]["source"],
                referring_domains=candidates[n].get("referring_domains"),
                feed_flags=candidates[n].get("feed_flags"),
                lane=candidates[n].get("lane"),
                acquire_deadline=candidates[n].get("acquire_deadline"),
                visitors=candidates[n].get("visitors"), tic=candidates[n].get("tic"),
                acquire_price=(candidates[n].get("price")
                               or (__import__("app.services.pricing", fromlist=["x"]).cached_backorder_price()
                                   if candidates[n].get("source") == "backorder" else None)),
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
        jobs.report("discovery", done=1, total=1, current="", message=f"собрано {n} доменов")
        return n


if __name__ == "__main__":  # pure normalize self-check (no network)
    nr = normalize_row({"domainname": "Example.COM.", "links": "12"})
    assert nr["domain"] == "example.com" and nr["referring_domains"] == 12 and nr["lane"] == "bid"
    assert normalize_row({"domainname": "пример.рф", "links": 3})["domain"] == "xn--e1afmkfd.xn--p1ai"
    assert normalize_row({"domainname": "under_score.ru", "links": 1}) is None
    assert normalize_row({"domainname": "", "links": 5}) is None
    sentinel = normalize_row({"domainname": "x.ru", "links": 5, "visitors": -1, "yandex_tic": -1, "price": 190})
    assert sentinel["visitors"] is None and sentinel["tic"] is None and sentinel["price"] == 190.0
    assert canonical_domain("www.a.ru") == "a.ru" and canonical_domain("x@y.ru") is None
    print("discovery normalize_row ok")
