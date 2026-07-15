"""Политика статусов домена: куда его вправе двинуть ЧЕЛОВЕК и что грязи запрещено навсегда.

ЗАЧЕМ (аудит 2026-07-14, F9+F13). Между M1 и кассой был открыт коридор: домен, отклонённый
воронкой за РКН, возвращался в оборот ОДНОЙ кнопкой — «↩ вернуть в approved» панель предлагала
и для грязи, а `set_status_action` проверял ТОЛЬКО целевой статус (`_MANUAL_STATUSES`), исходный
и `reject_reason` не смотрел вовсе. Дальше домен ехал в «Готовы к выкупу», в очередь выкупа и на
ставку, НИ РАЗУ не показав, что он грязный: `reject_reason` при этом даже не стирался — он просто
нигде не был показан там, где решают о деньгах. Второй вход в тот же коридор — `mark_purchased`
(`POST /api/domains/{id}/purchase`): он ставил `purchased` из ЛЮБОГО статуса, не спросив ничего.

ЧТО ЗДЕСЬ ЛЕЖИТ. Два запрета, оба — про РУЧНЫЕ действия:
  1. переход разрешён, только если он есть в MANUAL_TRANSITIONS (проверяется ИСХОДНЫЙ статус,
     а не только целевой);
  2. грязный домен (`dirty_reason`) не входит в статусы, ведущие к деньгам, — никаким ручным
     действием. Мимо этого запрета не пройти и в M2: `create_order`, `confirm_order` И
     `execute_confirmed_order` спрашивают `dirty_reason` отдельно. Последний — САМАЯ КАССА, и
     до ревью Задачи 6 он не спрашивал ничего: заказ, подтверждённый ДО фикса (`confirmed_by_
     human=True` уже стоит), приходил прямо на отправку, минуя и очередь, и гейт, — и списывал
     деньги за РКН-домен. Гард на входе в коридор не заменяет гарда у кассы.

ЧЕГО ЗДЕСЬ НЕТ И ПОЧЕМУ (осознанно — не «забыли»). `Domain.status` пишется ещё в четырёх местах,
и НИ ОДНО из них не является ручным решением о деньгах:
  · `scoring.score_domain` / `scoring.recheck_acquirability` — ВЕРДИКТ МАШИНЫ. Он и есть источник
    `reject_reason`; провести его через политику, читающую `reject_reason`, значит замкнуть круг:
    домен, однажды признанный грязным, не смог бы РЕАБИЛИТИРОВАТЬСЯ повторным скорингом, даже
    если РКН его сегодня разблокировал. А перескор — это и есть единственный ЧЕСТНЫЙ путь грязи
    обратно в оборот: не кнопка, а новые улики (score_domain берёт домены из `rejected`, чистит
    `reject_reason` и переписывает сигналы). Ровно поэтому кнопка «↩ вернуть» для грязи снята,
    а «▶ перепроверить» — поставлена рядом.
    НО: реабилитация законна ТОЛЬКО по уликам, которые проверка реально добыла. Перескор с ранним
    выходом воронки (T0 low_rd, T1 not_acquirable) РКН и Wayback не вызывает вовсе — и пока
    score_domain писал сигнальные колонки безусловно, туда ложился None: улики исчезали, домен
    становился чист для `dirty_reason`, и «▶ перепроверить» превращалась в ту же кнопку отмывки,
    вместо которой её и поставили (ревью Задачи 6, Critical 2). Теперь score_domain пишет сигнал
    только из отработавшей проверки — политика опирается на это.
  · `acquisition.mark_caught` / `poll_orders` (-> `purchased`) — деньги УЖЕ потрачены (заказ ушёл
    через денежный гейт, провайдер домен поймал). Это запись факта, а не решение; запретить её
    значило бы врать в БД про домен, которым мы уже владеем.
  · `acquisition.cancel_order` (`purchasing` -> `approved`) — ОТКАТ перехода, который политика уже
    разрешила на входе (в `purchasing` домен попадает только из `approved`, и только чистым).
    Запрет здесь ничего бы не закрыл, а вот залипший заказ на легаси-домене (грязный, попавший в
    очередь ДО фикса) стало бы невозможно снять — домен застрял бы в `purchasing` навсегда.
"""

# Причины отказа, за которыми стоит ФАКТ О ДОМЕНЕ, а не наш порог. Порог («мало доноров»,
# «молодой», «низкий скор») крутится на /settings, и вернуть такой домен в оборот руками —
# законное решение оператора. Эти четыре не крутятся ничем: РКН — реестр государства, блэклист —
# внешний вердикт, грязная история и флаг фида — прошлое домена. Для портфеля, который держится
# на ЧИСТОЙ ИСТОРИИ (CLAUDE.md), они значат «никогда».
#
# `not_acquirable` здесь НЕТ намеренно: «домен занят» — это не грязь, а чужая покупка. Оператор,
# знающий, что домен всё-таки дропнулся, вправе вернуть его руками.
DIRTY_REASONS = frozenset({"rkn", "blacklist", "history_dirty", "feed_flag"})

# Куда домен вправе двинуть ЧЕЛОВЕК. Ключ — ИСХОДНЫЙ статус (именно его и не смотрели).
# Пустое множество = «отсюда руками не двигают»:
#   purchasing — домен держит живой заказ, им распоряжается M2 (экран /queue: подтвердить,
#                отправить, снять). Ручной перевод разъехался бы с заказом.
#   purchased / live — деньги потрачены, сайт живёт. Отматывать статус назад нечем.
MANUAL_TRANSITIONS = {
    "discovered": frozenset({"rejected"}),                    # выбросить сырьё, не тратя воронку
    "scored":     frozenset({"approved", "rejected"}),        # ГЕЙТ КУРАЦИИ — инбокс M1
    "approved":   frozenset({"purchasing", "purchased",       # в очередь / «купил руками»
                             "rejected"}),                    # передумал
    "rejected":   frozenset({"approved"}),                    # реабилитация — но НЕ для грязи
    "purchasing": frozenset(),
    "purchased":  frozenset(),
    "live":       frozenset(),
}

# Статусы, вход в которые = движение К ДЕНЬГАМ. `approved` попал сюда не «за компанию»: это
# витрина «Готовы к выкупу», откуда идут в очередь выкупа и на ставку — грязи там не место.
TOWARD_MONEY = frozenset({"approved", "purchasing", "purchased"})


class TransitionDenied(ValueError):
    """Ручной перевод домена запрещён политикой (грязь или недопустимый исходный статус).

    Наследник ValueError: панель и M2 уже ловят ValueError от сервисов и показывают текст
    оператору — новый тип исключения не потребовал бы отдельной обработки нигде.
    """


def dirty_reason(d) -> str | None:
    """Почему домен НИКОГДА не должен доехать до кассы — или None, если он чист.

    Смотрит и ВЕРДИКТ машины (`reject_reason`), и СЫРЫЕ СИГНАЛЫ, из которых он вырос
    (`rkn_listed`, `blacklisted`, история — через `scoring.history_verdict`). Это не
    перестраховка: `reject_reason` — ЕДИНСТВЕННОЕ поле, которое ручная реабилитация НЕ трогала
    (домен уезжал в approved с живым «rkn» на борту), а перескор, наоборот, переписывает всё
    сразу. Судить о деньгах по одному полю значило бы верить, что оно всегда обновлялось вместе
    с остальными; на живой базе это уже не так.

    Историю спрашиваем у `history_verdict` — ЕДИНОГО предиката волны 1, а не читаем `prior_flags`
    заново: два места, реконструирующие «что мы знаем об истории» порознь, эта ветка разводила
    уже трижды.
    """
    from app.services.scoring import history_verdict     # ленивый импорт: scoring зовёт нас в ответ
    if d.reject_reason in DIRTY_REASONS:
        return d.reject_reason
    if d.rkn_listed:
        return "rkn"
    if d.blacklisted is True:                            # None = «не проверяли», это не грязь
        return "blacklist"
    if history_verdict(d) == "dirty":
        return "history_dirty"
    return None


def _dirty_ru(reason: str, domain: str) -> str:
    from app.services.labels import reject_ru
    return (f"домен «{domain}» помечен как грязный ({reject_ru(reason)}, код {reason}) — "
            "в выкуп он не идёт. Портфель держится на чистой истории: вернуть домен в оборот "
            "может только ПЕРЕСКОРИНГ («▶ перепроверить»), если проверки скажут, что он чист.")


def refuse_dirty(d) -> None:
    """Грязный домен не участвует в денежных действиях. Бросает TransitionDenied.

    Отдельно от `check`, потому что деньги тратят и БЕЗ смены статуса домена: заявка на выкуп
    и подтверждение ставки статус не двигают, а грязный домен мог попасть в очередь ДО этого
    фикса — и там его ждала бы кнопка «✓ подтвердить выкуп».
    """
    reason = dirty_reason(d)
    if reason:
        raise TransitionDenied(_dirty_ru(reason, d.domain))


def check(d, target: str) -> None:
    """Разрешён ли РУЧНОЙ перевод домена `d` в `target`. Бросает TransitionDenied."""
    src = d.status
    if target not in MANUAL_TRANSITIONS.get(src, frozenset()):
        raise TransitionDenied(
            f"домен «{d.domain}» в статусе {src!r}: ручной перевод в {target!r} не разрешён")
    if target in TOWARD_MONEY:
        refuse_dirty(d)


def set_status(d, target: str) -> None:
    """Проверить политику и перевести домен. Коммит — на вызывающем (он владеет сессией)."""
    check(d, target)
    d.status = target


if __name__ == "__main__":  # self-check без БД: политика чистая, ORM ей не нужен
    from types import SimpleNamespace as NS

    rkn = NS(domain="bad.ru", status="rejected", reject_reason="rkn",
             rkn_listed=True, blacklisted=None, prior_flags={}, wayback_checked=True)
    weak = NS(domain="weak.ru", status="rejected", reject_reason="low_score",
              rkn_listed=False, blacklisted=None, prior_flags={}, wayback_checked=True)
    assert dirty_reason(rkn) == "rkn" and dirty_reason(weak) is None
    try:
        check(rkn, "approved")
        raise AssertionError("грязь обязана быть отвергнута")
    except TransitionDenied:
        pass
    check(weak, "approved")                       # отсеянный ПОРОГОМ домен возвращается руками
    try:
        check(NS(domain="raw.ru", status="discovered", reject_reason=None, rkn_listed=None,
                 blacklisted=None, prior_flags={}, wayback_checked=True), "purchased")
        raise AssertionError("покупка сырья мимо воронки обязана быть отвергнута")
    except TransitionDenied:
        pass
    print("transitions policy ok")
