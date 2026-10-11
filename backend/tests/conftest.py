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
import app.models.domain_score_log
import app.models.secret
import app.models.domain_list
import app.models.domain_rank
# reference the modules so their table-registration side effect (create_all needs
# every table, incl. index_history from publish.check_index) isn't seen as a dead import
_REGISTER_TABLES = (app.models.domain, app.models.site, app.models.offer, app.models.monitoring,
                    app.models.settings, app.models.autonomy, app.models.job,
                    app.models.domain_score_log, app.models.secret, app.models.domain_list,
                    app.models.domain_rank)

from app.integrations.rdap import RdapClient

# iCloud-дубли («test_x 2.py», «fixture 2.json») — мусор синхронизации Documents на Mac; без этого
# сьют собирает их как лишние тесты и считает 955 вместо 944 passed (S7-21).
collect_ignore_glob = ["* 2.py", "* 2.json", "* 2.csv"]

# настоящий бутстрап — для фикстуры real_rdap_bootstrap (autouse _no_paid_keys его подменяет)
_REAL_RDAP_BOOTSTRAP = RdapClient._bootstrap


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
def _clean_diag_and_panel_state():
    """Предохранитель aaPanel (пауза после отказа авторизации), кэш /diag и TTL-кэш проб LLM/SearXNG —
    модульные глобалы процесса: без сброса пауза от одного теста блокировала бы панель в соседнем,
    а кэш /diag протекал бы в чужой рендер."""
    from app.integrations import aapanel
    from app.services import diag_cache, diagnostics

    def _reset():
        aapanel.reset_block()
        diagnostics.reset_probe_cache()
        diag_cache._checks = None
        diag_cache._checked_at = None
    _reset()
    yield
    _reset()


@pytest.fixture(autouse=True)
def _close_http_pool():
    """Пул httpx-клиентов (integrations/base.py) — модульный глобал: без сброса клиент, созданный в
    одном тесте, жил бы в следующем."""
    from app.integrations import base
    base.close_pool()
    yield
    base.close_pool()


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
def _no_key_overrides(monkeypatch):
    """Переопределения ключей из БД («Ключи и доступы») по умолчанию ВЫКЛЮЧЕНЫ в тестах: иначе каждое
    чтение settings.X ходило бы в sqlite из потоков воронки (общее соединение StaticPool, THREADSAFE=2
    — см. _drain_background_jobs) и кэш протекал бы между тестами с разными движками. Тесты самой
    фичи берут фикстуру `key_overrides` — она включает механизм."""
    from app.services import api_keys
    monkeypatch.setattr(api_keys, "ENABLED", False)
    api_keys.invalidate()
    yield
    api_keys.invalidate()


@pytest.fixture
def key_overrides(_no_key_overrides, monkeypatch):
    """Включить переопределения ключей из БД (sqlite-харнесс) на время теста."""
    from app.services import api_keys
    monkeypatch.setattr(api_keys, "ENABLED", True)
    api_keys.invalidate()
    return api_keys


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
def _default_sources_offline(sqlite_db, monkeypatch):
    """Структурный офлайн-гвард (финальное ревью v1, Finding 4): по умолчанию сид настроек видит
    ВСЕ источники v2 выключенными — все они сетевые (DropCatch, Nominet, registry.mx), а EMD без
    наборов пуст, — чтобы тест run_discovery() не мог тихо уйти в живую сеть. Достигается
    монки-патчем самого дефолта в scoring_config (не отдельным update_settings-вызовом), поэтому
    test_settings.py::test_get_settings_seeds_defaults (сверяет seed с cfg.SOURCES_ENABLED)
    остаётся верным — обе стороны сравнения видят один и тот же патченный дефолт. Тесты, которым
    нужны источники, сами зовут update_settings(sources_enabled=...) и подменяют
    discovery._clients. Зависимость от sqlite_db — только порядок фикстур."""
    from app.services import scoring_config as cfg
    monkeypatch.setattr(cfg, "SOURCES_ENABLED",
                        {"dropcatch": False, "nominet": False, "mx": False, "emd": False,
                         "namesilo_auction": False})
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
def _no_live_publish_verify(monkeypatch):
    """HTTP-проверка опубликованной страницы (publish._verify_live) ходит на сам домен — в тестах
    по умолчанию выключена; тесты проверки включают её и подменяют siteprobe.fetch."""
    from app.config import settings
    monkeypatch.setattr(settings, "PUBLISH_VERIFY", False)


@pytest.fixture(autouse=True)
def _acq_zones_open(monkeypatch):
    """Кассовый гард зоны (S3-06) судит по белому списку v2 — а старые тесты денежного пути живут на
    .ru-доменах v1. Для них список расширен; тесты самого гарда возвращают реальный
    (`monkeypatch.setattr(acquisition, "_zone_allowlist", ...)`)."""
    from app.services import acquisition
    monkeypatch.setattr(acquisition, "_zone_allowlist",
                        lambda: ["com", "net", "org", "co.uk", "ru", "рф", "xn--p1ai"])
    # баланс провайдера перед отправкой (S3-09) — сеть; тесты проверки баланса подменяют её сами
    monkeypatch.setattr(acquisition, "_balance_of", lambda client: None)


@pytest.fixture(autouse=True)
def _no_paid_keys(monkeypatch):
    """Тесты герметичны к .env оператора и к сети реестров.

    Ключи: на боксе тесты гоняются в контейнере, где заданы БОЕВЫЕ AHREFS_API_KEY/WEBRISK_API_KEY/
    SPAMHAUS_DQS_KEY, а config.py читает .env относительно cwd — из корня репо ключ виден, из
    backend/ нет, и тест зеленел бы или краснел в зависимости от каталога. Клиент с ключом сам
    идёт в сеть, а рубильник _no_live_network роняет такой тест BaseException'ом. Ключи пусты на
    время теста; тест, которому ключ нужен, ставит его сам через monkeypatch.setattr(settings, …).

    RDAP: по умолчанию НИ ОДНА зона не имеет RDAP (`_bootstrap` -> {}), в IANA никто не ходит —
    иначе настоящий RdapClient из _make_clients()/recheck_acquirability() полез бы в IANA из
    фонового потока. Тест, которому нужен RDAP в воронке, передаёт фейк через clients["rdap"];
    юнит-тесты самого клиента берут фикстуру real_rdap_bootstrap."""
    from app.config import settings
    for key in ("AHREFS_API_KEY", "WEBRISK_API_KEY", "SPAMHAUS_DQS_KEY", "NAMESILO_API_KEY",
                "GSC_SERVICE_ACCOUNT_JSON",
                # боксовые значения из локального .env (путь к серту панели в контейнере, account_id CF):
                # с ними 25 тестов провижна/CF краснеют на Mac и зеленеют в контейнере — результат
                # не должен зависеть от .env оператора
                "AAPANEL_CA_BUNDLE", "CLOUDFLARE_ACCOUNT_ID"):
        monkeypatch.setattr(settings, key, "")
    # IndexNow по умолчанию включён и ходит в сеть после публикации; в тестах выключен
    # (тесты самого пинга включают его и подставляют мок-транспорт).
    # досье: скриншоты через Browserless в тестах выключены (сеть и так заблокирована)
    monkeypatch.setattr(settings, "RESEARCH_SCREENSHOTS", False)
    monkeypatch.setattr(settings, "INDEXNOW_ENABLED", False)
    monkeypatch.setattr(settings, "INDEXNOW_SECRET", "test-installation-secret")
    monkeypatch.setattr(RdapClient, "_bootstrap", lambda self: {})
    yield


@pytest.fixture
def real_rdap_bootstrap(_no_paid_keys, monkeypatch):
    """Настоящий RdapClient._bootstrap — для юнит-тестов клиента (HTTP они подменяют на инстансе:
    monkeypatch.setattr(c, "request", …)). Зависит от _no_paid_keys, чтобы встать ПОСЛЕ его подмены."""
    monkeypatch.setattr(RdapClient, "_bootstrap", _REAL_RDAP_BOOTSTRAP)


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
    from app.integrations.optimizator import OptimizatorClient
    monkeypatch.setattr(OptimizatorClient, "balance", lambda self: 0.0)   # шапка /queue (S3-09)
    return TestClient(app)


@pytest.fixture(autouse=True)
def origin_probe(monkeypatch):
    """Пробы origin провижна (services/provisioning.probe_marker) — по умолчанию в MockTransport:
    http и https отвечают 200, Origin CA выключен. Проба ищет в ТЕЛЕ nonce маркер-файла
    (`/cm-probe-*`), поэтому «наш vhost» эмулируется выдачей nonce; `_new_nonce` подменён на константу.
    Тест правит `.http`/`.https` (код ответа или исключение httpx), `.marker_http`/`.marker_https`/`.www`
    (False = на этот вход отвечает ЧУЖОЙ/дефолтный vhost: 200 без nonce) и читает `.requests`."""
    import httpx
    from app.config import settings
    from app.services import provisioning

    class _Origin:
        http = 200
        https = 200
        marker_http = True      # наш vhost обслуживает apex по HTTP
        marker_https = True     # ...по HTTPS
        www = True              # ...и www-алиас
        nonce = "test-nonce"
        requests: list = []

    o = _Origin()
    o.requests = []

    def handler(req: httpx.Request) -> httpx.Response:
        o.requests.append(req)
        https = req.url.scheme == "https"
        res = o.https if https else o.http
        if isinstance(res, Exception):
            raise res
        ours = o.marker_https if https else o.marker_http
        if req.headers["host"].startswith("www."):
            ours = ours and o.www
        if res == 200 and ours and req.url.path.startswith("/cm-probe-"):
            return httpx.Response(200, text=o.nonce)
        return httpx.Response(res, text="ok")

    monkeypatch.setattr(provisioning, "_origin_client",
                        lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(provisioning, "_new_nonce", lambda: o.nonce)
    monkeypatch.setattr(settings, "ORIGIN_CA_AUTO", False, raising=False)
    return o
