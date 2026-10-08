"""Discovery v2: зоны -> известные -> DR на входе (с памятью dr_seen) -> вставка. Сеть подменена."""
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote

import httpx
import pytest

import app.db as db
from app.models.domain import Domain, DrSeen
from app.services import discovery, jobs
from app.services.settings import update_settings

DL = datetime(2026, 10, 3, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def slept(monkeypatch):
    """Пауза между пачками DR и ожидание после 429 — без настоящего сна; что «проспали» — видно."""
    out = []
    monkeypatch.setattr(discovery, "_sleep", out.append)
    return out


@pytest.fixture(autouse=True)
def classic_dr_gate():
    """Тесты этого файла описывают «классический» вход: DR — условие появления кандидата. Резерв
    без DR (S1-01, решение оператора: ключа Ahrefs не будет) выключен кап-0; сам резерв —
    в tests/test_discovery_g4.py."""
    update_settings(max_candidates_per_run=0)


class _Src:
    rows: list = []

    def list_dropping(self):
        return list(self.rows)


def _sources(monkeypatch, **rows):
    classes = {}
    for name in ("dropcatch", "nominet", "mx"):
        classes[name] = type(name, (_Src,), {"rows": rows.get(name, [])})
    monkeypatch.setattr(discovery, "_clients", lambda: classes)


class _Ahrefs:
    calls: list = []
    drs: dict = {}
    fail = False
    missing: set = set()          # домены, которых «нет в ответе»
    errors: list = []             # исключения первых вызовов, по очереди

    def dr_free(self, domains):
        _Ahrefs.calls.append(list(domains))
        if _Ahrefs.errors:
            raise _Ahrefs.errors.pop(0)
        if _Ahrefs.fail:
            raise RuntimeError("ahrefs down")
        return {d: _Ahrefs.drs.get(d, 0.0) for d in domains if d not in _Ahrefs.missing}


def _ahrefs(monkeypatch, drs, fail=False, missing=(), errors=()):
    import app.integrations.ahrefs as ah
    _Ahrefs.calls, _Ahrefs.drs, _Ahrefs.fail = [], drs, fail
    _Ahrefs.missing, _Ahrefs.errors = set(missing), list(errors)
    monkeypatch.setattr(ah, "AhrefsClient", _Ahrefs)


def _http_error(code, headers=None):
    req = httpx.Request("POST", "https://api.ahrefs.com/v3/public/domain-rating-free")
    return httpx.HTTPStatusError(str(code), request=req,
                                 response=httpx.Response(code, headers=headers or {}, request=req))


def _row(d, src="nominet", lane="bid", dl=DL):
    return {"domain": d, "source": src, "lane": lane, "acquire_deadline": dl}


def _all():
    with db.SessionLocal() as s:
        return {d.domain: d for d in s.query(Domain)}


def _seen():
    with db.SessionLocal() as s:
        return {r.domain: (None if r.dr is None else float(r.dr)) for r in s.query(DrSeen)}


def test_zone_filter_dr_filter_and_save(monkeypatch):
    update_settings(sources_enabled={"nominet": True, "mx": True})
    _sources(monkeypatch,
             nominet=[_row("Good.co.uk"), _row("junk.co.uk"), _row("x.org.uk")],
             mx=[_row("a.mx", "mx", "free", None), _row("b.com.mx", "mx", "free", None)])
    _ahrefs(monkeypatch, {"good.co.uk": 12.0, "a.mx": 6.0})
    assert discovery.run_discovery() == 2
    got = _all()
    assert set(got) == {"good.co.uk", "a.mx"}                  # junk: DR 0; x.org.uk, b.com.mx: не наши зоны
    assert float(got["good.co.uk"].dr) == 12.0 and got["good.co.uk"].lane == "bid"
    assert got["good.co.uk"].acquire_deadline.replace(tzinfo=timezone.utc) == DL
    assert got["a.mx"].source == "mx" and got["a.mx"].lane == "free"
    assert sorted(_Ahrefs.calls[0]) == ["a.mx", "good.co.uk", "junk.co.uk"]   # DR только для своих зон


def test_rerun_is_idempotent_and_skips_dr_for_known_and_remembered(monkeypatch):
    # Р4: домен ниже порога в `domains` не попадает — без памяти dr_seen его DR спрашивался бы на
    # каждом прогоне, а лицензия DR-free запрещает систематический сбор
    update_settings(sources_enabled={"nominet": True})
    _sources(monkeypatch, nominet=[_row("good.co.uk"), _row("junk.co.uk")])
    _ahrefs(monkeypatch, {"good.co.uk": 12.0})                   # junk.co.uk -> DR 0.0
    assert discovery.run_discovery() == 1
    assert _seen() == {"good.co.uk": 12.0, "junk.co.uk": 0.0}    # запомнены ВСЕ спрошенные
    with db.SessionLocal() as s:
        s.query(Domain).filter_by(domain="good.co.uk").update({"status": "scored"})
        s.commit()
    _Ahrefs.calls = []
    assert discovery.run_discovery() == 0
    assert _Ahrefs.calls == []                                   # известный и запомненный — DR не тратим
    assert _all()["good.co.uk"].status == "scored"               # решённый статус не откатился
    assert set(_all()) == {"good.co.uk"}
    assert "DR из памяти (4 сут) — 1" in jobs.last("discovery")["message"]


def test_dr_memory_older_than_4_days_is_purged_and_asked_again(monkeypatch):
    old = datetime.now(timezone.utc) - timedelta(days=5)
    with db.SessionLocal() as s:
        s.add_all([DrSeen(domain="junk.co.uk", dr=0.0, checked_at=old),
                   DrSeen(domain="gone.co.uk", dr=1.0, checked_at=old)])
        s.commit()
    update_settings(sources_enabled={"nominet": True})
    _sources(monkeypatch, nominet=[_row("junk.co.uk")])
    _ahrefs(monkeypatch, {})
    assert discovery.run_discovery() == 0
    assert _Ahrefs.calls == [["junk.co.uk"]]                     # память протухла — спросили снова
    assert _seen() == {"junk.co.uk": 0.0}                        # gone.co.uk вычищен в начале прогона
    with db.SessionLocal() as s:
        fresh = s.get(DrSeen, "junk.co.uk").checked_at.replace(tzinfo=timezone.utc)
    assert fresh > old + timedelta(days=4)


def test_dr_unavailable_saves_nothing_and_remembers_nothing(monkeypatch):
    update_settings(sources_enabled={"nominet": True})
    _sources(monkeypatch, nominet=[_row("good.co.uk")])
    _ahrefs(monkeypatch, {}, fail=True)
    assert discovery.run_discovery() == 0 and _all() == {}
    assert "DR недоступен — 1 пропущено" in jobs.last("discovery")["message"]
    assert _seen() == {}                                         # сбой не запоминаем: следующий прогон спросит


def test_domain_missing_from_dr_answer_is_skipped_not_below_threshold(monkeypatch):
    # частичный ответ — аномалия (на несуществующий домен Ahrefs отдаёт 0.0), а не «DR ниже порога»
    update_settings(sources_enabled={"nominet": True})
    _sources(monkeypatch, nominet=[_row("good.co.uk"), _row("lost.co.uk")])
    _ahrefs(monkeypatch, {"good.co.uk": 12.0}, missing={"lost.co.uk"})
    assert discovery.run_discovery() == 1
    assert set(_all()) == {"good.co.uk"}
    assert "DR недоступен — 1 пропущено" in jobs.last("discovery")["message"]
    assert _seen() == {"good.co.uk": 12.0, "lost.co.uk": None}   # не переспрашиваем его каждый час


def test_dr_batches_pause_between_and_wait_retry_after_on_429(monkeypatch, slept):
    monkeypatch.setattr(discovery, "_DR_BATCH", 2)
    update_settings(sources_enabled={"nominet": True})
    names = [f"d{i}.co.uk" for i in range(5)]
    _sources(monkeypatch, nominet=[_row(n) for n in names])
    _ahrefs(monkeypatch, {n: 9.0 for n in names}, errors=[_http_error(429, {"Retry-After": "7"})])
    assert discovery.run_discovery() == 5
    assert _Ahrefs.calls == [names[0:2], names[0:2], names[2:4], names[4:5]]   # после 429 — один повтор
    assert slept == [7.0, 1.0, 1.0]                              # Retry-After, затем пауза ≥1 с между пачками


def test_second_429_skips_the_batch_and_does_not_remember_it(monkeypatch, slept):
    update_settings(sources_enabled={"nominet": True})
    _sources(monkeypatch, nominet=[_row("good.co.uk")])
    _ahrefs(monkeypatch, {"good.co.uk": 12.0}, errors=[_http_error(429), _http_error(429)])
    assert discovery.run_discovery() == 0
    assert len(_Ahrefs.calls) == 2 and slept == [60.0]           # без Retry-After — минута; повтор один
    assert "DR недоступен — 1 пропущено" in jobs.last("discovery")["message"]
    assert _seen() == {}


def test_dr_answer_without_any_asked_domain_is_a_failed_batch(monkeypatch):
    # находка R2-7: 200, но в ответе нет НИ ОДНОГО спрошенного домена (пустое или чужое тело) — это
    # сбой пачки, а не «DR неизвестен у всех»: запомни мы её, пачка была бы похоронена на 4 суток
    update_settings(sources_enabled={"nominet": True})
    _sources(monkeypatch, nominet=[_row("good.co.uk"), _row("lost.co.uk")])
    _ahrefs(monkeypatch, {"good.co.uk": 12.0}, missing={"good.co.uk", "lost.co.uk"})
    assert discovery.run_discovery() == 0 and _all() == {}
    assert "DR недоступен — 2 пропущено" in jobs.last("discovery")["message"]
    assert _seen() == {}                                         # следующий прогон спросит снова


@pytest.mark.parametrize("code", [401, 403])
def test_dr_auth_error_stops_asking_remaining_batches(monkeypatch, code):
    # находка R2-8: ключ не принят — остальные пачки ответят так же; не тратим на них запросы
    # (DropCatch — ~134 пачки за прогон), весь остаток — «DR недоступен»
    monkeypatch.setattr(discovery, "_DR_BATCH", 1)
    update_settings(sources_enabled={"nominet": True})
    names = ["a.co.uk", "b.co.uk", "c.co.uk"]
    _sources(monkeypatch, nominet=[_row(n) for n in names])
    _ahrefs(monkeypatch, {n: 9.0 for n in names}, errors=[_http_error(code)])
    assert discovery.run_discovery() == 0 and _all() == {}
    assert _Ahrefs.calls == [["a.co.uk"]]                        # после отказа в доступе — ни одной пачки
    assert "DR недоступен — 3 пропущено" in jobs.last("discovery")["message"]
    assert _seen() == {}


def test_empty_ahrefs_key_asks_nothing(monkeypatch):
    # находка R2-8: пустой AHREFS_API_KEY (autouse _no_paid_keys) — настоящий клиент в сеть не ходит
    # (иначе рубильник _no_live_network уронил бы тест), домены — «DR недоступен», в память не пишутся
    update_settings(sources_enabled={"nominet": True})
    _sources(monkeypatch, nominet=[_row("good.co.uk")])
    assert discovery.run_discovery() == 0 and _all() == {}
    assert "DR недоступен — 1 пропущено" in jobs.last("discovery")["message"]
    assert _seen() == {}


def test_cancel_between_dr_batches_then_remembered_dr_saves_without_asking(monkeypatch):
    import app.integrations.ahrefs as ah
    monkeypatch.setattr(discovery, "_DR_BATCH", 1)
    update_settings(sources_enabled={"nominet": True})
    _sources(monkeypatch, nominet=[_row("a.co.uk"), _row("b.co.uk")])
    _ahrefs(monkeypatch, {"a.co.uk": 9.0, "b.co.uk": 9.0})

    class _StopDuringFirst(_Ahrefs):
        def dr_free(self, domains):
            jobs.request_cancel("discovery")                     # «стоп» во время первой пачки DR
            return super().dr_free(domains)
    monkeypatch.setattr(ah, "AhrefsClient", _StopDuringFirst)
    discovery.run_discovery()
    assert jobs.progress("discovery")["status"] == "cancelled"
    assert _Ahrefs.calls == [["a.co.uk"]] and _all() == {}       # вторую пачку не спросили, записи не было
    monkeypatch.setattr(ah, "AhrefsClient", _Ahrefs)
    _Ahrefs.calls = []
    assert discovery.run_discovery() == 2
    assert _Ahrefs.calls == [["b.co.uk"]]                        # a.co.uk — из памяти (DR 9), не теряется


def test_cancel_between_save_chunks_keeps_written_chunk(monkeypatch):
    monkeypatch.setattr(discovery, "_CHUNK", 1)
    update_settings(sources_enabled={"nominet": True})
    _sources(monkeypatch, nominet=[_row("a.co.uk"), _row("b.co.uk")])
    _ahrefs(monkeypatch, {"a.co.uk": 9.0, "b.co.uk": 9.0})
    real = discovery._new_domain

    def new_domain(name, c, dr):
        jobs.request_cancel("discovery")                         # «стоп» во время записи первого чанка
        return real(name, c, dr)
    monkeypatch.setattr(discovery, "_new_domain", new_domain)
    discovery.run_discovery()
    assert jobs.progress("discovery")["status"] == "cancelled"
    assert set(_all()) == {"a.co.uk"}                            # записанное остаётся, второй чанк не начат


def test_dr_and_save_stages_report_progress(monkeypatch):
    monkeypatch.setattr(discovery, "_DR_BATCH", 2)
    monkeypatch.setattr(discovery, "_CHUNK", 2)
    update_settings(sources_enabled={"nominet": True})
    names = [f"d{i}.co.uk" for i in range(3)]
    _sources(monkeypatch, nominet=[_row(n) for n in names])
    _ahrefs(monkeypatch, {n: 9.0 for n in names})
    real, seen = jobs.report, []

    def spy(run_id, **kw):
        seen.append(kw.get("current"))
        return real(run_id, **kw)
    monkeypatch.setattr(jobs, "report", spy)
    assert discovery.run_discovery() == 3
    assert [c for c in seen if c and c.startswith(("DR:", "запись:"))] == [
        "DR: 0 из 3", "DR: 2 из 3", "запись: 0 из 3", "запись: 2 из 3"]


def test_emd_saved_without_dr_with_market_lang(monkeypatch):
    update_settings(sources_enabled={"emd": True},
                    emd_sets='[{"market":"es-MX","lang":"es","keywords":["mejor vpn"],"tlds":["com"]}]')
    _sources(monkeypatch)
    _ahrefs(monkeypatch, {})
    assert discovery.run_discovery() == 2
    got = _all()
    assert set(got) == {"mejorvpn.com", "mejor-vpn.com"}
    assert got["mejorvpn.com"].source == "emd" and got["mejorvpn.com"].market_lang == "es"
    assert got["mejorvpn.com"].lane == "free" and _Ahrefs.calls == []


def test_emd_name_also_found_by_auto_source_bypasses_dr_filter(monkeypatch):
    # у свободного EMD ссылок и не должно быть: DR 0 — не повод терять имя, найденное и registry.mx
    update_settings(sources_enabled={"mx": True, "emd": True},
                    emd_sets='[{"market":"es-MX","lang":"es","keywords":["mejor vpn"],"tlds":["mx"]}]')
    _sources(monkeypatch, mx=[_row("mejorvpn.mx", "mx", "free", None)])
    _ahrefs(monkeypatch, {})                                     # спроси мы DR — был бы 0.0
    assert discovery.run_discovery() == 2
    got = _all()
    assert _Ahrefs.calls == []
    assert got["mejorvpn.mx"].source == "mx" and got["mejorvpn.mx"].market_lang == "es"
    assert got["mejor-vpn.mx"].source == "emd"


def test_known_discovered_row_gets_missing_lane_and_deadline(monkeypatch):
    with db.SessionLocal() as s:
        s.add(Domain(domain="good.co.uk", source="list", status="discovered"))
        s.commit()
    update_settings(sources_enabled={"nominet": True})
    _sources(monkeypatch, nominet=[_row("good.co.uk")])
    _ahrefs(monkeypatch, {})
    discovery.run_discovery()
    d = _all()["good.co.uk"]
    assert d.lane == "bid" and d.acquire_deadline is not None and d.source == "list"


def test_chunked_lookup_and_insert(monkeypatch):
    monkeypatch.setattr(discovery, "_CHUNK", 2)
    update_settings(sources_enabled={"nominet": True})
    names = [f"d{i}.co.uk" for i in range(5)]
    _sources(monkeypatch, nominet=[_row(n) for n in names])
    _ahrefs(monkeypatch, {n: 9.0 for n in names})
    assert discovery.run_discovery() == 5 and set(_all()) == set(names)


def test_failing_source_does_not_sink_others(monkeypatch, caplog):
    update_settings(sources_enabled={"nominet": True, "mx": True})

    class Boom:
        def list_dropping(self):
            raise RuntimeError("nominet down")
    classes = {"dropcatch": _Src, "nominet": Boom,
               "mx": type("mx", (_Src,), {"rows": [_row("a.mx", "mx", "free", None)]})}
    monkeypatch.setattr(discovery, "_clients", lambda: classes)
    _ahrefs(monkeypatch, {"a.mx": 7.0})
    assert discovery.run_discovery() == 1
    assert "nominet" in caplog.text
    assert "Nominet (.uk): упал (RuntimeError)" in jobs.last("discovery")["message"]


def test_failed_source_visible_even_when_no_candidates(monkeypatch):
    # «все источники упали» не должно выглядеть как пустой день (и как успешный done)
    update_settings(sources_enabled={"nominet": True})

    class Boom:
        def list_dropping(self):
            raise RuntimeError("nominet down")
    monkeypatch.setattr(discovery, "_clients", lambda: {"nominet": Boom})
    _ahrefs(monkeypatch, {})
    # S1-07: все источники упали — задача FAILED с причиной, а не «успешный ноль»
    with pytest.raises(RuntimeError, match="Nominet"):
        discovery.run_discovery()
    last = jobs.last("discovery")
    assert last["status"] == "failed" and "Nominet (.uk): упал (RuntimeError)" in last["error"]


def test_stale_v1_source_keys_in_db_fall_back_to_code_defaults(monkeypatch):
    # в БД бокса лежат ключи v1 (backorder/cctld/…): они не должны молча выключить источники v2
    from app.models.settings import ScoringSettings
    from app.services import scoring_config as cfg
    from app.services.settings import get_settings
    monkeypatch.setattr(cfg, "SOURCES_ENABLED",
                        {"dropcatch": False, "nominet": True, "mx": True, "emd": True})
    get_settings()
    with db.SessionLocal() as s:
        s.get(ScoringSettings, 1).sources_enabled = {"backorder": True, "nominet": False}
        s.commit()
    assert get_settings()["sources_enabled"] == {"dropcatch": False, "nominet": False,
                                                 "mx": True, "emd": True}


def test_add_list_counts_bad_known_and_added():
    with db.SessionLocal() as s:
        s.add(Domain(domain="known.com", status="scored"))
        s.commit()
    out = discovery.add_list("New.com\nknown.com, new.com ;;  not_a_domain  x.ru")
    assert out == {"added": 2, "known": 1, "bad": 1, "cut": 0}   # x.ru принят: W0 скажет tld_closed
    got = _all()
    assert got["new.com"].source == "list" and got["new.com"].status == "discovered"


def test_add_list_reports_what_was_cut_over_max(monkeypatch, client):
    monkeypatch.setattr(discovery, "_LIST_MAX", 2)
    assert discovery.add_list("a.com b.com c.com") == {"added": 2, "known": 0, "bad": 0, "cut": 1}
    r = client.post("/domains/add-list", data={"domains": "d.com e.com f.com"}, follow_redirects=False)
    assert r.status_code == 303
    assert "сверх 2 за раз отброшено 1" in unquote(r.headers["location"])


def test_format_change_valueerror_reported_failed_and_others_still_run(monkeypatch):
    # парсеры дропов на смене формата бросают ValueError (а не тихий []): источник помечается
    # «упал», остальные продолжают работать
    update_settings(sources_enabled={"nominet": True, "mx": True})

    class Changed:
        def list_dropping(self):
            raise ValueError("Nominet: сменился формат файла")
    classes = {"dropcatch": _Src, "nominet": Changed,
               "mx": type("mx", (_Src,), {"rows": [_row("a.mx", "mx", "free", None)]})}
    monkeypatch.setattr(discovery, "_clients", lambda: classes)
    _ahrefs(monkeypatch, {"a.mx": 7.0})
    assert discovery.run_discovery() == 1
    assert set(_all()) == {"a.mx"}
    assert "Nominet (.uk): упал (ValueError)" in jobs.last("discovery")["message"]


def test_idn_and_mixed_case_source_rows_are_canonicalized_and_deduped(monkeypatch):
    # строка источника в юникоде/с заглавными -> punycode в нижнем регистре; тот же домен в двух
    # написаниях — одна запись; уже известный домен (в канон-форме) не вставляется повторно
    with db.SessionLocal() as s:
        s.add(Domain(domain="xn--e1afmkfd.com", source="list", status="scored"))
        s.commit()
    update_settings(sources_enabled={"nominet": True, "mx": True})
    _sources(monkeypatch,
             nominet=[_row("Пример.com"), _row("GOOD.co.uk"), _row("good.CO.uk")],
             mx=[_row("ПРИМЕР.com", "mx", "free", None)])
    _ahrefs(monkeypatch, {"good.co.uk": 12.0})
    assert discovery.run_discovery() == 1
    assert set(_all()) == {"xn--e1afmkfd.com", "good.co.uk"}
    assert _Ahrefs.calls == [["good.co.uk"]]                     # известный пример.com — без DR


# --- финальная фикс-волна (minor «е»): причина «DR недоступен» и потолок Retry-After -----------

def test_dr_unavailable_message_names_the_reason_without_url(monkeypatch):
    """«DR недоступен — N пропущено» без причины не говорил оператору, что чинить: ключ, доступ или
    сбой. Причина — имя класса исключения / HTTP-код, никогда не URL и не ключ."""
    update_settings(sources_enabled={"nominet": True})
    _sources(monkeypatch, nominet=[_row("good.co.uk")])
    assert discovery.run_discovery() == 0                       # autouse _no_paid_keys: ключ пуст
    assert ("DR недоступен — 1 пропущено (ключ AHREFS_API_KEY не задан)"
            in jobs.last("discovery")["message"])
    _ahrefs(monkeypatch, {"good.co.uk": 12.0}, errors=[_http_error(401)])
    discovery.run_discovery()
    msg = jobs.last("discovery")["message"]
    assert "DR недоступен — 1 пропущено (HTTPStatusError 401)" in msg and "ahrefs.com" not in msg
    _ahrefs(monkeypatch, {}, fail=True)
    discovery.run_discovery()
    assert "DR недоступен — 1 пропущено (сбой Ahrefs: RuntimeError)" in jobs.last("discovery")["message"]


@pytest.mark.parametrize("header,wait", [("100000", 120.0), ("-5", 1.0), ("0", 1.0), ("nan", 1.0)])
def test_retry_after_is_capped_and_never_below_a_second(monkeypatch, slept, header, wait):
    """Retry-After из ответа 429 — в [1, 120] с: огромное значение подвесило бы discovery на сутки
    (и держало бы замок задачи), отрицательное/нулевое/NaN — ретрай без паузы (или ValueError сна)."""
    update_settings(sources_enabled={"nominet": True})
    _sources(monkeypatch, nominet=[_row("good.co.uk")])
    _ahrefs(monkeypatch, {"good.co.uk": 12.0}, errors=[_http_error(429, {"Retry-After": header})])
    assert discovery.run_discovery() == 1
    assert slept == [wait]
