"""Швы полного цикла (обзор 2026-10-10): куплен -> карточка сайта -> NS у регистратора -> оффер.

Раньше между M2 и M3 было четыре ручных шага: «✓ пойман», «создать сайт», «пропиши NS у регистратора»,
«привяжи оффер». Денежный гейт и гейт редактуры не тронуты — проверяем, что их по-прежнему держит код.
"""
from datetime import datetime, timedelta, timezone

import app.db as db
from app.config import settings
from app.integrations import registrar
from app.models.domain import AcquisitionOrder, Domain
from app.models.offer import Offer
from app.models.site import Site
from app.services import acquisition, provisioning
from app.services.settings import update_settings


def _approved(name="intl.com", lang=None) -> int:
    with db.SessionLocal() as s:
        d = Domain(domain=name, source="dropcatch", status="approved", lane="free", market_lang=lang)
        s.add(d)
        s.commit()
        return d.id


def _offer(brand, lang=None, active=True) -> int:
    with db.SessionLocal() as s:
        o = Offer(brand=brand, affiliate_link=f"https://aff.example/{brand}", language=lang, active=active)
        s.add(o)
        s.commit()
        return o.id


class _Reg:
    name, configured = "fake", True

    def __init__(self, ns_ok=True, boom=None):
        self.registered, self.ns_calls, self.ns_ok, self.boom = [], [], ns_ok, boom

    def check_available(self, domain): return True
    def price(self, domain, auction=False): return registrar.Money(10.0, "USD")
    def renew_price(self, domain): return registrar.Money(11.0, "USD")
    def balance(self): return registrar.Money(100.0, "USD")

    def register(self, domain, period=1):
        self.registered.append(domain)
        return {"order_id": "R1", "order_amount": 10.0, "currency": "USD"}

    def bid(self, domain, ceiling): return {"order_id": "A1"}

    def set_nameservers(self, domain, ns):
        if self.boom:
            raise self.boom
        self.ns_calls.append((domain, list(ns)))
        return {"ok": True, "verified": self.ns_ok, "nameservers": sorted(ns) if self.ns_ok else []}


def _buy(monkeypatch, fake, name="intl.com", lang=None):
    monkeypatch.setattr(registrar, "get_registrar", lambda: fake)
    update_settings(zone_channels={"com": "registrar"})
    did = _approved(name, lang)
    oid = acquisition.create_order(did)
    acquisition.confirm_order(oid)
    return did, oid


# --- M2 -> M3: регистрация = покупка, карточка сайта сразу -----------------------------------

def test_registrar_success_is_purchase_and_creates_site(monkeypatch):
    fake = _Reg()
    did, oid = _buy(monkeypatch, fake)
    out = acquisition.execute_confirmed_order(oid)
    assert out["status"] == "caught" and fake.registered == ["intl.com"]
    with db.SessionLocal() as s:
        assert s.get(Domain, did).status == "purchased"
        site = s.query(Site).filter_by(domain_id=did).one()
        assert site.status == "provisioning" and site.doc_root.endswith("intl.com")


def test_unconfirmed_order_still_buys_nothing(monkeypatch):
    fake = _Reg()
    did, oid = _buy(monkeypatch, fake)
    with db.SessionLocal() as s:
        s.get(AcquisitionOrder, oid).confirmed_by_human = False
        s.commit()
    assert "gate" in acquisition.execute_confirmed_order(oid)["error"]
    assert fake.registered == []
    with db.SessionLocal() as s:
        assert s.get(Domain, did).status == "purchasing" and s.query(Site).count() == 0


def test_mark_caught_creates_site_for_async_channels():
    with db.SessionLocal() as s:
        d = Domain(domain="drop.co.uk", source="nominet", status="purchasing", lane="bid")
        s.add(d)
        s.commit()
        o = AcquisitionOrder(domain_id=d.id, provider="registrar", status="ordered", confirmed_by_human=True)
        s.add(o)
        s.commit()
        did, oid = d.id, o.id
    r = acquisition.mark_caught(oid)
    assert r["status"] == "caught" and r["site_id"]
    with db.SessionLocal() as s:
        assert s.get(Domain, did).status == "purchased"
        assert s.get(Site, r["site_id"]).domain_id == did


def test_site_creation_failure_does_not_undo_purchase(monkeypatch):
    fake = _Reg()
    did, oid = _buy(monkeypatch, fake)
    monkeypatch.setattr(provisioning, "create_site_for", lambda d: (_ for _ in ()).throw(RuntimeError("db")))
    assert acquisition.execute_confirmed_order(oid)["status"] == "caught"
    with db.SessionLocal() as s:
        assert s.get(Domain, did).status == "purchased" and s.query(Site).count() == 0


# --- оффер по умолчанию -------------------------------------------------------------------

def test_default_offer_prefers_market_language_then_single_active():
    with db.SessionLocal() as s:
        d_es = Domain(domain="a.mx", source="mx", status="purchased", market_lang="es")
        d_xx = Domain(domain="b.com", source="list", status="purchased", market_lang="pl")
        s.add_all([d_es, d_xx])
        s.commit()
        d_unknown = Domain(domain="c.com", source="list", status="purchased", market_lang=None)
        s.add(d_unknown)
        s.commit()
        assert provisioning.default_offer_id(s, d_es) is None            # офферов нет
        en = _offer("A", "en")
        assert provisioning.default_offer_id(s, d_unknown) == en         # язык рынка неизвестен -> единственный активный
        assert provisioning.default_offer_id(s, d_es) is None            # язык известен, совпадения нет — чужое гео не навязываем
        es = _offer("B", "ES")
        assert provisioning.default_offer_id(s, d_es) == es              # язык рынка, регистр не важен
        assert provisioning.default_offer_id(s, d_unknown) is None       # два активных без языка — оператор
        assert provisioning.default_offer_id(s, d_xx) is None            # pl: совпадения нет
        _offer("C", "pl", active=False)
        assert provisioning.default_offer_id(s, d_xx) is None            # выключенный не считается


def test_create_site_for_assigns_default_offer_once():
    es = _offer("B", "es")
    with db.SessionLocal() as s:
        d = Domain(domain="a.mx", source="mx", status="purchased", market_lang="es")
        s.add(d)
        s.commit()
        did = d.id
    sid = provisioning.create_site_for(did)
    with db.SessionLocal() as s:
        assert s.get(Site, sid).offer_id == es
        s.get(Site, sid).offer_id = None            # оператор снял — повторный вызов не навязывает
        s.commit()
    assert provisioning.create_site_for(did) == sid
    with db.SessionLocal() as s:
        assert s.get(Site, sid).offer_id is None


# --- NS у регистратора из провижна -----------------------------------------------------------

class _CFPending:
    account_id = "acc"

    def __init__(self):
        self.checks = 0

    def ensure_zone(self, domain):
        return {"id": "z1", "status": "pending", "name_servers": ["a.ns.cf", "b.ns.cf"]}

    def get_zone(self, zid):
        return self.ensure_zone("")

    def activation_check(self, zid):
        self.checks += 1
        return True


def _pending_site(monkeypatch) -> int:
    monkeypatch.setattr(settings, "VPS_ORIGIN_IP", "203.0.113.9")
    monkeypatch.setattr("app.integrations.cloudflare.CloudflareClient", lambda: _CFPending())
    with db.SessionLocal() as s:
        d = Domain(domain="ex.com", source="dropcatch", status="purchased")
        s.add(d)
        s.commit()
        site = Site(domain_id=d.id, status="provisioning", doc_root="/www/wwwroot/ex.com")
        s.add(site)
        s.commit()
        return site.id


def test_await_ns_pushes_cf_nameservers_to_registrar(monkeypatch):
    fake = _Reg()
    monkeypatch.setattr(registrar, "get_registrar", lambda: fake)
    sid = _pending_site(monkeypatch)
    out = provisioning.provision(sid)
    assert out["status"] == "awaiting_ns"
    assert fake.ns_calls == [("ex.com", ["a.ns.cf", "b.ns.cf"])]
    assert "записаны у регистратора" in out["hint"]
    provisioning.provision(sid)                               # тот же час — повторно не дёргаем
    assert len(fake.ns_calls) == 1
    with db.SessionLocal() as s:
        s.get(Site, sid).ns_checked_at = datetime.now(timezone.utc) - timedelta(hours=2)
        s.commit()
    provisioning.provision(sid)
    assert len(fake.ns_calls) == 2                            # идемпотентный повтор раз в час


def test_await_ns_without_registrar_keeps_manual_hint(monkeypatch):
    monkeypatch.setattr(registrar, "get_registrar", lambda: registrar.NotConfiguredRegistrar())
    sid = _pending_site(monkeypatch)
    out = provisioning.provision(sid)
    assert out["status"] == "awaiting_ns" and out["hint"].startswith("пропиши у регистратора")


def test_await_ns_registrar_failure_is_hint_not_crash(monkeypatch):
    fake = _Reg(boom=RuntimeError("домен не в этом аккаунте"))
    monkeypatch.setattr(registrar, "get_registrar", lambda: fake)
    sid = _pending_site(monkeypatch)
    out = provisioning.provision(sid)
    assert out["status"] == "awaiting_ns" and "не записались" in out["hint"] and "a.ns.cf" in out["hint"]


# --- /queue/{id}/buy: один клик = гейт + отправка ---------------------------------------------

def test_buy_route_confirms_and_executes_in_one_click(client, monkeypatch):
    fake = _Reg()
    monkeypatch.setattr(registrar, "get_registrar", lambda: fake)
    update_settings(zone_channels={"com": "registrar"})
    did = _approved("one.com")
    oid = acquisition.create_order(did)
    r = client.post(f"/queue/{oid}/buy", data={"max_price": "12"}, follow_redirects=False)
    assert r.status_code == 303 and fake.registered == ["one.com"]
    with db.SessionLocal() as s:
        o = s.get(AcquisitionOrder, oid)
        assert o.status == "caught" and o.confirmed_by_human is True
        assert s.get(Domain, did).status == "purchased"


def test_buy_route_above_ceiling_confirms_but_does_not_send(client, monkeypatch):
    fake = _Reg()                                                     # котировка 10 USD
    monkeypatch.setattr(registrar, "get_registrar", lambda: fake)
    update_settings(zone_channels={"com": "registrar"})
    did = _approved("three.com")
    oid = acquisition.create_order(did)
    r = client.post(f"/queue/{oid}/buy", data={"max_price": "5"}, follow_redirects=False)
    assert r.status_code == 303 and "выше потолка" in r.headers["location"] or fake.registered == []
    assert fake.registered == []
    with db.SessionLocal() as s:
        o = s.get(AcquisitionOrder, oid)
        assert o.status == "pending_confirm" and o.confirmed_by_human is True and float(o.cost) == 10.0
    r = client.post(f"/queue/{oid}/buy", follow_redirects=False)        # без потолка — отказ формы, ничего не ушло
    assert r.status_code in (303, 422) and fake.registered == []


def test_buy_route_refusal_at_confirm_sends_nothing(client, monkeypatch):
    fake = _Reg()
    monkeypatch.setattr(registrar, "get_registrar", lambda: fake)
    update_settings(zone_channels={"com": "registrar"})
    did = _approved("two.com")
    oid = acquisition.create_order(did)
    monkeypatch.setattr(acquisition, "confirm_order", lambda *a, **k: (_ for _ in ()).throw(ValueError("нет")))
    r = client.post(f"/queue/{oid}/buy", data={"max_price": "50"}, follow_redirects=False)
    assert r.status_code == 303 and fake.registered == []
    with db.SessionLocal() as s:
        assert s.get(AcquisitionOrder, oid).status == "pending_confirm"
