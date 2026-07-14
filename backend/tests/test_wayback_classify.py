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
import pytest

from app.integrations.wayback import STOPWORDS, WaybackClient, _classify_html, _visible_text
from app.services.scoring import compute_score

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
