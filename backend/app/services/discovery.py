"""M1a — discovery v2: международные источники -> `domains` (status='discovered').

Конвейер АВТОМАТИЧЕСКИХ источников (dropcatch/nominet/mx): канон-форма -> белый список зон ->
отсев известных -> бесплатный DR Ahrefs только для НОВЫХ, не спрошенных за 4 суток (`dr_seen`) ->
вставка тех, у кого DR >= min_dr.

Ключа Ahrefs НЕТ (решение оператора 2026-10: его не будет) — DR не может быть условием входа
(S1-01). Домены, чей DR получить не удалось (ключ пуст, 401/403, пачка упала), НЕ выбрасываются, а
сохраняются в резерв (dr=NULL) с капом `max_candidates_per_run` и приоритетом по дешёвым признакам
имени; цену решает бесплатная часть воронки (RDAP-возраст, архив, риск). Перед DR/резервом — фильтры
качества имени (`domain_filters.name_reject`), чтобы не гнать мусор ни в лимит DR, ни в базу.

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

from app.services.domain_filters import (canonical_domain, emd_candidates, name_reject, tld_match,
                                         zone_of)

logger = logging.getLogger(__name__)

AUTO_SOURCES = ("dropcatch", "nominet", "mx", "namesilo_auction")
_SOURCE_RU = {"dropcatch": "DropCatch", "nominet": "Nominet (.uk)", "mx": "registry.mx", "emd": "EMD",
              "namesilo_auction": "NameSilo (аукционы)"}
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
    from app.integrations.namesilo import NameSiloClient
    return {"dropcatch": DropCatchClient, "nominet": NominetClient, "mx": RegistryMxClient,
            "namesilo_auction": NameSiloClient}


def _collect(enabled: dict, st: dict, run=None, state: dict | None = None) -> tuple[dict, dict, dict, list]:
    """({источник: строки}, {упавший источник: имя исключения}, {источник: новые валидаторы},
    [источник, ответивший 304]).
    Сбой одного источника не топит остальные, но и не молчит: вызывающий пишет его в сообщение
    задачи — иначе «все источники упали» неотличимо от пустого дня. Стоп проверяется между
    источниками.

    `state` — валидаторы условного GET с прошлого УСПЕШНОГО прогона (S1-11): 304 -> источник
    пропущен (ни в `out`, ни в `failed`: вызывающий пишет «не менялся»). Новые валидаторы
    возвращаются третьим значением и сохраняются только после успешной записи."""
    from app.integrations.base import NotModified
    from app.services import jobs
    clients, out, failed, fresh_state, unchanged = _clients(), {}, {}, {}, []
    state = state or {}
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
                cli = clients[name]()
                if state.get(name):
                    cli.validators = state[name]
                rows = cli.list_dropping()
                if getattr(cli, "validators", None):
                    fresh_state[name] = cli.validators
        except NotModified:
            unchanged.append(name)      # не пустой день и не сбой
            continue
        except Exception as e:  # noqa: BLE001 — один источник упал, остальные идут
            logger.warning("discovery source %s failed: %s", name, e)
            failed[name] = type(e).__name__
            continue
        if not rows:
            logger.warning("discovery source %s дал 0 строк (пусто/сменился формат?)", name)
        out[name] = rows
    return out, failed, fresh_state, unchanged


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


def _dr_filter(names: list, min_dr: float, ahrefs, run=None) -> tuple[dict, int, set, list, set]:
    """({домен: DR} прошедших порог, сколько ПРОПУЩЕНО, имена, взятые из памяти dr_seen, причины
    пропуска, {домены, чей DR получить НЕ удалось} — кандидаты в резерв без DR, S1-01). Причины — для сообщения задачи (финальное ревью, minor «е»): «DR недоступен — N»
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
        return kept, len(ask), set(memo), ["ключ AHREFS_API_KEY не задан"], set(ask)
    skipped, why, unknown = 0, [], set()

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
                unknown |= set(ask[i:])
                break
            logger.warning("DR-фильтр: пачка из %d пропущена (%s)", len(part), type(e).__name__)
            skipped += len(part)
            unknown |= set(part)
            continue
        want = set(part)
        drs = {d: float(dr) for d, dr in got.items() if d in want}
        if not drs:
            logger.warning("DR-фильтр: в ответе нет ни одного из %d спрошенных — пачка пропущена",
                           len(part))
            _why("ответ Ahrefs без спрошенных доменов")
            skipped += len(part)
            unknown |= set(part)
            continue
        if want - set(drs):
            _why("доменов нет в ответе Ahrefs")
        skipped += len(want - set(drs))
        kept.update({d: dr for d, dr in drs.items() if dr >= min_dr})
        _dr_remember(part, drs, now)
    return kept, skipped, set(memo), why, unknown


def _new_domain(name: str, c: dict, dr):
    from app.models.domain import Domain
    return Domain(domain=name, source=c.get("source"), lane=c.get("lane"),
                  acquire_deadline=c.get("acquire_deadline"), market_lang=c.get("market_lang"),
                  # лот аукциона: дата создания (возраст) и текущая ставка; у прочих источников их нет
                  whois_created=c.get("created"), acquire_price=c.get("bid"),
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


def _cheap_rank(name: str, c: dict) -> tuple:
    """Приоритет резерва без DR по дешёвым признакам имени (меньше — раньше): меньше цифр/дефисов,
    короче метка, ближе дроп. Не оценка ценности, а порядок отбора под кап."""
    label = name.split(".", 1)[0]
    far = datetime.max.replace(tzinfo=timezone.utc)
    dl = c.get("acquire_deadline")
    return (sum(ch.isdigit() or ch == "-" for ch in label), len(label),
            (dl if dl and dl.tzinfo else far))


_NAME_RU = {"length": "длина", "digits": "цифры", "hyphens": "дефисы", "junk": "мусорное слово"}


def _top(counter: dict, n: int = 5) -> str:
    return ", ".join(f"{k} {v}" for k, v in sorted(counter.items(), key=lambda kv: -kv[1])[:n])


def run_discovery() -> int:
    """Собрать включённые источники и записать новых кандидатов. Прогресс — через jobs.track
    (видно и когда зовёт оркестратор из воркера). Возвращает, сколько доменов вставлено.

    Все включённые автоматические источники упали (S1-07) — задача завершается `failed`
    (RuntimeError), а не «успешным нулём»: оркестратор и Пульт видят отказ."""
    from app.db import SessionLocal
    from app.integrations.ahrefs import AhrefsClient
    from app.services import jobs
    from app.services.settings import get_settings, get_source_state, set_source_state

    st = get_settings()
    enabled, min_dr = st["sources_enabled"], float(st["min_dr"])
    on = [k for k in (*AUTO_SOURCES, "emd") if enabled.get(k)]
    stages = ([{"key": k, "label": _SOURCE_RU[k]} for k in on]
              + [{"key": "dr", "label": "DR-фильтр"}, {"key": "save", "label": "запись"}])
    with jobs.track("discovery", stages=stages) as run:
        _dr_purge()
        state = get_source_state()
        by_src, failed, fresh_state, unchanged = _collect(enabled, st, run, state)
        fails = [f"{_SOURCE_RU[k]}: упал ({v})" for k, v in failed.items()]
        skip304 = [f"{_SOURCE_RU[k]}: не менялся" for k in unchanged]
        cand, stats, emd = {}, {}, set()
        zone_cut, name_cut = {}, {}
        for src, rows in by_src.items():
            s = stats.setdefault(src, {"rows": len(rows), "zone": 0, "new": 0, "saved": 0})
            for r in rows:
                d = canonical_domain(r.get("domain"))
                if not d:
                    continue
                if src in AUTO_SOURCES:
                    if not tld_match(d, st["tld_allowlist"]):
                        z = zone_of(d)                       # S1-08: видно, что отрезала зона
                        zone_cut[z] = zone_cut.get(z, 0) + 1
                        continue
                    why = name_reject(d, st["name_filters"])
                    if why:                                  # S1-10: до DR — не жжём лимит и базу
                        s["zone"] += 1
                        name_cut[_NAME_RU[why]] = name_cut.get(_NAME_RU[why], 0) + 1
                        continue
                s["zone"] += 1
                c = cand.setdefault(d, {**r, "domain": d})
                if src == "emd":
                    emd.add(d)
                    c.setdefault("market_lang", r.get("market_lang"))
        extra = []
        if zone_cut:
            extra.append(f"вне белого списка зон: {_top(zone_cut)}")
        if name_cut:
            extra.append(f"отсечено по имени: {_top(name_cut)}")
        if not cand:
            if failed and not any(by_src.values()) and not unchanged:
                # упали ВСЕ (включённые) источники, остальные пусты — это отказ, не «пустой день»
                raise RuntimeError(" · ".join(["все источники упали", *fails])[:300])
            jobs.report(run, done=0, total=0, current="",
                        message=" · ".join(["нет кандидатов", *skip304, *fails, *extra]))
            if fresh_state and not failed:
                set_source_state({**state, **fresh_state})
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
        drs, skipped, remembered, why, unknown = (_dr_filter(auto, min_dr, AhrefsClient(), run)
                                                  if auto else ({}, 0, set(), [], set()))
        for n in fresh:
            if n not in remembered:              # «новых» = не известных и не спрошенных за 4 суток
                stats[cand[n]["source"]]["new"] += 1
        # Резерв без DR (S1-01): домены, чей DR получить не удалось, не пропадают, а входят под кап
        # по дешёвым признакам имени. Домен с ОТВЕЧЕННЫМ низким DR в резерв не идёт — это не «не знаем».
        cap = int(st["max_candidates_per_run"])
        pool = sorted((n for n in auto if n in unknown and n not in drs),
                      key=lambda n: _cheap_rank(n, cand[n]))
        reserve = set(pool[:cap]) if cap > 0 else set()
        keep = [n for n in fresh if n not in auto or n in drs or n in reserve]
        jobs.report(run, stage="save", current=f"запись {len(keep)}")
        inserted = _insert(keep, cand, drs, run)
        for n in keep:
            stats[cand[n]["source"]]["saved"] += 1
        msg = " · ".join(_line(src, s, min_dr) for src, s in stats.items())
        if remembered:
            msg += f" · DR из памяти (4 сут) — {len(remembered)}"
        if reserve or pool:
            msg += (f" · без DR (резерв): {len(reserve)} из {len(pool)}"
                    + (f", кап {cap} — отсечено {len(pool) - len(reserve)}" if len(pool) > len(reserve) else ""))
        if skipped:
            msg += f" · DR недоступен — {skipped} пропущено" + (f" ({'; '.join(why)})" if why else "")
        jobs.report(run, done=1, total=1, current="",
                    message=" · ".join([msg, *skip304, *fails, *extra]))
        if fresh_state and not failed:
            # валидаторы условного GET — только после УСПЕШНОЙ записи и если ничего не упало
            set_source_state({**state, **fresh_state})
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
