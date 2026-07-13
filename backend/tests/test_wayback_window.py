"""Окно снимков Wayback: мы смотрим на КОНЕЦ жизни домена, а не только на его молодость.

Корень бага (живой замер 2026-07-13 на web.archive.org, lenta.ru): единственный CDX-запрос с
`limit=400` отдаёт первые 400 записей ПО ВОЗРАСТАНИЮ времени -> `199911…200512`. Чем богаче
архив (а богатый архив = ценный старый донор), тем УЖЕ окно и тем ближе оно к рождению домена.
Годы перед дропом — ровно те, где вчера ещё чистый домен становится казино, — не запрашивались
ВООБЩЕ, а домен получал `wayback_checked=True` и мог быть авто-одобрен как «история чистая».
"""
from datetime import datetime, timedelta, timezone

import app.db as db
from app.models.domain import Domain
from app.services import scoring

CLEAN = "Новости дня, политика и экономика. Обзор событий, аналитика, репортажи."
CASINO = "Игровые автоматы и казино онлайн: джекпот, вулкан казино, бонусы"


class _Resp:
    def __init__(self, json_=None, text=""):
        self._json, self.text = json_, text

    def json(self):
        return self._json


class _Archive:
    """CDX-сервер настолько, насколько его видит воронка. Семантика снята С ЖИВОГО
    web.archive.org (2026-07-13), а не из головы:

    * точный матч (без `matchType`) — записи ПО ВОЗРАСТАНИЮ времени; `limit=N` режет сверху,
      `limit=-N` отдаёт ПОСЛЕДНИЕ N (тоже по возрастанию). www-хост канонизируется в тот же
      urlkey, что и голый домен;
    * `matchType=domain` — записи в порядке urlkey, а НЕ по времени (проверено: «хвост»
      domain-матча — это алфавитно-последние URL, а не свежие капчуры). Значит по домену
      ходить можно ТОЛЬКО окнами `from`/`to`, а не хвостовым лимитом;
    * `from`/`to` — YYYYMMDD, фильтр по времени.
    """

    def __init__(self, captures: list[tuple[str, str, str]]):
        self.captures = sorted(captures)          # (timestamp, original, body)
        self.cdx: list[dict] = []                 # параметры каждого CDX-запроса
        self.fetched: list[tuple[str, str]] = []  # реально скачанные снимки

    # BaseClient.request подменяется на ИНСТАНСЕ клиента — до транспорта httpx дело не доходит,
    # рубильник живой сети из conftest не при чём (см. его докстринг).
    def request(self, method, url, params=None, **kw):
        if "/cdx/" in url:
            return _Resp(json_=self._rows(params or {}))
        ts, original = url.split("/web/", 1)[1].split("id_/", 1)
        self.fetched.append((ts, original))
        for c_ts, c_url, body in self.captures:
            if (c_ts, c_url) == (ts, original):
                return _Resp(text=body)
        raise AssertionError(f"скачивают несуществующий снимок: {ts} {original}")

    def _rows(self, p):
        self.cdx.append(dict(p))
        dom = p["url"]
        if p.get("matchType") == "domain":
            hit = [c for c in self.captures if _host(c[1]) == dom or _host(c[1]).endswith("." + dom)]
            hit.sort(key=lambda c: (c[1], c[0]))          # urlkey-порядок, как у живого CDX
        else:
            hit = [c for c in self.captures if _is_root(c[1], dom)]
            hit.sort(key=lambda c: c[0])                  # точный матч — по времени
        if p.get("from"):
            hit = [c for c in hit if c[0][:8] >= p["from"]]
        if p.get("to"):
            hit = [c for c in hit if c[0][:8] <= p["to"]]
        n = int(p["limit"])
        hit = hit[:n] if n > 0 else hit[n:]
        return [["timestamp", "original", "statuscode"]] + [[c[0], c[1], "200"] for c in hit]


def _host(url: str) -> str:
    return url.split("://", 1)[1].split("/", 1)[0]


def _is_root(url: str, dom: str) -> bool:
    host, _, path = url.split("://", 1)[1].partition("/")
    return host in (dom, "www." + dom) and path in ("", "/")


def _client(archive: _Archive):
    from app.integrations.wayback import WaybackClient
    c = WaybackClient()
    c.request = archive.request
    return c


def _ts(dt: datetime) -> str:
    return dt.strftime("%Y%m%d%H%M%S")


def _casino_before_the_drop() -> list[tuple[str, str, str]]:
    """Профиль lenta.ru: плотно архивируемая молодость (она и съедает весь limit=400) —
    и казино в последние годы, перед самым дропом."""
    caps = [(_ts(datetime(1999, 11, 27) + timedelta(days=5 * i)), "http://old.ru/", CLEAN)
            for i in range(400)]                                          # 1999-11 … 2005-05
    caps += [(_ts(datetime(2023, 6, 1) + timedelta(days=18 * i)), "http://old.ru/", CASINO)
             for i in range(60)]                                          # 2023-06 … 2026-05
    return caps


def _clean_root_inner_casino() -> list[tuple[str, str, str]]:
    """Корень остался заглушкой-новостником, а казино жило на внутренних страницах и
    поддомене. Точный матч по домену такого не видит — нужен matchType=domain."""
    caps = [(_ts(datetime(2004, 1, 1) + timedelta(days=60 * i)), "http://old.ru/", CLEAN)
            for i in range(135)]                                          # 2004-01 … 2026-02
    caps += [(_ts(datetime(2025, 6, 1) + timedelta(days=10 * i)), "http://old.ru/casino/", CASINO)
             for i in range(30)]                                          # 2025-06 … 2026-03
    caps += [(_ts(datetime(2025, 7, 1) + timedelta(days=30 * i)), "http://play.old.ru/", CASINO)
             for i in range(8)]
    return caps


# --- окно выборки -----------------------------------------------------------------------

def test_last_capture_is_in_the_window():
    """РЕГРЕССИЯ (F1): при limit=400 без окон выборка упиралась в 1999–2005, последний
    capture домена не запрашивался вовсе."""
    arch = _Archive(_casino_before_the_drop())
    snaps = _client(arch).get_snapshots("old.ru")

    assert snaps, "CDX ответил, а снимков нет"
    assert snaps[-1]["timestamp"] == arch.captures[-1][0]      # последний capture — обязателен
    assert any(s["timestamp"] >= "2025" for s in snaps), "последний год жизни домена не запрошен"
    assert snaps[0]["timestamp"] == arch.captures[0][0]        # и рождение домена не потеряли


def test_late_casino_dirties_the_history():
    """Домен, ставший казино за 3 года до дропа, обязан получить prior_flags['casino'] —
    именно на этом вердикте стоит авто-одобрение."""
    arch = _Archive(_casino_before_the_drop())
    h = _client(arch).classify_history("old.ru", polite=0)

    assert h["wayback_checked"] is True
    assert h["prior_flags"]["casino"] is True
    last = arch.captures[-1][0]
    assert any(ts == last for ts, _ in arch.fetched), "последний снимок не скачивали"


def test_domain_match_catches_inner_casino_under_clean_root():
    """Корень чист, казино — на /casino/ и поддомене: точный матч слеп, нужен matchType=domain."""
    arch = _Archive(_clean_root_inner_casino())
    c = _client(arch)
    snaps = c.get_snapshots("old.ru")
    assert any(s["original"].endswith("/casino/") for s in snaps), "внутренние URL не в выборке"
    assert any(_host(s["original"]) == "play.old.ru" for s in snaps), "поддомены не в выборке"

    h = c.classify_history("old.ru", polite=0)
    assert h["prior_flags"]["casino"] is True


def test_cdx_budget_stays_small():
    """Несколько окон — норма, десятки запросов на домен — нет (воронка и так ~60 с/домен)."""
    arch = _Archive(_casino_before_the_drop())
    _client(arch).classify_history("old.ru", sample=5, polite=0)
    assert len(arch.cdx) <= 5, f"CDX-запросов на домен: {len(arch.cdx)}"
    assert len(arch.fetched) <= 5


def test_sparse_archive_survives_windowing():
    """Домен с тремя капчурами: окна схлопываются, дублей нет, порядок по времени."""
    caps = [(_ts(datetime(2011, 3, 1)), "http://thin.ru/", CLEAN),
            (_ts(datetime(2016, 8, 9)), "http://thin.ru/", CLEAN),
            (_ts(datetime(2024, 2, 2)), "http://thin.ru/", CLEAN)]
    arch = _Archive(caps)
    snaps = _client(arch).get_snapshots("thin.ru")
    assert [s["timestamp"] for s in snaps] == [c[0] for c in caps]
    assert len(arch.cdx) == 3, "весь архив влез в первое окно — хвостовой запрос холостой"


# --- evidence ---------------------------------------------------------------------------

def test_evidence_lists_what_was_actually_looked_at():
    arch = _Archive(_casino_before_the_drop())
    h = _client(arch).classify_history("old.ru", polite=0)

    ev = h["evidence"]
    assert ev and all({"url", "timestamp", "cats"} <= set(e) for e in ev)
    assert [e["timestamp"] for e in ev] == sorted(e["timestamp"] for e in ev)
    assert any("casino" in e["cats"] for e in ev), "вердикт «грязный» нечем подтвердить"
    assert {(e["timestamp"], e["url"]) for e in ev} <= set(arch.fetched)   # только реально скачанное


class _WB:
    """Клиент Wayback уже проверен выше — здесь важно лишь, доедет ли evidence до куратора."""
    def __init__(self, casino=False):
        self.casino = casino

    def classify_history(self, domain):
        return {"prior_flags": {c: (c == "casino" and self.casino)
                                for c in ("adult", "pharma", "casino", "gambling", "spam")},
                "first_seen": None, "age_years": 9.0, "wayback_checked": True, "sampled": 5,
                "evidence": [{"url": f"http://{domain}/", "timestamp": "20260101000000",
                              "cats": ["casino"] if self.casino else []}]}


def _clients(wayback):
    class _W:
        def whois_probe(self, dom):
            return {"available": False, "created": datetime(2012, 1, 1, tzinfo=timezone.utc)}
    class _R:
        def is_listed(self, dom): return False
    class _B:
        def is_blacklisted(self, dom): return False
    class _S:
        def indexed_echo(self, dom): return True
    return {"aparser": _W(), "rkn": _R(), "blacklist": _B(), "searxng": _S(), "wayback": wayback}


def _mk(name):
    with db.SessionLocal() as s:
        d = Domain(domain=name, source="backorder", status="discovered",
                   referring_domains=80, lane="bid")
        s.add(d); s.commit(); s.refresh(d)
        return d.id


def _breakdown(did):
    """Улики живут в СОХРАНЁННОМ score_breakdown (как errors/ahrefs_backlinks): out["breakdown"] —
    это только результат compute_score, см. test_funnel.py:411."""
    with db.SessionLocal() as s:
        return s.get(Domain, did).score_breakdown


def test_score_breakdown_carries_history_evidence():
    did = _mk("ev-clean.ru")
    scoring.score_domain(did, clients=_clients(_WB()))
    assert _breakdown(did)["history_evidence"], "куратору нечем проверить вердикт истории"


def test_rejected_domain_also_keeps_its_evidence():
    """Отказ по истории — тоже вердикт, и он тоже ошибается: показывать, ЧТО его вызвало."""
    did = _mk("ev-dirty.ru")
    out = scoring.score_domain(did, clients=_clients(_WB(casino=True)))
    assert out["reject_reason"] == "history_dirty"
    ev = _breakdown(did)["history_evidence"]
    assert ev and "casino" in ev[0]["cats"]
