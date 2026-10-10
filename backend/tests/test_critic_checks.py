"""Критик: проверки кодом (план Б, задача 5) — копирование, бренд, язык, объём, числа без источника.
Чистые функции над текстом: без БД, сети и LLM."""
import time
import unicodedata
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


def test_copy_survives_decomposed_unicode():
    # NFD: «й» = «и» + отдельный знак краткости, «ё» = «е» + две точки. Текст приводится целиком ДО
    # разбиения на слова, иначе знак рвёт слово и одинаковый текст не совпадает ни одним шинглом
    plain = ("Провайдер всё ещё видит объём трафика, но не видит адреса сайтов, "
             "который открывает каждый пользователь своей домашней сети")
    nfd = unicodedata.normalize("NFD", plain)
    assert len(nfd) > len(plain) and unicodedata.normalize("NFC", nfd) == plain
    assert len(only(check(f"{RU} {plain}.", sources=[nfd]), "копирование")) == 1        # источник в NFD
    [issue] = only(check(f"{RU} {nfd}.", sources=[plain]), "копирование")               # страница в NFD
    assert issue == "копирование источника: «Провайдер всё ещё видит объём трафика, но не видит адреса сайтов, который»"


def test_copy_quote_when_lowercasing_changes_length():
    # турецкая «I с точкой» в нижнем регистре — два знака: позиции слов к исходному тексту не приложить,
    # цитата берётся из приведённого текста (и не съезжает, и не падает)
    dotted = chr(0x130)
    run = SOURCE.split()[:12]
    [issue] = only(check(f"{dotted}stanbul {dotted}zmir. " + " ".join(run).upper(), sources=[SOURCE]), "копирование")
    assert issue == f"копирование источника: «{' '.join(run)}»"


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


def nums(text, allowed=(), sources=()):
    return only(check(f"{RU} {text}", allowed=allowed, sources=sources), "числа без источника")


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


def test_thousands_integer_decimal_reading_is_not_accepted():
    # малые целые почти всегда есть среди разрешённых (тарифы, устройства, сроки): «6» не узаконивает «6,000»
    small = {"2", "3", "6", "7", "10"}
    for written in ("6,000", "6.000", "10,000", "2,000,000", "2.000.000"):
        assert nums(f"В сети {written} серверов.", allowed=small) == [f"числа без источника: {written}"], written
    # тысячное прочтение принимается, нецелая дробь — тоже
    assert nums("В сети 6,000 серверов.", allowed={"6000"}) == []
    assert nums("В сети 6.000 серверов.", allowed={"6000"}) == []
    assert nums("В сети 5,500 серверов.", allowed={"5.5"}) == []
    assert nums("В сети 5,050 серверов.", allowed={"5.05"}) == []
    assert nums("В сети 5,500 серверов.", allowed={"5", "55", "550"}) == ["числа без источника: 5,500"]


def test_small_int_with_magnitude_word_is_a_fact():
    for written in ("6 тысяч", "10 тыс.", "3 млн", "2 млрд", "5K", "5k", "6 thousand", "2 million",
                    "6\u00a0тысяч", "7 миллионов", "3 Billion"):
        shown = written.replace("\u00a0", " ")
        assert nums(f"В сети {written} серверов.") == [f"числа без источника: {shown}"], written
    assert nums("Настройка в 6 шагов и 5 кликов, тариф на 12 месяцев.") == []
    # годится только УМНОЖЕННОЕ значение («6000», «6500», «3000000»): голое «6» есть почти в любом наборе
    small = {"2", "3", "5", "6", "10"}
    for written in ("6 тысяч", "10 тыс.", "3 млн", "5K", "6 thousand", "2 million"):
        assert nums(f"В сети {written} серверов.", allowed=small) == [f"числа без источника: {written}"], written
    assert nums("Аудитория 3 млн пользователей.", allowed={"3"}) == ["числа без источника: 3 млн"]
    assert nums("В сети 6 тысяч серверов.", allowed={"6000"}) == []
    assert nums("В сети 6 тысяч серверов и 6,5 тыс. адресов.", allowed={"6000", "6500"}) == []
    assert nums("Аудитория 3 млн, было 5K.", allowed={"3000000", "5000"}) == []
    assert nums("В сети 65 тысяч серверов.", allowed={"65000"}) == []
    assert nums("В сети 65 тысяч серверов.") == ["числа без источника: 65 тысяч"]


def test_english_plural_is_not_a_multiplier():
    # «millions of users» — «миллионы пользователей», а не множитель при числе перед ним
    assert nums(f"In {YEAR} millions of users rely on it.") == []
    assert nums("Over 450 thousands of reviews.") == ["числа без источника: 450"]
    assert nums("Over 3 billions.", allowed={"3000000000"}) == []          # малое целое, не «3 billion»
    assert nums("Audience of 450 million.", sources=["In 450 millions of homes"]) == [
        "числа без источника: 450 million"]


def test_magnitude_word_must_be_on_the_same_line():
    # перевод строки — граница блока: число из одной ячейки и слово из соседней — не «10 млн»
    cells = "Устройств: 10\nМиллионы пользователей"
    assert nums("Аудитория 10 млн человек.", sources=[cells]) == ["числа без источника: 10 млн"]
    assert nums("Аудитория 450 млн человек.", sources=["Серверов: 450\tмлн пользователей"]) == [
        "числа без источника: 450 млн"]
    assert nums("Серверов: 450\nмлн пользователей.", allowed={"450"}) == []        # на странице — тоже два блока
    assert nums("Аудитория 10\u00a0млн человек.", sources=["около 10\u00a0млн человек"]) == []


def test_magnitude_in_source_text_legitimises_it_on_the_page():
    # в досье «6 тыс.» лежит как «6»; узаконить множитель может только сам текст источника
    source = "По данным сервиса, у него около 6 тыс. серверов, 2 million users и 5K отзывов в магазине."
    assert nums("В сети 6 тысяч серверов.", sources=[source]) == []
    assert nums("В сети 6 thousand серверов, 2 млн клиентов, 5K отзывов.", sources=["", source]) == []
    assert nums("В сети 6000 серверов, а точнее 6,000.", sources=[source]) == []      # то же число цифрами
    # другое число или другой множитель — не из источника
    assert nums("В сети 7 тысяч серверов.", sources=[source]) == ["числа без источника: 7 тысяч"]
    assert nums("В сети 6 млн серверов.", sources=[source]) == ["числа без источника: 6 млн"]
    assert nums("В сети 6 тысяч серверов.", sources=["у него 6 серверов и тысяча причин"]) == [
        "числа без источника: 6 тысяч"]
    # из текста источника берутся ТОЛЬКО числа с множителем: простое число узаконивает лишь `allowed`
    assert nums("Скорость 450 Мбит/с.", sources=[source + " Скорость 450 Мбит/с."]) == ["числа без источника: 450"]
    # разрешение видео в источнике — не «четыре тысячи»
    assert nums("В сети 4K серверов.", sources=["Стриминг в 4K без буферизации."]) == ["числа без источника: 4K"]


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


def test_identifier_names_do_not_hide_facts():
    # «имя + число» — идентификатор только при настоящих значениях; скорость, рейтинг, счёт — факты
    old = YEAR - 4
    probes = {
        "Скорость на WireGuard 450 Мбит/с, на OpenVPN 210 Мбит/с": "450, 210",
        "WireGuard 950 Mbps": "950",
        "по Wi-Fi 100 Мбит/с": "100",
        "На Windows 95 Мбит/с, на Android 80 Мбит/с": "95, 80",
        "для iOS 4.7, для Android 4,5": "4.7, 4,5",                      # рейтинги магазинов
        "для Android 4500 отзывов": "4500",
        "apps support 3200 servers": "3200",                              # sup-port — не «port»
        "We support 5000 servers": "5000",
        f"transparency report {old}": f"{old}",
        "Транспорт 300 рублей": "300",
        "Over 4K servers": "4K",                                          # не разрешение видео, а 4000
        "В сети 2.000.000.000 адресов": "2.000.000.000",                  # не IPv4
        "289/10 устройств": "289",                                        # не оценка N/10
        "Android 14 устройств и iOS 15 серверов": "14, 15",               # за версией — счётное слово
        "OpenVPN 2.5 Гбит/с": "2.5",
        "шифр AES-300 и RSA-999": "300, 999",                             # не длина ключа
        "порт 300 рублей и ports 700000": "300, 700000",                   # за портом единица; шесть цифр
        "адрес 300.1.1.1": "300.1",                                       # октет больше 255
        "Android 4.4 в рейтинге, iOS 3 звезды": "4.4",
        "Оценка: 85/10": "85",
        "Скорость 256 Мбит/с": "256",                                     # не разрядность
        "ключ 300-bit": "300",
        "по Wi-Fi 300 точек доступа": "300",                              # поколение Wi-Fi — одна цифра 4–7
        "OpenVPN 15 раз быстрее": "15",                                   # версия — только с точкой
        "TLS 450 соединений": "450",
        "macOS 4.8 в рейтинге": "4.8",
        "код 45.13.2020": "45.13",                                        # не дата
    }
    for text, shown in probes.items():
        assert nums(text + ".") == [f"числа без источника: {shown}"], text
    # идентификатор не съедает старшие разряды числа: «Android 15 000» — это пятнадцать тысяч, а не
    # версия и безобидные «000» (или «500», которое нашлось бы среди разрешённых)
    spaced = {
        "для Android 15 000 отзывов": "15 000",
        "для Android 14 500 отзывов": "14 500",
        "На Windows 10 000 серверов": "10 000",
        "для iOS 17 000 оценок": "17 000",
        "TLS 12 000 соединений": "12 000",
        "порт 443 000": "443 000",
        "AES-256 000": "256 000",
        "для Android 15\u00a0000 отзывов": "15 000",                     # неразрывный пробел
        "для iOS 17\u202f000 оценок": "17 000",                          # узкий неразрывный
        "На Windows 10\u202f000 серверов": "10 000",
    }
    for text, shown in spaced.items():
        assert nums(text + ".", allowed={"500"}) == [f"числа без источника: {shown}"], text
    # версия и отдельное число после неё — по-прежнему версия и число
    assert nums("Android 14, 500 серверов.") == ["числа без источника: 500"]
    assert nums("iOS 17 и 4500 серверов.") == ["числа без источника: 4500"]
    assert nums("Android 14, 500 серверов.", allowed={"500"}) == []


def test_real_identifiers_stay_clean():
    for written in ("AES-256", "256-bit", "TLS 1.3", "24/7", "1.1.1.1", "05.10.2026", "порт 443", "Оценка: 8.5/10",
                    "OpenVPN 2.6", "iOS 17", "Android 14", "Windows 11", "SHA-2", "SSL 3.0", "RSA 2048", "64-bit",
                    "WireGuard 1.0.20", "macOS 10.15", "Windows 8.1", "Android 5.0", "iOS 17.4.1", "Wi-Fi 6",
                    "порта 1194", "ports 51820", "255.255.255.0", "Оценка: 10/10", "8K"):
        assert nums(f"Сервис: {written}, и точка.") == [], written
        assert nums(f"Сервис: {written}.") == [], written                 # точка в конце фразы — не продолжение


def test_prices_and_percents_need_a_source():
    text = "Тариф 289 руб/мес, скидка 67%."
    assert nums(text) == ["числа без источника: 289, 67"]
    assert nums(text, allowed={"289", "67"}) == []


def test_unsourced_numbers_are_deduplicated():
    assert nums("Скидка 67%, повторим: 67%, и ещё раз 67 процентов, а серверов 4500 и 4500.") == [
        "числа без источника: 67, 4500"]


def test_digit_floods_are_linear_and_never_raise(monkeypatch):
    # зациклившаяся модель: на Python 3.11+ int() длинной строки бросает ValueError (у нас 3.12 в проде).
    # Локальный 3.10 этого не покажет, поэтому int модуля подменён таким же строгим.
    def strict_int(x, *args):
        assert len(str(x)) <= 4300, "int() на потоке цифр"
        return int(x, *args)
    monkeypatch.setattr(cc, "int", strict_int, raising=False)

    [issue] = nums("Число " + "7" * 5000 + " серверов.")
    assert issue == "числа без источника: 777777777777…"       # помечено, показано начало, а не 5000 цифр
    assert nums("А ещё " + "1 111" * 2000 + " и " + "2,000" * 2000 + ".") != []

    floods = ["9" * 100_000, "1." * 50_000, "1," * 50_000, "1 111 " * 16_000, "24/7" * 25_000, "4K" * 50_000,
              "192.168.1.1." * 8_000, "AES-2" * 20_000, "5 тыс. " * 14_000, "0" * 50_000 + "." + "0" * 50_000]
    for flood in floods:
        started = time.perf_counter()
        issues = cc.code_checks(text=flood, kind="howto", lang="ru", brand="Durev VPN", sources=[flood, flood],
                                allowed={"1", "5"})
        assert time.perf_counter() - started < 1.0, flood[:12]
        assert isinstance(issues, list) and issues, flood[:12]


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
