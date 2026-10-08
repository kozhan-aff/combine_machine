"""Ранги доменов (W2d-cc-ranks): бесплатный сигнал authority вместо Ahrefs DR.

Источники (транспорт и лицензии — integrations/crawlrank.py):
  * Common Crawl web graph, файл domain-ranks.txt.gz актуального среза (~133 млн строк). Читается ПОТОКОМ;
    из него в таблицу domain_ranks попадают только строки ПУЛА кандидатов (набор доменов из БД) — таблица
    ограничена размером пула (десятки тысяч), а не графом.
  * Majestic Million (топ-1M) — необязательный ПОЛОЖИТЕЛЬНЫЙ бонус: нет в топе — ничего не значит, поэтому
    для него хранятся только совпавшие строки.

Три состояния домена, которые нельзя смешивать (инвариант «не знаем != плохо»):
  * строка cc с pagerank       — домен есть в графе, ранг известен;
  * строка cc с pagerank NULL  — ПРОВЕРИЛИ, в графе краулинга нет (это данные: «ссылок не нашли», authority 0);
  * строки нет                 — не проверяли (домен появился после загрузки / ранги не загружены): «нет данных»,
                                 authority нейтральная 0.5, а не ноль.

Перцентиль pagerank = 1 - pr_pos / N, где N — число строк графа (наибольший pr_pos файла). Нормировка authority —
линейно между порогами `rank_pct_low`/`rank_pct_full` из /settings. Хвост harmonic centrality почти плоский,
поэтому ранжируем по pagerank; harmonic и n_hosts хранятся для калибровки и показа.

Загрузка раз в месяц (воркер) и по кнопке; повторная загрузка того же среза пропускается, если все домены пула
уже покрыты (иначе пул вырос — перечитываем, чтобы новые кандидаты не остались «без данных»).
"""
import logging
from collections.abc import Iterable
from datetime import datetime, timezone

from sqlalchemy import delete, func, insert, select

from app.integrations.crawlrank import domain_to_rev, rev_to_domain

log = logging.getLogger(__name__)

CC_MIN_ROWS = 1_000_000        # файл короче — усечён/подменён: применять нельзя (иначе весь пул «нет в графе»)
MAJESTIC_MIN_ROWS = 500_000
MAJESTIC_BONUS = 0.25          # добавка к authority за попадание в топ-1M (потолок 1.0)
CHUNK = 5000
_CC_DEFAULT_COLS = ("harmonicc_pos", "harmonicc_val", "pr_pos", "pr_val", "host_rev", "n_hosts")


def _num(x, cast):
    try:
        return cast(x)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------------------------- разбор
def parse_cc(lines: Iterable[str], wanted_rev: set) -> tuple[dict, int, int]:
    """Поток строк domain-ranks -> ({домен: {harmonic, pagerank, n_hosts, pr_pos}}, строк_всего, макс_pr_pos).
    Колонки — по ИМЕНАМ шапки (`#host_rev` …), без шапки — по дефолтному порядку. Отбор идёт по сырому
    `host_rev` против готового набора обратных имён (разворачивать каждую из 133 млн строк незачем)."""
    cols = {n: i for i, n in enumerate(_CC_DEFAULT_COLS)}
    found, total, max_pos = {}, 0, 0
    for line in lines:
        if not line.strip():
            continue
        if line.startswith("#"):
            names = [c.lstrip("#").strip() for c in line.rstrip("\n").split("\t")]
            if "host_rev" not in names:
                raise ValueError("domain-ranks: в шапке нет колонки host_rev — формат изменился")
            cols = {n: i for i, n in enumerate(names)}
            continue
        parts = line.rstrip("\n").split("\t")
        try:
            rev = parts[cols["host_rev"]].strip().lower()
        except (KeyError, IndexError):
            continue
        total += 1
        pos = _num(parts[cols["pr_pos"]], int) if "pr_pos" in cols and len(parts) > cols["pr_pos"] else None
        if pos is not None and pos > max_pos:
            max_pos = pos
        if rev in wanted_rev:
            def col(name, cast):
                i = cols.get(name)
                return _num(parts[i], cast) if i is not None and i < len(parts) else None
            found[rev_to_domain(rev)] = {"harmonic": col("harmonicc_val", float), "pagerank": col("pr_val", float),
                                         "n_hosts": col("n_hosts", int), "pr_pos": pos}
    return found, total, max_pos


def parse_majestic(lines: Iterable[str], wanted: set) -> tuple[dict, int]:
    """CSV Majestic Million -> ({домен: GlobalRank}, строк_всего). Колонки по именам шапки (GlobalRank, Domain)."""
    import csv
    rd = csv.reader(lines)
    head = next(rd, None)
    if not head or "GlobalRank" not in head or "Domain" not in head:
        raise ValueError("Majestic: в шапке нет GlobalRank/Domain — формат изменился")
    ir, idm = head.index("GlobalRank"), head.index("Domain")
    found, total = {}, 0
    for row in rd:
        if len(row) <= max(ir, idm):
            continue
        total += 1
        dom = row[idm].strip().lower()
        if dom in wanted:
            pos = _num(row[ir], int)
            if pos is not None:
                found[dom] = pos
    return found, total


# ---------------------------------------------------------------------------------------------- пул и загрузка
def pool_domains(db) -> set:
    """Кандидаты, для которых нужны ранги: все домены БД, кроме унаследованных ru-записей v1."""
    from app.models.domain import Domain
    rows = db.scalars(select(Domain.domain).where(func.coalesce(Domain.reject_reason, "") != "legacy_ru"))
    return {d.lower() for d in rows if d}


def _insert(db, rows: list) -> None:
    from app.models.domain_rank import DomainRank
    for i in range(0, len(rows), CHUNK):
        db.execute(insert(DomainRank), rows[i:i + CHUNK])


def load_cc(db, found: dict, pool: set, total: int, release: str, now=None) -> dict:
    """Заменить срез cc ОДНОЙ транзакцией (коммитит вызывающий): строка на КАЖДЫЙ домен пула — со рангом,
    если он в графе, и с NULL, если нет («проверили — нет»). -> {matched, absent, rows}."""
    from app.models.domain_rank import DomainRank
    db.execute(delete(DomainRank).where(DomainRank.source == "cc"))
    rows = []
    for d in sorted(pool):
        r = found.get(d)
        row = {"domain": d, "source": "cc", "release": release[:64], "harmonic_centrality": None,
               "pagerank": None, "n_hosts": None, "pct": None}
        if r:
            pos = r["pr_pos"]
            row.update(harmonic_centrality=r["harmonic"], pagerank=r["pagerank"], n_hosts=r["n_hosts"],
                       pct=round(max(0.0, min(1.0, 1 - pos / total)), 6) if pos is not None and total else None)
        if now is not None:
            row["updated_at"] = now
        rows.append(row)
    _insert(db, rows)
    return {"matched": len(found), "absent": len(pool) - len(found), "rows": len(rows)}


def load_majestic(db, found: dict, release: str, now=None) -> dict:
    from app.models.domain_rank import DomainRank
    db.execute(delete(DomainRank).where(DomainRank.source == "majestic"))
    rows = [{"domain": d, "source": "majestic", "rank_pos": pos, "release": release[:64]}
            | ({"updated_at": now} if now is not None else {}) for d, pos in sorted(found.items())]
    _insert(db, rows)
    return {"matched": len(rows)}


def _uncovered(db, pool: set) -> int:
    from app.models.domain_rank import DomainRank
    have = set(db.scalars(select(DomainRank.domain).where(DomainRank.source == "cc")))
    return len(pool - have)


def refresh(client=None, force: bool = True) -> dict:
    """Обновить ранги: CC всегда, Majestic — если включён в /settings. Один источник не валит другой.
    -> {cc: {...}, majestic: {...}}; status: updated | not_modified | skipped | error.
    `force=False` (cron): тот же срез и пул покрыт целиком — стрим не начинаем."""
    from app.db import SessionLocal
    from app.integrations.crawlrank import RankClient
    from app.services import jobs, settings as st_mod

    client = client or RankClient()
    state = st_mod.get_list_state()
    want_maj = bool(st_mod.get_settings().get("rank_majestic"))
    out = {}
    with jobs.track("domain_ranks", trigger="cron" if not force else "manual") as run:
        with SessionLocal() as db:
            pool = pool_domains(db)
            # покрытие читаем ДО стрима, а транзакцию закрываем: многогигабайтный gz качается минутами,
            # и idle-in-transaction держал бы ACCESS SHARE на domains/domain_ranks — ALTER из миграции
            # встал бы в очередь, а за ним вся панель
            uncovered = _uncovered(db, pool) if (pool and not force) else None
            db.commit()
            # CC
            res = {}
            try:
                if not pool:
                    res["status"] = "skipped"
                    res["message"] = "пул кандидатов пуст"
                else:
                    url, release = client.latest_ranks_url()
                    prev = state.get("ranks:cc") or {}
                    if not force and prev.get("release") == release and uncovered == 0:
                        res.update(status="not_modified", release=release)
                    else:
                        jobs.report(run, done=0, total=2, current=f"Common Crawl {release}")
                        wanted = {domain_to_rev(d) for d in pool}
                        found, total, max_pos = parse_cc(client.iter_gz_lines(url), wanted)
                        if total < CC_MIN_ROWS:
                            raise ValueError(f"в файле {total} строк (< {CC_MIN_ROWS}) — похоже на обрыв или "
                                             "подмену, прошлый срез оставлен")
                        res.update(load_cc(db, found, pool, max_pos, release,
                                           now=datetime.now(timezone.utc)), status="updated", release=release)
                        db.commit()
                        state["ranks:cc"] = {"release": release, "at": datetime.now(timezone.utc).isoformat()}
                        st_mod.set_list_state(state)
            except Exception as e:  # noqa: BLE001 — сбой одного источника не валит второй
                db.rollback()
                res.update(status="error", error=f"{type(e).__name__}: {e}"[:200])
                log.warning("domain_ranks cc: %s", res["error"])
            out["cc"] = res
            jobs.report(run, done=1, total=2, current="Majestic")
            # Majestic
            if want_maj and pool:
                mres = {}
                try:
                    from app.config import settings
                    found, total = parse_majestic(client.iter_text_lines(settings.MAJESTIC_URL), pool)
                    if total < MAJESTIC_MIN_ROWS:
                        raise ValueError(f"в файле {total} строк (< {MAJESTIC_MIN_ROWS}) — прошлый набор оставлен")
                    rel = datetime.now(timezone.utc).date().isoformat()
                    mres.update(load_majestic(db, found, rel, now=datetime.now(timezone.utc)),
                                status="updated", release=rel)
                    db.commit()
                except Exception as e:  # noqa: BLE001
                    db.rollback()
                    mres.update(status="error", error=f"{type(e).__name__}: {e}"[:200])
                    log.warning("domain_ranks majestic: %s", mres["error"])
                out["majestic"] = mres
        bad = [f"{k}: {v['error']}" for k, v in out.items() if v.get("status") == "error"]
        msg = "; ".join(f"{k} — {v.get('status')}" + (f" ({v.get('matched')} из пула)" if "matched" in v else "")
                        for k, v in out.items())
        jobs.report(run, done=2, total=2, message=(msg + ("; ошибки: " + "; ".join(bad) if bad else ""))[:600])
    return out


# ---------------------------------------------------------------------------------------------- чтение
def lookup(domains: list[str]) -> "dict[str, dict] | None":
    """{домен: {cc_checked, pct, pagerank, n_hosts, release, majestic}}. None — таблица пуста (ранги не
    загружены): вызывающий сигнал НЕ пишет — «нет данных». cc_checked False при непустой таблице — домен
    не был в пуле на момент загрузки (тоже «нет данных»)."""
    from app.db import SessionLocal
    from app.models.domain_rank import DomainRank
    names = [d.lower() for d in domains]
    with SessionLocal() as db:
        if db.scalar(select(DomainRank.id).limit(1)) is None:
            return None
        got: dict[str, dict] = {}
        for i in range(0, len(names), 500):
            for r in db.scalars(select(DomainRank).where(DomainRank.domain.in_(names[i:i + 500]))):
                e = got.setdefault(r.domain, {"cc_checked": False, "pct": None, "pagerank": None,
                                              "n_hosts": None, "release": None, "majestic": None})
                if r.source == "cc":
                    e.update(cc_checked=True, pct=r.pct, pagerank=r.pagerank, n_hosts=r.n_hosts, release=r.release)
                elif r.source == "majestic":
                    e["majestic"] = r.rank_pos
    blank = {"cc_checked": False, "pct": None, "pagerank": None, "n_hosts": None, "release": None, "majestic": None}
    return {d: got.get(d.lower(), dict(blank)) for d in domains}


def authority_from_rank(info: dict, low: float, full: float, majestic_on: bool) -> "tuple[float | None, dict]":
    """Данные ранга домена -> (authority 0..1 | None, сводка для score_breakdown). None — данных нет:
    в compute_score это нейтральные 0.5 (или DR Ahrefs, если он есть), а не ноль."""
    val, src = None, None
    if info.get("cc_checked"):
        if info.get("pct") is not None:
            span = max(full - low, 1e-9)
            val, src = max(0.0, min(1.0, (info["pct"] - low) / span)), "cc"
        else:
            val, src = 0.0, "cc_absent"         # проверили: в графе краулинга домена нет
    if majestic_on and info.get("majestic"):
        val, src = min(1.0, (val or 0.0) + MAJESTIC_BONUS), (f"{src}+majestic" if src else "majestic")
    summary = {"source": src, "authority": val, "pct": info.get("pct"), "pagerank": info.get("pagerank"),
               "n_hosts": info.get("n_hosts"), "majestic": info.get("majestic"), "release": info.get("release")}
    return val, summary


def overview(db) -> list[dict]:
    """Для /settings: по источнику — сколько строк со рангом, сколько «нет в графе», срез и дата."""
    from app.models.domain_rank import DomainRank
    rows = db.execute(select(DomainRank.source, func.count(), func.count(DomainRank.pct),
                             func.max(DomainRank.release), func.max(DomainRank.updated_at))
                      .group_by(DomainRank.source).order_by(DomainRank.source)).all()
    return [{"source": s, "n": n, "ranked": r if s == "cc" else n, "release": rel, "updated_at": u}
            for s, n, r, rel, u in rows]
