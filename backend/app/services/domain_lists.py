"""Списки чистоты доменов: загрузчик UT1 blacklists + blocklistproject и поиск по ним (W2c-ut1).

Зачем: STOPWORDS истории (wayback.py) знают только EN/RU, а v2 нацелен на .mx/.de/.com.br — испанское
или португальское казино проходит словарь. Списки закрывают это на уровне САМИХ ДОМЕНОВ, языконезависимо:
«этот домен числился в gambling/adult/phishing/malware/drugs». Это сигнал о домене, а не история страниц.

ЛИЦЕНЗИИ и транспорт — в integrations/domainlists.py (UT1 — CC BY-SA 4.0, blocklistproject — Unlicense;
файлы качаются в рантайме, в репозиторий не попадает ничего; GPL-списки не используются).

Как работает:
  * `refresh()` — раз в сутки (воркер, 03:30 UTC) качает категории условным GET (ETag/Last-Modified,
    валидаторы в scoring_settings.discovery_opts["list_state"]) и ПОТОКОМ разбирает файл. В таблицу идут
    только регистрируемые имена из зон белого списка (tld_match): adult у UT1 — миллионы хостов, а
    кандидатам нужны единицы зон.
  * Обновление категории атомарно (одна транзакция, diff с прошлым набором). Пустой файл или список,
    усохший вдвое, — отказ: битая отдача не должна «очистить» всю базу и выдать непроверенное за чистое.
  * `lookup()` — для волны risk: {домен: [категории]}; None — таблица пуста (списки не загружены),
    это «не знаем», а не «чисто».
Попадание — МЯГКИЙ сигнал (`score_breakdown.list_hits`): закрывает пакетное одобрение. Жёсткий отказ
(категории scoring_config.HARD_LIST_CATEGORIES) — только при включённой настройке `hard_reject_lists`.
"""
import logging
import tarfile
from collections.abc import Iterator

from sqlalchemy import delete, func, insert, select

from app.services import scoring_config as cfg
from app.services.domain_filters import tld_match

log = logging.getLogger(__name__)

# (источник, имя на стороне источника, наша категория). Выбор категорий:
#   adult/gambling — жёсткие кандидаты; phishing/malware/drugs — мягкие (шумные, но дешёвые).
#   ads/tracker/redirector и пр. НЕ берём: реклама на сайте — не «грязная история» домена.
LISTS = [
    ("ut1", "adult", "adult"), ("ut1", "gambling", "gambling"), ("ut1", "phishing", "phishing"),
    ("ut1", "malware", "malware"), ("ut1", "drogue", "drugs"),
    ("blp", "porn", "adult"), ("blp", "gambling", "gambling"), ("blp", "phishing", "phishing"),
    ("blp", "malware", "malware"), ("blp", "drugs", "drugs"),
]
SOURCE_RU = {"ut1": "UT1 (CC BY-SA 4.0)", "blp": "blocklistproject (Unlicense)"}
CATEGORY_RU = {"adult": "adult", "gambling": "казино", "phishing": "фишинг", "malware": "малварь",
               "drugs": "наркотики"}
SHRINK_GUARD = 0.5            # новый список меньше половины прошлого (при прошлом >= MIN) — не применяем
SHRINK_MIN = 1000
CHUNK = 5000


def list_url(source: str, remote: str) -> str:
    from app.config import settings
    if source == "ut1":
        return f"{settings.DOMAIN_LISTS_UT1_URL.rstrip('/')}/{remote}.tar.gz"
    return f"{settings.DOMAIN_LISTS_BLP_URL.rstrip('/')}/{remote}-nl.txt"


# ---------------------------------------------------------------------------------------------- разбор
def iter_ut1(path: str) -> Iterator[str]:
    """Строки файла `<категория>/domains` внутри tar.gz — потоком, архив целиком в память не читаем."""
    with tarfile.open(path, "r:gz") as tf:
        for m in tf:
            if m.isfile() and (m.name == "domains" or m.name.endswith("/domains")):
                f = tf.extractfile(m)
                if f is None:
                    continue
                for raw in f:
                    yield raw.decode("utf-8", errors="replace")
                return
    raise ValueError("UT1: в архиве нет файла domains — формат изменился")


def iter_plain(path: str) -> Iterator[str]:
    with open(path, encoding="utf-8", errors="replace") as f:
        yield from f


def normalize(line: str, allow) -> str | None:
    """Строка списка -> регистрируемое имя из белого списка зон или None. Понимает и «домен», и
    hosts-формат «0.0.0.0 домен»; комментарии и URL с путём отбрасывает."""
    line = line.split("#", 1)[0].strip().lower()
    if not line:
        return None
    d = line.split()[-1].rstrip(".")
    if d.startswith("www."):
        d = d[4:]
    if "/" in d or ":" in d or not d or " " in d:
        return None
    return d if tld_match(d, allow) else None


# ---------------------------------------------------------------------------------------------- загрузка
def _allow_sig(allow) -> str:
    return ",".join(sorted(allow))


def load_list(db, source: str, category: str, lines, now=None) -> dict:
    """Применить новый набор доменов категории (diff с прошлым, ОДНА транзакция — коммитит вызывающий).
    -> {added, removed, total}. Бросает ValueError на пустой/усохший вдвое список (БД не тронута)."""
    from app.models.domain_list import DomainList
    new = set(lines)
    if not new:
        raise ValueError(f"{source}/{category}: список пуст после разбора — не применяем")
    cond = (DomainList.source == source, DomainList.category == category)
    old = set(db.scalars(select(DomainList.domain).where(*cond)))
    if len(old) >= SHRINK_MIN and len(new) < len(old) * SHRINK_GUARD:
        raise ValueError(f"{source}/{category}: список усох с {len(old)} до {len(new)} — похоже на "
                         "битую отдачу, прошлый набор оставлен")
    gone, fresh = sorted(old - new), sorted(new - old)
    for i in range(0, len(gone), CHUNK):
        db.execute(delete(DomainList).where(*cond, DomainList.domain.in_(gone[i:i + CHUNK])))
    for i in range(0, len(fresh), CHUNK):
        rows = [{"domain": d, "category": category, "source": source} for d in fresh[i:i + CHUNK]]
        if now is not None:
            for r in rows:
                r["updated_at"] = now
        db.execute(insert(DomainList), rows)
    return {"added": len(fresh), "removed": len(gone), "total": len(new)}


def refresh(client=None, lists=None) -> list[dict]:
    """Обновить все списки (или `lists`). Один сбойный список не валит остальные; итог по каждому:
    {list, status: updated|not_modified|error, ...}. Валидаторы пишутся ПОСЛЕ успешного применения
    (упавший разбор не должен «потерять день» — следующий прогон получил бы 304 на неразобранный файл)."""
    import os
    from datetime import datetime, timezone
    from app.db import SessionLocal
    from app.integrations.base import NotModified
    from app.integrations.domainlists import DomainListClient
    from app.services import jobs, settings as st_mod

    client = client or DomainListClient()
    allow = st_mod.get_settings()["tld_allowlist"]
    sig = _allow_sig(allow)
    state = st_mod.get_list_state()
    results = []
    with jobs.track("domain_lists", trigger="cron") as run:
        todo = list(lists if lists is not None else LISTS)
        for i, (source, remote, cat) in enumerate(todo):
            key = f"{source}:{cat}"
            res = {"list": key}
            path = None
            try:
                prev = state.get(key) or {}
                # другой белый список зон — другой срез файла: старые валидаторы не годятся
                validators = prev if prev.get("allow") == sig else None
                path, new_v = client.download(list_url(source, remote), validators)
                it = iter_ut1(path) if source == "ut1" else iter_plain(path)
                names = (n for n in (normalize(x, allow) for x in it) if n)
                with SessionLocal() as db:
                    res.update(load_list(db, source, cat, names, now=datetime.now(timezone.utc)))
                    db.commit()
                state[key] = {**new_v, "allow": sig}
                st_mod.set_list_state(state)
                res["status"] = "updated"
            except NotModified:
                res["status"] = "not_modified"
            except Exception as e:  # noqa: BLE001 — один список не валит остальные
                res.update(status="error", error=f"{type(e).__name__}: {e}"[:200])
                log.warning("domain_lists %s: %s", key, res["error"])
            finally:
                if path:
                    try:
                        os.unlink(path)
                    except OSError:
                        pass
            results.append(res)
            jobs.report(run, done=i + 1, total=len(todo), current=key)
        bad = [r for r in results if r["status"] == "error"]
        jobs.report(run, message=f"списки чистоты: {len(results) - len(bad)} из {len(results)} ок"
                                 + (f", ошибки: {', '.join(r['list'] for r in bad)}" if bad else ""))
    return results


# ---------------------------------------------------------------------------------------------- чтение
def lookup(domains: list[str]) -> "dict[str, list[str]] | None":
    """{домен: [категории]} (пустой список — в списках нет). None — таблица пуста: списки не загружены,
    «не знаем» (вызывающий НЕ пишет сигнал, чтобы не выдать незнание за чистоту)."""
    from app.db import SessionLocal
    from app.models.domain_list import DomainList
    names = [d.lower() for d in domains]
    with SessionLocal() as db:
        if db.scalar(select(DomainList.id).limit(1)) is None:
            return None
        found: dict[str, set] = {}
        for i in range(0, len(names), 500):
            for dom, cat in db.execute(select(DomainList.domain, DomainList.category)
                                       .where(DomainList.domain.in_(names[i:i + 500]))):
                found.setdefault(dom, set()).add(cat)
    return {d: sorted(found.get(d.lower(), ())) for d in domains}


def hard_hits(cats) -> list[str]:
    """Категории попадания, дающие жёсткий отказ при включённой настройке."""
    return [c for c in (cats or ()) if c in cfg.HARD_LIST_CATEGORIES]


def pool_counts(db) -> dict:
    """Для /settings: сколько кандидатов пула (discovered/scored/approved) попало бы под жёсткий отказ и
    под любое попадание — оператор видит эффект тумблера ДО его включения."""
    from app.models.domain import Domain
    from app.models.domain_list import DomainList
    live = (Domain.status.in_(("discovered", "scored", "approved")),
            func.coalesce(Domain.reject_reason, "") != "legacy_ru")

    def n(*extra) -> int:
        return db.scalar(select(func.count(func.distinct(Domain.id)))
                         .select_from(Domain).join(DomainList, DomainList.domain == Domain.domain)
                         .where(*live, *extra)) or 0
    return {"hard": n(DomainList.category.in_(cfg.HARD_LIST_CATEGORIES)), "any": n()}


def overview(db) -> list[dict]:
    """По каждому списку: сколько доменов и когда последний раз пополнялся (для /settings)."""
    from app.models.domain_list import DomainList
    rows = db.execute(select(DomainList.source, DomainList.category, func.count(), func.max(DomainList.updated_at))
                      .group_by(DomainList.source, DomainList.category)
                      .order_by(DomainList.source, DomainList.category)).all()
    return [{"source": s, "category": c, "n": n, "updated_at": u} for s, c, n, u in rows]
