"""Доступность и дата регистрации домена — W2 `avail` воронки и перепроверка занятости.

v2: RDAP по бутстрапу IANA — для зон, где он есть (.com/.net/.org/.uk/.online/.xyz/.site/.si/.nl/
.in …): бесплатно и структурированно. Зоны без RDAP (.mx/.co/.nz/.de …) — whois:43 через A-Parser
Net::Whois, как в v1. TCI (.ru) удалён вместе с РФ. Логика выбора канала живёт здесь, а не в
транспорте (конвенция проекта: integrations/ = только транспорт).

ПРЕДОХРАНИТЕЛИ (урок v1: TCI и A-Parser в живом инциденте 2026-07-20). Лежащий канал без
предохранителя — ретрай-шторм BaseClient (3 попытки × backoff) на КАЖДЫЙ домен волны из 12
потоков. После `_FAILURE_LIMIT` сбоев ПОДРЯД канал считается мёртвым до конца прогона и не
вызывается вовсе: `probe` сразу поднимает `CircuitOpen`, воронка пишет `whois:circuit_open` и
обрабатывает домен как обычный сбой whois (unresolved для не-bid). Счётчик — атрибут инстанса
клиента (`rdap.lookup_failures_<зона>`, `aparser.whois_failures`); клиенты пересоздаются раз в прогон
(`scoring._make_clients()`), поэтому сработавший предохранитель не переживает прогон. Под
конкурентностью волны счётчик меняется под общим локом из `_make_clients` (`_rdap_lock`,
`_whois_lock`): голый `+= 1` не атомарен.
"""
import logging
from contextlib import nullcontext

_log = logging.getLogger(__name__)

# После скольких сбоев ПОДРЯД канал считается мёртвым на этот прогон (RDAP и A-Parser whois).
_FAILURE_LIMIT = 3


class CircuitOpen(RuntimeError):
    """Предохранитель канала сработал — до конца прогона канал не вызывается."""


def guarded(client, attr: str, call, name: str, lock=None, soft: tuple = ()):
    """Вызвать `call()` под предохранителем со счётчиком `client.<attr>`. `soft` — классы
    исключений, которые НЕ считаются падением канала (троттлинг: канал жив, просит подождать). `lock` — общий лок
    волны; гейт-чек и запись счётчика — обе под ним (детерминированно проверяют спай-локом).
    Тем же помощником волна risk защищает Google Web Risk (scoring._risk_one)."""
    cm = lock if lock is not None else nullcontext()
    with cm:
        breaker_open = getattr(client, attr, 0) >= _FAILURE_LIMIT
    if breaker_open:
        raise CircuitOpen(f"{name}: предохранитель сработал, канал пропускается до конца прогона")
    try:
        out = call()
    except soft:
        raise
    except Exception:
        with cm:
            setattr(client, attr, getattr(client, attr, 0) + 1)
            tripped = getattr(client, attr) == _FAILURE_LIMIT
        if tripped:
            _log.warning("%s: %d сбоев подряд — предохранитель сработал, до конца прогона канал "
                         "пропускается", name, _FAILURE_LIMIT)
        raise
    with cm:
        setattr(client, attr, 0)            # канал жив — счётчик сбоев сброшен
    return out


def _aparser_whois(ap, domain: str, lock=None) -> dict:
    """A-Parser whois_probe под предохранителем (счётчик `ap.whois_failures`)."""
    return guarded(ap, "whois_failures", lambda: ap.whois_probe(domain), "A-Parser whois", lock)


def _rdap_lookup(rdap, domain: str, lock=None) -> dict:
    """RDAP lookup под предохранителем ПО ЗОНЕ (счётчик `rdap.lookup_failures_<tld>`): три .nl
    подряд (SIDN отвечает 429 на второй запрос) не должны отключать RDAP для .com/.co.uk до
    конца прогона (S1-06). 404 — не сбой: lookup отвечает «домена нет» без исключения; троттлинг
    (`RdapThrottled`) — тоже не падение канала."""
    from app.integrations.rdap import RdapThrottled
    zone = domain.rsplit(".", 1)[-1].lower()
    return guarded(rdap, f"lookup_failures_{zone}", lambda: rdap.lookup(domain), f"RDAP .{zone}", lock,
                   soft=(RdapThrottled,))


def probe(domain: str, clients: dict) -> dict:
    """{"available", "created", "free_date", "whois_source", "status"}.

    `free_date` всегда None: проекцию «освободится» давал только TCI (.ru). `status` — статусы
    RDAP в нижнем регистре (`["pending delete", …]`); у whois:43 — пустой список. Локи —
    `clients["_rdap_lock"]`/`clients["_whois_lock"]` (scoring._make_clients); вне волны (юнит-тесты)
    их нет — nullcontext."""
    rdap = clients.get("rdap")
    if rdap is not None and rdap.has_rdap(domain):
        r = _rdap_lookup(rdap, domain, clients.get("_rdap_lock"))
        return {"available": not r["exists"],
                "created": r["registered_at"] if r["exists"] else None,
                "free_date": None, "whois_source": "rdap", "status": list(r.get("status") or [])}
    pr = _aparser_whois(clients["aparser"], domain, clients.get("_whois_lock"))
    return {"available": pr.get("available"), "created": pr.get("created"),
            "free_date": None, "whois_source": "aparser", "status": []}
