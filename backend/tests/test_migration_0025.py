"""Миграция 0025: цепочка ревизий, архив РФ-пула (SQL исполняется на SQLite), единый дефолт
источников, таблица dr_seen = модель DrSeen, грязные причины legacy_ru/tld_closed, зона вне белого
списка не возвращается в approved."""
import importlib.util
import pathlib
from datetime import datetime, timezone
from types import SimpleNamespace as NS

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

import app.db as db
from app.models.domain import Domain


def _mig():
    p = pathlib.Path(__file__).parents[1] / "alembic" / "versions" / "0025_v2_m1.py"
    spec = importlib.util.spec_from_file_location("m0025", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class _RecOp:
    """Подмена alembic.op: записывает вызовы, ничего не исполняет (миграция постгресовая —
    `::jsonb`, SQLite её не прогонит)."""
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        return lambda *a, **kw: self.calls.append((name, a, kw))


def test_revision_chain():
    m = _mig()
    assert m.revision == "0025_v2_m1" and m.down_revision == "0024_domain_score_log"


def test_archive_sql_hits_only_unbought_ru_candidates_and_is_reversible():
    rows = [("a.ru", "discovered"), ("b.xn--p1ai", "scored"), ("c.ru", "purchased"),
            ("d.com", "discovered"), ("e.su", "approved"), ("f.com.ru", "rejected"),
            ("g.ru", "purchasing"), ("h.ru", "live")]
    with db.SessionLocal() as s:
        for name, st in rows:
            s.add(Domain(domain=name, status=st, reject_reason="rkn" if st == "rejected" else None))
        s.commit()
        s.execute(text(_mig().ARCHIVE_SQL))
        s.commit()
        s.expire_all()          # expire_on_commit=False: иначе читались бы объекты сессии, а не строки после UPDATE
        got = {d.domain: (d.status, d.reject_reason) for d in s.query(Domain)}
    for name in ("a.ru", "b.xn--p1ai", "e.su"):
        assert got[name] == ("rejected", "legacy_ru"), name
    assert got["c.ru"] == ("purchased", None)          # купленный — не трогаем
    assert got["g.ru"] == ("purchasing", None)         # живой заказ у M2 — не трогаем
    assert got["h.ru"] == ("live", None)               # сайт живёт — не трогаем
    assert got["d.com"] == ("discovered", None)        # международный — не трогаем
    assert got["f.com.ru"] == ("rejected", "rkn")      # уже отклонённый — причину не перетираем
    with db.SessionLocal() as s:
        s.execute(text(_mig().UNARCHIVE_SQL))
        s.commit()
        assert s.query(Domain).filter_by(domain="a.ru").one().status == "discovered"


def test_sources_default_single_source_of_truth():
    # миграция сеет то же, что код считает дефолтом (Задача 6 переключит cfg.SOURCES_ENABLED)
    assert set(_mig().SOURCES_V2) == {"dropcatch", "nominet", "mx", "emd"}


def test_dr_seen_table_in_migration_matches_model(monkeypatch):
    from app.models.domain import DrSeen
    m, rec = _mig(), _RecOp()
    monkeypatch.setattr(m, "op", rec)
    m.upgrade()
    made = [a for name, a, _ in rec.calls if name == "create_table" and a[0] == "dr_seen"]
    assert len(made) == 1
    cols = {c.name: c for c in made[0][1:]}
    model = DrSeen.__table__.c
    assert set(cols) == set(model.keys()) == {"domain", "dr", "checked_at"}
    for name, col in cols.items():                     # живой PG получит ровно то, что видят тесты
        assert (col.primary_key, col.nullable, type(col.type)) == (
            model[name].primary_key, model[name].nullable, type(model[name].type)), name
    assert cols["domain"].type.length == model["domain"].type.length == 253
    assert cols["checked_at"].type.timezone is True and model["checked_at"].type.timezone is True
    added = {(a[0], a[1].name) for name, a, _ in rec.calls if name == "add_column"}
    assert ("scoring_settings", "units_floor") in added
    rec.calls.clear()
    m.downgrade()
    assert ("drop_table", ("dr_seen",), {}) in rec.calls
    assert ("drop_column", ("scoring_settings", "units_floor"), {}) in rec.calls


def test_dr_seen_model_roundtrip_and_checked_at_required():
    from app.models.domain import DrSeen
    now = datetime.now(timezone.utc)
    with db.SessionLocal() as s:
        s.add_all([DrSeen(domain="low-dr.com", dr=0, checked_at=now),
                   DrSeen(domain="no-dr.com", dr=None, checked_at=now)])     # Ahrefs не вернул DR
        s.commit()
        assert float(s.get(DrSeen, "low-dr.com").dr) == 0.0
        assert s.get(DrSeen, "no-dr.com").dr is None
        s.add(DrSeen(domain="no-time.com", dr=3))
        with pytest.raises(IntegrityError):
            s.commit()


def test_legacy_ru_and_closed_zone_never_back_to_approved():
    # архив РФ-пула и чужая зона — факт о домене, не наш порог: к кассе руками не вернуть
    from app.services import transitions
    for reason in ("legacy_ru", "tld_closed"):
        d = NS(domain="old.ru", status="rejected", reject_reason=reason, rkn_listed=None,
               blacklisted=None, prior_flags={}, wayback_checked=True, score_breakdown=None)
        assert transitions.dirty_reason(d) == reason
        with pytest.raises(transitions.TransitionDenied, match="грязный"):
            transitions.check(d, "approved")


def test_dirt_is_refused_before_zone():
    # порядок «грязь раньше зоны»: у грязного домена в закрытой зоне оператор видит причину-грязь
    # (путь назад — перескор), а не «добавь зону в белый список»
    from app.services import transitions
    d = NS(domain="bad.ru", status="rejected", reject_reason="rkn", rkn_listed=True,
           blacklisted=None, prior_flags={}, wayback_checked=True, score_breakdown=None)
    with pytest.raises(transitions.TransitionDenied, match="грязный") as e:
        transitions.check(d, "approved", allowlist=["com"])      # .ru вне списка
    assert "белом списке" not in str(e.value)
    with pytest.raises(transitions.TransitionDenied, match="грязный"):
        transitions.check(d, "approved", allowlist=["com", "ru"])  # зона разрешена — отказ тот же


def test_threshold_reject_outside_allowlist_cannot_return_to_approved():
    # находка R2-19: v1-домен .ru, отклонённый ПОРОГОМ, не грязный — но его зоны нет в белом
    # списке: «↩ вернуть в approved» повела бы его в очередь backorder, который .ru покупает
    from app.services import transitions
    from app.services.settings import update_settings

    def d(name):
        return NS(domain=name, status="rejected", reject_reason="low_score", rkn_listed=None,
                  blacklisted=None, prior_flags={}, wayback_checked=True, score_breakdown=None)
    with pytest.raises(transitions.TransitionDenied, match="нет в списке зон"):
        transitions.check(d("weak.ru"), "approved")
    transitions.check(d("weak.com"), "approved")      # зона в списке — порог возвращается руками
    update_settings(tld_allowlist="com ru")
    transitions.check(d("weak.ru"), "approved")       # список — из /settings, не из кода
