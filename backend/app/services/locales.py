"""Словарь служебных строк публикуемой страницы по языку (CTA, промокод, раскрытие).

Единственное место, где живут тексты, которые рендер подмешивает вокруг контента: раскрытие
партнёрства обязано быть на языке читателя (FTC/ASA), а не по-русски на сайте для de/es. Это ШОВ
для расширения языков (G8): новый язык = новая запись в TEXTS, код рендера не трогается.
Неизвестный язык -> английский (международный дефолт v2), а не русский.
"""
TEXTS = {
    "en": {
        "disclosure": ("Disclosure: this page contains affiliate links. We may earn a "
                       "commission on purchases made through them, at no extra cost to you."),
        "cta": "Go to {brand}",
        "promo": "Promo code",
    },
    "ru": {
        "disclosure": ("Раскрытие: страница содержит партнёрские ссылки. Мы можем получить "
                       "комиссию за покупки по ним — без доплаты для вас."),
        "cta": "Перейти к {brand}",
        "promo": "Промокод",
    },
}
DEFAULT_LANG = "en"


def norm_lang(lang: str | None) -> str:
    """'ru-RU' / 'RU' -> 'ru'; язык без словаря -> DEFAULT_LANG."""
    base = (lang or "").strip().lower().replace("_", "-").split("-")[0]
    return base if base in TEXTS else DEFAULT_LANG


def t(lang: str | None, key: str, **fmt) -> str:
    s = TEXTS[norm_lang(lang)][key]
    return s.format(**fmt) if fmt else s
