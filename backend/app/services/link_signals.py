"""W6: сигналы ссылочного профиля финалистов — доля спам-анкоров и пик органического трафика.

Живой факт 2026-10-01: почти каждый дропающийся .com засыпан автоматическим SEO-спамом («Expert SEO
Links and Backlinks for <домен>…», is_spam=true у 10 из 10 анкоров). Доля считается ПО REFDOMAINS,
а не по числу строк: один анкор с 300 донорами весит больше десяти единичных.
Штрафа «обвал трафика» НЕТ намеренно: у expired-домена трафик падает ВСЕГДА (сайт умер) — без
сверки с датой последнего живого снимка это неотличимо от санкций и било бы по каждому ценному дропу.
"""
import re

_STOP = re.compile(r"\b(casinos?|slots?|judi|togel|viagra|cialis|replica|loans?|porn\w*|essay|payday|"
                   r"backlinks?|guest\s*posts?|seo)\b", re.I)
# Нелатинские скрипты: на ЛАТИНСКОМ домене это чужой язык спам-волны — кроме скрипта языка
# прошлого сайта (решение оператора Р1).
_SCRIPTS = {"cyr": re.compile(r"[Ѐ-ӿ]"), "thai": re.compile(r"[฀-๿]"),
            "kana": re.compile(r"[぀-ヿ]"), "cjk": re.compile(r"[一-鿿]"),
            "hangul": re.compile(r"[가-힯]")}
_LANG_SCRIPTS = {"ja": {"kana", "cjk"}, "zh": {"cjk"}, "ko": {"hangul"}, "th": {"thai"},
                 **{lg: {"cyr"} for lg in ("ru", "uk", "bg", "sr", "kk", "be", "mk")}}


def _foreign_script(text: str, allowed: set) -> bool:
    return any(rx.search(text) for name, rx in _SCRIPTS.items() if name not in allowed)


def spam_anchor_ratio(anchors, domain: str, market_lang: str | None = None,
                      scripts: bool = True) -> float | None:
    """Доля refdomains у анкоров со спам-признаком. None — данных нет (0 доноров).

    Нелатинский скрипт на латинском домене — признак спам-волны, КРОМЕ скрипта языка прошлого сайта
    (`market_lang` из W5, Р1): японский блог на .com иначе отклонялся бы навсегда. IDN-домен (xn--)
    скрипты не судит вовсе. `scripts=False` — доля без правила скрипта: когда язык прошлого сайта
    неизвестен, W6 смотрит, не держится ли отказ на одном этом правиле (находка R2-2). Стоп-слово и
    флаг Ahrefs `is_spam` — спам при любом языке."""
    latin = scripts and not any(lbl.startswith("xn--") for lbl in domain.split("."))
    allowed = _LANG_SCRIPTS.get((market_lang or "").lower(), set())
    total = spam = 0
    for a in anchors or ():
        rd = int(a.get("refdomains") or 0)
        text = str(a.get("anchor") or "")
        bad = (bool(a.get("is_spam")) or bool(_STOP.search(text))
               or (latin and _foreign_script(text, allowed)))
        total += rd
        spam += rd if bad else 0
    return None if total == 0 else round(spam / total, 4)


def peak_traffic(history) -> int | None:
    vals = [int(m.get("org_traffic") or 0) for m in history or ()]
    return max(vals) if vals else None
