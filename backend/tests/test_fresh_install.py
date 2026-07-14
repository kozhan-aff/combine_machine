"""Свежая установка не жжёт деньги (аудит 2026-07-14, F21 + F30).

F21: миграция `0002` сеяла `scoring_settings.sources_enabled` со ВСЕМИ источниками
включёнными, хотя рантайм-дефолт кода (`scoring_config.SOURCES_ENABLED`) держит витрины
(cctld/reg_ru/sweb) ВЫКЛЮЧЕННЫМИ. На `docker compose up` с нуля (`alembic upgrade head` на
пустую БД) это молча включает сырые источники без RD/лейна/дедлайна — платный Ahrefs зовётся
ровно для доменов БЕЗ RD, то есть витрины были прямым источником трат на капчу, которые
оператор не включал осознанно.

F30: `canonical_domain()` пропускал мусор дальше в whois/Ahrefs — ведущий/хвостовой дефис в
метке, числовой/однобуквенный TLD, голый IP.

Тест (а) реально гоняет alembic (0001 -> 0002) на чистой SQLite-БД — не переизобретает
логику INSERT'а текстом, а проверяет то, что миграция ФАКТИЧЕСКИ кладёт в таблицу. Тест для
корректирующей 0015 исполняет её собственный upgrade() против seed-строки, имитирующей уже
накатанную багом базу (полный прогон 0001..0015 на SQLite физически невозможен: миграции
0004+ используют постгресовые `::jsonb`-касты, которых SQLite не понимает — этот тест
изолированно вызывает ТОЛЬКО 0015, не идя в обход её реального кода).
"""
import importlib.util
import json
import pathlib
import sqlite3

from sqlalchemy import create_engine, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import StaticPool

from app.services.discovery import canonical_domain

_BACKEND = pathlib.Path(__file__).resolve().parents[1]
_VERSIONS = _BACKEND / "alembic" / "versions"


@compiles(JSONB, "sqlite")
def _jsonb_as_json_for_migration_test(element, compiler, **kw):  # pragma: no cover - DDL only
    return "JSON"


def test_fresh_alembic_upgrade_seeds_only_backorder(tmp_path, monkeypatch):
    """(а) `alembic upgrade` 0001->0002 с нуля -> sources_enabled = только backorder=true.

    До фикса 0002 вставляла все четыре источника true — это расходилось с рантайм-дефолтом
    `SOURCES_ENABLED` и на свежей установке включало платные витрины без спроса оператора.
    """
    from alembic import command
    from alembic.config import Config
    from app.config import settings

    db_path = tmp_path / "fresh.db"
    monkeypatch.setattr(settings, "DATABASE_URL", f"sqlite:///{db_path}")

    cfg = Config()
    cfg.set_main_option("script_location", str(_BACKEND / "alembic"))
    command.upgrade(cfg, "0002")   # ровно migration под тестом; 0003+ ломается на sqlite (::jsonb)

    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT sources_enabled FROM scoring_settings WHERE id = 1"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, "0002 обязана засеять строку scoring_settings id=1"
    assert json.loads(row[0]) == {
        "backorder": True, "cctld": False, "reg_ru": False, "sweb": False,
    }


def _load_migration_module(filename: str):
    spec = importlib.util.spec_from_file_location(filename, _VERSIONS / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_corrective_migration_forces_witrines_off_on_dirty_db():
    """(б) 0015 на УЖЕ накатанной базе, испорченной старым 0002 (все источники true),
    принудительно возвращает cctld/reg_ru/sweb к false — тот же приём, что 0008 lane_backfill
    применила к данным, испорченным другим багом. `backorder` остаётся включённым (единственный
    источник, дающий RD/лейн/дедлайн без платной проверки)."""
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                            poolclass=StaticPool)
    with engine.connect() as conn:
        conn.execute(text(
            "CREATE TABLE scoring_settings (id INTEGER PRIMARY KEY, sources_enabled TEXT)"
        ))
        conn.execute(text(
            "INSERT INTO scoring_settings (id, sources_enabled) VALUES (1, "
            "'{\"backorder\": true, \"cctld\": true, \"reg_ru\": true, \"sweb\": true}')"
        ))
        conn.commit()

        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            _load_migration_module("0015_source_defaults.py").upgrade()

        row = conn.execute(
            text("SELECT sources_enabled FROM scoring_settings WHERE id = 1")
        ).fetchone()
    assert json.loads(row[0]) == {
        "backorder": True, "cctld": False, "reg_ru": False, "sweb": False,
    }


def test_canonical_domain_rejects_leading_trailing_hyphen():
    assert canonical_domain("-foo.ru") is None
    assert canonical_domain("foo-.ru") is None


def test_canonical_domain_rejects_numeric_tld():
    assert canonical_domain("foo.123") is None


def test_canonical_domain_rejects_single_char_tld():
    assert canonical_domain("foo.a") is None


def test_canonical_domain_rejects_bare_ip():
    assert canonical_domain("1.2.3.4") is None


def test_canonical_domain_still_accepts_legit_domains():
    """Ужесточение не должно срубить легитимные случаи."""
    assert canonical_domain("example.ru") == "example.ru"
    assert canonical_domain("example.com") == "example.com"
    assert canonical_domain("sub.example.ru") == "sub.example.ru"          # поддомен
    assert canonical_domain("a.ru") == "a.ru"                              # 1-симв. метка, НЕ tld
    assert canonical_domain("пример.рф") == "xn--e1afmkfd.xn--p1ai"       # IDN -> punycode
    assert canonical_domain("xn--e1afmkfd.xn--p1ai") == "xn--e1afmkfd.xn--p1ai"  # уже punycode
