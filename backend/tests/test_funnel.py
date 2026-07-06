"""Воронка скоринга дёшево→дорого: ранний выход, reject_reason, дорогой Wayback только для выживших."""
from datetime import datetime, timezone, timedelta
import app.db as db
from app.models.domain import Domain
from app.services import scoring


def _mk(**kw):
    with db.SessionLocal() as s:
        d = Domain(domain=kw.pop("domain", "x.ru"), source="cctld", status="discovered", **kw)
        s.add(d); s.commit(); s.refresh(d)
        return d.id


class _Wayback:
    def __init__(self): self.calls = 0
    def classify_history(self, domain):
        self.calls += 1
        return {"prior_flags": {c: False for c in ("adult", "pharma", "casino", "gambling", "spam")},
                "first_seen": None, "age_years": 9.0, "wayback_checked": True, "sampled": 5}


def _clients(whois_dt, wb, rkn=False, bl=False):
    class _W:  # aparser
        def whois_created(self, dom): return whois_dt
    class _R:
        def is_listed(self, dom): return rkn
    class _B:
        def is_blacklisted(self, dom): return bl
    class _S:
        def indexed_echo(self, dom): return True
    return {"aparser": _W(), "rkn": _R(), "blacklist": _B(), "searxng": _S(),
            "wayback": wb, "opr": None}


def test_too_young_rejects_before_wayback():
    did = _mk(domain="young.ru", referring_domains=5)
    wb = _Wayback()
    young = datetime.now(timezone.utc) - timedelta(days=365)   # 1 год
    out = scoring.score_domain(did, clients=_clients(young, wb))
    assert out["status"] == "rejected" and out["reject_reason"] == "too_young"
    assert wb.calls == 0            # ЯДРО: дорогой Wayback НЕ вызван для молодого домена


def test_feed_flag_rejects_first():
    did = _mk(domain="blocked.ru", referring_domains=50, feed_flags={"rkn": True})
    wb = _Wayback()
    out = scoring.score_domain(did, clients=_clients(None, wb))
    assert out["reject_reason"] == "feed_flag" and wb.calls == 0


def test_low_rd_rejects():
    did = _mk(domain="thin.ru", referring_domains=0)
    wb = _Wayback()
    from app.services import settings as st
    st.update_settings(min_referring_domains=1)
    out = scoring.score_domain(did, clients=_clients(None, wb))
    assert out["reject_reason"] == "low_rd" and wb.calls == 0


def test_rkn_rejects_before_wayback():
    did = _mk(domain="rkn.ru", referring_domains=50)
    wb = _Wayback()
    old = datetime.now(timezone.utc) - timedelta(days=365 * 8)
    out = scoring.score_domain(did, clients=_clients(old, wb, rkn=True))
    assert out["reject_reason"] == "rkn" and wb.calls == 0


def test_whois_none_falls_through_to_wayback_age():
    did = _mk(domain="nowhois.ru", referring_domains=3000)
    wb = _Wayback()
    out = scoring.score_domain(did, clients=_clients(None, wb))   # whois не отдал дату
    assert wb.calls == 1                                          # дошли до T3
    assert out["status"] in ("approved", "scored")               # чистый сильный домен
    with db.SessionLocal() as s:
        d = s.get(Domain, did)
    assert float(d.age_years) == 9.0                             # возраст — фолбэком из Wayback


def test_clean_strong_domain_approved():
    did = _mk(domain="good.ru", referring_domains=3000)
    wb = _Wayback()
    old = datetime.now(timezone.utc) - timedelta(days=365 * 9)
    out = scoring.score_domain(did, clients=_clients(old, wb))
    assert wb.calls == 1 and out["status"] == "approved" and out["reject_reason"] is None
