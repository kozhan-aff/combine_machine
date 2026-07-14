"""Классификатор истории судит по ВИДИМОМУ ТЕКСТУ, а не по сырому HTML. И два флага-призрака.

Главный инвариант проекта — «домены берём за ЧИСТУЮ ИСТОРИЮ» (CLAUDE.md). Единственный, кто
эту историю устанавливает, — классификатор архивных снимков. Аудит показал, что он считает
подстроки в СЫРОЙ разметке: счётчик посещений, рекламный тег, `alt`/`title` картинки, кусок
JS-конфига — всё это «слова из прошлого домена». Порог `_MIN_HITS=2` набирается двумя
вхождениями в `<script>`, и мебельный сайт уезжает в `history_dirty` как казино.

Ошибка дорогая В ОБЕ СТОРОНЫ: ложный отказ выбрасывает чистый дроп (мы их и ищем), ложное
«чисто» тащит в портфель казино. Поэтому здесь же — контрольные случаи НАСТОЯЩЕЙ грязи: они
обязаны ловиться после ослабления счёта.
"""
import httpx
import pytest

from app.integrations.wayback import (MIN_TEXT_CHARS, STOPWORDS, WaybackClient, _classify_html,
                                      _decode, _visible_text)
from app.services.scoring import blind_reason, bulk_ok, compute_score, history_evidence

FURNITURE = "<h1>Мебель на заказ</h1><p>Диваны, кресла и шкафы-купе. Доставка по Москве.</p>"


def _client(pages: dict[str, str]) -> WaybackClient:
    """Клиент, у которого архив — это словарь {url: html}. Сети нет: подменены оба выхода
    наружу (CDX и скачивание снимка), транспорт httpx не зовётся вовсе."""
    c = WaybackClient()
    snaps = [{"timestamp": f"2020010{i}120000", "original": u}
             for i, u in enumerate(pages, 1)]
    c.get_snapshots = lambda domain, **kw: list(snaps)          # noqa: ARG005
    c._fetch_raw = lambda ts, original: pages[original]         # noqa: ARG005
    return c


def _flags(pages: dict[str, str]) -> dict:
    return _client(pages).classify_history("x.ru", polite=0)["prior_flags"]


# ---- репро аудита: разметка — это НЕ содержимое страницы ----

def test_script_body_is_not_the_history_of_the_domain():
    """Репро №1: два «casino» в `<script>` + мебель в тексте -> домен помечался казино."""
    pages = {f"http://x.ru/{i}": FURNITURE + '<script>var ad = "casino roulette casino";</script>'
             for i in range(1, 4)}
    assert _flags(pages)["casino"] is False


def test_attributes_are_not_the_history_of_the_domain():
    """Репро №2: `title=`/`alt=` — атрибуты разметки, а не видимый текст."""
    pages = {f"http://x.ru/{i}":
             FURNITURE + '<a title="casino" href="/casino"><img alt="casino" src="c.png">каталог</a>'
             for i in range(1, 4)}
    assert _flags(pages)["casino"] is False


@pytest.mark.parametrize("markup", [
    '<style>.b{background:url(/img/casino-casino.png)}</style>',   # CSS — не текст
    '<!-- casino casino -->',                                      # комментарий верстальщика
    '<meta name="keywords" content="casino, roulette, slots">',    # мета-теги не видит читатель
])
def test_invisible_markup_never_dirties_history(markup):
    pages = {f"http://x.ru/{i}": FURNITURE + markup for i in range(1, 4)}
    assert _flags(pages)["casino"] is False


# ---- обратная сторона: НАСТОЯЩАЯ грязь обязана ловиться ----

def test_real_casino_in_visible_text_is_still_caught():
    pages = {f"http://x.ru/{i}":
             "<h1>Вулкан казино</h1><p>Игровые автоматы онлайн, джекпот и бонусы</p>"
             for i in range(1, 4)}
    assert _flags(pages)["casino"] is True


def test_title_counts_as_visible_text():
    """`<title>` читатель видит во вкладке и в выдаче — он часть содержимого, не разметки."""
    assert "casino" in _classify_html(
        "<html><head><title>Казино онлайн — игровые автоматы</title></head>"
        "<body><p>Добро пожаловать</p></body></html>")


def test_phrase_survives_the_markup_between_its_words():
    """Стоп-слова — ФРАЗЫ. Теги вырезаются С РАЗДЕЛИТЕЛЕМ, иначе «игровые</td><td>автоматы»
    склеилось бы в «игровыеавтоматы», и настоящее казино прошло бы как чистый домен."""
    assert "casino" in _classify_html(
        "<table><tr><td>игровые</td><td>автоматы</td></tr></table>"
        "<p>лучшие\n игровые\n автоматы\n страны</p>")


def test_evidence_cats_match_the_visible_text_verdict():
    """Улики (Задача 1) обязаны показывать, ПО ЧЕМУ судили: вердикт по тексту -> и категории
    в уликах по тексту, иначе куратор перепроверяет не то."""
    pages = {f"http://x.ru/{i}": FURNITURE + "<script>casino casino</script>" for i in range(1, 4)}
    ev = _client(pages).classify_history("x.ru", polite=0)["evidence"]
    assert ev and all(e["cats"] == [] for e in ev)


def test_visible_text_strips_markup_and_decodes_entities():
    out = _visible_text('<p>Диваны&nbsp;и&nbsp;кресла</p><script>casino</script>')
    assert "casino" not in out
    assert "Диваны и кресла" in out


# ---- слепая зона, открытая переездом на видимый текст (правки по ревью Задачи 3) ----
#
# Судить по видимому тексту — правильно, но у этого есть цена: у страницы-редиректа, frameset'а
# и SPA-оболочки видимого текста НЕТ ВООБЩЕ. Раньше их случайно ловил шум разметки (`location.href=
# ".../casino/"` — два «casino» в скрипте), теперь они дают НОЛЬ сигнала. А это ровно те снимки,
# в которые превращают домен перед сдачей: 200 + text/html (CDX-фильтры их пропускают), заглушка
# с редиректом на казино. «Нечего было прочитать» ≠ «чисто» (Задача 2) — снимок без текста НЕ
# считается прочитанным.

BLANK = {
    "meta-refresh": '<html><head><meta http-equiv="refresh" content="0;url=http://kazino.example/">'
                    "</head><body></body></html>",
    "js-redirect": '<html><head><script>location.href="http://kazino.example/casino/";</script>'
                   "</head><body></body></html>",
    "frameset": '<html><frameset rows="*"><frame src="http://kazino.example/casino/"></frameset>'
                "</html>",
    "spa": '<html><body><div id="root"></div><script src="/app.js"></script></body></html>',
}


@pytest.mark.parametrize("kind", sorted(BLANK))
def test_snapshot_without_visible_text_is_not_a_read(kind):
    """Снимок без видимого текста не инкрементит покрытие -> `wayback_checked=False` -> 'unknown'."""
    h = _client({f"http://x.ru/{i}": BLANK[kind] for i in range(1, 4)}).classify_history(
        "x.ru", polite=0)
    assert h["sampled"] == 0, "снимок без текста засчитан как прочитанный"
    assert h["wayback_checked"] is False
    assert h["evidence"] and all(e["chars"] < MIN_TEXT_CHARS for e in h["evidence"]), \
        "куратор не увидит, что читать было нечего"


def test_js_redirect_to_casino_is_not_approved_as_clean():
    """ПРЯМАЯ РЕГРЕССИЯ переезда на видимый текст: JS-редирект на казино СТАРЫЙ код ловил
    (два «casino» в разметке скрипта), новый прочитать не может — и молча одобрял домен со
    score 0.87 как «история чистая». Прочитать нечего -> к человеку, с пометкой «вслепую»."""
    from types import SimpleNamespace

    h = _client({f"http://x.ru/{i}": BLANK["js-redirect"] for i in range(1, 4)}).classify_history(
        "x.ru", polite=0)
    out = compute_score({"wayback_checked": h["wayback_checked"], "prior_flags": h["prior_flags"],
                         "age_years": 16, "referring_domains": 2219, "indexed_echo": True})
    assert out["status"] != "approved", "домен-редирект на казино авто-одобрен как чистый"

    d = SimpleNamespace(prior_flags=h["prior_flags"], wayback_checked=h["wayback_checked"],
                        score_breakdown={"errors": [], "sampled": h["sampled"],
                                         "history_evidence": h["evidence"]})
    assert blind_reason(d), "домен, чью историю не прочитали, выглядит проверенным"
    assert bulk_ok(d) is False, "непрочитанная история пошла бы в пакетное одобрение"


def test_evidence_marks_the_unread_snapshot_for_the_curator():
    """В инбоксе пустой снимок не должен подписываться «чисто»: у улики есть `unread`."""
    from types import SimpleNamespace

    d = SimpleNamespace(score_breakdown={"history_evidence": [
        {"url": "http://x.ru/", "timestamp": "20250101000000", "cats": [], "chars": 0},
        {"url": "http://x.ru/", "timestamp": "20200101000000", "cats": [], "chars": 900},
    ]})
    blank, read = history_evidence(d)
    assert blank["unread"] is True and blank["chars"] == 0
    assert read["unread"] is False


def test_page_with_real_text_is_still_read():
    """Порог — про ПУСТОТУ, а не про краткость: нормальная страница обязана классифицироваться."""
    h = _client({f"http://x.ru/{i}": FURNITURE for i in range(1, 4)}).classify_history(
        "x.ru", polite=0)
    assert h["sampled"] == 3 and h["wayback_checked"] is True
    assert all(e["chars"] >= MIN_TEXT_CHARS for e in h["evidence"])


# ---- кодировка: архивные .ru — это windows-1251, а не utf-8 ----

def _resp(body: bytes, content_type: str = "text/html") -> httpx.Response:
    """НАСТОЯЩИЙ httpx.Response (сети нет — он собран из байтов): подделка своим `.text`
    показала бы зелёное там, где живой httpx отдаёт мозаику."""
    return httpx.Response(200, content=body, headers={"content-type": content_type})


def _fetched(body: bytes, content_type: str = "text/html") -> str:
    c = WaybackClient()
    c.request = lambda method, url, **kw: _resp(body, content_type)   # noqa: ARG005
    return c._fetch_raw("20090101120000", "http://x.ru/")


CP1251_CASINO = (
    "<html><head><meta http-equiv='Content-Type' content='text/html; charset=windows-1251'>"
    "<title>Игровые автоматы онлайн</title></head>"
    "<body><h1>Вулкан казино</h1><p>Игровые автоматы, джекпот и бонусы каждый день</p>"
    "</body></html>").encode("cp1251")


def test_cp1251_snapshot_is_decoded_and_still_dirty():
    """РЕГРЕССИЯ: `_fetch_raw` возвращал `r.text`. Архивная .ru-страница 2000-х — windows-1251,
    и Wayback не всегда отдаёт charset в заголовке -> httpx раскодирует её utf-8 в мозаику, и
    RU-словарь (несущая половина: дропы, за которыми мы идём, — .ru) не находит НИ ОДНОГО слова.
    Домен-казино проходит как чистый."""
    raw = _fetched(CP1251_CASINO)
    assert "казино" in raw, "страница раскодирована в мозаику — русские стоп-слова мертвы"
    assert "casino" in _classify_html(raw)


def test_header_charset_wins_over_the_page():
    """Заголовок ответа — первоисточник; `<meta>` внутри страницы может ему противоречить."""
    body = "<p>Игровые автоматы и казино: джекпот</p>".encode("utf-8")
    assert "казино" in _fetched(body, "text/html; charset=utf-8")


def test_undeclared_cyrillic_falls_back_to_cp1251():
    """Ни charset в заголовке, ни `<meta>` — utf-8 на таких байтах падает, значит это cp1251
    (для .ru-архива это единственная разумная догадка, а не «заменить на ??»)."""
    body = "<p>Игровые автоматы и казино: джекпот</p>".encode("cp1251")
    assert "казино" in _decode(body, None)


def test_broken_bytes_do_not_sink_the_snapshot():
    """Битая/неизвестная кодировка не имеет права ронять проверку истории целиком."""
    assert isinstance(_decode(b"\xff\xfe\x00\x01 casino", "no-such-codec-42"), str)


# ---- F4: topic_switch — флаг-призрак ----

def test_topic_switch_key_is_gone_from_prior_flags():
    pages = {f"http://x.ru/{i}": FURNITURE for i in range(1, 4)}
    assert set(_flags(pages)) == set(STOPWORDS)


def test_topic_switch_could_never_add_a_single_reject():
    """ДОКАЗАТЕЛЬСТВО удаления. Флаг считал `(later − early) ∩ {adult,pharma,casino,gambling}`.
    Но `later ⊆ all_cats`, а `all_cats` — ровно то, из чего строятся категорийные флаги, и
    все четыре категории входят в HARD_REJECT_FLAGS. Значит непустое пересечение означает,
    что категорийный hard-reject УЖЕ сработал: строгое подмножество, ноль новых отказов.

    Здесь это показано на его собственном сценарии: чистое начало -> казино перед дропом.
    """
    pages = {"http://x.ru/1": "<h1>Новости дня</h1><p>Политика, экономика, аналитика</p>",
             "http://x.ru/2": "<h1>Новости дня</h1><p>Репортажи и обзоры событий</p>",
             "http://x.ru/3": "<h1>Вулкан казино</h1><p>Игровые автоматы, джекпот</p>"}
    pf = _flags(pages)
    assert pf["casino"] is True                       # смена темы поймана СВОЕЙ категорией
    assert "topic_switch" not in pf
    out = compute_score({"prior_flags": pf, "wayback_checked": True,
                         "age_years": 15, "referring_domains": 2000, "indexed_echo": True})
    assert out["status"] == "rejected" and out["score"] == 0.0
    assert out["breakdown"]["hard_reject"] == ["prior_casino"]


# ---- F5: trademark_risk — гейт без производителя ----

def test_trademark_risk_is_not_a_hard_reject_anymore():
    """Ветка отказа была, а расчёта — не было: значение всегда NULL, гейт лишь ПРИТВОРЯЛСЯ
    проверкой. Колонка в БД оставлена (данные не рушим), ветка в скоринге удалена."""
    out = compute_score({"wayback_checked": True, "prior_flags": {}, "trademark_risk": True,
                         "age_years": 10, "referring_domains": 300, "indexed_echo": True})
    assert "hard_reject" not in out["breakdown"]
    assert out["status"] != "rejected"


# ---- веса: в живой БД (миграция 0009) лежит СОХРАНЁННЫЙ JSON ----

def test_stale_weight_keys_from_db_do_not_break_scoring():
    """Пин, а не регрессия: в `scoring_settings.weights` на боксе может лежать JSON с ключами
    удалённых критериев. Скоринг обязан их игнорировать, а не падать KeyError."""
    out = compute_score({"wayback_checked": True, "prior_flags": {}, "age_years": 8,
                         "referring_domains": 100},
                        weights={"history_cleanliness": 0.5, "age": 0.5,
                                 "topic_switch": 0.9, "trademark_risk": 0.3})
    assert 0.0 <= out["score"] <= 1.0
    assert set(out["breakdown"]["weights"]) == {"history_cleanliness", "age"}


def test_settings_drop_unknown_weight_keys():
    from app.services.settings import _clean_weights
    from app.services import scoring_config as cfg
    w = _clean_weights({"history_cleanliness": 0.4, "topic_switch": 1.0, "trademark_risk": 1.0})
    assert set(w) == set(cfg.WEIGHTS)
