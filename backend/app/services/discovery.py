"""M1a — discovery v2: международные источники -> `domains` (status='discovered').

Конвейер АВТОМАТИЧЕСКИХ источников (dropcatch/nominet/mx): канон-форма -> белый список зон ->
отсев известных -> бесплатный DR Ahrefs только для НОВЫХ, не спрошенных за 4 суток (`dr_seen`) ->
вставка тех, у кого DR >= min_dr.

Почему DR-фильтр здесь, а не волной скоринга (живой замер 2026-10-01): Nominet отдаёт всё
расписание (~266 тыс. строк), DropCatch — ~134 тыс. в день, и почти всё — DR 0, засыпанный
автоматическим SEO-спамом (RD 700+ из спам-анкоров). Хранить их, чтобы потом отклонить, — раздувать
базу на десятки тысяч строк в день и делать столько же коммитов. EMD и ручной список фильтр по DR
не проходят: это выбор оператора / новореги, у которых ссылок и не должно быть.
"""
import logging
import math
import re
import time
from datetime import datetime, timedelta, timezone

import httpx

from app.services.domain_filters import canonical_domain, emd_candidates, tld_match

logger = logging.getLogger(__name__)

AUTO_SOURCES = ("dropcatch", "nominet", "mx")
_SOURCE_RU = {"dropcatch": "DropCatch", "nominet": "Nominet (.uk)", "mx": "registry.mx", "emd": "EMD"}
_CHUNK = 5000        # psycopg: не больше 65 535 параметров на запрос — IN и вставку режем чанками
_DR_BATCH = 1000     # public/domain-rating-free: до 1000 целей за запрос
_DR_PAUSE = 1.0      # с между пачками DR: лимит Ahrefs — 60 запросов в минуту
_DR_MEMORY = timedelta(days=4)   # спрошенный домен не спрашиваем снова 4 суток (Р4, лицензия DR-free)
_LIST_MAX = 5000     # ручной список за раз
_sleep = time.sleep  # пауза между пачками DR и ожидание после 429; тесты подменяют, чтобы не спать


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _clients() -> dict:
    from app.integrations.dropcatch import DropCatchClient
    from app.integrations.nominet import NominetClient
    from app.integrations.registry_mx import RegistryMxClient
    return {"dropcatch": DropCatchClient, "nominet": NominetClient, "mx": RegistryMxClient}


def _collect(enabled: dict, st: dict, run=None) -> tuple[dict, dict]:
    """({источник: строки}, {упавший источник: имя исключения}). Сбой одного источника не топит
    остальные, но и не молчит: вызывающий пишет его в сообщение задачи — иначе «все источники
    упали» неотличимо от пустого дня. Стоп проверяется между источниками."""
    from app.services import jobs
    clients, out, failed = _clients(), {}, {}
    for name in (*AUTO_SOURCES, "emd"):
        if not enabled.get(name):
            continue
        if jobs.cancelled(run):
            raise jobs.Cancelled()
        jobs.report(run, stage=name, current=f"собираю: {_SOURCE_RU[name]}")
        try:
            if name == "emd":
                rows = [{"domain": c["domain"], "source": "emd", "lane": "free",
                         "acquire_deadline": None, "market_lang": c["lang"] or None}
                        for c in emd_candidates(st["emd_sets"], st["brand_tokens"])]
            else:
                rows = clients[name]().list_dropping()
        except Exception as e:  # noqa: BLE001 — один источник упал, остальные идут
            logger.warning("discovery source %s failed: %s", name, e)
            failed[name] = type(e).__name__
            continue
        if not rows:
            logger.warning("discovery source %s дал 0 строк (пусто/сменился формат?)", name)
        out[name] = rows
    return out, failed


def _known(db, names: list) -> dict:
    """{домен: Domain} для уже известных — чанками (предел параметров psycopg)."""
    from sqlalchemy import select
    from app.models.domain import Domain
    out = {}
    for i in range(0, len(names), _CHUNK):
        part = names[i:i + _CHUNK]
        out.update({d.domain: d for d in db.execute(
            select(Domain).where(Domain.domain.in_(part))).scalars()})
    return out


def _dr_purge() -> None:
    """Удалить записи dr_seen старше 4 суток — в начале прогона, иначе таблица растёт без края."""
    from sqlalchemy import delete
    from app.db import SessionLocal
    from app.models.domain import DrSeen
    with SessionLocal() as db:
        db.execute(delete(DrSeen).where(DrSeen.checked_at < _now() - _DR_MEMORY))
        db.commit()


def _dr_memo(names: list, since: datetime) -> dict:
    """{домен: DR | None} спрошенных не раньше `since` — чанками (предел параметров psycopg)."""
    from sqlalchemy import select
    from app.db import SessionLocal
    from app.models.domain import DrSeen
    out = {}
    with SessionLocal() as db:
        for i in range(0, len(names), _CHUNK):
            part = names[i:i + _CHUNK]
            out.update({r.domain: r.dr for r in db.execute(
                select(DrSeen).where(DrSeen.domain.in_(part), DrSeen.checked_at >= since)).scalars()})
    return out


def _dr_remember(part: list, drs: dict, now: datetime) -> None:
    """Запомнить ВСЕ спрошенные домены пачки (`dr` None — Ahrefs его не вернул). Upsert как
    delete+insert: одинаково работает на SQLite тестов и PostgreSQL бокса."""
    from sqlalchemy import delete, insert
    from app.db import SessionLocal
    from app.models.domain import DrSeen
    with SessionLocal() as db:
        db.execute(delete(DrSeen).where(DrSeen.domain.in_(part)))
        db.execute(insert(DrSeen), [{"domain": d, "dr": drs.get(d), "checked_at": now} for d in part])
        db.commit()


def _retry_after(e: httpx.HTTPStatusError) -> float:
    """Секунды из Retry-After ответа 429; нет заголовка или в нём дата — 60 (окно лимита — минута).
    Число — в [1, 120] (финальное ревью): огромное подвесило бы discovery на сутки, держа замок
    задачи, а отрицательное, нулевое или NaN — повтор без паузы (NaN ещё и роняет сон ValueError)."""
    try:
        x = float(e.response.headers.get("Retry-After") or 60)
    except ValueError:
        return 60.0
    return 1.0 if math.isnan(x) else min(max(x, 1.0), 120.0)


def _dr_once(ahrefs, part: list) -> dict:
    """dr_free с одним повтором на 429. Ретрай BaseClient (3 попытки за ~3 с) короче минутного
    окна лимита Ahrefs (60 запросов в минуту) — после него ждём, сколько просит сервер."""
    try:
        return ahrefs.dr_free(part)
    except httpx.HTTPStatusError as e:
        if e.response.status_code != 429:
            raise
        _sleep(_retry_after(e))
        return ahrefs.dr_free(part)


def _dr_filter(names: list, min_dr: float, ahrefs, run=None) -> tuple[dict, int, set, list]:
    """({домен: DR} прошедших порог, сколько ПРОПУЩЕНО, имена, взятые из памяти dr_seen, причины
    пропуска). Причины — для сообщения задачи (финальное ревью, minor «е»): «DR недоступен — N»
    без причины не говорил оператору, что чинить. Только класс исключения / HTTP-код — ни URL, ни
    ключа (httpx кладёт полный URL в текст HTTPStatusError).

    Память (решение оператора Р4): домен, спрошенный за последние 4 суток, в Ahrefs не идёт.
    Отсеянные домены в `domains` не попадают, и без памяти их DR спрашивался бы на каждом прогоне
    (живьём: 123 запроса на 122 тыс. доменов при повторе в тот же день), а лицензия DR-free
    запрещает систематический сбор. Запомненный DR не выбрасывается: домен с DR >= порога из
    памяти проходит без запроса — иначе отмена или рестарт воркера между DR и записью похоронили
    бы ценный дроп на 4 суток.

    Пропущено («DR недоступен»):
      · пачка, на которой Ahrefs упал, — её домены не сохраняем и НЕ запоминаем: без DR нечем
        отличить ценный дроп от спам-мусора, а следующий прогон спросит снова;
      · пачка, на которую пришёл 200 без ЕДИНОГО спрошенного домена (пустое или чужое тело), — тоже
        сбой пачки, не запоминаем: иначе она похоронена на 4 суток (находка R2-7);
      · 401/403 — ключ не принят, остальные пачки ответят так же: опрос прекращается, весь остаток
        пропущен; пустой ключ у клиента — в Ahrefs не ходим вовсе (находка R2-8);
      · домен, которого нет в непустом ответе (аномалия: на несуществующий домен Ahrefs отдаёт 0.0), —
        он запоминается с dr=None, чтобы не переспрашивать его каждый час.
    Между пачками — пауза и проверка «стопа»: DropCatch даёт ~134 пачки за прогон.
    """
    from app.services import jobs
    now = _now()
    memo = _dr_memo(names, now - _DR_MEMORY)
    kept = {d: float(dr) for d, dr in memo.items() if dr is not None and float(dr) >= min_dr}
    ask = [n for n in names if n not in memo]
    if ask and getattr(ahrefs, "api_key", None) == "":     # у фейков тестов атрибута нет
        logger.warning("DR-фильтр: AHREFS_API_KEY пуст — %d доменов без DR пропущены", len(ask))
        return kept, len(ask), set(memo), ["ключ AHREFS_API_KEY не задан"]
    skipped, why = 0, []

    def _why(reason: str) -> None:
        if reason not in why:                              # одна причина на сотню пачек — один раз
            why.append(reason)
    for i in range(0, len(ask), _DR_BATCH):
        if jobs.cancelled(run):
            raise jobs.Cancelled()
        jobs.report(run, done=i, total=len(ask), current=f"DR: {i} из {len(ask)}")
        if i:
            _sleep(_DR_PAUSE)
        part = ask[i:i + _DR_BATCH]
        try:
            got = _dr_once(ahrefs, part)
        except Exception as e:  # noqa: BLE001 — пачка без DR пропускается, прогон идёт дальше
            code = e.response.status_code if isinstance(e, httpx.HTTPStatusError) else None
            _why(f"{type(e).__name__} {code}" if code else f"сбой Ahrefs: {type(e).__name__}")
            if code in (401, 403):
                logger.warning("DR-фильтр: Ahrefs %s — ключ не принят, остаток %d пропущен",
                               code, len(ask) - i)
                skipped += len(ask) - i
                break
            logger.warning("DR-фильтр: пачка из %d пропущена (%s)", len(part), type(e).__name__)
            skipped += len(part)
            continue
        want = set(part)
        drs = {d: float(dr) for d, dr in got.items() if d in want}
        if not drs:
            logger.warning("DR-фильтр: в ответе нет ни одного из %d спрошенных — пачка пропущена",
                           len(part))
            _why("ответ Ahrefs без спрошенных доменов")
            skipped += len(part)
            continue
        if want - set(drs):
            _why("доменов нет в ответе Ahrefs")
        skipped += len(want - set(drs))
        kept.update({d: dr for d, dr in drs.items() if dr >= min_dr})
        _dr_remember(part, drs, now)
    return kept, skipped, set(memo), why


def _new_domain(name: str, c: dict, dr):
    from app.models.domain import Domain
    return Domain(domain=name, source=c.get("source"), lane=c.get("lane"),
                  acquire_deadline=c.get("acquire_deadline"), market_lang=c.get("market_lang"),
                  dr=dr)


def _insert(names: list, cand: dict, drs: dict, run=None) -> int:
    """Вставка чанками; прогресс и «стоп» — между чанками (записанное остаётся). Гонка с
    параллельным прогоном (unique на domain) — откат чанка, перечитать известные и досыпать
    остаток (одной повторной попытки хватает)."""
    from sqlalchemy.exc import IntegrityError
    from app.db import SessionLocal
    from app.services import jobs
    n = 0
    for i in range(0, len(names), _CHUNK):
        if jobs.cancelled(run):
            raise jobs.Cancelled()
        jobs.report(run, done=i, total=len(names), current=f"запись: {i} из {len(names)}")
        part = names[i:i + _CHUNK]
        with SessionLocal() as db:
            db.add_all([_new_domain(x, cand[x], drs.get(x)) for x in part])
            try:
                db.commit()
                n += len(part)
            except IntegrityError:
                db.rollback()
                seen = _known(db, part)
                rest = [x for x in part if x not in seen]
                db.add_all([_new_domain(x, cand[x], drs.get(x)) for x in rest])
                db.commit()
                n += len(rest)
    return n


def _enrich(known: dict, cand: dict) -> None:
    """Уже известный, ещё НЕ обработанный домен дозаполняется тем, чего у него не было (лейн,
    дедлайн, язык EMD). Статус и прочее не трогаем: повторный прогон не откатывает решённое."""
    for name, d in known.items():
        if d.status != "discovered":
            continue
        c = cand[name]
        for attr in ("lane", "acquire_deadline", "market_lang"):
            if getattr(d, attr) is None and c.get(attr) is not None:
                setattr(d, attr, c[attr])


def _line(src: str, s: dict, min_dr: float) -> str:
    if src == "emd":
        return f"EMD: {s['rows']} вариантов → новых {s['new']}"
    return (f"{_SOURCE_RU[src]}: {s['rows']} строк → наши зоны {s['zone']} → новых {s['new']}"
            f" → DR≥{min_dr:g}: {s['saved']}")


def run_discovery() -> int:
    """Собрать включённые источники и записать новых кандидатов. Прогресс — через jobs.track
    (видно и когда зовёт оркестратор из воркера). Возвращает, сколько доменов вставлено."""
    from app.db import SessionLocal
    from app.integrations.ahrefs import AhrefsClient
    from app.services import jobs
    from app.services.settings import get_settings

    st = get_settings()
    enabled, min_dr = st["sources_enabled"], float(st["min_dr"])
    on = [k for k in (*AUTO_SOURCES, "emd") if enabled.get(k)]
    stages = ([{"key": k, "label": _SOURCE_RU[k]} for k in on]
              + [{"key": "dr", "label": "DR-фильтр"}, {"key": "save", "label": "запись"}])
    with jobs.track("discovery", stages=stages) as run:
        _dr_purge()
        by_src, failed = _collect(enabled, st, run)
        fails = [f"{_SOURCE_RU[k]}: упал ({v})" for k, v in failed.items()]
        cand, stats, emd = {}, {}, set()
        for src, rows in by_src.items():
            s = stats.setdefault(src, {"rows": len(rows), "zone": 0, "new": 0, "saved": 0})
            for r in rows:
                d = canonical_domain(r.get("domain"))
                if not d or (src in AUTO_SOURCES and not tld_match(d, st["tld_allowlist"])):
                    continue
                s["zone"] += 1
                c = cand.setdefault(d, {**r, "domain": d})
                if src == "emd":
                    emd.add(d)
                    c.setdefault("market_lang", r.get("market_lang"))
        if not cand:
            jobs.report(run, done=0, total=0, current="",
                        message=" · ".join(["нет кандидатов", *fails]))
            return 0
        with SessionLocal() as db:
            known = _known(db, list(cand))
            _enrich(known, cand)
            db.commit()
        fresh = [n for n in cand if n not in known]
        # EMD-имя, которое нашёл и автоматический источник, DR-фильтр не проходит: у новорега и
        # свободного EMD ссылок и не должно быть, DR 0 — не повод его терять
        auto = [n for n in fresh if cand[n]["source"] in AUTO_SOURCES and n not in emd]
        jobs.report(run, stage="dr", current=f"DR для {len(auto)} новых")
        drs, skipped, remembered, why = (_dr_filter(auto, min_dr, AhrefsClient(), run)
                                         if auto else ({}, 0, set(), []))
        for n in fresh:
            if n not in remembered:              # «новых» = не известных и не спрошенных за 4 суток
                stats[cand[n]["source"]]["new"] += 1
        keep = [n for n in fresh if n not in auto or n in drs]
        jobs.report(run, stage="save", current=f"запись {len(keep)}")
        inserted = _insert(keep, cand, drs, run)
        for n in keep:
            stats[cand[n]["source"]]["saved"] += 1
        msg = " · ".join(_line(src, s, min_dr) for src, s in stats.items())
        if remembered:
            msg += f" · DR из памяти (4 сут) — {len(remembered)}"
        if skipped:
            msg += f" · DR недоступен — {skipped} пропущено" + (f" ({'; '.join(why)})" if why else "")
        jobs.report(run, done=1, total=1, current="", message=" · ".join([msg, *fails]))
        return inserted


def add_list(text: str) -> dict:
    """Ручной список оператора (то, что он нашёл в ExpiredDomains/SpamZilla — их автоматизировать
    запрещено их же правилами) -> discovered, source='list'. Зону и бренды здесь НЕ режем: домен
    дойдёт до W0 и получит tld_closed/trademark — оператор увидит причину, а не тихую потерю.
    Больше _LIST_MAX за раз не берём, но и не молчим: сколько отброшено — в `cut`."""
    from app.db import SessionLocal
    tokens = [t for t in re.split(r"[\s,;]+", text or "") if t]
    bad, names = 0, []
    for x in tokens[:_LIST_MAX]:
        d = canonical_domain(x)
        if d is None:
            bad += 1
        elif d not in names:
            names.append(d)
    with SessionLocal() as db:
        known = _known(db, names)
    fresh = [n for n in names if n not in known]
    added = _insert(fresh, {n: {"source": "list"} for n in fresh}, {})
    return {"added": added, "known": len(names) - len(fresh), "bad": bad,
            "cut": max(0, len(tokens) - _LIST_MAX)}
