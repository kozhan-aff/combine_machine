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

См. docs/superpowers/specs/2026-07-19-tci-whois-design.md.
"""
import logging

_log = logging.getLogger(__name__)

# После скольких сбоев ПОДРЯД канал TCI считается мёртвым на этот прогон.
_TCI_FAILURE_LIMIT = 3


def probe(domain: str, clients: dict) -> dict:
    """{"available", "created", "free_date", "whois_source"}.

    Надмножество контракта AParserClient.whois_probe() — старые потребители
    ключей available/created не ломаются. whois_source показывает, ЧЕМ судили
    домен: сбой TCI молча не превращается в «A-Parser так решил».

    Предохранитель (см. модульный докстринг): пока `tci.consecutive_failures`
    < `_TCI_FAILURE_LIMIT`, TCI пробуется как обычно; успешный ответ сбрасывает
    счётчик в 0. Как только порог достигнут — TCI больше не вызывается вовсе
    (даже для доменов, что ему принадлежат по зоне), и это ОДИН раз логируется
    в момент срабатывания — не на каждый последующий домен."""
    tci = clients.get("tci")
    if tci is not None and tci.handles(domain):
        if getattr(tci, "consecutive_failures", 0) >= _TCI_FAILURE_LIMIT:
            source = "aparser_fallback"    # предохранитель уже сработал в этом прогоне — TCI не трогаем
        else:
            try:
                result = {**tci.probe(domain), "whois_source": "tci"}
                tci.consecutive_failures = 0    # канал жив — счётчик сбоев сброшен
                return result
            except Exception as e:                # noqa: BLE001 — сбой канала, не приговор домену
                # Безусловный след сбоя — только этот лог. score_breakdown.whois_source
                # получает "aparser_fallback" ТОЛЬКО когда домен проходит полный _funnel
                # (scoring.py кладёт его туда через whitelist-хелпер _kept) — а
                # recheck_acquirability whois_source из ответа вообще не читает, там
                # источник сбоя виден исключительно здесь.
                _log.warning("TCI whois сбой для %s (%s: %s) — фолбэк на A-Parser",
                             domain, type(e).__name__, e)
                tci.consecutive_failures = getattr(tci, "consecutive_failures", 0) + 1
                if tci.consecutive_failures >= _TCI_FAILURE_LIMIT:
                    # Срабатывание предохранителя — ОТДЕЛЬНЫЙ безусловный лог, ровно один раз
                    # на прогон (а не на каждый из оставшихся доменов, что и было находкой
                    # ревью): дальше domains до конца свипа даже не долетают до try выше.
                    _log.warning(
                        "TCI whois: %d сбоев подряд — предохранитель сработал, "
                        "до конца прогона TCI пропускается, whois идёт через A-Parser",
                        _TCI_FAILURE_LIMIT)
                source = "aparser_fallback"
        pr = clients["aparser"].whois_probe(domain)
    else:
        source = "aparser"
        pr = clients["aparser"].whois_probe(domain)
    return {"available": pr.get("available"), "created": pr.get("created"),
            "free_date": None, "whois_source": source}
