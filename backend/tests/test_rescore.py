"""Скоринг не теряет доказательства при рескоре (аудит F24 + F25, Задача 17).

Репро из брифа: 1-й скоринг видит DR от Ahrefs (`authority=1.0`), 2-й скоринг того же домена
Ahrefs не зовёт повторно (RD у него уже есть — T3b короткозамкнута), `sig["dr"]` остаётся
пустым, и `compute_score` считал `authority` от нуля — хотя `d.dr` в БД уже хранил проверенное
значение. Тот же корень, что уже чинили для сигналов T0-T3 (ревью Задачи 6, Critical 2): «нет
значения в ЭТОМ прогоне» — это «не проверяли ЗАНОВО», а не «неизвестно».
"""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

import app.db as db
from app.models.domain import Domain
from app.models.offer import Offer, SiteOffer
from app.models.site import Site
from app.services import scoring


def _add(**kw) -> int:
    """Домен в БД. kw — ровно те поля, которые пишет воронка (scoring.score_domain)."""
    with db.SessionLocal() as s:
        d = Domain(source=kw.pop("source", "backorder"), **kw)
        s.add(d)
        s.commit()
        s.refresh(d)
        return d.id


def _get(did: int) -> Domain:
    with db.SessionLocal() as s:
        return s.get(Domain, did)


class _CleanWayback:
    """История чистая и проверенная — воронка доходит до compute_score, не до history_dirty."""

    def classify_history(self, domain):
        return {"prior_flags": {c: False for c in
                                ("adult", "pharma", "casino", "gambling", "spam")},
                "wayback_checked": True, "sampled": 4, "first_seen": None, "age_years": None,
                "evidence": [{"url": domain, "timestamp": "20150101", "cats": [], "chars": 900}]}


def _survives_to_score(whois_created) -> dict:
    """Клиенты, под которыми домен ЖИВЫМ доезжает до compute_score: whois старый (не too_young),
    РКН/блэклист чисты, эхо есть, история чистая. Ahrefs НЕ в словаре намеренно — тест сам решает,
    нужен ли он (для lane='bid' + referring_domains задан T3b его и так не позовёт)."""
    class _Whois:
        def whois_probe(self, dom):
            return {"available": False, "created": whois_created}

    return {
        "aparser": _Whois(),
        "rkn": type("R", (), {"is_listed": lambda self, d: False})(),
        "blacklist": type("B", (), {"is_blacklisted": lambda self, d: False})(),
        "searxng": type("S", (), {"indexed_echo": lambda self, d: True})(),
        "wayback": _CleanWayback(),
        "tci": type("T", (), {"handles": lambda self, d: False})(),
    }


# --- (a) DR из прошлого прогона не обнуляет authority --------------------------------------

def test_rescore_keeps_authority_without_a_new_dr_observation():
    """ПРОХОДИЛО на 45654e3 (до фикса): рескор домена, у которого фид уже дал `referring_domains`
    (Ahrefs поэтому НЕ зовётся, см. T3b), терял `authority` — `sig["dr"]` не подхватывал
    сохранённый `d.dr`, и `compute_score` считал его от нуля.

    `dr=30.0` при `NORM["DR_FULL"]=30.0` даёт `authority=1.0` — полный балл, ровно как при
    живом наблюдении Ahrefs. Без фикса тот же домен получил бы `authority=0.0`, будто DR
    никогда не проверяли.
    """
    old = datetime.now(timezone.utc) - timedelta(days=365 * 10)
    did = _add(domain="rescore-dr.ru", status="scored", lane="bid",
               referring_domains=3000, dr=30.0)

    # `_survives_to_score`'s aparser mock has no `ahrefs_probe` at all — если бы T3b всё же
    # попыталась его позвать, тест упал бы AttributeError'ом. Она не пытается: RD (3000) уже
    # не None, T3b короткозамкнута ПО КОНСТРУКЦИИ (см. _funnel), это и воспроизводит баг.
    out = scoring.score_domain(did, clients=_survives_to_score(old), ahrefs_budget=[5])

    assert out["reject_reason"] is None
    assert out["breakdown"]["components"]["authority"] == 1.0, (
        f"authority обнулился при рескоре: {out['breakdown']['components']}")
    d = _get(did)
    assert d.dr == 30.0                             # DR из прошлого наблюдения не стёрт


# --- (б) ранний T0-отказ не стирает уже сохранённые улики ----------------------------------

def test_early_t0_reject_does_not_erase_saved_evidence():
    """Guard-тест на УЖЕ ДЕЙСТВУЮЩИЙ фикс (ревью Задачи 6, Critical 2 — scoring.py, цикл
    `for col in (...)`: пишем колонку ТОЛЬКО если `sig` реально её содержит в ЭТОМ прогоне).
    Регрессией к моменту старта Задачи 17 эта часть уже НЕ является — она была зелёной и до
    правок этой задачи. Оставляем как страховку: T0 (feed_flag) выходит ДО whois вообще, а
    значит `sig` в этом прогоне не содержит ни `prior_flags`, ни `age_years`, ни `rkn_listed` —
    старые значения в БД обязаны выжить.
    """
    did = _add(domain="still-flagged.ru", status="rejected", reject_reason="history_dirty",
               referring_domains=50, feed_flags={"rkn": True},
               prior_flags={"casino": True}, age_years=9.0, rkn_listed=True,
               wayback_checked=True)

    class _MustNotBeCalled:
        """Ни один из T1-T3 клиентов не имеет права позваться — T0 отклоняет раньше всех."""
        def whois_probe(self, dom):
            raise AssertionError("T0 обязан отклонить ДО whois (feed_flags.rkn)")
        def is_listed(self, dom):
            raise AssertionError("T0 обязан отклонить ДО РКН")
        def is_blacklisted(self, dom):
            raise AssertionError("T0 обязан отклонить ДО блэклиста")
        def indexed_echo(self, dom):
            raise AssertionError("T0 обязан отклонить ДО эха")
        def classify_history(self, dom):
            raise AssertionError("T0 обязан отклонить ДО Wayback")

    guard = _MustNotBeCalled()
    clients = {"aparser": guard, "rkn": guard, "blacklist": guard, "searxng": guard,
               "wayback": guard,
               "tci": type("T", (), {"handles": lambda self, d: False})()}

    out = scoring.score_domain(did, clients=clients)

    assert out["reject_reason"] == "feed_flag"
    d = _get(did)
    assert d.prior_flags == {"casino": True}         # улика НЕ стёрта отсутствием наблюдения
    assert d.age_years == 9.0
    assert d.rkn_listed is True


def test_scored_at_is_stamped_when_funnel_reaches_a_decision():
    """F24: колонка отсутствовала вообще — при рескоре (или отказе) непонятно было, КОГДА
    домен последний раз прошёл воронку до решения. Ставится и на approve/scored, и на reject —
    в обоих случаях воронка ДОШЛА до решения (в отличие от unresolved, где домен остаётся
    discovered и «оценённым» не является).
    """
    did = _add(domain="stamped.ru", status="discovered", referring_domains=50,
               feed_flags={"rkn": True})
    before = _get(did)
    assert before.scored_at is None

    out = scoring.score_domain(did, clients=_survives_to_score(None))
    assert out["reject_reason"] == "feed_flag"

    after = _get(did)
    assert after.scored_at is not None                # SQLite отдаёт datetime без tzinfo — не сверяем его


# --- (в) UNIQUE(site_offers.site_id, offer_id) — инвариант БД, не удача SELECT'а -----------

def _site_and_offer() -> tuple[int, int]:
    with db.SessionLocal() as s:
        dom = Domain(domain="offer-dup.ru", source="backorder", status="approved")
        s.add(dom)
        s.commit()
        s.refresh(dom)
        site = Site(domain_id=dom.id, status="content", niche="VPN")
        s.add(site)
        s.commit()
        s.refresh(site)
        offer = Offer(brand="NordVPN", affiliate_link="https://example.com/aff")
        s.add(offer)
        s.commit()
        s.refresh(offer)
        return site.id, offer.id


def test_db_refuses_a_second_site_offer_per_pair():
    """И `panel.py::attach_offer`, и `pipeline.py::attach_offer` гейтят дубль ОДИНАКОВО —
    SELECT «пара уже есть?», вставка отдельным COMMIT'ом: та же гонка ДВУХ ПРОЦЕССОВ, что уже
    чинили для `Site.domain_id` (uq_site_per_domain) и `Page.url_path` (uq_page_per_path).
    Вставляем ровно то, что коммитит проигравшая гонку транзакция.
    """
    site_id, offer_id = _site_and_offer()
    with db.SessionLocal() as s:
        s.add(SiteOffer(site_id=site_id, offer_id=offer_id))
        s.commit()
    with db.SessionLocal() as s:
        s.add(SiteOffer(site_id=site_id, offer_id=offer_id))
        with pytest.raises(IntegrityError):
            s.commit()
    with db.SessionLocal() as s:
        rows = s.execute(select(SiteOffer).where(
            SiteOffer.site_id == site_id, SiteOffer.offer_id == offer_id)).scalars().all()
        assert len(rows) == 1
