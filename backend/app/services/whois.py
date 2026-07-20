"""Маршрутизация whois: зоны TCI (.ru/.рф/.su) — прямым сокетом, остальные —
через A-Parser. Логика выбора канала живёт здесь, а не в транспорте (конвенция
проекта: integrations/ = только транспорт).

Предохранитель (повторное ревью 2026-07-20, IMPORTANT 1): если
whois.tcinet.ru:43 заблэкхолен (пакеты дропаются, а не connection refused —
типичная картина для фильтрации исходящего 43/tcp, а прод-бокс — Windows +
Docker Desktop), каждый .ru-домен платил бы полным `_TIMEOUT` на connect и ВСЁ
РАВНО уезжал в A-Parser — на капе `max_whois_per_run=200` это ~33 минуты
чистого ожидания и 200 строк warning в лог за один прогон, при том что вся
цель прямого TCI-whois — уйти ОТ A-Parser. После `_TCI_FAILURE_LIMIT` сбоев
ПОДРЯД TCI считается мёртвым и пропускается до конца прогона: домены сразу
уходят в A-Parser с `whois_source="aparser_fallback"`, без единого лишнего
обращения к TCI. Счётчик — `clients["tci"].consecutive_failures`, атрибут
самого клиента (см. `TciWhoisClient`); клиент создаётся заново на каждый
прогон (`scoring._make_clients()`), поэтому срабатывание предохранителя НЕ
переживает свип — следующий прогон получает новый инстанс со счётчиком 0 и
пробует TCI заново.

Предохранитель А-Parser whois (аудит серии 2026-07-20, находки 1/5): та же болезнь, что
и TCI, только на другом канале. `whois_probe()` у A-Parser вызывается ДВАЖДЫ без всякой
защиты — как фолбэк после срабатывания TCI-предохранителя (`aparser_fallback`), так и
безусловно для доменов вне зоны TCI (`aparser`, третий уровень типа shop.com.ru или
gTLD). Если A-Parser в это время сам недоступен (живой инцидент 2026-07-20 — тот же
день, когда чинили safebrowsing_check), КАЖДЫЙ такой домен платит полный ретрай-шторм
BaseClient (3 попытки × exponential backoff, ~30 с) — ровно тот симптом, ради которого
и заводился предохранитель SafeBrowsing, просто на соседнем вызове того же клиента.
Счётчик — `clients["aparser"].whois_failures`, тот же приём (атрибут инстанса,
пересоздаётся `_make_clients()` раз в прогон). Сработавший предохранитель не глотает
домен молча: `_aparser_whois()` поднимает исключение дальше — _funnel уже умеет
обрабатывать сбой whois (sig["errors"], acquirability_unresolved для не-bid) ровно так
же, как обычный сетевой сбой.

См. docs/superpowers/specs/2026-07-19-tci-whois-design.md.
"""
import logging
from contextlib import nullcontext

_log = logging.getLogger(__name__)

# После скольких сбоев ПОДРЯД канал TCI считается мёртвым на этот прогон.
_TCI_FAILURE_LIMIT = 3
# То же самое для A-Parser whois (фолбэк TCI + домены вне его зоны).
_APARSER_WHOIS_FAILURE_LIMIT = 3


def _aparser_whois(ap, domain: str, lock=None) -> dict:
    """whois_probe с предохранителем — см. модульный докстринг. `lock` — общий на волну
    (создаётся в _make_clients под ключом "_whois_lock"): под конкурентностью несколько
    потоков волны могут одновременно читать/писать ap.whois_failures на ОДНОМ инстансе
    клиента — голый += 1 не атомарен (LOAD/ADD/STORE — GIL может переключить поток между
    ними), гонка занижала бы счётчик или логировала срабатывание предохранителя дважды.
    lock=None (вызов вне волны, тесты) — используем no-op контекст, поведение как раньше."""
    cm = lock if lock is not None else nullcontext()
    with cm:
        breaker_open = getattr(ap, "whois_failures", 0) >= _APARSER_WHOIS_FAILURE_LIMIT
    if breaker_open:
        raise RuntimeError("A-Parser whois: предохранитель сработал, канал пропускается до конца прогона")
    try:
        pr = ap.whois_probe(domain)
        with cm:
            ap.whois_failures = 0          # канал жив — счётчик сбоев сброшен
        return pr
    except Exception:
        with cm:
            ap.whois_failures = getattr(ap, "whois_failures", 0) + 1
            tripped = ap.whois_failures >= _APARSER_WHOIS_FAILURE_LIMIT
        if tripped:
            _log.warning(
                "A-Parser whois: %d сбоев подряд — предохранитель сработал, "
                "до конца прогона канал пропускается", _APARSER_WHOIS_FAILURE_LIMIT)
        raise


def probe(domain: str, clients: dict) -> dict:
    """{"available", "created", "free_date", "whois_source"}.

    Надмножество контракта AParserClient.whois_probe() — старые потребители
    ключей available/created не ломаются. whois_source показывает, ЧЕМ судили
    домен: сбой TCI молча не превращается в «A-Parser так решил».

    `clients.get("_whois_lock")` — общий лок волны (см. scoring._make_clients),
    None вне волны (одиночный score_domain, юнит-тесты) — nullcontext, поведение
    как раньше (потокобезопасность не нужна при последовательном вызове).

    Предохранитель (см. модульный докстринг): пока `tci.consecutive_failures`
    < `_TCI_FAILURE_LIMIT`, TCI пробуется как обычно; успешный ответ сбрасывает
    счётчик в 0. Как только порог достигнут — TCI больше не вызывается вовсе
    (даже для доменов, что ему принадлежат по зоне), и это ОДИН раз логируется
    в момент срабатывания — не на каждый последующий домен."""
    cm = clients.get("_whois_lock") or nullcontext()
    tci = clients.get("tci")
    if tci is not None and tci.handles(domain):
        with cm:
            breaker_open = getattr(tci, "consecutive_failures", 0) >= _TCI_FAILURE_LIMIT
        if breaker_open:
            source = "aparser_fallback"    # предохранитель уже сработал в этом прогоне — TCI не трогаем
        else:
            try:
                result = {**tci.probe(domain), "whois_source": "tci"}
                with cm:
                    tci.consecutive_failures = 0    # канал жив — счётчик сбоев сброшен
                return result
            except Exception as e:                # noqa: BLE001 — сбой канала, не приговор домену
                _log.warning("TCI whois сбой для %s (%s: %s) — фолбэк на A-Parser",
                             domain, type(e).__name__, e)
                with cm:
                    tci.consecutive_failures = getattr(tci, "consecutive_failures", 0) + 1
                    tripped = tci.consecutive_failures >= _TCI_FAILURE_LIMIT
                if tripped:
                    _log.warning(
                        "TCI whois: %d сбоев подряд — предохранитель сработал, "
                        "до конца прогона TCI пропускается, whois идёт через A-Parser",
                        _TCI_FAILURE_LIMIT)
                source = "aparser_fallback"
        pr = _aparser_whois(clients["aparser"], domain, cm if clients.get("_whois_lock") else None)
    else:
        source = "aparser"
        pr = _aparser_whois(clients["aparser"], domain, cm if clients.get("_whois_lock") else None)
    return {"available": pr.get("available"), "created": pr.get("created"),
            "free_date": None, "whois_source": source}
