"""G4: discovery без ключа Ahrefs (S1-01), все источники упали = failed (S1-07), счётчик «вне белого
списка» (S1-08), IDNA 2008 (S1-09), фильтры имени (S1-10), условный GET (S1-11)."""
from datetime import datetime, timezone

import httpx
import pytest

from app.integrations import nominet, registry_mx
from app.integrations.base import NotModified, conditional_get
from app.services import discovery, jobs
from app.services.domain_filters import canonical_domain, name_reject, zone_of
from app.services.settings import get_settings, get_source_state, update_settings
from tests.test_discovery_v2 import _ahrefs, _all, _row, _seen, _sources

DL = datetime(2026, 10, 3, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(discovery, "_sleep", lambda s: None)


@pytest.fixture(autouse=True)
def reserve_on():
    update_settings(max_candidates_per_run=2000, sources_enabled={"nominet": True})


def _noresponse(monkeypatch):
    """Ahrefs, который падает: DR получить нечем."""
    _ahrefs(monkeypatch, {}, fail=True)


# ---- S1-01: резерв без DR ----

def test_empty_key_keeps_candidates_with_null_dr(monkeypatch):
    """Настоящий клиент без ключа (autouse _no_paid_keys): раньше 0 вставок, теперь резерв dr=NULL."""
    _sources(monkeypatch, nominet=[_row("alpha.co.uk"), _row("beta.co.uk")])
    assert discovery.run_discovery() == 2
    rows = _all()
    assert set(rows) == {"alpha.co.uk", "beta.co.uk"} and all(d.dr is None for d in rows.values())
    assert rows["alpha.co.uk"].status == "discovered" and rows["alpha.co.uk"].acquire_deadline is not None
    msg = jobs.last("discovery")["message"]
    assert "без DR (резерв): 2 из 2" in msg and "DR недоступен" in msg
    assert _seen() == {}                                      # в память DR не пишем: не спрашивали


def test_reserve_cap_keeps_best_by_cheap_signals(monkeypatch):
    update_settings(max_candidates_per_run=2)
    names = ["abc12def.co.uk", "plain.co.uk", "my-name.co.uk", "short.co.uk"]
    _sources(monkeypatch, nominet=[_row(n) for n in names])
    assert discovery.run_discovery() == 2
    assert set(_all()) == {"plain.co.uk", "short.co.uk"}      # без цифр/дефисов, короче — раньше
    assert "кап 2 — отсечено 2" in jobs.last("discovery")["message"]


def test_cap_zero_disables_reserve(monkeypatch):
    update_settings(max_candidates_per_run=0)
    _sources(monkeypatch, nominet=[_row("alpha.co.uk")])
    assert discovery.run_discovery() == 0 and _all() == {}


def test_failed_batch_goes_to_reserve_but_answered_low_dr_is_dropped(monkeypatch):
    _sources(monkeypatch, nominet=[_row("lowdr.co.uk"), _row("unknown.co.uk")])
    _ahrefs(monkeypatch, {"lowdr.co.uk": 1.0, "unknown.co.uk": 20.0}, missing={"unknown.co.uk"})
    # lowdr: ответ есть, DR 1 < 5 -> не наш; unknown: Ahrefs не вернул — «не знаем», но он ЗАПОМНЕН
    # как спрошенный (None) и в резерв не идёт; резерв — только для домена, до которого не дошли
    assert discovery.run_discovery() == 0
    _noresponse(monkeypatch)
    _sources(monkeypatch, nominet=[_row("third.co.uk")])
    assert discovery.run_discovery() == 1 and set(_all()) == {"third.co.uk"}


def test_with_key_dr_pass_is_unchanged(monkeypatch):
    _sources(monkeypatch, nominet=[_row("good.co.uk"), _row("bad.co.uk")])
    _ahrefs(monkeypatch, {"good.co.uk": 12.0, "bad.co.uk": 1.0})
    assert discovery.run_discovery() == 1
    assert float(_all()["good.co.uk"].dr) == 12.0


# ---- S1-10: фильтры имени ----

def test_name_filters_cut_junk_before_dr_and_reserve(monkeypatch):
    names = ["a" * 31 + ".co.uk", "1234567890.co.uk", "a-b-c-d.co.uk", "bestcasino.co.uk", "fine.co.uk"]
    _sources(monkeypatch, nominet=[_row(n) for n in names])
    assert discovery.run_discovery() == 1 and set(_all()) == {"fine.co.uk"}
    msg = jobs.last("discovery")["message"]
    assert "отсечено по имени" in msg and "длина 1" in msg and "цифры 1" in msg and "мусорное слово 1" in msg


def test_name_filters_are_configurable_and_do_not_touch_emd(monkeypatch):
    update_settings(name_filters={"max_hyphens": -1, "junk": []})
    assert get_settings()["name_filters"]["junk"] == []
    _sources(monkeypatch, nominet=[_row("a-b-c-d.co.uk"), _row("bestcasino.co.uk")])
    assert discovery.run_discovery() == 2


def test_name_reject_rules():
    assert name_reject("fine.com") is None
    assert name_reject("ab.com") is None and name_reject("a1.com") is None      # короткие не по цифрам
    assert name_reject("xn--bcher-kva.de") is None                               # punycode не судим
    assert name_reject("porn-site.com") == "junk"
    assert name_reject("y" * 31 + ".com") == "length"
    assert name_reject("y" * 31 + ".com", {"max_label_len": 0}) is None


def test_settings_validate_discovery_opts():
    update_settings(max_candidates_per_run=10**9, name_filters={"max_label_len": "x", "max_digit_share": 7})
    st = get_settings()
    assert st["max_candidates_per_run"] == 50_000
    assert st["name_filters"]["max_label_len"] == 30 and st["name_filters"]["max_digit_share"] == 1.0


# ---- S1-07 ----

def test_all_sources_down_is_failed_not_done(monkeypatch):
    update_settings(sources_enabled={"nominet": True, "mx": True})

    class Boom:
        def list_dropping(self):
            raise httpx.ConnectTimeout("t")
    monkeypatch.setattr(discovery, "_clients", lambda: {"nominet": Boom, "mx": Boom})
    with pytest.raises(RuntimeError, match="все источники упали"):
        discovery.run_discovery()
    last = jobs.last("discovery")
    assert last["status"] == "failed" and "ConnectTimeout" in last["error"]


def test_one_down_one_empty_still_failed_but_one_down_one_ok_is_done(monkeypatch):
    update_settings(sources_enabled={"nominet": True, "mx": True})

    class Boom:
        def list_dropping(self):
            raise RuntimeError("x")

    class Empty:
        def list_dropping(self):
            return []

    class Ok:
        def list_dropping(self):
            return [_row("alive.co.uk", src="mx", lane="free")]
    monkeypatch.setattr(discovery, "_clients", lambda: {"nominet": Boom, "mx": Empty})
    with pytest.raises(RuntimeError):
        discovery.run_discovery()
    monkeypatch.setattr(discovery, "_clients", lambda: {"nominet": Boom, "mx": Ok})
    assert discovery.run_discovery() == 1
    assert jobs.last("discovery")["status"] == "done"


# ---- S1-08 ----

def test_zone_cut_counter_in_message(monkeypatch):
    rows = ([_row(f"a{i}x.uk") for i in range(3)] + [_row("b.org.uk")] + [_row("c.co.uk")])
    _sources(monkeypatch, nominet=rows)
    discovery.run_discovery()
    msg = jobs.last("discovery")["message"]
    assert "вне белого списка зон: uk 3, org.uk 1" in msg
    assert zone_of("x.com.mx") == "com.mx" and zone_of("x.uk") == "uk"


# ---- S1-09 ----

def test_canonical_domain_is_idna_2008():
    assert canonical_domain("straße.com") == "xn--strae-oqa.com"      # не strasse.com
    assert canonical_domain("faß.de") == "xn--fa-hia.de"
    assert canonical_domain("Bücher.DE") == "xn--bcher-kva.de"
    assert canonical_domain("ΣΊΣΥΦΟΣ.com") == "xn--kxa6akbbkh.com"
    assert canonical_domain("a_b.com") is None and canonical_domain("-a.com") is None
    assert canonical_domain("x.com ") == "x.com" and canonical_domain("a b.com") is None


# ---- S1-11: условный GET ----

class _Resp:
    def __init__(self, code=200, headers=None, content=b""):
        self.status_code, self.headers, self.content, self.text = code, headers or {}, content, ""


def test_conditional_get_sends_validators_and_raises_on_304():
    class C:
        validators = {"etag": '"abc"', "last_modified": "Wed, 01 Oct 2026 03:01:00 GMT"}

        def request(self, method, url, headers=None, **kw):
            self.sent = headers
            return _Resp(304)
    c = C()
    with pytest.raises(NotModified):
        conditional_get(c, "http://x")
    assert c.sent == {"If-None-Match": '"abc"', "If-Modified-Since": "Wed, 01 Oct 2026 03:01:00 GMT"}


def test_conditional_get_stores_new_validators():
    class C:
        def request(self, method, url, headers=None, **kw):
            return _Resp(200, {"ETag": '"n"', "Last-Modified": "L"})
    c = C()
    conditional_get(c, "http://x")
    assert c.validators == {"etag": '"n"', "last_modified": "L"}


def test_nominet_and_mx_clients_use_conditional_get(monkeypatch):
    import gzip
    csv = "roid,domain,drop_time\n1,x.co.uk,2100-01-01T00:00:00Z\n"
    seen = []

    def req(self, method, url, headers=None, **kw):
        seen.append(headers)
        return _Resp(304) if headers else _Resp(200, {"ETag": '"v1"'}, gzip.compress(csv.encode()))
    monkeypatch.setattr(nominet.NominetClient, "request", req)
    c = nominet.NominetClient(lookahead_days=10 ** 5)
    assert c.list_dropping()[0]["domain"] == "x.co.uk" and c.validators["etag"] == '"v1"'
    with pytest.raises(NotModified):
        c.list_dropping()
    monkeypatch.setattr(registry_mx.RegistryMxClient, "request",
                        lambda self, m, u, headers=None, **kw: _Resp(304))
    m = registry_mx.RegistryMxClient()
    m.validators = {"last_modified": "L"}
    with pytest.raises(NotModified):
        m.list_dropping()


def test_unchanged_source_is_skipped_and_state_saved_only_after_success(monkeypatch):
    update_settings(sources_enabled={"nominet": True, "mx": True})
    calls = []

    class Nom:
        validators = None

        def list_dropping(self):
            calls.append(("nominet", self.validators))
            if self.validators:
                raise NotModified("x")
            self.validators = {"etag": '"v1"'}
            return [_row("fresh.co.uk")]

    class Mx:
        validators = None

        def list_dropping(self):
            return []
    monkeypatch.setattr(discovery, "_clients", lambda: {"nominet": Nom, "mx": Mx})
    _ahrefs(monkeypatch, {"fresh.co.uk": 20.0})
    assert discovery.run_discovery() == 1
    assert get_source_state()["nominet"] == {"etag": '"v1"'}
    assert discovery.run_discovery() == 0                          # 304: не качали и не парсили
    assert calls[1] == ("nominet", {"etag": '"v1"'})
    assert "Nominet (.uk): не менялся" in jobs.last("discovery")["message"]


def test_state_not_saved_when_a_source_failed(monkeypatch):
    update_settings(sources_enabled={"nominet": True, "mx": True})

    class Nom:
        validators = None

        def list_dropping(self):
            self.validators = {"etag": '"v2"'}
            return [_row("fresh2.co.uk")]

    class Mx:
        def list_dropping(self):
            raise RuntimeError("mx down")
    monkeypatch.setattr(discovery, "_clients", lambda: {"nominet": Nom, "mx": Mx})
    _ahrefs(monkeypatch, {"fresh2.co.uk": 20.0})
    discovery.run_discovery()
    assert get_source_state() == {}                                # завтра качаем снова
