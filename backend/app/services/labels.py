"""Одна точка правды человекочитаемых подписей панели: статусы (домен/заказ/сайт/страница),
причины отклонения, лейны выкупа. Регистрируются как Jinja-фильтры в panel.py.

Правило: неизвестный ключ возвращается как есть (не роняем шаблон), None/пусто → "".
CSS-класс бейджа остаётся на СЫРОМ значении — переводим только текст.
"""

# Все статусы конвейера в одной плоской мапе (значения enum не конфликтуют между
# домен/заказ/сайт/страница; "published" общий для сайта и страницы — смысл один).
STATUS_RU = {
    # домен (M1–M2)
    "discovered": "найден", "scored": "оценён", "approved": "одобрен",
    "rejected": "отклонён", "purchasing": "в очереди", "purchased": "куплен",
    "live": "живой",
    # заказ выкупа (M2). `ordering` — транзиентный claim отправки: живёт секунды, но если процесс
    # убили в этот момент, строка висит в нём, пока её не разберёт поллинг (F11). В очереди она
    # видна — значит и подпись у неё обязана быть человеческая, а не сырой `ordering`.
    "pending_confirm": "ждёт подтверждения", "ordering": "отправляется", "ordered": "отправлен",
    "caught": "пойман", "failed": "ошибка", "cancelled": "отменён",
    # сайт (M3–M5)
    "provisioning": "поднимается", "content": "контент", "published": "опубликован",
    # страница (M4–M5)
    "draft": "черновик", "edited": "отредактирован",
}

REJECT_RU = {
    "low_rd": "мало доноров", "feed_flag": "флаг источника", "too_young": "моложе порога",
    "rkn": "реестр РКН", "blacklist": "блэклист", "history_dirty": "грязная история",
    "low_score": "низкий скор", "not_acquirable": "нельзя купить",
    "safebrowsing": "Google Safe Browsing",
    # v2 (коды v1 выше остаются: в базе есть легаси-строки)
    "tld_closed": "зона не наша", "trademark": "чужой бренд", "spam_anchors": "спам-анкоры",
    "legacy_ru": "РФ (архив v1)", "list_hit": "в списке чистоты (казино/adult)",
}

# Источник домена (Domain.source). Легаси v1 — для старых строк реестра.
SOURCE_RU = {"dropcatch": "DropCatch", "nominet": "Nominet", "mx": "registry.mx", "emd": "EMD",
             "namesilo_auction": "NameSilo (аукцион)", "list": "вручную", "backorder": "backorder (v1)", "cctld": "cctld (v1)",
             "reg_ru": "reg.ru (v1)", "sweb": "sweb (v1)"}
# Бейдж в строке — явная карта, не срез подписи: [:3] давал «вру» и «reg» (путался с reg.ru).
SOURCE_BADGE = {"dropcatch": "dc", "nominet": "uk", "mx": "mx", "emd": "emd", "namesilo_auction": "ns", "list": "руч",
                "backorder": "bo", "cctld": "cc", "reg_ru": "rg", "sweb": "sw"}

LANE_RU = {"bid": "ставка", "free": "свободный"}

# Индексация страницы (M5). `unknown` — не «нет»: это состояние, чей смысл ОБЯЗАН быть
# произнесён вслух, иначе оператор прочтёт его как «страницы нет в индексе» и пойдёт чинить
# работающее (ровно та ложь, от которой лечился check_index, F15). Поэтому подпись несёт и
# причину («движки молчат»), а не одно слово.
INDEX_RU = {
    "indexed": "в индексе", "not_indexed": "не в индексе",
    "unknown": "не знаю (движки молчат)",
}


def status_ru(v):
    return STATUS_RU.get(v, v) if v else ""


def reject_ru(v):
    return REJECT_RU.get(v, v) if v else ""


def source_ru(v):
    return SOURCE_RU.get(v, v) if v else ""


def source_badge(v):
    return SOURCE_BADGE.get(v, "?") if v else "?"


def lane_ru(v):
    return LANE_RU.get(v, v) if v else ""


def index_ru(v):
    return INDEX_RU.get(v, v) if v else ""


if __name__ == "__main__":  # self-check без БД
    assert status_ru("approved") == "одобрен" and status_ru("zzz") == "zzz"
    assert status_ru(None) == "" and lane_ru("bid") == "ставка"
    assert reject_ru("not_acquirable") == "нельзя купить"
    assert reject_ru("safebrowsing") == "Google Safe Browsing"
    assert index_ru("unknown") == "не знаю (движки молчат)" and index_ru(None) == ""
    print("labels ok")
