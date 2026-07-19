"""Маршрутизация whois: зоны TCI (.ru/.рф/.su) — прямым сокетом, остальные —
через A-Parser. Логика выбора канала живёт здесь, а не в транспорте (конвенция
проекта: integrations/ = только транспорт).

См. docs/superpowers/specs/2026-07-19-tci-whois-design.md.
"""
import logging

_log = logging.getLogger(__name__)


def probe(domain: str, clients: dict) -> dict:
    """{"available", "created", "free_date", "whois_source"}.

    Надмножество контракта AParserClient.whois_probe() — старые потребители
    ключей available/created не ломаются. whois_source показывает, ЧЕМ судили
    домен: сбой TCI молча не превращается в «A-Parser так решил»."""
    tci = clients.get("tci")
    if tci is not None and tci.handles(domain):
        try:
            return {**tci.probe(domain), "whois_source": "tci"}
        except Exception as e:                # noqa: BLE001 — сбой канала, не приговор домену
            # Безусловный след сбоя — только этот лог. score_breakdown.whois_source
            # получает "aparser_fallback" ТОЛЬКО когда домен проходит полный _funnel
            # (scoring.py кладёт его туда через whitelist-хелпер _kept) — а
            # recheck_acquirability whois_source из ответа вообще не читает, там
            # источник сбоя виден исключительно здесь.
            _log.warning("TCI whois сбой для %s (%s: %s) — фолбэк на A-Parser",
                         domain, type(e).__name__, e)
            source = "aparser_fallback"
        pr = clients["aparser"].whois_probe(domain)
    else:
        source = "aparser"
        pr = clients["aparser"].whois_probe(domain)
    return {"available": pr.get("available"), "created": pr.get("created"),
            "free_date": None, "whois_source": source}
