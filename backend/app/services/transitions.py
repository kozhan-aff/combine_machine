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
# законное решение оператора. Эти не крутятся ничем: РКН — реестр государства, блэклист —
# внешний вердикт, грязная история и флаг фида — прошлое домена. Для портфеля, который держится
# на ЧИСТОЙ ИСТОРИИ (CLAUDE.md), они значат «никогда».
#
# v2 (миграция 0025): `legacy_ru` — архив РФ-пула (РФ из v2 исключена), `tld_closed` — зона вне
# белого списка. Без них «↩ вернуть в approved» открывала бы ручной путь к кассе домену, которого
# машина больше не судит (в 0025 `legacy_ru` перезаписывает и «отмытые» v1-домены с `rkn`). Зону
# добавили в белый список — путь назад тот же, что у грязи: перескор, а не кнопка.
#
# v2 (W0/W6): `trademark` — чужой VPN-бренд в имени (юридический риск), `spam_anchors` — ссылочный
# профиль засыпан спамом. Ни то ни другое не крутится порогом — до кассы никогда.
#
# `not_acquirable` здесь НЕТ намеренно: «домен занят» — это не грязь, а чужая покупка. Оператор,
# знающий, что домен всё-таки дропнулся, вправе вернуть его руками.
DIRTY_REASONS = frozenset({"rkn", "blacklist", "history_dirty", "feed_flag", "safebrowsing",
                           "legacy_ru", "tld_closed", "trademark", "spam_anchors"})

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
    # Угроза Web Risk — улика из score_breakdown, а не из колонки `blacklisted` (её Web Risk не
    # пишет, находка 1.5): перескор, на котором Web Risk упал, её не стирает (_kept).
    if (d.score_breakdown or {}).get("webrisk_threats"):
        return "blacklist"
    # Спам-анкоры — улика W6 в score_breakdown (_kept): перескор, на котором Ahrefs упал или W6 не
    # дошла (кап, пол units), её не стирает (находка 1.4).
    if (d.score_breakdown or {}).get("spam_anchors") is True:
        return "spam_anchors"
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


def refuse_closed_zone(d, allowlist=None) -> None:
    """Зона вне белого списка — в `approved` домен не вернуть даже руками. Бросает TransitionDenied.

    v2 судит и выкупает только зоны белого списка (/settings). Отказ `legacy_ru`/`tld_closed` —
    в DIRTY_REASONS, но v1-домен .ru, отклонённый ПОРОГОМ (`low_score`, `too_young`), грязным не
    считается, а миграция 0025 архивирует только ещё не решённые домены. Без этого гарда «↩ вернуть
    в approved» вела бы такой домен в очередь backorder, который .ru всё ещё покупает (находка
    R2-19). Зону добавили в белый список — домен возвращается той же кнопкой.
    `allowlist=None` — список из /settings; самопроверка без БД передаёт его явно.
    """
    from app.services.domain_filters import tld_match
    if allowlist is None:
        from app.services.settings import get_settings
        allowlist = get_settings()["tld_allowlist"]
    if not tld_match(d.domain, allowlist):
        raise TransitionDenied(
            f"домен «{d.domain}»: его зоны нет в белом списке зон (/settings) — v2 не судит и не "
            "выкупает такие домены, в approved его не вернуть")


def check(d, target: str, *, allowlist=None) -> None:
    """Разрешён ли РУЧНОЙ перевод домена `d` в `target`. Бросает TransitionDenied.

    Грязь проверяется раньше зоны: у грязного домена вне списка оператор увидит причину-грязь."""
    src = d.status
    if target not in MANUAL_TRANSITIONS.get(src, frozenset()):
        raise TransitionDenied(
            f"домен «{d.domain}» в статусе {src!r}: ручной перевод в {target!r} не разрешён")
    if target in TOWARD_MONEY:
        refuse_dirty(d)
    if target == "approved":
        refuse_closed_zone(d, allowlist)


def set_status(d, target: str) -> None:
    """Проверить политику и перевести домен. Коммит — на вызывающем (он владеет сессией)."""
    check(d, target)
    d.status = target


if __name__ == "__main__":  # self-check без БД: политика чистая, ORM ей не нужен
    from types import SimpleNamespace as NS

    rkn = NS(domain="bad.ru", status="rejected", reject_reason="rkn", rkn_listed=True,
             blacklisted=None, prior_flags={}, wayback_checked=True, score_breakdown={})
    weak = NS(domain="weak.com", status="rejected", reject_reason="low_score", rkn_listed=False,
              blacklisted=None, prior_flags={}, wayback_checked=True, score_breakdown={})
    assert dirty_reason(rkn) == "rkn" and dirty_reason(weak) is None
    # угроза Web Risk — грязь по улике в score_breakdown (находка 1.5)
    assert dirty_reason(NS(**{**vars(weak), "score_breakdown": {"webrisk_threats": ["MALWARE"]}})) \
        == "blacklist"
    try:
        check(rkn, "approved", allowlist=["com", "ru"])   # зона разрешена: отказ именно по грязи
        raise AssertionError("грязь обязана быть отвергнута")
    except TransitionDenied:
        pass
    check(weak, "approved", allowlist=["com"])    # отсеянный ПОРОГОМ домен возвращается руками
    try:
        check(NS(**{**vars(weak), "domain": "weak.ru"}), "approved", allowlist=["com"])
        raise AssertionError("зона вне белого списка обязана быть отвергнута")
    except TransitionDenied:
        pass
    try:
        check(NS(domain="raw.ru", status="discovered", reject_reason=None, rkn_listed=None,
                 blacklisted=None, prior_flags={}, wayback_checked=True, score_breakdown={}),
              "purchased")
        raise AssertionError("покупка сырья мимо воронки обязана быть отвергнута")
    except TransitionDenied:
        pass
    print("transitions policy ok")
