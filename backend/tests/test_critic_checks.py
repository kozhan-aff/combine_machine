"""Критик: проверки кодом (план Б, задача 5) — копирование, бренд, язык, объём, числа без источника.
Чистые функции над текстом: без БД, сети и LLM."""
import time
from datetime import datetime, timezone

from app.services import content_critic as cc
from app.services.page_doc import WORDS

YEAR = datetime.now(timezone.utc).year      # как в проверке: год по UTC на момент вызова

RU = ("Сервис шифрует трафик и скрывает адрес устройства от провайдера, а приложение ставится "
      "за пару минут и не требует настройки вручную.")
EN = ("The service encrypts traffic and hides the device address from the provider, and the app "
      "installs in a couple of minutes without manual setup.")
SOURCE = ("наш тест показал что скорость загрузки через ближайший сервер падает совсем немного "
          "а стриминговые сервисы открываются без задержек и без повторных проверок входа")


def check(text, *, kind=None, lang="ru", brand=None, sources=(), allowed=()):
    return cc.code_checks(text=text, kind=kind, lang=lang, brand=brand,
                          sources=list(sources), allowed=set(allowed))


def only(issues, prefix):
    return [i for i in issues if i.startswith(prefix)]


def words(n, word="слово"):
    return " ".join([word] * n)


# ── копирование ──────────────────────────────────────────────────────────────

def test_shingle_copy_detected_and_quoted():
    # 15 слов источника подряд, в другом регистре и с пунктуацией — шингл считается по словам;
    # цитата — первые 12 слов так, как они стоят в тексте страницы (оператор найдёт её поиском)
    copied = "Наш тест показал, что скорость загрузки через ближайший сервер падает совсем немного, а стриминговые сервисы"
    issues = only(check(f"{RU} {copied} работают. {RU}", sources=[SOURCE]), "копирование")
    assert issues == ["копирование источника: «Наш тест показал, что скорость загрузки через "
                      "ближайший сервер падает совсем немного»"]


def test_paraphrase_is_not_copy():
    # те же факты своими словами; общие куски короче 12 слов подряд — не копия
    text = ("По нашим замерам скорость загрузки через ближайший сервер снижается незначительно, "
            "а стриминговые сервисы открываются сразу и без повторных проверок входа.")
    assert only(check(text, sources=[SOURCE]), "копирование") == []
    # граница: 11 слов источника подряд — ещё не копия, 12 — уже да
    run = SOURCE.split()
    assert only(check(f"{RU} {' '.join(run[:11])} иначе.", sources=[SOURCE]), "копирование") == []
    assert len(only(check(f"{RU} {' '.join(run[:12])} иначе.", sources=[SOURCE]), "копирование")) == 1


def test_short_source_ignored():
    short = "скорость загрузки через ближайший сервер падает совсем немного"      # 8 слов < шингла
    assert only(check(f"{RU} {short} {RU}", sources=[short, "", None]), "копирование") == []


def test_copy_capped_at_three_without_overlap():
    # один скопированный кусок в 30 слов — это 19 пересекающихся шинглов, но фраз две (по 12 слов встык)
    src = " ".join(f"с{i}" for i in range(30))
    assert len(only(check(src, sources=[src]), "копирование")) == 2
    # пять разных скопированных кусков — показываем не больше трёх, в порядке текста
    srcs = [" ".join(f"и{k}с{i}" for i in range(12)) for k in range(5)]
    issues = only(check(" разрыв ".join(srcs), sources=srcs), "копирование")
    assert len(issues) == 3 and "и0с0" in issues[0] and "и2с0" in issues[2]


def test_copy_check_is_fast_on_real_sizes():
    # страница 2500 слов против пяти источников по 7000: линейный проход, не квадратичный
    text = " ".join(f"т{i}" for i in range(2500))
    srcs = [" ".join(f"и{k}с{i}" for i in range(7000)) for k in range(5)]
    started = time.perf_counter()
    assert only(check(text, sources=srcs), "копирование") == []
    assert time.perf_counter() - started < 2.0      # на деле десятки миллисекунд; запас под занятую машину


# ── бренд ────────────────────────────────────────────────────────────────────

def test_missing_brand():
    assert only(check(RU, brand="Durev VPN"), "в тексте нет бренда") == ["в тексте нет бренда Durev VPN"]
    assert only(check(RU, brand=None), "в тексте нет бренда") == []      # бренд не задан — нечего искать
    assert only(check(RU, brand="  "), "в тексте нет бренда") == []


def test_brand_case_insensitive():
    assert only(check(f"{RU} Мы проверили DUREV vpn на трёх устройствах.", brand="Durev VPN"),
                "в тексте нет бренда") == []


# ── язык ─────────────────────────────────────────────────────────────────────

def test_language_ru_ok_en_flagged():
    # латинские названия (бренд, протокол) русскому тексту не мешают
    assert only(check(f"{RU} NordVPN работает по протоколу WireGuard.", lang="ru"), "язык") == []
    issues = only(check(EN, lang="ru"), "язык")
    assert len(issues) == 1 and "ru" in issues[0] and "60%" in issues[0]


def test_language_en_ok_ru_flagged():
    assert only(check(EN, lang="en"), "язык") == []
    assert only(check(EN.replace("service", "Dienst"), lang="de"), "язык") == []     # любой не-ru — латиница
    issues = only(check(RU, lang="de"), "язык")
    assert len(issues) == 1 and "de" in issues[0] and "20%" in issues[0]
    assert only(check(RU, lang="ru-RU"), "язык") == []                               # код с регионом — тот же ru


def test_text_without_letters_is_flagged():
    # пустая страница не должна выйти «чистой» только потому, что в ней нечего проверять
    assert only(check("", lang="ru"), "язык") != []
    assert only(check("2 + 2 = 4", lang="en"), "язык") != []


# ── объём ────────────────────────────────────────────────────────────────────

def test_volume_bounds_per_kind():
    for kind, (lo, hi) in WORDS.items():
        assert only(check(words(lo), kind=kind), "объём") == []
        assert only(check(words(hi), kind=kind), "объём") == []
        assert only(check(words(lo - 1), kind=kind), "объём") == [f"объём {lo - 1} слов, нужно {lo}–{hi}"]
        assert only(check(words(hi + 1), kind=kind), "объём") == [f"объём {hi + 1} слов, нужно {lo}–{hi}"]


def test_volume_skipped_for_unknown_kind():
    assert only(check(words(3), kind=None), "объём") == []
    assert only(check(words(3), kind="landing"), "объём") == []      # тип без границ — тоже без проверки


# ── числа без источника ──────────────────────────────────────────────────────

def test_unsourced_number_flagged():
    text = f"{RU} В сети 4500 серверов в 37 странах, тариф стоит 3.99, а скидка 37 процентов."
    issues = only(check(text, allowed={"3.99"}), "числа без источника")
    assert issues == ["числа без источника: 4500, 37"]      # одно замечание, по порядку, без повторов


def test_small_ints_and_year_ignored():
    text = (f"{RU} Установка в 3 шага, оценка 9 из 10, тариф на 12 месяцев. "
            f"Обзор {YEAR - 1} года обновлён в {YEAR}, планы на {YEAR + 1}.")
    assert only(check(text), "числа без источника") == []
    # граница: 13 — уже не «счёт шагов», позапрошлый год — уже факт, дробь до 12 — тоже факт
    text = f"{RU} Гарантия 13 дней, запуск в {YEAR - 2}, рейтинг 4.5."
    assert only(check(text), "числа без источника") == [f"числа без источника: 13, {YEAR - 2}, 4.5"]


def test_number_formats_match_allowed():
    # в тексте «1 500» (и с неразрывным пробелом), «2,50» и «30.0» — в разрешённых они в виде norm_number
    text = f"{RU} Всего 1 500 серверов и ещё 1 500 в резерве, цена 2,50 в месяц, возврат 30.0 дней."
    assert only(check(text, allowed={"1500", "2.5", "30"}), "числа без источника") == []
    assert only(check(text, allowed={"2.5", "30"}), "числа без источника") == ["числа без источника: 1500"]


def test_unsourced_numbers_capped_at_ten():
    text = RU + " " + ", ".join(str(n) for n in range(100, 113)) + "."
    [issue] = only(check(text), "числа без источника")
    assert issue == ("числа без источника: 100, 101, 102, 103, 104, 105, 106, 107, 108, 109 и ещё 3")


# ── всё вместе ───────────────────────────────────────────────────────────────

def test_clean_text_has_no_issues():
    lo, hi = WORDS["howto"]
    n = len(RU.split())
    text = " ".join([RU] * ((lo + hi) // 2 // n)) + f" Durev VPN держит 4500 серверов, обзор {YEAR} года."
    assert check(text, kind="howto", lang="ru", brand="Durev VPN", sources=[SOURCE], allowed={"4500"}) == []


def test_all_checks_report_together():
    text = f"{EN} {EN} {SOURCE} 4500."
    issues = check(text, kind="howto", lang="ru", brand="Durev VPN", sources=[SOURCE])
    for prefix in ("копирование", "в тексте нет бренда", "язык", "объём", "числа без источника"):
        assert only(issues, prefix), prefix


# ── видимый текст ────────────────────────────────────────────────────────────

def test_visible_text_strips_tags_and_entities():
    html = ('<h2>Цена &amp; тарифы</h2><p>Первый</p><p>второй&nbsp;абзац,\n  1&nbsp;500 &lt;серверов&gt;</p>'
            '<script>var x = 777;</script><ul><li>раз</li><li>два</li></ul>')
    # блочные теги не склеивают слова, сущности раскрыты, скрипт выброшен целиком, пробелы схлопнуты
    assert cc.visible_text(html) == "Цена & тарифы Первый второй абзац, 1 500 <серверов> раз два"
    assert cc.visible_text("<p>a</p><p>b</p>") == "a b"
    assert cc.visible_text("") == "" and cc.visible_text(None) == ""
