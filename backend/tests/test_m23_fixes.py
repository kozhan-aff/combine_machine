"""Regression tests for the reviewed M2 (acquisition) + M3 (provisioning) fixes.

Runs on the shared SQLite harness (conftest.py). No network: provider transports
raise NotImplementedError by default; where a provider "success" is needed it's
monkeypatched. The hard money gate (order goes to a provider ONLY when
confirmed_by_human) is asserted to still hold after every change.
"""
import pytest

import app.db as db
from app.models.domain import Domain, AcquisitionOrder


def _add(obj):
    with db.SessionLocal() as s:
        s.add(obj)
        s.commit()
        s.refresh(obj)
        return obj.id


# --- fix 2/4: money gate + atomic claim + retry-from-failed --------------------

def test_execute_refuses_without_confirmation():
    """ГЕЙТ: execute до confirm отказывает и статус не трогает (деньги не на автопилоте)."""
    from app.services import acquisition
    did = _add(Domain(domain="gate.ru", source="backorder", status="approved"))
    oid = acquisition.create_order(did, "backorder")
    r = acquisition.execute_confirmed_order(oid)
    assert "gate" in (r.get("error") or "")
    with db.SessionLocal() as s:
        assert s.get(AcquisitionOrder, oid).status == "pending_confirm"


def test_claim_blocks_double_execute(monkeypatch):
    """После успешной отправки заказ 'ordered' — повторный execute отвергается claim'ом."""
    from app.services import acquisition
    did = _add(Domain(domain="claim.ru", source="backorder", status="approved"))
    oid = acquisition.create_order(did, "backorder")
    acquisition.confirm_order(oid)
    # провайдер «успешно» принял заказ -> ordered
    monkeypatch.setattr("app.integrations.backorder.BackorderClient.order",
                        lambda self, domain, price_id, period_id: {"order_id": "OK1"})
    r1 = acquisition.execute_confirmed_order(oid)
    assert r1["status"] == "ordered"
    # второй клик: claim не находит 'ordered' в (pending_confirm, failed) -> отказ, без второго заказа
    r2 = acquisition.execute_confirmed_order(oid)
    assert r2.get("status") == "ordered" and "note" in r2
    with db.SessionLocal() as s:
        assert s.get(AcquisitionOrder, oid).provider_order_id == "OK1"


def test_in_flight_order_is_not_re_sent():
    """Заказ, уже забранный в 'ordering' (claim другим кликом), повторный execute не шлёт."""
    from app.services import acquisition
    did = _add(Domain(domain="inflight.ru", source="backorder", status="approved"))
    oid = acquisition.create_order(did, "backorder")
    acquisition.confirm_order(oid)
    with db.SessionLocal() as s:                       # симулируем in-flight claim
        s.get(AcquisitionOrder, oid).status = "ordering"
        s.commit()
    r = acquisition.execute_confirmed_order(oid)
    assert r.get("status") == "ordering" and "note" in r


def test_retry_from_failed_works():
    """Fix 4: заказ в 'failed' + confirmed можно повторить (транспорт не готов -> честный failed)."""
    from app.services import acquisition
    did = _add(Domain(domain="retry.ru", source="backorder", status="approved"))
    oid = acquisition.create_order(did, "backorder")
    acquisition.confirm_order(oid)
    r1 = acquisition.execute_confirmed_order(oid)      # real transport -> NotImplementedError
    assert r1["status"] == "failed" and "implement" in (r1.get("error") or "").lower()
    r2 = acquisition.execute_confirmed_order(oid)      # ретрай из failed реально отрабатывает
    assert r2["status"] == "failed" and "implement" in (r2.get("error") or "").lower()


# --- fix 3: create_order only from approved ------------------------------------

def test_create_order_rejects_non_approved():
    from app.services import acquisition
    did = _add(Domain(domain="dirty.ru", source="backorder", status="rejected"))
    with pytest.raises(ValueError):
        acquisition.create_order(did, "backorder")
    with db.SessionLocal() as s:
        assert s.get(Domain, did).status == "rejected"   # не затёрли решение скоринга


# --- fix 1: mark_caught / cancel_order unstick the 'purchasing' domain ----------

def test_mark_caught_moves_domain_to_purchased(monkeypatch):
    from app.services import acquisition
    did = _add(Domain(domain="caught.ru", source="backorder", status="approved"))
    oid = acquisition.create_order(did, "backorder")
    acquisition.confirm_order(oid)
    monkeypatch.setattr("app.integrations.backorder.BackorderClient.order",
                        lambda self, domain, price_id, period_id: {"order_id": "OK2"})
    assert acquisition.execute_confirmed_order(oid)["status"] == "ordered"
    r = acquisition.mark_caught(oid)
    assert r["status"] == "caught"
    with db.SessionLocal() as s:
        assert s.get(AcquisitionOrder, oid).status == "caught"
        assert s.get(Domain, did).status == "purchased"


def test_mark_caught_rejects_non_ordered():
    from app.services import acquisition
    did = _add(Domain(domain="notordered.ru", source="backorder", status="approved"))
    oid = acquisition.create_order(did, "backorder")   # pending_confirm
    r = acquisition.mark_caught(oid)
    assert "note" in r
    with db.SessionLocal() as s:
        assert s.get(Domain, did).status == "purchasing"   # без изменений


def test_cancel_returns_domain_to_approved():
    from app.services import acquisition
    did = _add(Domain(domain="cancel.ru", source="backorder", status="approved"))
    oid = acquisition.create_order(did, "backorder")
    with db.SessionLocal() as s:
        assert s.get(Domain, did).status == "purchasing"
    r = acquisition.cancel_order(oid)
    assert r["status"] == "cancelled"
    with db.SessionLocal() as s:
        assert s.get(AcquisitionOrder, oid).status == "cancelled"
        assert s.get(Domain, did).status == "approved"     # вернулся в очередь на покупку


def test_cancel_from_failed_also_returns_approved():
    from app.services import acquisition
    did = _add(Domain(domain="cancelfailed.ru", source="backorder", status="approved"))
    oid = acquisition.create_order(did, "backorder")
    acquisition.confirm_order(oid)
    assert acquisition.execute_confirmed_order(oid)["status"] == "failed"
    r = acquisition.cancel_order(oid)
    assert r["status"] == "cancelled"
    with db.SessionLocal() as s:
        assert s.get(Domain, did).status == "approved"


# --- fix 6: create_site_for only for a purchased domain ------------------------

def test_create_site_for_rejects_unpurchased():
    from app.services import provisioning
    did = _add(Domain(domain="notbought.ru", source="backorder", status="discovered"))
    with pytest.raises(ValueError):
        provisioning.create_site_for(did)


def test_create_site_for_allows_purchased():
    from app.services import provisioning
    from app.models.site import Site
    did = _add(Domain(domain="bought.ru", source="backorder", status="purchased"))
    sid = provisioning.create_site_for(did)
    with db.SessionLocal() as s:
        assert s.get(Site, sid) is not None
    assert provisioning.create_site_for(did) == sid      # идемпотентно


# --- fix 5: ensure_a_record reconciles a stale A record ------------------------

def test_ensure_a_record_reconciles(monkeypatch):
    from app.integrations.cloudflare import CloudflareClient
    cf = CloudflareClient()
    try:
        calls = {}

        def _list(zone_id, type=None, name=None):
            return [{"id": "rec1", "content": "1.1.1.1", "proxied": True}]

        def _update(zone_id, record_id, name, ip, proxied=True):
            calls["update"] = (record_id, ip, proxied)
            return {"id": record_id, "content": ip, "proxied": proxied}

        def _add(zone_id, name, ip, proxied=True):
            calls["add"] = True
            return {"id": "new"}

        monkeypatch.setattr(cf, "list_dns", _list)
        monkeypatch.setattr(cf, "update_a_record", _update)
        monkeypatch.setattr(cf, "add_a_record", _add)

        # content changed -> PATCH, not a duplicate POST
        cf.ensure_a_record("z", "ex.com", "2.2.2.2", proxied=True)
        assert calls == {"update": ("rec1", "2.2.2.2", True)}

        # identical record -> return existing untouched
        calls.clear()
        out = cf.ensure_a_record("z", "ex.com", "1.1.1.1", proxied=True)
        assert out["id"] == "rec1" and calls == {}

        # proxied flag changed -> PATCH
        calls.clear()
        cf.ensure_a_record("z", "ex.com", "1.1.1.1", proxied=False)
        assert calls == {"update": ("rec1", "1.1.1.1", False)}
    finally:
        cf._client.close()
