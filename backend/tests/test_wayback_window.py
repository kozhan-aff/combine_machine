"""Окно снимков Wayback: мы смотрим на КОНЕЦ жизни домена, а не только на его молодость.

Корень бага (живой замер 2026-07-13 на web.archive.org, lenta.ru): единственный CDX-запрос с
`limit=400` отдаёт первые 400 записей ПО ВОЗРАСТАНИЮ времени -> `199911…200512`. Чем богаче
архив (а богатый архив = ценный старый донор), тем УЖЕ окно и тем ближе оно к рождению домена.
Годы перед дропом — ровно те, где вчера ещё чистый домен становится казино, — не запрашивались
ВООБЩЕ, а домен получал `wayback_checked=True` и мог быть авто-одобрен как «история чистая».
"""
from datetime import datetime, timedelta, timezone

import pytest

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
    * `from`/`to` — YYYYMMDD, фильтр по времени;
    * `collapse=ПОЛЕ` схлопывает СОСЕДНИЕ (в порядке выдачи) записи с одинаковым значением
      поля, оставляя первую, и применяется ДО лимита. Отсюда и весь смысл `collapse=urlkey`
      на domain-окне: поток отсортирован по urlkey, значит соседние записи одного URL — это
      его капчуры, и лимит покупает N РАЗНЫХ URL вместо N капчур алфавитно-первого.
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
        hit = _collapse(hit, p.get("collapse"))
        n = int(p["limit"])
        hit = hit[:n] if n > 0 else hit[n:]
        return [["timestamp", "original", "statuscode"]] + [[c[0], c[1], "200"] for c in hit]


_COLLAPSE_KEYS = {"urlkey": lambda c: c[1], "timestamp:8": lambda c: c[0][:8]}


def _collapse(rows, field):
    """Схлопнуть соседние записи с одинаковым значением поля (первая выживает) — как CDX."""
    if not field:
        return rows
    key = _COLLAPSE_KEYS[field]                   # неизвестное поле = тест врёт про живой CDX
    out, prev = [], object()
    for c in rows:
        k = key(c)
        if k != prev:
            out.append(c)
            prev = k
    return out


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


def _clean_root_inner_casino(n_urls: int = 40) -> list[tuple[str, str, str]]:
    """Корень остался заглушкой-новостником, а казино жило на внутренних страницах и
    поддомене. Точный матч по домену такого не видит — нужен matchType=domain.

    Архив ПЛОТНЫЙ, как у живого старого сайта: у `n_urls` безобидных внутренних URL — по дюжине
    капчур каждый, и алфавитно они РАНЬШЕ `/casino/`. Domain-окно CDX отдаёт поток в порядке
    urlkey, значит без `collapse=urlkey` лимит целиком съедают капчуры `/arch0000/…`, и ни
    `/casino/`, ни `play.old.ru` в выборку не попадают ВОВСЕ (замер ревьюера на живом клиенте).
    Разреженная фикстура из трёх URL этого не ловила — тест обещал больше, чем гарантировал код.

    `n_urls` параметризован НЕ ради разнообразия: `collapse=urlkey` покупает N РАЗНЫХ URL, но N
    ограничено лимитом CDX-запроса — сам по себе `collapse` потолок не снимает, только поднимает
    точку, где он начинает резать алфавит. При `n_urls=40` (старый потолок фикстуры) это ниже
    ЛЮБОГО разумного лимита и не отличило бы «лимит снят» от «лимит просто ещё не задет» — ровно
    та подмена, которую здесь и правим. 150 и 400 — числа, на которых ревьюер поймал регресс
    живым замером.
    """
    caps = [(_ts(datetime(2004, 1, 1) + timedelta(days=60 * i)), "http://old.ru/", CLEAN)
            for i in range(135)]                                          # 2004-01 … 2026-02
    caps += [(_ts(datetime(2024, 3, 1) + timedelta(days=30 * j)), f"http://old.ru/arch{i:04d}/",
              CLEAN)
             for i in range(n_urls) for j in range(12)]    # N*12 капчур, все алфавитно < /casino/
    caps += [(_ts(datetime(2025, 6, 1) + timedelta(days=10 * i)), "http://old.ru/casino/", CASINO)
             for i in range(30)]                                          # 2025-06 … 2026-03
    caps += [(_ts(datetime(2025, 7, 1) + timedelta(days=30 * i)), "http://play.old.ru/", CASINO)
             for i in range(8)]
    return caps


def _laundered_before_the_drop() -> list[tuple[str, str, str]]:
    """Казино 2008–2020, отмытая витрина последних лет. Плотный архив (капчура раз в 2 дня,
    2004→2026): ровно тот профиль, на котором ревьюер замерил, что окно «середины жизни»
    оплачивается CDX-запросом, но не классифицируется никогда."""
    caps, day, end = [], datetime(2004, 1, 1), datetime(2026, 4, 1)
    while day <= end:
        caps.append((_ts(day), "http://old.ru/", CASINO if 2008 <= day.year <= 2020 else CLEAN))
        day += timedelta(days=2)
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


@pytest.mark.parametrize("n_urls", [40, 150, 400])
def test_domain_match_catches_inner_casino_under_clean_root(n_urls):
    """Корень чист, казино — на /casino/ и поддомене: точный матч слеп, нужен matchType=domain.

    Потолок domain-окна поднят (`_DOMAIN_WINDOW_LIMIT`), но не убран — старая фикстура (40 URL)
    сидела ПОД любым разумным потолком и не отличала «лимит снят» от «лимит ещё не задет». 150 и
    400 — числа живого замера ревьюера: >=100 безобидных URL в опасном окне для старого донора
    норма, не экзотика, и на нынешнем (2026-07-13) HEAD они давали ложное «история чистая»."""
    arch = _Archive(_clean_root_inner_casino(n_urls))
    c = _client(arch)
    snaps = c.get_snapshots("old.ru")
    assert any(s["original"].endswith("/casino/") for s in snaps), "внутренние URL не в выборке"
    assert any(_host(s["original"]) == "play.old.ru" for s in snaps), "поддомены не в выборке"

    h = c.classify_history("old.ru", polite=0)
    assert h["prior_flags"]["casino"] is True


def test_domain_window_ceiling_is_higher_but_still_real():
    """Потолок domain-окна не снят — он ПОДНЯТ. `collapse=urlkey` покупает N РАЗНЫХ URL, но N —
    это `_DOMAIN_WINDOW_LIMIT`, число конечное: ровно на нём казино ещё видно, сразу за ним —
    снова срезается алфавитом (root < archNNNN < casino по urlkey). Пришиваем ЧИСЛО тестом, чтобы
    следующий читатель не открывал потолок заново эмпирически, как это сделал ревьюер живым
    замером на 99/150/400 URL."""
    from app.integrations.wayback import _DOMAIN_WINDOW_LIMIT

    def fixture(n_inner: int) -> list[tuple[str, str, str]]:
        # root: рождение + capture НА ГРАНИЦЕ опасного окна — держит "last" домена и сам входит
        # в urlkey-набор ровно одной записью (без дублей — collapse её и так схлопнёт).
        caps = [(_ts(datetime(2004, 1, 1)), "http://old.ru/", CLEAN),
                (_ts(datetime(2026, 4, 1)), "http://old.ru/", CLEAN)]
        caps += [(_ts(datetime(2024, 6, 1)), f"http://old.ru/arch{i:05d}/", CLEAN)
                 for i in range(n_inner)]                      # все алфавитно < /casino/
        caps += [(_ts(datetime(2025, 6, 1)), "http://old.ru/casino/", CASINO)]
        return caps

    # urlkey-порядок в опасном окне: root(1) + arch(n_inner) + casino(1) = n_inner + 2 записей.
    # На самой границе лимита (n_inner = LIMIT - 2) казино — последняя влезающая запись.
    within = _Archive(fixture(_DOMAIN_WINDOW_LIMIT - 2))
    snaps = _client(within).get_snapshots("old.ru")
    assert any(s["original"].endswith("/casino/") for s in snaps), \
        "казино ровно на границе потолка обязано быть видно"

    # на один безобидный URL больше — казино алфавитно уходит ЗА лимит.
    over = _Archive(fixture(_DOMAIN_WINDOW_LIMIT - 1))
    snaps = _client(over).get_snapshots("old.ru")
    assert not any(s["original"].endswith("/casino/") for s in snaps), \
        "потолок должен быть настоящим — если казино видно и здесь, тест не пришивает число"


def test_mid_sample_is_picked_by_time_not_by_index():
    """РЕГРЕССИЯ: «середина жизни» бралась по ИНДЕКСУ склеенного списка (`snaps[len//2]`), а
    список — это 4 блока по ~100 записей, поэтому индексная медиана падала на ГРАНИЦУ блоков —
    в опасное окно, а не в середину жизни. Замер ревьюера на этом профиле: CDX-запрос №3
    (from=20140902 to=20150828) вернул 100 записей, скачано из них 0; реально скачаны
    ['200401', '202404', '202604', '202604', '202604'] — 19 из 22 лет домена не смотрел никто,
    хотя запрос за них уплачен. Отмытое перед дропом казино снова получало «история чистая»."""
    arch = _Archive(_laundered_before_the_drop())
    h = _client(arch).classify_history("old.ru", polite=0)

    years = sorted(ts[:4] for ts, _ in arch.fetched)
    assert any("2008" <= y <= "2020" for y in years), f"середина жизни не смотрена: {years}"
    assert h["prior_flags"]["casino"] is True, "казино 2008–2020 не увидено"


def test_small_sample_still_looks_at_the_end_of_life():
    """`classify_history(sample=...)` — публичная ручка экономии квоты. При sample<=2 последний
    capture выпадал из выборки (список добивался до отказа ещё рождением и серединой) — попытка
    сэкономить тихо возвращала ровно тот баг, который здесь чинится: смотрим на молодость."""
    for sample in (1, 2):
        arch = _Archive(_casino_before_the_drop())
        _client(arch).classify_history("old.ru", sample=sample, polite=0)

        last = arch.captures[-1][0]
        assert any(ts == last for ts, _ in arch.fetched), f"sample={sample}: конец жизни не смотрен"
        assert len(arch.fetched) <= sample, f"sample={sample}: скачано {len(arch.fetched)}"


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


# --- выбор снимков на скачивание ---------------------------------------------------------

def test_pick_spends_every_slot_on_a_different_snapshot():
    """РЕГРЕССИЯ: первый цикл дедуплицировал только по `original`, но не проверял, что снимок
    уже взят, — и мог добавить рождение/середину ВТОРОЙ раз; финальный `uniq` дубль схлопывал.
    Property-прогон ревьюера: 788 из 3000 случайных архивов давали len(_pick) < sample, то есть
    качалось 3–4 снимка вместо 5. А при `checked = ok >= sample//2 + 1` одного 429 от archive.org
    тогда хватает, чтобы вердикт слетел в «история не проверена»."""
    from app.integrations.wayback import _pick

    snaps = [{"timestamp": _ts(datetime(*d)), "original": u} for d, u in [
        ((2019, 1, 1), "http://old.ru/"),
        ((2020, 1, 1), "http://old.ru/"),
        ((2024, 6, 1), "http://old.ru/"),
        ((2024, 7, 1), "http://old.ru/inner/"),   # индексная медиана И самая свежая капчура /inner/
        ((2024, 8, 1), "http://old.ru/"),
        ((2025, 1, 1), "http://old.ru/"),
        ((2026, 1, 1), "http://old.ru/"),
    ]]
    got = _pick(snaps, 5)

    assert len(got) == 5, f"слот потрачен на дубликат: скачаем {len(got)} снимков вместо 5"
    assert len({(s["timestamp"], s["original"]) for s in got}) == 5


def test_thin_danger_window_still_fills_the_sample():
    """Второй источник той же недостачи: опасное окно ТОНКОЕ (архив редкий — одна капчура за
    последние 2 года). Оба хвостовых цикла черпали только из хвоста, он кончался, и выборка
    молча схлопывалась до 3 снимков из 5 — при `checked = ok >= sample//2 + 1` одного 429
    хватает, чтобы вердикт слетел в «история не проверена». Добираем из остальной жизни:
    `sample` — это и есть бюджет на скачивание, недобор его не экономит, а только слепит."""
    from app.integrations.wayback import _pick

    snaps = [{"timestamp": _ts(datetime(2006, 1, 1) + timedelta(days=90 * i)),
              "original": "http://old.ru/"} for i in range(60)]        # 2006 … 2020
    snaps.append({"timestamp": _ts(datetime(2026, 1, 1)), "original": "http://old.ru/"})

    got = _pick(snaps, 5)

    assert len(got) == 5, f"хвост тоньше бюджета: скачаем {len(got)} снимков вместо 5"


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
