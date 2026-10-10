"""«Ключи и сервисы»: переопределения из БД поверх .env, экран /settings/keys, секреты не утекают."""
import pathlib
import threading
import time

import pytest
from sqlalchemy import text

import app.db as db
from app.config import settings
from app.models.secret import SecretOverride


@pytest.fixture
def ak(key_overrides):
    return key_overrides


@pytest.fixture(autouse=True)
def _panel_auth(request, monkeypatch):
    """POST /settings/keys требует настроенного Basic-auth (PANEL_USER/PANEL_PASS) — тестовый клиент
    логинится. Тест на «auth не настроен» сам обнуляет оба поля и client.auth."""
    if "client" not in request.fixturenames:
        return
    monkeypatch.setattr(settings, "PANEL_USER", "op")
    monkeypatch.setattr(settings, "PANEL_PASS", "pw")
    request.getfixturevalue("client").auth = ("op", "pw")


def _rows() -> dict:
    with db.SessionLocal() as s:
        return {r.key: r.value for r in s.query(SecretOverride).all()}


def _post(client, **fields):
    return client.post("/settings/keys", data=fields, follow_redirects=False)


# ---- (а) override из БД побеждает .env и сбрасывается чекбоксом ----

def test_override_beats_env_and_reset_checkbox_restores_env(client, ak):
    env = settings.env_value("LLM_MODEL")
    assert settings.LLM_MODEL == env
    r = _post(client, v_LLM_MODEL="my-model")
    assert r.status_code == 303
    assert settings.LLM_MODEL == "my-model"
    assert settings.env_value("LLM_MODEL") == env            # .env не тронут
    r = _post(client, r_LLM_MODEL="1")
    assert r.status_code == 303
    assert settings.LLM_MODEL == env
    assert "LLM_MODEL" not in _rows()


def test_override_reaches_integration_clients(client, ak):
    from app.integrations.llm import LlmClient
    _post(client, v_LLM_BASE_URL="http://llm.example:4000", v_LLM_MODEL="m1")
    c = LlmClient()
    assert c.base_url == "http://llm.example:4000" and c.model == "m1"


def test_value_and_reset_together_is_rejected(client, ak):
    r = _post(client, v_LLM_MODEL="x", r_LLM_MODEL="1")
    assert r.status_code == 400
    assert _rows() == {}


# ---- (б) нет таблицы / сбой БД -> .env ----

def test_missing_table_falls_back_to_env(ak):
    with db.engine.begin() as c:
        c.execute(text("DROP TABLE secret_override"))
    ak.invalidate()
    assert settings.LLM_MODEL == settings.env_value("LLM_MODEL")      # ничего не упало


def test_db_error_falls_back_to_env(ak, monkeypatch):
    def boom():
        raise RuntimeError("db down")
    monkeypatch.setattr(ak, "_load", boom)
    ak.invalidate()
    assert settings.LLM_MODEL == settings.env_value("LLM_MODEL")


def test_save_without_table_gives_safe_error(client, ak):
    with db.engine.begin() as c:
        c.execute(text("DROP TABLE secret_override"))
    r = _post(client, v_LLM_MODEL="TOPSECRETVALUE")
    assert r.status_code == 500
    assert "0026_secret_override" in r.text and "TOPSECRETVALUE" not in r.text


# ---- (в) пустое поле не стирает ----

def test_empty_field_does_not_erase(client, ak):
    _post(client, v_LLM_MODEL="keep-me")
    r = _post(client, v_LLM_MODEL="", v_LLM_BASE_URL="   ")
    assert r.status_code == 303 and "msg=" in r.headers["location"]
    assert _rows() == {"LLM_MODEL": "keep-me"}
    assert settings.LLM_MODEL == "keep-me"


def test_values_are_trimmed(client, ak):
    _post(client, v_LLM_MODEL="  spaced  ")
    assert _rows()["LLM_MODEL"] == "spaced"


# ---- (г) секрет в HTML только маской, не-секрет — целиком ----

def test_secret_masked_nonsecret_shown_in_html(client, ak):
    secret = "sk-ABCDEFGHIJKLMNOP-1234"
    _post(client, v_AHREFS_API_KEY=secret, v_LLM_BASE_URL="http://llm.example:4000",
          v_AAPANEL_API_KEY="short")
    html = client.get("/settings/keys").text
    assert secret not in html and "ABCDEFGHIJKLMNOP" not in html
    assert "••••1234" in html
    assert "http://llm.example:4000" in html
    assert "short" not in html.replace("не менять", "")          # короткий секрет: хвост тоже скрыт
    assert 'type="password" name="v_AHREFS_API_KEY"' in html


def test_flash_and_redirect_never_contain_values(client, ak):
    r = _post(client, v_AHREFS_API_KEY="sk-LEAKCHECK-123456789")
    loc = r.headers["location"]
    assert "AHREFS_API_KEY" in loc and "LEAKCHECK" not in loc
    page = client.get(loc).text
    assert "LEAKCHECK" not in page


def test_gsc_json_is_masked(client, ak):
    blob = '{"type": "service_account", "private_key": "-----BEGIN KEY-----ZZZ"}'
    r = _post(client, v_GSC_SERVICE_ACCOUNT_JSON=blob)
    assert r.status_code == 303
    html = client.get("/settings/keys").text
    assert "private_key" not in html and "BEGIN KEY" not in html


def test_source_labels(client, ak):
    html = client.get("/settings/keys").text
    assert "не задан" in html
    _post(client, v_LLM_MODEL="abc")
    assert 'title="значение из БД, вписано в панели"' in client.get("/settings/keys").text


def test_page_links_present(client):
    assert "/settings/keys" in client.get("/settings").text
    assert "/settings/keys" in client.get("/settings/cloudflare").text
    r = client.get("/settings/keys?msg=ok")
    assert 'href="/diag"' in r.text


# ---- (д) валидация ----

@pytest.mark.parametrize("field,value", [
    ("v_LLM_BASE_URL", "ftp://x.example"),
    ("v_LLM_BASE_URL", "llm.example:4000"),
    ("v_LLM_BASE_URL", "http://"),
    ("v_LLM_MODEL", "line1\nline2"),
    ("v_LLM_MODEL", "tab\x00null"),
    ("v_LLM_MODEL", "a" * 513),
    ("v_VPS_ORIGIN_IP", "not-an-ip"),
    ("v_LLM_THINK", "maybe"),
    ("v_LLM_BASE_URL", "http://[::1"),                          # urlsplit -> ValueError, не 500
    ("v_GSC_SERVICE_ACCOUNT_JSON", '{"a":' + "[" * 5000),     # RecursionError в json.loads
    ("v_GSC_SERVICE_ACCOUNT_JSON", "not json"),
    ("v_GSC_SERVICE_ACCOUNT_JSON", "[1, 2]"),
])
def test_validation_rejects(client, ak, field, value):
    r = _post(client, **{field: value, "v_LLM_API_KEY": "valid-key-1"})
    assert r.status_code == 400
    assert "Не сохранено ничего" in r.text
    assert _rows() == {}                                  # всё или ничего: валидное соседнее тоже не записано


def test_validation_accepts_good_values(client, ak):
    big_json = '{"type": "service_account",\n "k": "' + "x" * 3000 + '"}'
    r = _post(client, v_LLM_BASE_URL="https://llm.example/v1", v_VPS_ORIGIN_IP="203.0.113.5",
              v_DNS_RESOLVER="10.0.0.2", v_LLM_THINK="true",
              v_GSC_SERVICE_ACCOUNT_JSON=big_json)
    assert r.status_code == 303
    assert _rows()["GSC_SERVICE_ACCOUNT_JSON"] == big_json        # переводы строк внутри JSON допустимы


def test_validation_error_keeps_nonsecret_draft_not_secrets(client, ak):
    r = _post(client, v_LLM_BASE_URL="bad", v_LLM_MODEL="draft-model", v_AHREFS_API_KEY="sk-DRAFTSECRET-999999")
    assert r.status_code == 400
    assert "draft-model" in r.text and "DRAFTSECRET" not in r.text
    assert "LLM_BASE_URL" in r.text                       # ошибка называет поле, но не значение


# ---- (е) вне белого списка не пишется ----

def test_non_whitelisted_keys_are_not_written(client, ak):
    pass_before = settings.PANEL_PASS
    r = _post(client, v_PANEL_PASS="hacked", PANEL_PASS="hacked", v_DATABASE_URL="sqlite://x",
              DATABASE_URL="x", v_APP_ENV="prod", v_CLOUDFLARE_SECRETS_DIR="/etc", v_RANDOM_NAME="x",
              r_PANEL_PASS="1", r_DATABASE_URL="1")
    assert r.status_code == 303 and "Ничего не изменено" in r.headers["location"] or "%D0%9D%D0%B8%D1%87" in r.headers["location"]
    assert _rows() == {}
    assert settings.PANEL_PASS == pass_before


def test_save_rejects_forbidden_keys_directly(ak):
    for k in ("PANEL_PASS", "PANEL_USER", "DATABASE_URL", "APP_ENV", "CLOUDFLARE_SECRETS_DIR",
              "GITHUB_REPO", "APARSER_PROXY_CHECKER", "NOPE"):
        with pytest.raises(ValueError):
            ak.save({k: "x"})
        with pytest.raises(ValueError):
            ak.save({}, resets={k})
    assert _rows() == {}


def test_row_for_forbidden_key_in_db_is_ignored(ak):
    """Даже если строка для запретного ключа попала в таблицу в обход панели — Settings её не видит."""
    with db.SessionLocal() as s:
        s.add(SecretOverride(key="PANEL_PASS", value="evil"))
        s.add(SecretOverride(key="DATABASE_URL", value="evil"))
        s.commit()
    ak.invalidate()
    assert settings.PANEL_PASS != "evil" and settings.DATABASE_URL != "evil"


def test_whitelist_excludes_locked_fields():
    from app.config import NOT_EDITABLE
    from app.services import api_keys
    assert not (set(api_keys.EDITABLE) & NOT_EDITABLE)
    assert {"PANEL_PASS", "PANEL_USER", "DATABASE_URL", "APP_ENV", "CLOUDFLARE_SECRETS_DIR",
            "GITHUB_REPO"} <= NOT_EDITABLE


def test_whitelist_matches_settings_fields():
    from app.services import api_keys
    assert set(api_keys.EDITABLE) <= set(type(settings).model_fields)


# ---- (ж) _scrub затирает override-секрет ----

def test_scrub_masks_override_secret(client, ak):
    from app.services import diagnostics
    _post(client, v_AHREFS_API_KEY="sk-SCRUBME-0123456789", v_GSC_SERVICE_ACCOUNT_JSON='{"a": "b"}')
    out = diagnostics._scrub("HTTPStatusError ...?api_key=sk-SCRUBME-0123456789 failed")
    assert "SCRUBME" not in out and "***" in out


def test_scrub_also_masks_old_env_value(ak, monkeypatch):
    from app.services import diagnostics
    monkeypatch.setattr(settings, "LLM_API_KEY", "old-env-key-1234")
    with db.SessionLocal() as s:
        s.add(SecretOverride(key="LLM_API_KEY", value="new-db-key-5678"))
        s.commit()
    ak.invalidate()
    assert settings.LLM_API_KEY == "new-db-key-5678"
    out = diagnostics._scrub("old-env-key-1234 / new-db-key-5678")
    assert "old-env" not in out and "new-db" not in out


# ---- (з) TTL-кэш ----

def test_save_is_visible_immediately_in_this_process(client, ak):
    assert settings.LLM_MODEL == settings.env_value("LLM_MODEL")      # кэш прогрет (пустой)
    _post(client, v_LLM_MODEL="instant")
    assert settings.LLM_MODEL == "instant"                            # без ожидания TTL


def test_cache_expires_by_ttl_for_foreign_writers(ak, monkeypatch):
    """Правка из ДРУГОГО процесса (worker/панель) видна не позже TTL."""
    assert settings.LLM_MODEL == settings.env_value("LLM_MODEL")
    with db.SessionLocal() as s:
        s.add(SecretOverride(key="LLM_MODEL", value="from-other-process"))
        s.commit()
    assert settings.LLM_MODEL == settings.env_value("LLM_MODEL")      # ещё в кэше
    monkeypatch.setattr(ak, "_loaded_at", time.monotonic() - ak.TTL - 1)
    assert settings.LLM_MODEL == "from-other-process"


def test_cache_is_not_hit_on_every_read(ak, monkeypatch):
    calls = {"n": 0}
    real = ak._load

    def counting():
        calls["n"] += 1
        return real()
    monkeypatch.setattr(ak, "_load", counting)
    ak.invalidate()
    for _ in range(50):
        settings.LLM_MODEL, settings.LLM_BASE_URL, settings.AHREFS_API_KEY
    assert calls["n"] == 1


def test_no_recursion_when_load_reads_settings(ak, monkeypatch):
    """_load, прочитавший settings.*, не должен зациклиться/задедлочиться. При регрессии тест ПАДАЕТ
    (join с таймаутом), а не вешает весь сьют."""
    def reentrant():
        return {"LLM_MODEL": settings.LLM_API_KEY or "x"}
    monkeypatch.setattr(ak, "_load", reentrant)
    ak.invalidate()
    out = []
    t = threading.Thread(target=lambda: out.append(settings.LLM_MODEL), daemon=True)
    t.start()
    t.join(5)
    assert not t.is_alive(), "чтение settings зависло (рекурсия/дедлок в _snapshot)"
    assert out == ["x"]


# ---- сбой БД не сбрасывает последний снимок (особенно BACKORDER_*) ----

def _expire(ak, monkeypatch):
    monkeypatch.setattr(ak, "_loaded_at", time.monotonic() - ak.TTL - 1)


def _boom():
    raise RuntimeError("db down")


def test_db_failure_keeps_last_good_snapshot(client, ak, monkeypatch):
    _post(client, v_BACKORDER_LOGIN="db-login", v_BACKORDER_PASSWORD="db-pass-123456", v_LLM_MODEL="m-db")
    assert settings.BACKORDER_LOGIN == "db-login"               # снимок успешно загружен
    monkeypatch.setattr(ak, "_load", _boom)
    _expire(ak, monkeypatch)
    assert settings.BACKORDER_LOGIN == "db-login"               # сбой -> override НЕ пропал
    assert settings.BACKORDER_PASSWORD == "db-pass-123456" and settings.LLM_MODEL == "m-db"
    # пауза перед повтором: сразу следующее чтение БД не дёргает
    calls = []
    monkeypatch.setattr(ak, "_load", lambda: calls.append(1) or _boom())
    settings.BACKORDER_LOGIN
    assert calls == []
    # БД вернулась — снимок обновился
    monkeypatch.setattr(ak, "_load", lambda: {"BACKORDER_LOGIN": "new"})
    _expire(ak, monkeypatch)
    assert settings.BACKORDER_LOGIN == "new"


def test_failure_before_any_success_falls_back_to_env(ak, monkeypatch):
    monkeypatch.setattr(ak, "_load", _boom)
    ak.invalidate()
    monkeypatch.setattr(ak, "_last_good", None)
    assert settings.LLM_MODEL == settings.env_value("LLM_MODEL")


# ---- stale-while-revalidate и поколение: детерминированно, спай-замком, не таймингом ----

class _SpyLock:
    def __init__(self, nonblocking_result=True):
        self.blocking_acquires = 0
        self.nonblocking_calls = 0
        self.nonblocking_result = nonblocking_result
        self.releases = 0

    def acquire(self, blocking=True, *a):
        if blocking:
            self.blocking_acquires += 1
            return True
        self.nonblocking_calls += 1
        return self.nonblocking_result

    def release(self):
        self.releases += 1


def test_stale_reader_does_not_wait_while_another_thread_refreshes(ak, monkeypatch):
    monkeypatch.setattr(ak, "_load", lambda: {"LLM_MODEL": "v1"})
    ak.invalidate()
    assert settings.LLM_MODEL == "v1"                           # снимок есть
    _expire(ak, monkeypatch)
    spy = _SpyLock(nonblocking_result=False)                    # «обновляет другой поток»
    monkeypatch.setattr(ak, "_lock", spy)
    loads = []
    monkeypatch.setattr(ak, "_load", lambda: loads.append(1) or {"LLM_MODEL": "v2"})
    assert settings.LLM_MODEL == "v1"                           # отдали устаревший, не ждали
    assert spy.blocking_acquires == 0 and spy.nonblocking_calls == 1 and loads == []


def test_stale_refresher_loads_once_and_releases(ak, monkeypatch):
    monkeypatch.setattr(ak, "_load", lambda: {"LLM_MODEL": "v1"})
    ak.invalidate()
    settings.LLM_MODEL
    _expire(ak, monkeypatch)
    spy = _SpyLock(nonblocking_result=True)
    monkeypatch.setattr(ak, "_lock", spy)
    monkeypatch.setattr(ak, "_load", lambda: {"LLM_MODEL": "v2"})
    assert settings.LLM_MODEL == "v2"
    assert spy.blocking_acquires == 0 and spy.releases == 1


def test_no_snapshot_reader_blocks_for_first_load(ak, monkeypatch):
    ak.invalidate()                                             # снимка нет вовсе -> ждём свежего
    spy = _SpyLock()
    monkeypatch.setattr(ak, "_lock", spy)
    monkeypatch.setattr(ak, "_load", lambda: {"LLM_MODEL": "first"})
    assert settings.LLM_MODEL == "first"
    assert spy.blocking_acquires == 1 and spy.releases == 1


def test_invalidate_during_load_discards_stale_result(ak, monkeypatch):
    """save() commit + invalidate() случились, пока чужой поток читал таблицу: его результат
    (до save) не должен лечь в кэш и «отменить» свежую запись на TTL."""
    state = {"db": {"LLM_MODEL": "old"}, "n": 0}

    def load():
        state["n"] += 1
        snapshot = dict(state["db"])                  # поток прочитал таблицу...
        if state["n"] == 1:
            state["db"] = {"LLM_MODEL": "saved"}      # ...пока шёл save() в этом процессе
            ak.invalidate()
        return snapshot

    monkeypatch.setattr(ak, "_load", load)
    ak.invalidate()
    assert settings.LLM_MODEL == "old"                # тот самый читатель получил то, что прочитал
    assert settings.LLM_MODEL == "saved"              # но кэш не отравлен: следующее чтение — свежее
    assert state["n"] == 2


def test_overrides_disabled_by_default_in_tests(client):
    """Автоюз-фикстура держит механизм выключенным: запись в БД на чтение не влияет."""
    with db.SessionLocal() as s:
        s.add(SecretOverride(key="LLM_MODEL", value="ignored"))
        s.commit()
    assert settings.LLM_MODEL == settings.env_value("LLM_MODEL")


# ---- побочные места, где подмена должна подействовать без рестарта ----

def test_blacklist_control_cache_invalidates_on_resolver_change(monkeypatch):
    from app.integrations import blacklist
    B = blacklist.BlacklistClient
    monkeypatch.setattr(B, "_control_ok", None)
    monkeypatch.setattr(B, "_control_key", None)
    monkeypatch.setattr(settings, "DNS_RESOLVER", "10.0.0.1")
    c = B()
    calls = []
    monkeypatch.setattr(c, "_resolve", lambda host: calls.append(host) or "127.0.1.2")
    c._ensure_control()
    c._ensure_control()
    assert len(calls) == 1                                  # тот же резолвер — кэш работает
    monkeypatch.setattr(settings, "DNS_RESOLVER", "10.0.0.2")
    c._ensure_control()
    assert len(calls) == 2                                  # резолвер сменили — контроль заново


# ---- миграция ----

def test_migration_0026_chain():
    import re
    d = pathlib.Path(__file__).parents[1] / "alembic" / "versions"
    revs = {}
    for f in d.glob("0*.py"):
        t = f.read_text()
        revs[re.search(r'^revision = "([^"]+)"', t, re.M).group(1)] = \
            re.search(r'^down_revision = "?([^"\n]+?)"?$', t, re.M).group(1)
    assert revs["0026_secret_override"] == "0025_v2_m1"
    heads = set(revs) - set(revs.values())            # у 0001 down_revision = None -> строка "None"
    assert len(heads) == 1                                      # одна голова, цепочка линейна
    node, chain = next(iter(heads)), []
    while node in revs:
        chain.append(node)
        node = revs[node]
    assert "0026_secret_override" in chain


# ---- гейт панели: без PANEL_USER/PANEL_PASS ключи менять нельзя ----

def test_post_refused_without_panel_auth(client, ak, monkeypatch):
    monkeypatch.setattr(settings, "PANEL_USER", "")
    monkeypatch.setattr(settings, "PANEL_PASS", "")
    client.auth = None
    r = _post(client, v_LLM_MODEL="x", v_AHREFS_API_KEY="sk-NOAUTH-1234567890")
    assert r.status_code == 403
    assert "PANEL_USER" in r.text and "NOAUTH" not in r.text
    assert _rows() == {}
    page = client.get("/settings/keys").text
    assert "Закрыто" in page and "disabled" in page


def test_post_refused_with_only_user_configured(client, ak, monkeypatch):
    monkeypatch.setattr(settings, "PANEL_PASS", "")
    client.auth = None
    assert _post(client, v_LLM_MODEL="x").status_code == 403
    assert _rows() == {}


def test_github_repo_not_editable(client, ak):
    html = client.get("/settings/keys").text
    assert 'name="v_GITHUB_REPO"' not in html
    r = _post(client, v_GITHUB_REPO="evil/repo")
    assert r.status_code == 303 and _rows() == {}


def test_url_hints_warn_about_key_exfiltration(ak):
    urls = [f for f in ak.EDITABLE.values() if f.kind == "url"]
    assert urls and all("новый хост" in f.hint for f in urls)
    assert "APARSER_PROXY_CHECKER" not in ak.EDITABLE


def test_json_textarea_has_maxlength(client, ak):
    html = client.get("/settings/keys").text
    i = html.index('name="v_GSC_SERVICE_ACCOUNT_JSON"')
    assert 'maxlength="8192"' in html[i:i + 400]


def test_broken_url_and_json_errors_name_the_field(ak):
    """Исключение urlsplit/json не должно уходить голым: сообщение называет поле и ничего не цитирует."""
    for key, bad in (("LLM_BASE_URL", "http://[::1"), ("GSC_SERVICE_ACCOUNT_JSON", '{"a":' + "[" * 5000)):
        with pytest.raises(ValueError) as e:
            ak.validate(ak.EDITABLE[key], bad)
        assert str(e.value).startswith(key + ":") and "[::1" not in str(e.value)


def test_research_keys_are_editable_and_screenshots_is_choice():
    from app.services import api_keys
    for k in ("BROWSERLESS_URL", "BROWSERLESS_TOKEN", "RESEARCH_SCREENSHOTS", "RESEARCH_DIR"):
        assert k in api_keys.EDITABLE, k
    assert api_keys.EDITABLE["RESEARCH_SCREENSHOTS"].kind == "choice"
    assert api_keys.EDITABLE["BROWSERLESS_TOKEN"].secret is True


def test_writer_and_critic_models_are_editable():
    """Модели писателя и критика — поля экрана ключей в группе LLM; пусто -> модель для текстов."""
    from app.services import api_keys
    group = next(fields for gid, _, _, fields in api_keys.GROUPS if gid == "m45")
    keys = [f.key for f in group]
    for k in ("LLM_WRITER_MODEL", "LLM_CRITIC_MODEL"):
        assert k in keys and type(settings).model_fields[k].default == ""
        f = api_keys.EDITABLE[k]
        assert f.kind == "text" and not f.secret and "Пусто" in f.hint
    assert keys.index("LLM_MODEL") < keys.index("LLM_WRITER_MODEL") < keys.index("SEARXNG_URL")
