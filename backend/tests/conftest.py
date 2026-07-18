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
import app.models.job
# reference the modules so their table-registration side effect (create_all needs
# every table, incl. index_history from publish.check_index) isn't seen as a dead import
_REGISTER_TABLES = (app.models.domain, app.models.site, app.models.offer, app.models.monitoring,
                    app.models.settings, app.models.autonomy, app.models.job)


@compiles(JSONB, "sqlite")
def _jsonb_as_json(element, compiler, **kw):  # DDL only; bind/result still json.dumps/loads
    return "JSON"


class LiveNetworkAttempt(BaseException):
    """Тест полез в живую сеть. Наследник BaseException СПЕЦИАЛЬНО: прикладной код полон
    широких `except Exception` (execute_confirmed_order, queue_view, jobs, scoring), и
    ловушка на Exception была бы им проглочена — тест «проходил» бы зелёным ровно на том
    роуте, который она защищает. BaseException проходит сквозь них насквозь."""


class LivePaidOrder(BaseException):
    """Тест чуть не отправил ЖИВОЙ ПЛАТНЫЙ заказ. Тоже BaseException — по той же причине."""


@pytest.fixture(autouse=True)
def _no_live_network(monkeypatch):
    """РУБИЛЬНИК ЖИВОЙ СЕТИ. Инвариант герметичности — структурный, не «на честном слове».

    До этого гвардов было два (источники + фикстура `client`), и оба дырявые: юнит-тесты
    денежного пути не брали `client`, и сьют доказуемо ходил в боевой billmgr backorder
    с реальными BACKORDER_LOGIN/PASSWORD из .env (`confirm_order` -> pick_tariff -> живой
    price-JSON; `execute` -> find_order -> живой authed-запрос). Зелёный сьют держался на
    интернете, а от списания денег отделял один забытый monkeypatch.

    Рубим ТРАНСПОРТ httpx (реальные сокеты), а НЕ httpx.Client: TestClient — подкласс
    httpx.Client и ходит через ASGITransport, панель обязана работать. Юнит-тесты транспорта
    подменяют request/_client.request на ИНСТАНСЕ — инстанс-атрибут перекрывает классовый,
    до транспорта не доходит. Значит фикстура ловит ровно то, что должна: настоящий выход
    в сеть. Плюс DNS (blacklist.py ходит резолвером мимо httpx).
    """
    import socket

    import httpx

    def _boom(self, request, *a, **kw):
        raise LiveNetworkAttempt(
            f"живой сетевой запрос из теста: {request.method} {request.url.host}{request.url.path}. "
            "Тесты герметичны — подмени клиент/метод через monkeypatch.")

    def _boom_dns(host, *a, **kw):
        raise LiveNetworkAttempt(f"живой DNS-запрос из теста: {host}")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", _boom)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", _boom)
    monkeypatch.setattr(socket, "getaddrinfo", _boom_dns)
    monkeypatch.setattr(socket, "gethostbyname", _boom_dns)
    # blacklist.py при заданном DNS_RESOLVER ходит через dnspython — это СЫРЫЕ UDP-сокеты
    # мимо getaddrinfo, патчи выше его не ловят.
    try:
        import dns.resolver
        monkeypatch.setattr(dns.resolver.Resolver, "resolve",
                            lambda self, qname, *a, **kw: _boom_dns(qname))
    except ImportError:                       # dnspython опционален — этого пути просто нет
        pass
    yield


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
def _drain_background_jobs(sqlite_db):
    """ДОПОЛНЕНИЕ ПРОТИВ БРИФА Task 1 (не было в спеке, добавлено при эмпирической проверке).

    jobs.py теперь реально пишет job_run из ФОНОВОГО потока (spawn/track), а не в dict процесса
    — старый in-memory jobs.py никогда не касался БД из другого потока, поэтому эта гонка была
    физически невозможна раньше. Не каждый тест дожидается is_running()==False перед возвратом
    (test_autopilot_panel.py::test_autopilot_run_starts_job поллит побочный эффект внутри
    target(), а не реестр) — тогда фоновый поток ещё дописывает "done" в job_run, когда
    `sqlite_db` уже снёс таблицы под ним. На этой машине SQLite собран THREADSAFE=2
    («multi-thread»): по документации SQLite это segfault, не гипотетика — воспроизведено.
    Зависимость от `sqlite_db` в сигнатуре — не для доступа к движку, а чтобы pytest завершил
    ЭТОТ фиксчур (наш drain) РАНЬШЕ, чем teardown `sqlite_db` (drop_all): фикстуры сворачиваются
    в обратном порядке, а этот объявлен позже/поверх sqlite_db."""
    yield
    from app.services import jobs
    jobs._drain()


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
    """Кэш тарифа в pricing.py живёт на процесс (`_TARIFF`, по зоне: `.RU`/`.РФ`), а pytest
    гоняет всю сессию в одном процессе — без сброса test_refresh_prices_only_backorder
    (мутирует `_TARIFF`) протекает в последующие файлы (run_discovery() в test_sources.py
    увидел бы чужую цену вместо None). Save/restore-стиль, как _no_panel_auth."""
    from app.services import pricing
    saved = dict(pricing._TARIFF)
    yield
    pricing._TARIFF = saved


def _no_live_order(self, domain, price_id, period_id):
    raise LivePaidOrder(
        f"живой ПЛАТНЫЙ заказ backorder из теста ({domain})! Тест, которому нужен «успех», "
        "обязан сам подменить BackorderClient.order своим monkeypatch.")


@pytest.fixture
def client(monkeypatch):
    """TestClient + офлайн-гвард на backorder.

    Структурный гвард (как _default_sources_backorder_only). Настоящего сетевого блока в
    харнессе НЕТ, а панель денежного пути ходит к провайдеру с БОЕВЫМИ кредами из .env:
      /queue        -> tariffs() + balance()   (чтение)
      /queue/poll   -> client_orders()         (чтение)
      /queue/{}/exec-> find_order() + order()  (ПЛАТНО!)
    Без патча любой тест на этих роутах уходил бы в живую сеть, а execute при ненулевом
    балансе — списал бы деньги. Поэтому order() тут не «заглушка», а ловушка: падает громко.
    Патчим на фикстуре `client`, а не autouse — юнит-тесты транспорта (test_pricing /
    test_backorder_order) должны гонять НАСТОЯЩИЕ tariffs()/pick_tariff()/order().
    Баланс 0 ₽ — честный дефолт: он же и на живом счету."""
    from fastapi.testclient import TestClient
    from app.integrations.backorder import BackorderClient
    from app.main import app
    monkeypatch.setattr(BackorderClient, "tariffs",
                        lambda self, zone=".RU", refresh=False: [
                            {"price_id": "4769", "period_id": "3442", "price": 190.0},
                            {"price_id": "4770", "period_id": "3443", "price": 400.0}])
    monkeypatch.setattr(BackorderClient, "balance", lambda self, ttl=60.0: 0.0)
    monkeypatch.setattr(BackorderClient, "client_orders", lambda self: [])
    monkeypatch.setattr(BackorderClient, "find_order", lambda self, domain: None)
    monkeypatch.setattr(BackorderClient, "order", _no_live_order)
    return TestClient(app)
