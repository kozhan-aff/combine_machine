"""cf_sync: S4-01 (пустой /accounts), S4-05 (пул, лимитер, 403-гейтинг), S4-07 (429), S4-08 (сбои
соединения), S4-10 (статус токена). Клиент CF — фейк на уровне методов; HTTP-слой покрыт
test_cf_transport_stack."""
import threading
import time

import httpx
import pytest

import app.services.cf_sync as cf_sync
from app.db import SessionLocal
from app.integrations.cloudflare import CloudflareClient, CloudflareError
from app.models.cloudflare import CloudflareConnection, CloudflareZoneMirror


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(cf_sync, "_RPS", 0)              # без лимитера: тесты не должны спать
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "tok")


def _zones(n):
    return [{"id": f"z{i}", "name": f"d{i}.com", "status": "active", "account": {"id": "accHEX"}}
            for i in range(n)]


class _CF:
    """Фейк с настраиваемыми отказами; считает вызовы и пиковую параллельность."""
    cur = None

    def __init__(self, zones=(), accounts=((("accHEX", "A")),), universal=None, packs=None,
                 settings_exc=None, verify=None, delay=0.0):
        self.zones = list(zones)
        self.accounts = [{"id": a, "name": n} for a, n in accounts]
        self.universal, self.packs, self.settings_exc = universal, packs, settings_exc
        self.verify, self.delay = verify or {"status": "active"}, delay
        self.calls = {}
        self.active = self.peak = 0
        self.lock = threading.Lock()

    @classmethod
    def with_token(cls, *a, **k):
        return cls.cur

    def _hit(self, name):
        with self.lock:
            self.calls[name] = self.calls.get(name, 0) + 1
            self.active += 1
            self.peak = max(self.peak, self.active)
        if self.delay:
            time.sleep(self.delay)
        with self.lock:
            self.active -= 1

    def verify_token(self, kind, account_id=""):
        return self.verify
    def list_accounts_paginated(self): return self.accounts
    def list_zones_paginated(self, account_id): return self.zones
    def list_dns_paginated(self, zid, type=None, name=None):
        self._hit("dns"); return []
    def get_zone_setting(self, zid, sid):
        self._hit("setting")
        if self.settings_exc:
            raise self.settings_exc
        return {"value": "off", "editable": True}
    def get_universal_ssl(self, zid):
        self._hit("universal")
        if self.universal:
            raise self.universal
        return {"enabled": True}
    def list_universal_certificate_packs(self, zid):
        self._hit("packs")
        if self.packs:
            raise self.packs
        return []
    def get_dnssec(self, zid):
        self._hit("dnssec"); return {"status": "disabled"}


def _http_error(code, headers=None):
    req = httpx.Request("GET", "https://api.cloudflare.com/client/v4/zones/z/x")
    return CloudflareError(httpx.Response(code, headers=headers or {}, request=req,
                                          json={"success": False, "errors": [{"code": 9109, "message": "denied"}]}))


def _run(monkeypatch, fake, *, kind="user", owner=None, secret_ref="env:CLOUDFLARE_API_TOKEN"):
    _CF.cur = fake
    monkeypatch.setattr(cf_sync, "CloudflareClient", _CF)
    with SessionLocal() as db:
        c = CloudflareConnection(label="t", secret_ref=secret_ref, token_kind=kind,
                                 owner_cf_account_id=owner, status="unverified")
        db.add(c); db.commit()
        cf_sync.sync_connection(db, c)
        db.refresh(c)
        zones = {m.cf_zone_id: m for m in db.query(CloudflareZoneMirror).all()}
        return c.status, c.last_error_safe, zones


def test_empty_accounts_falls_back_to_owner_account_and_warns(monkeypatch):
    """S4-01: боевой user-токен без Account:Read -> GET /accounts = [] -> раньше 0 зон и зелёный ok."""
    st, err, zones = _run(monkeypatch, _CF(zones=_zones(2), accounts=()), owner="accHEX")
    assert st == "warn" and "0 аккаунтов" in err          # не ok: деградация видна
    assert set(zones) == {"z0", "z1"}                     # но зоны прочитаны по account_id


def test_empty_accounts_without_any_account_id_warns_and_reads_nothing(monkeypatch):
    st, err, zones = _run(monkeypatch, _CF(zones=_zones(2), accounts=()))
    assert st == "warn" and "0 аккаунтов" in err and "account_id не задан" in err
    assert zones == {}


def test_legacy_connection_falls_back_to_settings_account_id(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "CLOUDFLARE_ACCOUNT_ID", "accHEX", raising=False)
    st, err, zones = _run(monkeypatch, _CF(zones=_zones(1), accounts=()))
    assert st == "warn" and set(zones) == {"z0"}


def test_expired_token_marks_connection_error_via_real_client(monkeypatch):
    """S4-10 сквозь настоящий CloudflareClient: verify -> 200 status=expired."""
    def h(req):
        return httpx.Response(200, json={"success": True, "result": {"status": "expired"}})

    class _Real(CloudflareClient):
        @classmethod
        def with_token(cls, token, account_id=""):
            c = super().with_token(token, account_id)
            c._client = httpx.Client(transport=httpx.MockTransport(h))
            return c
    monkeypatch.setattr(cf_sync, "CloudflareClient", _Real)
    with SessionLocal() as db:
        c = CloudflareConnection(label="t", secret_ref="env:CLOUDFLARE_API_TOKEN",
                                 token_kind="user", status="unverified")
        db.add(c); db.commit()
        cf_sync.sync_connection(db, c)
        assert c.status == "error" and "expired" in c.last_error_safe


def test_403_on_universal_ssl_and_packs_is_not_repeated_per_zone(monkeypatch):
    """S4-05: 40 из 222 запросов были заведомо 403 (токен без SSL-прав) — теперь после первого
    отказа способность пропускается. Один поток: счёт детерминирован."""
    monkeypatch.setattr(cf_sync, "_WORKERS", 1)
    fake = _CF(zones=_zones(6), universal=_http_error(403), packs=_http_error(403))
    st, _, zones = _run(monkeypatch, fake)
    assert fake.calls["universal"] == 1 and fake.calls["packs"] == 1     # не 6 и не 6
    assert fake.calls["setting"] == 6 * len(cf_sync._OBSERVED_SETTINGS)   # остальное читается
    assert "403" in zones["z0"].cert_error_safe or "Cloudflare 403" in zones["z0"].cert_error_safe
    assert "пропущено" in zones["z5"].cert_error_safe


def test_zone_details_are_fetched_concurrently(monkeypatch):
    fake = _CF(zones=_zones(8), delay=0.01)
    _run(monkeypatch, fake)
    assert fake.peak > 1, "чтение зон должно идти пулом, а не строго последовательно"


def test_429_aborts_connection_and_does_not_mark_unvisited_zones_missing(monkeypatch):
    """S4-07: лимит исчерпан -> прогон прерван, connection красная с причиной; зоны, до которых
    не дошли, НЕ получают missing_since (omission ≠ deleted)."""
    monkeypatch.setattr(cf_sync, "_WORKERS", 1)
    fake = _CF(zones=_zones(3))
    _CF.cur = fake
    monkeypatch.setattr(cf_sync, "CloudflareClient", _CF)
    with SessionLocal() as db:
        c = CloudflareConnection(label="t", secret_ref="env:CLOUDFLARE_API_TOKEN",
                                 token_kind="user", status="unverified")
        db.add(c); db.commit()
        cf_sync.sync_connection(db, c)           # первый прогон: зоны попадают в зеркало
        fake.settings_exc = _http_error(429, {"Retry-After": "300"})
        cf_sync.sync_connection(db, c)
        db.refresh(c)
        assert c.status == "error" and "429" in c.last_error_safe and "300" in c.last_error_safe
        assert all(m.missing_since is None for m in db.query(CloudflareZoneMirror).all())


def test_consecutive_transport_failures_abort_connection(monkeypatch):
    """S4-08: N подряд сбоев соединения -> стоп (раньше 220+ запросов x 90 с)."""
    monkeypatch.setattr(cf_sync, "_WORKERS", 1)
    fake = _CF(zones=_zones(10), settings_exc=httpx.ConnectTimeout("boom"))
    st, err, _ = _run(monkeypatch, fake)
    assert st == "error" and "недоступен" in err
    assert fake.calls["setting"] == cf_sync._TRANSPORT_STOP             # дальше не стучались


def test_limiter_blocks_when_burst_exhausted():
    lim = cf_sync._Limiter(rps=50, burst=2)
    t0 = time.monotonic()
    for _ in range(4):
        lim.acquire()
    assert time.monotonic() - t0 >= 0.03        # 2 лишних токена при 50 rps ≈ 40 мс
