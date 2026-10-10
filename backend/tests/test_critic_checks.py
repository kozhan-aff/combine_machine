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


def test_copy_survives_yo_and_invisible_characters():
    # одно правило нормализации слова на обе стороны: «ё» = «е», мягкий перенос и пробел нулевой ширины
    # внутри слова не рвут его и в сравнение не идут
    plain = ("провайдер все еще видит объем трафика но не видит адреса сайтов "
             "которые открывает пользователь его устройства")
    yo = plain.replace("все еще", "всё ещё").replace("объем", "объём")
    assert yo != plain
    assert len(only(check(f"{RU} {yo}.", sources=[plain]), "копирование")) == 1          # «ё» на странице
    assert len(only(check(f"{RU} {plain}.", sources=[yo]), "копирование")) == 1          # «ё» в источнике
    soft = plain.replace("провайдер", "про\u00adвай\u00adдер").replace("трафика", "тра\u00adфика")
    assert len(only(check(f"{RU} {plain}.", sources=[soft]), "копирование")) == 1        # переносы в источнике
    zero = plain.replace("видит", "ви\u200bдит").replace("сайтов", "\ufeffсай\u200dтов")
    [issue] = only(check(f"{RU} {zero}.", sources=[plain]), "копирование")               # невидимые на странице
    assert issue.startswith("копирование источника: «про")
    # NFKC: полноширинные буквы и лигатура fi (U+FB01) — те же буквы
    latin = plain.replace("трафика", "traffic").replace("сайтов", "wifi")
    wide = plain.replace("трафика", "\uff54\uff52\uff41\uff46\uff46\uff49\uff43").replace("сайтов", "wi\ufb01")
    assert wide != latin
    assert len(only(check(f"{RU} {wide.upper()}.", sources=[latin]), "копирование")) == 1


def test_copy_quote_is_one_line():
    # копия, разорванная границей блоков (перевод строки в видимом тексте), цитируется одной строкой
    run = SOURCE.split()
    [issue] = only(check(" ".join(run[:6]) + "\n" + " ".join(run[6:12]), sources=[SOURCE]), "копирование")
    assert "\n" not in issue and " ".join(run[:12]) in issue


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


def test_brand_matches_across_spacing_and_blocks():
    for written in ("DurevVPN", "Durev\nVPN", "durev-vpn", "«Durev VPN»", "Durev\u00a0VPN"):
        assert only(check(f"{RU} Сервис {written} работает.", brand="Durev VPN"), "в тексте нет бренда") == [], written
    assert only(check(f"{RU} Сервис DurevVPNs работает.", brand="Durev VPN"), "в тексте нет бренда") != []


def test_brand_needs_word_boundaries():
    # короткий бренд внутри чужого слова — не упоминание
    assert only(check(f"{RU} Это не utopia.", brand="PIA"), "в тексте нет бренда") == ["в тексте нет бренда PIA"]
    assert only(check(f"{RU} Это PIA.", brand="PIA"), "в тексте нет бренда") == []
    assert only(check(f"{RU} Клиент Linux VPN.", brand="X-VPN"), "в тексте нет бренда") == ["в тексте нет бренда X-VPN"]
    assert only(check(f"{RU} Клиент x vpn.", brand="X-VPN"), "в тексте нет бренда") == []


def test_brand_without_alphanumerics_is_skipped():
    assert only(check(RU, brand="—"), "в тексте нет бренда") == []
    assert only(check(RU, brand="***"), "в тексте нет бренда") == []


# ── язык ─────────────────────────────────────────────────────────────────────

def test_language_ru_ok_en_flagged():
    # латинские названия (бренд, протокол) русскому тексту не мешают
    assert only(check(f"{RU} NordVPN работает по протоколу WireGuard.", lang="ru"), "язык") == []
    issues = only(check(EN, lang="ru"), "язык")
    assert len(issues) == 1 and "для ru" in issues[0] and "50%" in issues[0]


def test_language_ru_floor_is_half():
    # инструкция с латинскими названиями кнопок и пунктов меню: 54% кириллицы — ещё русский текст
    assert only(check("ж" * 54 + " " + "w" * 46, lang="ru"), "язык") == []
    assert only(check("ж" * 50 + " " + "w" * 50, lang="ru"), "язык") == []
    assert len(only(check("ж" * 45 + " " + "w" * 55, lang="ru"), "язык")) == 1


def test_language_en_ok_ru_flagged():
    assert only(check(EN, lang="en"), "язык") == []
    assert only(check(EN.replace("service", "Dienst"), lang="de"), "язык") == []     # любой не-ru — латиница
    issues = only(check(RU, lang="de"), "язык")
    assert len(issues) == 1 and "de" in issues[0] and "20%" in issues[0]
    assert only(check(RU, lang="ru-RU"), "язык") == []                               # код с регионом — тот же ru


def test_language_foreign_alphabet_flagged():
    # буквы не латиницы и не кириллицы сверх 10% — замечание при любом языке страницы
    greek, cjk = "αβγδεζηθικλμνξοπρστυφχψω", "安全快速稳定的网络服务提供商"
    for lang, base in (("en", EN), ("de", EN), ("ru", RU)):
        assert only(check(f"{base} {greek}", lang=lang), "язык: посторонний алфавит") != [], lang
        assert only(check(f"{base} {cjk * 2}", lang=lang), "язык: посторонний алфавит") != [], lang
        assert only(check(f"{base} {base} α и 安", lang=lang), "язык") == [], lang      # пара знаков — не дрейф
    # диакритика языков проекта — латиница, не «посторонний алфавит»
    assert only(check("Größe, façade, niño, ação, perché, één, Œuvre, straße " * 3, lang="de"), "язык") == []


def test_language_unknown_code_is_named():
    # язык не задан или без словаря: меряем как латиницу, но не пишем «для en»
    [empty] = only(check(RU, lang=""), "язык")
    assert "не задан" in empty and "для en" not in empty
    [unknown] = only(check(RU, lang="xx"), "язык")
    assert "«xx»" in unknown and "неизвестен" in unknown and "для en" not in unknown
    assert only(check(EN, lang=""), "язык") == [] and only(check(EN, lang="xx"), "язык") == []


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
    text = f"{RU} Всего 1 500 серверов и ещё 1\xa0500 в резерве, цена 2,50 в месяц, возврат 30.0 дней."
    assert only(check(text, allowed={"1500", "2.5", "30"}), "числа без источника") == []
    # в замечании число стоит так, как написано на странице (неразрывный пробел — обычным), без повтора
    assert only(check(text, allowed={"2.5", "30"}), "числа без источника") == ["числа без источника: 1 500"]


def nums(text, allowed=()):
    return only(check(f"{RU} {text}", allowed=allowed), "числа без источника")


def test_thousands_with_separator_are_never_exempt():
    # «5,000» нормализуется в 5, но это пять тысяч: под «малое целое» и «год» такая запись не подпадает
    for written in ("5,000", "10,000", "6.000", "2,000,000", "2.000.000", "2,026"):
        assert nums(f"В сети {written} серверов.") == [f"числа без источника: {written}"], written
    assert nums("Задержка 0.500 секунды и 0,250 в пике.", allowed={"0.5", "0.25"}) == []     # ноль впереди — дробь


def test_thousands_accept_any_reading():
    for allowed in ({"5500"}, {"5.5"}):
        assert nums("В сети 5,500 серверов.", allowed=allowed) == [], allowed
    assert nums("В сети 5,500 серверов.", allowed={"55"}) == ["числа без источника: 5,500"]
    assert nums("Скачали 2,000,000 раз.", allowed={"2000000"}) == []
    assert nums("Скачали 2,000,000 раз.", allowed={"2", "2000"}) == ["числа без источника: 2,000,000"]


def test_small_int_with_magnitude_word_is_a_fact():
    for written in ("6 тысяч", "10 тыс.", "3 млн", "2 млрд", "5K", "5k", "6 thousand", "2 million",
                    "6\u00a0тысяч", "7 миллионов", "3 Billion"):
        shown = written.replace("\u00a0", " ")
        assert nums(f"В сети {written} серверов.") == [f"числа без источника: {shown}"], written
    assert nums("Настройка в 6 шагов и 5 кликов, тариф на 12 месяцев.") == []
    # число из источника годится и со словом: само («6») или умноженное («6000», «6500», «3000000»)
    assert nums("В сети 6 тысяч серверов.", allowed={"6"}) == []
    assert nums("В сети 6 тысяч серверов и 6,5 тыс. адресов.", allowed={"6000", "6500"}) == []
    assert nums("Аудитория 3 млн, было 5K.", allowed={"3000000", "5000"}) == []
    assert nums("В сети 65 тысяч серверов.", allowed={"65000"}) == []
    assert nums("В сети 65 тысяч серверов.") == ["числа без источника: 65 тысяч"]


def test_table_cells_do_not_fuse_into_one_number():
    text = cc.visible_text("<table><tr><td>10</td><td>111</td><td>289</td></tr></table><p>Сервис шифрует трафик</p>")
    assert text == "10\n111\n289\nСервис шифрует трафик"
    assert only(check(text, allowed={"10", "111", "289"}), "числа без источника") == []
    assert only(check(text), "числа без источника") == ["числа без источника: 111, 289"]


def test_identifiers_are_not_facts():
    for written in ("AES-256", "AES 256", "256-bit", "256 бит", "256-битное шифрование", "TLS 1.3", "SHA-512",
                    "RSA-4096", "IKEv2", "OpenVPN 2.6", "Windows 11", "Android 14", "macOS 15", "iOS 17.4",
                    "Wi-Fi 6", "24/7", "1.1.1.1", "192.168.1.77", "05.10.2019", "2019-03-15", "порт 443",
                    "port 1194", "Оценка: 8.5/10", "Оценка: 9 / 10", "видео в 4K"):
        assert nums(f"Сервис: {written}, и точка.") == [], written
    # имя и число — в одной строке: ячейка «Android» не прячет число из соседней ячейки
    cells = cc.visible_text("<table><tr><td>Android</td><td>4500</td><td>порт</td><td>777</td></tr></table>")
    assert nums(cells) == ["числа без источника: 4500, 777"]
    # идентификатор не прячет соседний факт
    assert nums("Шифр AES-256, серверов 4500, поддержка 24/7.") == ["числа без источника: 4500"]
    assert nums("Скорость 300 Мбит/с через порт 443.") == ["числа без источника: 300"]
    assert nums("Оценка 85/100 и 7/7.") == ["числа без источника: 85, 100"]


def test_prices_and_percents_need_a_source():
    text = "Тариф 289 руб/мес, скидка 67%."
    assert nums(text) == ["числа без источника: 289, 67"]
    assert nums(text, allowed={"289", "67"}) == []


def test_unsourced_numbers_are_deduplicated():
    assert nums("Скидка 67%, повторим: 67%, и ещё раз 67 процентов, а серверов 4500 и 4500.") == [
        "числа без источника: 67, 4500"]


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


# ── кривой вход: проверки не падают ──────────────────────────────────────────

def test_bad_input_never_raises():
    copied = f"{RU} {SOURCE} И ещё 4500."
    base = dict(kind="howto", lang="ru", brand="Durev VPN")

    # sources: None — источников нет; строка — один источник; не-строки в списке пропускаются
    assert only(cc.code_checks(text=copied, sources=None, allowed=set(), **base), "копирование") == []
    assert len(only(cc.code_checks(text=copied, sources=SOURCE, allowed=set(), **base), "копирование")) == 1
    assert len(only(cc.code_checks(text=copied, sources=[None, 5, b"x", ["y"], SOURCE], allowed=set(), **base),
                    "копирование")) == 1
    assert only(cc.code_checks(text=copied, sources=7, allowed=set(), **base), "копирование") == []

    # allowed: None — разрешённых нет (число помечено); список и не-строки внутри — тоже без падения
    assert only(cc.code_checks(text=copied, sources=[], allowed=None, **base), "числа") == ["числа без источника: 4500"]
    assert only(cc.code_checks(text=copied, sources=[], allowed=["4500", 4500, None, ["x"]], **base), "числа") == []
    assert only(cc.code_checks(text=copied, sources=[], allowed=4500, **base), "числа") == ["числа без источника: 4500"]

    # text: None и не-строка — пустой текст; он не «чист», а помечен
    for text in (None, 5, b"bytes", ["a"]):
        issues = cc.code_checks(text=text, sources=[SOURCE], allowed=set(), **base)
        assert "язык: в тексте нет букв" in issues and only(issues, "объём 0 слов") != [], text

    # kind, lang, brand не того типа — проверка пропущена или отработала как для пустого значения
    issues = cc.code_checks(text=RU, kind=["howto"], lang=None, brand=5, sources=[], allowed=set())
    assert only(issues, "объём") == [] and only(issues, "в тексте нет бренда") == []
    assert len(only(issues, "язык")) == 1 and "не задан" in only(issues, "язык")[0]


# ── видимый текст ────────────────────────────────────────────────────────────

def test_visible_text_strips_tags_and_entities():
    html = ('<h2>Цена &amp; тарифы</h2><p>Первый</p><p>второй&nbsp;абзац,\n  1&nbsp;500 &lt;серверов&gt;</p>'
            '<script>var x = 777;</script><ul><li>раз</li><li>два</li></ul>')
    # каждый тег — перевод строки (блоки и ячейки не склеиваются), перенос в разметке — пробел, сущности
    # раскрыты один раз, скрипт выброшен целиком, пробелы схлопнуты, пустых строк нет
    assert cc.visible_text(html) == "Цена & тарифы\nПервый\nвторой абзац, 1 500 <серверов>\nраз\nдва"
    assert cc.visible_text("<p>a</p><p>b</p>") == "a\nb"
    assert cc.visible_text("<p>текст &amp;lt;b&amp;gt; и &amp;amp;</p>") == "текст &lt;b&gt; и &amp;"
    assert cc.visible_text("<td> 10 </td>\n\n<td>\t111</td>") == "10\n111"
    assert cc.visible_text("") == "" and cc.visible_text(None) == "" and cc.visible_text(5) == ""
    assert cc.visible_text("<p>a\ud800b</p>") == "a?b"       # одиночный суррогат (кривой JSON модели) не роняет
