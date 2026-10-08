"""Чистые фильтры доменов v2: канон-форма, белый список зон, чужие VPN-бренды, генератор EMD.

Без I/O. Отдельным модулем, потому что их зовут ДВА места: discovery (зоны на входе, EMD как
источник) и воронка (W0 — тот же белый список и бренды для ручного списка). Две копии правила
«какая зона наша» разъехались бы молча.
"""
import re
import unicodedata

import idna

# Проверяем punycode-форму (ASCII), метка-за-меткой (аудит 2026-07-14, F30): старый
# `[a-z0-9-]+` пропускал мусор, который потом платно бьётся о whois/Ahrefs —
# ведущий/хвостовой дефис в метке ("-foo.ru"/"foo-.ru"), голый IP ("1.2.3.4" — цифровая
# последняя метка ловится тем же правилом, что и числовой TLD) и однобуквенный TLD
# ("foo.a"). Метка — не более 63 симв., не начинается/не кончается дефисом (RFC 1035);
# TLD — та же форма МЕТКИ, но с минимум двумя символами и без права быть числом целиком
# (punycode "xn--..." проходит: начинается/кончается буквой/цифрой, дефисы только внутри).
_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_TLD = r"(?!\d+$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])"     # >=2 симв. и не чисто цифровой
_DOMAIN_RE = re.compile(rf"^(?:{_LABEL}\.)+{_TLD}$")


def canonical_domain(raw) -> str | None:
    """Единая канон-форма домена для ВСЕХ источников: lower, без www./точки, IDN→punycode.
    None если не домен (мусор, e-mail, пустое, недопустимые метки).

    IDNA 2008 (пакет `idna`, S1-09), а не кодек `s.encode("idna")` (IDNA 2003): тот молча
    отображает `straße.com` в `strasse.com` и `faß.de` в `fass.de` — это ДРУГИЕ домены, и купить
    мы могли бы чужое имя. Спорные (непропускаемые IDNA 2008) символы — явный отказ (None), а не
    тихое отображение."""
    s = (raw or "").strip().lower().rstrip(".")
    if s.startswith("www."):
        s = s[4:]
    if not s or len(s) > 253 or "@" in s or " " in s:
        return None
    try:
        puny = idna.encode(unicodedata.normalize("NFC", s), uts46=False).decode("ascii")
    except (idna.IDNAError, UnicodeError, ValueError):
        return None                       # пустая метка, >63, недопустимый/спорный символ
    return puny if _DOMAIN_RE.match(puny) else None


def tld_match(domain: str, allowlist) -> str | None:
    """Зона из белого списка, в которой домен — РЕГИСТРИРУЕМОЕ имя, или None.

    Матч только когда перед зоной ровно одна метка: `foo.co.uk` — зона `co.uk`, а не `uk`;
    `x.com.mx` при списке [mx] — не наш (его зона com.mx, NameSilo её не продаёт). Без PSL:
    источники отдают регистрируемые имена, а белый список и есть наш перечень зон.
    """
    # Нижний регистр и здесь, хотя discovery канонизирует имя: W0 судит строку из БД (ручной список,
    # строки v1), и `Foo.COM` иначе ушёл бы в tld_closed при зоне в белом списке (финальное ревью).
    domain = domain.lower()
    for raw in allowlist or ():
        t = str(raw).strip().strip(".").lower()
        if t and domain.endswith("." + t) and "." not in domain[: -len(t) - 1]:
            return t
    return None


# Фильтры качества имени на входе discovery (S1-10). Живая статистика окна Nominet (9 047 co.uk):
# 2 917 с меткой длиннее 15 символов, 893 с дефисом, 363 с цифрами; без платного DR отсекать спам
# нечем, кроме признаков самого имени. Дефолты КОНСЕРВАТИВНЫЕ — режут явный мусор, а не «некрасивое»;
# оператор подкручивает через update_settings(name_filters=…).
DEFAULT_NAME_FILTERS = {
    "max_label_len": 30,        # символов в первой метке (0 — не проверять)
    "max_digit_share": 0.4,     # доля цифр в метке (1.0 — не проверять)
    "max_hyphens": 2,           # дефисов в метке (-1 — не проверять)
    "junk": ["xxx", "porn", "casino", "viagra", "escort"],   # подстроки-мусор в имени
}


def name_reject(domain: str, f: dict | None = None) -> str | None:
    """Почему имя не годится как кандидат ('length' | 'digits' | 'hyphens' | 'junk') или None.
    Punycode-метки (`xn--…`) длину/цифры/дефисы не судим: они кодируют юникод-имя, а не мусор."""
    f = {**DEFAULT_NAME_FILTERS, **(f or {})}
    label = domain.split(".", 1)[0].lower()
    if not label.startswith("xn--"):
        if f["max_label_len"] and len(label) > int(f["max_label_len"]):
            return "length"
        # доля цифр — только у меток от 6 символов: у «3m»/«a1» она ничего не говорит о мусоре
        if len(label) >= 6 and sum(c.isdigit() for c in label) / len(label) > float(f["max_digit_share"]):
            return "digits"
        if int(f["max_hyphens"]) >= 0 and label.count("-") > int(f["max_hyphens"]):
            return "hyphens"
    if any(j and j in label for j in (str(x).lower() for x in f.get("junk") or ())):
        return "junk"
    return None


def zone_of(domain: str) -> str:
    """Зона вне белого списка для счётчика «отсечено зоной» (S1-08): всё после первой метки."""
    return domain.split(".", 1)[1] if "." in domain else domain


_SUBSTRING_MIN = 7


def brand_hit(domain: str, tokens) -> str | None:
    """Чужой VPN-бренд в имени (первая метка) — или None.

    Подстрокой в имени без дефисов ищем только токены, которые в обычных словах не встречаются:
    с `vpn` внутри (`nordvpn` в `mynordvpn`) или длиной от 7 символов (`surfshark` в
    `my-surf-shark`). Остальные (`avast`, `norton`, `hideme`, `pia`) — только ЦЕЛОЙ частью между
    дефисами: подстрокой они бьют по обычным словам (`javastudio` содержит avast, `nortonville` —
    norton), а отказ по бренду жёсткий.
    ponytail: склейку с коротким брендом без дефиса (`bestavastdeal`) не ловим; понадобится —
    словарь брендов с границами слов.
    """
    label = domain.split(".", 1)[0].lower()
    parts = label.split("-")
    flat = label.replace("-", "")
    for raw in tokens or ():
        t = str(raw).strip().lower()
        if not t:
            continue
        if "vpn" in t or len(t) >= _SUBSTRING_MIN:
            if t in flat:
                return t
        elif t in parts:
            return t
    return None


_SPLIT = re.compile(r"[\s_\-]+")


def _as_list(v) -> list:
    """Строка вместо списка — это ОДИН элемент: перебор строки дал бы буквы (m.com, e.com…)."""
    return [v] if isinstance(v, str) else list(v or ())


def emd_candidates(sets, tokens) -> list[dict]:
    """Наборы оператора {market, lang, keywords[], tlds[]} -> EMD-кандидаты без сети.

    Ключ -> слова -> склейка и (если слов > 1) через дефис -> × каждый TLD набора. Имена с чужим
    брендом не генерируются (W0 их всё равно отклонил бы). Дубли между наборами убираются, порядок
    стабилен: ключ за ключом, склейка раньше дефиса, TLD в порядке набора.
    ponytail: без модификаторов best-/top- — оператор впишет их в ключи сам.
    """
    out, seen = [], set()
    for s in sets or ():
        market, lang = str(s.get("market") or "")[:16], str(s.get("lang") or "")[:8]
        for kw in _as_list(s.get("keywords")):
            words = [w for w in _SPLIT.split(str(kw).strip().lower()) if w]
            if not words:
                continue
            names = ["".join(words)] + (["-".join(words)] if len(words) > 1 else [])
            for name in names:
                for tld in _as_list(s.get("tlds")):
                    d = canonical_domain(f"{name}.{str(tld).strip().strip('.').lower()}")
                    if d and d not in seen and not brand_hit(d, tokens):
                        seen.add(d)
                        out.append({"domain": d, "market": market, "lang": lang})
    return out
