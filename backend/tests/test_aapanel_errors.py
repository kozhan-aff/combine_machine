"""aaPanel отказал — значит отказал (аудит F14/F16).

Та же болезнь, что была у A-Parser (Задача 4): панель отвечает **HTTP 200 даже на отказ** —
сбой живёт в ТЕЛЕ ({"status": false, "msg": "..."}), `raise_for_status` его не видит. А
`ensure_site()` и `write_file()` звались как statement: результат не смотрел никто, следом
безусловно шло `site.status = "content"` / `page.status = "published"`.

Чем это грозило. Панель отказывает буднично: протух api_sk, IP выпал из whitelist, кончилось
место, нет прав на docroot. В каждом таком случае машина рапортовала об успехе:
  · провижн: сайт объявлен `content` (инфраструктура готова) — без vhost'а. Человек идёт
    вычитывать контент для сайта, которого нет;
  · публикация: страница `published`, сайт `published` — а в docroot пусто. Дальше M5 честно
    спрашивает у поисковика про несуществующий URL и пишет `not_indexed`: машина расследует
    последствия собственного вранья.
Асимметрия была вопиющей: отказ Cloudflare летел RuntimeError, отказ aaPanel — молчал.

СТРОКИ `msg` ЗДЕСЬ НЕ ВЫДУМАНЫ. Все три сняты с живой панели и зафиксированы в докстрингах
`integrations/aapanel.py`: "Requested file exists!" (CreateFile на существующем файле),
"Configuration file not exist" (SaveFileBody на несозданном), "Secret key verification failed"
(протухший api_sk). Успешный AddSite отвечает БЕЗ ключа `status` — {"siteStatus": true, ...}
(docs/api/aapanel.md, PDF p.6), и валидатор обязан такой ответ пропускать: валидатор, который
считает ошибкой законный ответ, хуже отсутствующего — он сломал бы работающий провижн.
"""
from types import SimpleNamespace

import httpx
import pytest

import app.db as db
from app.config import settings
from app.integrations.aapanel import AaPanelClient
from app.models.domain import Domain
from app.models.site import Site, Page

# --- конверты живой панели ---------------------------------------------------
ADD_OK = {"siteStatus": True, "ftpStatus": False, "databaseStatus": False}
AUTH_FAIL = {"status": False, "msg": "Secret key verification failed"}
FILE_EXISTS = {"status": False, "msg": "Requested file exists!"}   # НЕ отказ: идемпотентность
NO_SUCH_FILE = {"status": False, "msg": "Configuration file not exist"}
SAVE_OK = {"status": True, "msg": "Saved successfully!"}
CREATE_OK = {"status": True, "msg": "Created successfully!"}
LIST_EMPTY = {"data": [], "where": "type_id=0", "page": "<div>...</div>"}   # НЕ отказ: сайтов нет
LIST_EX_RU = {"data": [{"id": 7, "name": "ex.ru", "status": "1", "path": "/www/wwwroot/ex.ru"}],
              "where": "type_id=0", "page": "<div>...</div>"}
# AddSite на существующем домене: docs/api/aapanel.md §WEBSITE-add — «status/msg error (Chinese
# text like "网站已存在"/site already exists)». Текст НЕ выдуман и НЕ английский — именно поэтому
# отличать его от настоящего отказа по подстроке нельзя.
SITE_EXISTS = {"status": False, "msg": "网站已存在"}


@pytest.fixture(autouse=True)
def _loopback_panel(monkeypatch):
    """Клиент aaPanel fail-close'ит на не-loopback URL без CA-бандла, а в .env оператора лежит
    контейнерный путь к сертификату — тест не должен зависеть ни от того, ни от другого."""
    monkeypatch.setattr(settings, "AAPANEL_URL", "https://127.0.0.1:8888")
    monkeypatch.setattr(settings, "AAPANEL_CA_BUNDLE", "")
    monkeypatch.setattr(settings, "AAPANEL_API_KEY", "testsk")


# ============================ 1. транспорт: конверт ============================
#
# Подменяем `_client` (httpx-клиент ВНУТРИ BaseClient), а не `request`: так тело проходит через
# НАСТОЯЩИЕ BaseClient.request + @retry + resp.json() + _ok. Это и позволяет доказать, что отказ
# поднимается ВНЕ ретрая (иначе один «permission denied» = ТРИ попытки создать сайт).

class _Panel:
    """Живая панель на HTTP-уровне: 200 на всё, правда — в теле. Считает запросы."""

    def __init__(self, list_body=None, add=None, create=None, save=None, list_after_add=None):
        self.list_body = LIST_EMPTY if list_body is None else list_body
        # чем панель отвечает на getData ПОСЛЕ AddSite: так изображается гонка (сайт создал
        # параллельный свип/оператор ровно между нашим списком и нашим AddSite).
        self.list_after_add = list_after_add
        self.add = add or ADD_OK
        self.create = create or CREATE_OK
        self.save = save or SAVE_OK
        self.calls: list[str] = []

    def request(self, method, url, **kw):
        self.calls.append(url)
        if "getData" in url:
            body = (self.list_after_add
                    if self.list_after_add is not None and self.n("AddSite") else self.list_body)
        elif "AddSite" in url:
            body = self.add
        elif "CreateFile" in url:
            body = self.create
        elif "SaveFileBody" in url:
            body = self.save
        else:                                    # pragma: no cover — сторож теста, не путь кода
            raise AssertionError(f"тест не ждал такого вызова панели: {url}")
        return httpx.Response(200, json=body, request=httpx.Request(method, url))

    def close(self):
        pass

    def n(self, action: str) -> int:
        return sum(1 for u in self.calls if action in u)


def _client(panel: _Panel) -> AaPanelClient:
    c = AaPanelClient()
    c._client = panel
    return c


def test_add_site_refusal_raises():
    """РЕГРЕССИЯ. Протух api_sk — панель отвечает 200 + отказом в теле. Раньше add_site
    возвращал этот словарь как обычный результат, и провижн ехал дальше как ни в чём не бывало."""
    c = _client(_Panel(add=AUTH_FAIL))
    with pytest.raises(RuntimeError, match="Secret key verification failed"):
        c.add_site("ex.ru", "/www/wwwroot/ex.ru")


def test_refusal_is_raised_outside_retry():
    """Отказ — это НЕ повод повторить. Он поднимается после того, как request() вернул ответ,
    то есть вне @retry: иначе один отказ «нет прав» стал бы ТРЕМЯ попытками создать сайт
    (шум в панели и риск полусоздания). Ровно этот урок оплачен на A-Parser (Задача 4)."""
    p = _Panel(add=AUTH_FAIL)
    with pytest.raises(RuntimeError):
        _client(p).add_site("ex.ru", "/www/wwwroot/ex.ru")
    assert p.n("AddSite") == 1, p.calls


def test_post_recomputes_signature_on_each_retry_attempt(monkeypatch):
    """РЕГРЕССИЯ (S16, аудит 2026-07-18). Раньше payload (включая request_time/
    request_token) строился ОДИН раз до первой попытки — ретрай слал ту же, стареющую
    подпись. Теперь каждая попытка _post строит payload заново: request_time второй
    попытки должен отличаться от первой."""
    times = iter([1000.0, 2000.0, 3000.0, 4000.0, 5000.0, 6000.0])
    monkeypatch.setattr("app.integrations.aapanel.time.time", lambda: next(times))

    class _FlakyPanel:
        def __init__(self):
            self.seen_request_times = []

        def request(self, method, url, **kw):
            self.seen_request_times.append(kw.get("data", {}).get("request_time"))
            if len(self.seen_request_times) == 1:
                return httpx.Response(500, json={"msg": "boom"},
                                      request=httpx.Request(method, url))
            return httpx.Response(200, json=ADD_OK, request=httpx.Request(method, url))

        def close(self):
            pass

    panel = _FlakyPanel()
    c = _client(panel)
    c.add_site("ex.ru", "/www/wwwroot/ex.ru")   # первая попытка 500 -> ретрай -> вторая 200
    assert len(panel.seen_request_times) == 2
    assert panel.seen_request_times[0] != panel.seen_request_times[1]


def test_delete_site_refusal_raises():
    """S17 (аудит 2026-07-18). delete_site был ЕДИНСТВЕННЫМ write-методом без _ok(): панель
    отвечает 200 + {"status": false} на провал teardown (протух api_sk / нет прав), а метод
    возвращал это как успех — вызывающий M6 пометил бы сайт снесённым, пока vhost ещё жив.
    Теперь _ok() поднимает RuntimeError, как у всех соседей."""
    class _DelPanel(_Panel):
        def request(self, method, url, **kw):
            self.calls.append(url)
            assert "DeleteSite" in url
            return httpx.Response(200, json=AUTH_FAIL, request=httpx.Request(method, url))

    c = _client(_DelPanel())
    with pytest.raises(RuntimeError, match="Secret key verification failed"):
        c.delete_site("ex.ru", 7)


def test_delete_site_success_passes():
    """ГРАНИЦА: успешный DeleteSite ({"status": true, ...}) проходит без исключения."""
    class _DelOkPanel(_Panel):
        def request(self, method, url, **kw):
            self.calls.append(url)
            return httpx.Response(200, json={"status": True, "msg": "Deleted"},
                                  request=httpx.Request(method, url))

    assert _client(_DelOkPanel()).delete_site("ex.ru", 7) == {"status": True, "msg": "Deleted"}


def test_add_site_success_envelope_passes():
    """ГРАНИЦА. Успешный AddSite приходит БЕЗ ключа `status` ({"siteStatus": true, ...}).
    Валидатор, требующий `status is True`, объявил бы ошибкой каждый успешный провижн."""
    res = _client(_Panel()).add_site("ex.ru", "/www/wwwroot/ex.ru")
    assert res == ADD_OK


def test_list_sites_refusal_raises():
    """РЕГРЕССИЯ, и она про ИДЕМПОТЕНТНОСТЬ. На list_sites стоит check-before-create: отказ,
    принятый за «сайтов нет» (`res["data"]` нет -> []), заставлял ensure_site создавать уже
    существующий сайт — то есть ломал ровно то правило, ради которого этот список и читают."""
    c = _client(_Panel(list_body=AUTH_FAIL))
    with pytest.raises(RuntimeError, match="Secret key verification failed"):
        c.list_sites()


def test_list_sites_empty_is_not_a_failure():
    """ГРАНИЦА «пусто ≠ сбой»: на свежей панели сайтов нет, и это законный ответ."""
    assert _client(_Panel()).list_sites() == []


def test_ensure_site_still_skips_existing():
    """Контроль идемпотентности: сайт уже есть -> AddSite не зовём вовсе (и ничего не падает)."""
    p = _Panel(list_body=LIST_EX_RU)
    assert _client(p).ensure_site("ex.ru", "/www/wwwroot/ex.ru") == {"exists": True, "name": "ex.ru"}
    assert p.n("AddSite") == 0, p.calls


def test_ensure_site_survives_site_already_exists():
    """РЕГРЕССИЯ (минор ревью). Отказ «сайт уже есть» — это ДОСТИГНУТОЕ ЖЕЛАЕМОЕ СОСТОЯНИЕ, а не
    сбой. Сайт появился между нашим списком и нашим AddSite (параллельный свип, оператор руками;
    плюс каверат docs/api/aapanel.md: getData на части сборок показывает не все проекты) — и
    строгий фикс Задачи 9 стал ронять RuntimeError на ровном месте: сайт запирался в вечном
    `provisioning`, а свип каждый прогон писал одну и ту же ошибку.

    Отличить этот отказ от настоящего по тексту НЕЛЬЗЯ (msg китайский), поэтому спрашиваем
    панель ещё раз — она и есть источник правды."""
    p = _Panel(add=SITE_EXISTS, list_after_add=LIST_EX_RU)
    assert _client(p).ensure_site("ex.ru", "/www/wwwroot/ex.ru") == {"exists": True, "name": "ex.ru"}
    assert p.n("AddSite") == 1 and p.n("getData") == 2, p.calls   # спросили до и после


def test_ensure_site_still_raises_when_site_really_absent():
    """ГРАНИЦА. Перепроверка не смеет глотать НАСТОЯЩИЙ отказ: панель отказала (протух api_sk) —
    сайта в ней не появилось, и повторный список это подтверждает. RuntimeError летит наверх."""
    p = _Panel(add=AUTH_FAIL, list_after_add=LIST_EMPTY)
    with pytest.raises(RuntimeError, match="Secret key verification failed"):
        _client(p).ensure_site("ex.ru", "/www/wwwroot/ex.ru")
    assert p.n("AddSite") == 1, p.calls          # и повторять AddSite не пытаемся


def test_write_file_refusal_raises():
    """РЕГРЕССИЯ. SaveFileBody отказал — страница НЕ на диске. Раньше write_file возвращал
    отказ словарём, publish_site его не смотрел и штамповал страницу `published`."""
    c = _client(_Panel(save=NO_SUCH_FILE))
    with pytest.raises(RuntimeError, match="Configuration file not exist"):
        c.write_file("/www/wwwroot/ex.ru/index.html", "<h1>hi</h1>")


def test_write_file_existing_file_is_success_not_failure():
    """ГРАНИЦА, которую легко перейти и убить идемпотентность. При ПОВТОРНОЙ публикации
    CreateFile отвечает «Requested file exists!» — тем же `status: false`, что и настоящий
    отказ. Это УСПЕХ: файл на месте, SaveFileBody перезапишет тело. Валидатор, который поднял
    бы здесь исключение, ломал бы каждую вторую публикацию."""
    p = _Panel(create=FILE_EXISTS, save=SAVE_OK)
    assert _client(p).write_file("/www/wwwroot/ex.ru/index.html", "<h1>hi</h1>") == SAVE_OK
    assert p.n("SaveFileBody") == 1, p.calls


def test_write_file_carries_createfile_reason():
    """Настоящий отказ CreateFile (нет прав, диск полон) не проскакивает: файла не появилось,
    SaveFileBody падает «Configuration file not exist» — и оператор должен увидеть ПЕРВОПРИЧИНУ,
    а не только последнее звено цепочки."""
    c = _client(_Panel(create=AUTH_FAIL, save=NO_SUCH_FILE))
    with pytest.raises(RuntimeError) as e:
        c.write_file("/www/wwwroot/ex.ru/vs/index.html", "<h1>hi</h1>")
    assert "Configuration file not exist" in str(e.value)
    assert "Secret key verification failed" in str(e.value), str(e.value)


# ============================ 2. провижн (M3) ============================

def _fake_post(routes: dict):
    """Подмена `_post`: методы клиента (ensure_site/write_file) и их _ok работают настоящие,
    фальшивый только HTTP. Ключ маршрута — фрагмент action, как в реальном URL."""
    def _post(self, path, data=None):
        for frag, body in routes.items():
            if frag in path:
                return body
        raise AssertionError(f"тест не ждал такого вызова панели: {path}")   # pragma: no cover
    return _post


class _CF:
    """Cloudflare: зона активна, A-запись ставится. set_ssl падает, если попросили.

    `ssl_current_mode` имитирует ТЕКУЩИЙ режим зоны, который provision() читает через
    get_zone_setting перед set_ssl (S6, аудит 2026-07-18). Дефолт "off" — «ещё не
    настроено»: сохраняет поведение существующих тестов, которые НЕ передают этот
    параметр и ждут, что set_ssl всё равно позовётся ("off" не входит в ("full",
    "strict"))."""

    def __init__(self, ssl_boom: Exception | None = None, ssl_current_mode: str = "off"):
        self.ssl_boom = ssl_boom
        self.ssl_calls = 0
        self.ssl_current_mode = ssl_current_mode

    def ensure_zone(self, domain):
        return {"id": "zone1", "status": "active", "name_servers": ["a.ns.cf", "b.ns.cf"]}

    def get_zone(self, zid):
        return {"id": zid, "status": "active"}

    def ensure_a_record(self, zid, name, ip, proxied=True):
        return {"id": "rec1"}

    def get_zone_setting(self, zid, setting_id):
        return {"value": self.ssl_current_mode}

    def set_ssl(self, zid, mode="full"):
        self.ssl_calls += 1
        if self.ssl_boom:
            raise self.ssl_boom
        return True


def _seed_site(page_statuses=()) -> int:
    with db.SessionLocal() as s:
        d = Domain(domain="ex.ru", source="backorder", status="purchased")
        s.add(d)
        s.commit()
        s.refresh(d)
        site = Site(domain_id=d.id, status="provisioning", doc_root="/www/wwwroot/ex.ru")
        s.add(site)
        s.commit()
        s.refresh(site)
        for i, st in enumerate(page_statuses):
            s.add(Page(site_id=site.id, url_path="/" if i == 0 else f"/p{i}",
                       title=f"стр {i}", status=st, body="<p>текст</p>"))
        s.commit()
        return site.id


def _panel_env(monkeypatch, cf=None, **routes):
    monkeypatch.setattr(settings, "VPS_ORIGIN_IP", "185.201.252.187")
    cf = cf or _CF()
    monkeypatch.setattr("app.integrations.cloudflare.CloudflareClient", lambda: cf)
    monkeypatch.setattr(AaPanelClient, "_post", _fake_post(routes))
    return cf


def test_provision_aapanel_refusal_keeps_site_in_provisioning(monkeypatch):
    """РЕГРЕССИЯ, сквозная. Панель отказала на AddSite — vhost'а НЕТ. До фикса provision
    возвращал {"status": "provisioned"}, а сайт уезжал в `content`: карточка показывала
    «инфраструктура готова», и человек шёл писать контент для сайта, которого не существует."""
    from app.services import provisioning
    _panel_env(monkeypatch, getData=LIST_EMPTY, AddSite=AUTH_FAIL)
    sid = _seed_site()

    with pytest.raises(RuntimeError, match="Secret key verification failed"):
        provisioning.provision(sid)

    with db.SessionLocal() as s:
        site = s.get(Site, sid)
        assert site.status == "provisioning"        # не «content» — машина не врёт о готовности
        assert site.aapanel_site_name is None
        assert site.cf_zone_id == "zone1"           # шаг 1 закоммичен — повтор доедет с него


def test_provision_after_failure_finishes_on_retry(monkeypatch):
    """Провижн ДОВОДИТ дело: оператор чинит причину (обновляет api_sk), жмёт Provision ещё раз —
    и сайт доезжает до `content` с того же места. Первый заход упал на AddSite, то есть сайта в
    панели НЕ создалось: список по-прежнему пуст, и AddSite на повторе законно зовётся — теперь
    успешно. (Про «сайт уже есть, AddSite не зовём» — соседний тест ниже.)"""
    from app.services import provisioning
    _panel_env(monkeypatch, getData=LIST_EMPTY, AddSite=AUTH_FAIL)
    sid = _seed_site()
    with pytest.raises(RuntimeError):
        provisioning.provision(sid)

    # панель починена; сайт с первого (упавшего) захода в ней не создался
    _panel_env(monkeypatch, getData=LIST_EMPTY, AddSite=ADD_OK)
    out = provisioning.provision(sid)

    assert out["status"] == "provisioned"
    with db.SessionLocal() as s:
        site = s.get(Site, sid)
        assert site.status == "content" and site.aapanel_site_name == "ex.ru"
        assert site.ssl_error is None


def test_provision_of_existing_site_never_calls_addsite(monkeypatch):
    """СТОРОЖ ИДЕМПОТЕНТНОСТИ ПРОВИЖНА (а не регрессия бага: она держится и на 0265ac3 — но
    держалась НИ ОДНИМ тестом не прикрытой). Правило проекта «провижн идемпотентен» опирается
    ровно на этот путь: свип зовёт provision повторно, оператор жмёт кнопку второй раз, прошлый
    заход умер ПОСЛЕ AddSite (сайт в панели есть, а Site так и висит в `provisioning`).

    Маршрут AddSite тесту не дан ВОВСЕ: сентинел `_fake_post` уронит AssertionError, если клиент
    к нему полезет. Дубликата сайта в панели быть не может."""
    from app.services import provisioning
    _panel_env(monkeypatch, getData=LIST_EX_RU)          # AddSite не объявлен — вызов = провал теста
    sid = _seed_site()

    out = provisioning.provision(sid)

    assert out["status"] == "provisioned"
    with db.SessionLocal() as s:
        site = s.get(Site, sid)
        assert site.status == "content" and site.aapanel_site_name == "ex.ru"


def test_provision_records_ssl_error(monkeypatch):
    """РЕГРЕССИЯ (F16). Смена SSL-режима падает — раньше это глотал `except Exception: pass`,
    и оператор видел зелёное «Provision готов: DNS + vhost + SSL». При origin, слушающем только
    :80, именно этот режим решает, поедет ли HTTPS: посетитель получал ошибку Cloudflare, а
    панель — галочку. Vhost при этом РАБОТАЕТ, поэтому провижн не роняем: говорим правду."""
    from app.services import provisioning
    cf = _CF(ssl_boom=RuntimeError("Cloudflare 403: Authentication error"))
    _panel_env(monkeypatch, cf=cf, getData=LIST_EMPTY, AddSite=ADD_OK)
    sid = _seed_site()

    out = provisioning.provision(sid)

    assert out["status"] == "provisioned"                     # vhost поднят — не «error»
    assert "Cloudflare 403" in out["ssl_error"], out
    with db.SessionLocal() as s:
        site = s.get(Site, sid)
        assert site.status == "content"
        assert "Cloudflare 403" in site.ssl_error             # след живёт на карточке сайта


def test_provision_clears_ssl_error_when_ssl_recovers(monkeypatch):
    """Починенное не должно висеть вечным обвинением: удачный повтор затирает прошлый ssl_error."""
    from app.services import provisioning
    sid = _seed_site()
    with db.SessionLocal() as s:
        s.get(Site, sid).ssl_error = "RuntimeError: Cloudflare 403"
        s.commit()

    _panel_env(monkeypatch, getData=LIST_EMPTY, AddSite=ADD_OK)
    out = provisioning.provision(sid)

    assert "ssl_error" not in out
    with db.SessionLocal() as s:
        assert s.get(Site, sid).ssl_error is None


def test_provision_does_not_downgrade_strict_ssl(monkeypatch):
    """РЕГРЕССИЯ (S6, аудит 2026-07-18). provision() идемпотентен и может звонить повторно
    (свип, повторное нажатие кнопки) — а безусловный `set_ssl(zone, "full")` на каждый
    такой повтор молча откатывал режим, который оператор осознанно поднял до "strict"
    после установки origin-сертификата. read-then-set: текущий режим уже "strict" ->
    ничего не трогаем, set_ssl вовсе не зовётся."""
    from app.services import provisioning
    cf = _CF(ssl_current_mode="strict")
    _panel_env(monkeypatch, cf=cf, getData=LIST_EMPTY, AddSite=ADD_OK)
    sid = _seed_site()

    out = provisioning.provision(sid)

    assert out["status"] == "provisioned"
    assert cf.ssl_calls == 0                     # strict не откатился на full
    with db.SessionLocal() as s:
        site = s.get(Site, sid)
        assert site.status == "content"
        assert site.ssl_error is None             # осознанный no-op — не ошибка


def test_provision_still_sets_ssl_when_off(monkeypatch):
    """ГРАНИЦА рядом с фиксом S6: домен, у которого SSL ещё не настроен ("off"/"flexible"),
    по-прежнему получает set_ssl("full") — read-then-set не превращается в «никогда не
    ставим SSL». Дефолт `_CF()` уже "off", тест лишь подтверждает поведение явно."""
    from app.services import provisioning
    cf = _CF(ssl_current_mode="off")
    _panel_env(monkeypatch, cf=cf, getData=LIST_EMPTY, AddSite=ADD_OK)
    sid = _seed_site()

    out = provisioning.provision(sid)

    assert out["status"] == "provisioned"
    assert cf.ssl_calls == 1
    with db.SessionLocal() as s:
        assert s.get(Site, sid).ssl_error is None


def test_sweep_reports_ssl_failure_instead_of_clean_done(monkeypatch):
    """РЕГРЕССИЯ (минор ревью). На автопилоте провал SSL ЗВУЧИТ. Провижн сайта с упавшим
    set_ssl — это `done` (vhost поднят, ронять нечего), и отчёт свипа выглядел безупречно
    чистым: «провижн: 1, ошибок нет». Между тем HTTPS под вопросом. След был в БД и на карточке
    сайта — но именно автопилотный прогон, который этот сайт и трогал, о нём молчал.
    Счётчик рядом с `queue_dirty`: стадия отчитывается не только числом сделанного."""
    from app.services import autonomy, orchestrator
    cf = _CF(ssl_boom=RuntimeError("Cloudflare 403: Authentication error"))
    _panel_env(monkeypatch, cf=cf, getData=LIST_EMPTY, AddSite=ADD_OK)
    _seed_site()
    autonomy.update_autonomy(autopilot_on=True, auto_provision=True)

    out = orchestrator.run_sweep(trigger="cron")

    assert out["counts"]["provision"] == 1        # vhost поднят — это не ошибка стадии
    assert out["counts"]["ssl_failed"] == 1       # ...но и не чистый успех, и свип это говорит
    assert out["errors"] == []
    assert orchestrator.COUNT_RU["ssl_failed"]    # у счётчика есть русская подпись в журнале


def test_site_card_shows_ssl_error(client, monkeypatch):
    """Молчаливое поле в JSON никто не прочтёт. Провал SSL живёт ТАМ, где человек смотрит на
    сайт, — на шаге 3 карточки /sites/{id}, рядом с кнопкой, которой его чинят."""
    sid = _seed_site()
    with db.SessionLocal() as s:
        site = s.get(Site, sid)
        site.status = "content"
        site.ssl_error = "RuntimeError: Cloudflare 403: Authentication error"
        s.commit()

    html = client.get(f"/sites/{sid}").text
    assert "SSL-режим Cloudflare не переключился" in html
    assert "Cloudflare 403" in html


# ============================ 3. публикация (M5) ============================

def test_publish_aapanel_refusal_keeps_pages_edited(monkeypatch):
    """РЕГРЕССИЯ, сквозная. Панель отказала на записи файла — в docroot ПУСТО. До фикса
    страница получала `published`, сайт — `published`, и проверка индексации потом искала в
    поисковике страницу, которой нет. Гейт редактуры не сдвинут: статус остаётся `edited`,
    публикация просто честно не состоялась."""
    from app.services import publish
    _panel_env(monkeypatch, CreateFile=CREATE_OK, SaveFileBody=NO_SUCH_FILE)
    sid = _seed_site(page_statuses=("edited",))

    with pytest.raises(RuntimeError, match="Configuration file not exist"):
        publish.publish_site(sid)

    with db.SessionLocal() as s:
        site = s.get(Site, sid)
        page = s.query(Page).filter_by(site_id=sid).one()
        assert page.status == "edited" and page.published_at is None
        assert site.status != "published" and site.published_at is None


def test_publish_partial_failure_publishes_nothing(monkeypatch):
    """Отказ на ВТОРОЙ странице: первая уже легла на диск, но в БД `published` не получает
    никто — транзакция откатывается целиком. Рассинхрона нет: write_file идемпотентен
    (CreateFile+SaveFileBody перезаписывают тело), повтор просто положит первую страницу снова.
    Лучше записать дважды, чем соврать один раз."""
    from app.services import publish
    written = []

    def _post(self, path, data=None):
        if "CreateFile" in path:
            return CREATE_OK
        if "SaveFileBody" in path:
            if data["path"].endswith("/p1/index.html"):
                return NO_SUCH_FILE
            written.append(data["path"])
            return SAVE_OK
        raise AssertionError(path)   # pragma: no cover

    monkeypatch.setattr(AaPanelClient, "_post", _post)
    sid = _seed_site(page_statuses=("edited", "edited"))

    with pytest.raises(RuntimeError):
        publish.publish_site(sid)

    assert written == ["/www/wwwroot/ex.ru/index.html"]        # первая реально записана
    with db.SessionLocal() as s:
        assert [p.status for p in s.query(Page).filter_by(site_id=sid).order_by(Page.id)] \
            == ["edited", "edited"]
        assert s.get(Site, sid).status != "published"


def test_publish_still_publishes_when_panel_answers(monkeypatch):
    """Контроль: живая панель — публикация работает как раньше. Иначе «фикс» просто запретил
    бы публиковать вообще."""
    from app.services import publish
    _panel_env(monkeypatch, CreateFile=CREATE_OK, SaveFileBody=SAVE_OK)
    sid = _seed_site(page_statuses=("edited", "draft"))

    out = publish.publish_site(sid)

    assert out["status"] == "published" and out["pages"] == ["/"]
    with db.SessionLocal() as s:
        statuses = sorted(p.status for p in s.query(Page).filter_by(site_id=sid))
        assert statuses == ["draft", "published"]              # ГЕЙТ: draft наружу не вышел
        assert s.get(Site, sid).status == "published"


def test_publish_gate_untouched_without_edited_pages(monkeypatch):
    """Гейт редактуры на месте и панель ради него не дёргается: одни черновики — публикации нет."""
    from app.services import publish
    calls = []
    monkeypatch.setattr(AaPanelClient, "_post",
                        lambda self, path, data=None: calls.append(path) or SAVE_OK)
    sid = _seed_site(page_statuses=("draft",))

    assert publish.publish_site(sid)["status"] == "no_edited_pages"
    assert calls == []


def test_fake_panel_shape_matches_the_real_client():
    """Сторож фикстур (урок ветки, оплаченный шесть раз): конверты выше — не фантазия из брифа,
    а то, что реально парсит клиент. Успех AddSite без ключа `status`, отказ — с `status: false`
    и текстом в `msg`; getData отдаёт строки в `data`. Если форма ответа панели изменится,
    падать должен ЭТОТ тест, а не молча зеленеть остальные."""
    from app.integrations.aapanel import _fail_msg
    assert _fail_msg(ADD_OK) is None                  # успех БЕЗ `status` — не отказ
    assert _fail_msg(LIST_EMPTY) is None              # пустой список — не отказ
    assert _fail_msg(0) is None                       # GetTaskCount отдаёт голый int
    assert _fail_msg(SAVE_OK) is None
    assert _fail_msg(AUTH_FAIL) == "Secret key verification failed"
    assert _fail_msg(FILE_EXISTS) == "Requested file exists!"
    # и это ровно те поля, по которым клиент достаёт данные из живого ответа
    assert isinstance(LIST_EMPTY["data"], list) and SimpleNamespace(**ADD_OK).siteStatus is True
