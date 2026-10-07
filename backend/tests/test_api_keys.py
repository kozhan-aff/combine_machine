"""«Ключи и сервисы»: переопределения из БД поверх .env, экран /settings/keys, секреты не утекают."""
import importlib.util
import pathlib
import time

import pytest
from sqlalchemy import text

import app.db as db
from app.config import settings
from app.models.secret import SecretOverride


@pytest.fixture
def ak(key_overrides):
    return key_overrides


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
    ("v_SEO_DATA_PROVIDER", "bing"),
    ("v_GITHUB_REPO", "no-slash"),
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
              v_DNS_RESOLVER="10.0.0.2", v_SEO_DATA_PROVIDER="serpapi",
              v_GITHUB_REPO="owner/repo.name", v_GSC_SERVICE_ACCOUNT_JSON=big_json)
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
    for k in ("PANEL_PASS", "PANEL_USER", "DATABASE_URL", "APP_ENV", "CLOUDFLARE_SECRETS_DIR", "NOPE"):
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
    assert {"PANEL_PASS", "PANEL_USER", "DATABASE_URL", "APP_ENV", "CLOUDFLARE_SECRETS_DIR"} <= NOT_EDITABLE


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
    """_load, прочитавший settings.*, не должен зациклиться/задедлочиться."""
    def reentrant():
        return {"LLM_MODEL": settings.LLM_API_KEY or "x"}
    monkeypatch.setattr(ak, "_load", reentrant)
    ak.invalidate()
    assert settings.LLM_MODEL == "x"


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
    p = pathlib.Path(__file__).parents[1] / "alembic" / "versions" / "0026_secret_override.py"
    spec = importlib.util.spec_from_file_location("m0026", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    assert m.revision == "0026_secret_override" and m.down_revision == "0025_v2_m1"
    heads = [f.name for f in p.parent.glob("0*.py")]
    assert sorted(heads)[-1] == "0026_secret_override.py"
