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
}

LANE_RU = {"bid": "ставка", "free": "свободный"}


def status_ru(v):
    return STATUS_RU.get(v, v) if v else ""


def reject_ru(v):
    return REJECT_RU.get(v, v) if v else ""


def lane_ru(v):
    return LANE_RU.get(v, v) if v else ""


if __name__ == "__main__":  # self-check без БД
    assert status_ru("approved") == "одобрен" and status_ru("zzz") == "zzz"
    assert status_ru(None) == "" and lane_ru("bid") == "ставка"
    assert reject_ru("not_acquirable") == "нельзя купить"
    print("labels ok")
