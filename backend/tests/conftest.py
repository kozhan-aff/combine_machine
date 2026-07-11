"""Offline test harness: run the whole pipeline on in-memory SQLite, no Docker/PG.

The models declare Postgres JSONB columns; a compile hook renders those as plain
JSON on SQLite so `create_all` works. Services grab `app.db.SessionLocal` at
call-time, so rebinding the sessionmaker to a SQLite engine redirects every DB
call (services + FastAPI `get_session`) at once. Network integrations are mocked
per-test — nothing here touches the box.
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB

import app.db as db
from app.db import Base
# import models so their tables register on Base.metadata before create_all
import app.models.domain
import app.models.site
import app.models.offer
import app.models.monitoring
import app.models.settings
import app.models.autonomy
# reference the modules so their table-registration side effect (create_all needs
# every table, incl. index_history from publish.check_index) isn't seen as a dead import
_REGISTER_TABLES = (app.models.domain, app.models.site, app.models.offer, app.models.monitoring, app.models.settings, app.models.autonomy)


@compiles(JSONB, "sqlite")
def _jsonb_as_json(element, compiler, **kw):  # DDL only; bind/result still json.dumps/loads
    return "JSON"


@pytest.fixture(autouse=True)
def sqlite_db():
    """Fresh in-memory DB per test, bound into app.db. StaticPool = one shared conn."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(engine)
    db.engine = engine
    db.SessionLocal.configure(bind=engine)
    yield engine
    Base.metadata.drop_all(engine)


@pytest.fixture(autouse=True)
def _default_sources_backorder_only(sqlite_db, monkeypatch):
    """Структурный офлайн-гвард (финальное ревью, Finding 4): по умолчанию сид настроек
    воронки видит только backorder включённым — многоисточниковые cctld/reg_ru/sweb (httpx)
    выключены, чтобы будущий тест run_discovery() не мог тихо уйти в живую сеть. Достигается
    монки-патчем самого дефолта в scoring_config (не отдельным update_settings-вызовом), поэтому
    test_settings.py::test_get_settings_seeds_defaults (сверяет seed с cfg.SOURCES_ENABLED)
    остаётся верным — обе стороны сравнения видят один и тот же патченный дефолт. Тесты,
    которым нужны другие источники, сами зовут update_settings(sources_enabled=...) и
    переопределяют это явно (несколько уже так делают — их вызов становится избыточным, но
    безвредным). Зависимость от sqlite_db — только порядок фикстур, самого патча она не требует."""
    from app.services import scoring_config as cfg
    monkeypatch.setattr(cfg, "SOURCES_ENABLED",
                        {"backorder": True, "cctld": False, "reg_ru": False, "sweb": False})
    yield


@pytest.fixture(autouse=True)
def _no_panel_auth():
    """Тесты герметичны к .env оператора: Basic-auth панели выключен на время прогона
    (иначе заданные в .env PANEL_USER/PANEL_PASS отдают 401 вместо 303/200 на панельных
    роутах). CSRF-guard не трогаем — TestClient шлёт запросы без Origin, он их и так пускает."""
    from app.config import settings
    saved = settings.PANEL_USER, settings.PANEL_PASS
    settings.PANEL_USER = settings.PANEL_PASS = ""
    yield
    settings.PANEL_USER, settings.PANEL_PASS = saved


@pytest.fixture(autouse=True)
def _reset_pricing_cache():
    """Кэш тарифа в pricing.py живёт на процесс (`_TARIFF`), а pytest гоняет всю сессию
    в одном процессе — без сброса test_refresh_prices_only_backorder (мутирует _TARIFF["price"])
    протекает в последующие файлы (run_discovery() в test_sources.py увидел бы чужую цену
    вместо None). Save/restore-стиль, как _no_panel_auth."""
    from app.services import pricing
    saved = dict(pricing._TARIFF)
    yield
    pricing._TARIFF = saved


@pytest.fixture
def client(monkeypatch):
    """TestClient + офлайн-гвард на backorder.

    Структурный гвард (как _default_sources_backorder_only): /queue рендерит сетку ставок и
    баланс лицевого счёта через httpx, поэтому ЛЮБОЙ тест, открывающий панель с заявкой в
    очереди, без этого патча тихо уходил бы в живую сеть. Патчим на фикстуре `client`, а не
    autouse: панельные роуты достижимы только через неё, а юнит-тесты транспорта
    (test_pricing / test_backorder_order) должны гонять НАСТОЯЩИЙ tariffs()/pick_tariff().
    Баланс 0 ₽ — честный дефолт: он же и на живом счету."""
    from fastapi.testclient import TestClient
    from app.integrations.backorder import BackorderClient
    from app.main import app
    monkeypatch.setattr(BackorderClient, "tariffs",
                        lambda self, zone=".RU", refresh=False: [
                            {"price_id": "4769", "period_id": "3442", "price": 190.0},
                            {"price_id": "4770", "period_id": "3443", "price": 400.0}])
    monkeypatch.setattr(BackorderClient, "balance", lambda self: 0.0)
    return TestClient(app)
