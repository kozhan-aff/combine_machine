"""Чистые фильтры доменов v2: канон-форма, белый список зон, чужие VPN-бренды, генератор EMD.

Без I/O. Отдельным модулем, потому что их зовут ДВА места: discovery (зоны на входе, EMD как
источник) и воронка (W0 — тот же белый список и бренды для ручного списка). Две копии правила
«какая зона наша» разъехались бы молча.
"""
import re

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
    None если не домен (мусор, e-mail, пустое, недопустимые метки)."""
    s = (raw or "").strip().lower().rstrip(".")
    if s.startswith("www."):
        s = s[4:]
    if not s or len(s) > 253 or "@" in s or " " in s:
        return None
    try:
        puny = s.encode("idna").decode("ascii")
    except (UnicodeError, ValueError):
        return None                       # пустая метка, >63, недопустимый символ
    return puny if _DOMAIN_RE.match(puny) else None


def tld_match(domain: str, allowlist) -> str | None:
    """Зона из белого списка, в которой домен — РЕГИСТРИРУЕМОЕ имя, или None.

    Матч только когда перед зоной ровно одна метка: `foo.co.uk` — зона `co.uk`, а не `uk`;
    `x.com.mx` при списке [mx] — не наш (его зона com.mx, NameSilo её не продаёт). Без PSL:
    источники отдают регистрируемые имена, а белый список и есть наш перечень зон.
    """
    for raw in allowlist or ():
        t = str(raw).strip().strip(".").lower()
        if t and domain.endswith("." + t) and "." not in domain[: -len(t) - 1]:
            return t
    return None


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
