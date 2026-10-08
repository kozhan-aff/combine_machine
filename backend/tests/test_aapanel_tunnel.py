"""aaPanel через SSH-туннель (W2e): compose-сайдкар и клиентские ветки.

Сети нет: compose только парсится (yaml), клиент гоняется на подменённом `_client`.
Прямой режим (AAPANEL_TUNNEL пусто) обязан остаться как был.
"""
import re
from pathlib import Path

import httpx
import pytest
import yaml

from app.config import settings
from app.integrations import aapanel
from app.integrations.aapanel import AaPanelClient, tunnel_mode
from app.services import api_keys

ROOT = Path(__file__).resolve().parents[2]
TUN = "https://aapanel-tunnel:18839"


@pytest.fixture
def pem():
    """create_default_context(cafile=) требует валидный PEM — берём настоящий бандл certifi (тянет httpx)."""
    import certifi
    return certifi.where()


@pytest.fixture(autouse=True)
def _base(monkeypatch):
    monkeypatch.setattr(settings, "AAPANEL_API_KEY", "testsk")
    monkeypatch.setattr(settings, "AAPANEL_CA_BUNDLE", "")
    monkeypatch.setattr(settings, "AAPANEL_TUNNEL", "")
    aapanel.reset_block()


# ---------------------------- compose ----------------------------

def _svc():
    return yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))["services"]["aapanel-tunnel"]


def test_compose_sidecar_opt_in_and_not_published():
    s = _svc()
    assert s["profiles"] == ["tunnel"]           # без профиля обычный `up` не требует ключей туннеля
    assert "ports" not in s                      # наружу не торчит
    assert s["build"] == "./tunnel"
    assert "healthcheck" in s
    assert "nc -z" in " ".join(s["healthcheck"]["test"])
    ro = [v for v in s["volumes"] if v.startswith("./secrets/tunnel:")]
    assert ro and ro[0].endswith(":ro")          # ключ read-only


def test_compose_nothing_depends_on_tunnel():
    svcs = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))["services"]
    for name, s in svcs.items():
        assert "aapanel-tunnel" not in (s.get("depends_on") or {}), name


def test_entrypoint_fails_loud_and_pins_host_key():
    sh = (ROOT / "tunnel" / "entrypoint.sh").read_text(encoding="utf-8")
    assert sh.count("exit 78") == 1 and "die " in sh
    for need in ("id_ed25519", "known_hosts", "TUNNEL_VPS_HOST"):
        assert re.search(rf"\[ (-f|-n).*{need}", sh), need
    assert "StrictHostKeyChecking=yes" in sh
    assert "ExitOnForwardFailure=yes" in sh
    assert "BatchMode=yes" in sh


def test_env_example_has_tunnel_keys():
    env = (ROOT / ".env.example").read_text(encoding="utf-8")
    for k in ("AAPANEL_TUNNEL", "TUNNEL_VPS_HOST", "TUNNEL_VPS_USER", "TUNNEL_VPS_SSH_PORT", "TUNNEL_PORT"):
        assert re.search(rf"^{k}=", env, re.M), k


def test_tunnel_flag_in_whitelist_and_config():
    keys = {f.key for _, _, _, fields in api_keys.GROUPS for f in fields}
    assert "AAPANEL_TUNNEL" in keys
    assert hasattr(settings, "AAPANEL_TUNNEL")


# ---------------------------- клиент ----------------------------

@pytest.mark.parametrize("val,exp", [("1", True), ("true", True), ("True", True), ("on", True),
                                     ("false", False), ("0", False), ("", False), (None, False)])
def test_tunnel_mode_parses_strings(monkeypatch, val, exp):
    monkeypatch.setattr(settings, "AAPANEL_TUNNEL", val)
    assert tunnel_mode() is exp       # строка "false" с экрана ключей НЕ включает режим


def test_tunnel_requires_ca_bundle(monkeypatch):
    monkeypatch.setattr(settings, "AAPANEL_TUNNEL", "1")
    monkeypatch.setattr(settings, "AAPANEL_URL", TUN)
    with pytest.raises(RuntimeError, match="AAPANEL_TUNNEL=1 требует"):
        AaPanelClient()


def test_tunnel_forbids_verify_false_even_on_loopback(monkeypatch):
    # прямой режим: loopback без CA -> verify=False допустим; в туннельном — нет
    monkeypatch.setattr(settings, "AAPANEL_URL", "https://127.0.0.1:18839")
    AaPanelClient()
    monkeypatch.setattr(settings, "AAPANEL_TUNNEL", "true")
    with pytest.raises(RuntimeError, match="AAPANEL_TUNNEL=1 требует"):
        AaPanelClient()


@pytest.mark.parametrize("url", ["https://185.201.252.187:18839", "https://panel.example.com:18839"])
def test_tunnel_rejects_public_url(monkeypatch, pem, url):
    monkeypatch.setattr(settings, "AAPANEL_TUNNEL", "1")
    monkeypatch.setattr(settings, "AAPANEL_CA_BUNDLE", pem)
    monkeypatch.setattr(settings, "AAPANEL_URL", url)
    with pytest.raises(RuntimeError, match="обойдён"):
        AaPanelClient()


@pytest.mark.parametrize("url", [TUN, "https://127.0.0.1:18839", "https://host.docker.internal:18839",
                                 "https://172.28.0.9:18839"])
def test_tunnel_accepts_internal_urls_with_pinned_ctx(monkeypatch, pem, url):
    import ssl
    monkeypatch.setattr(settings, "AAPANEL_TUNNEL", "1")
    monkeypatch.setattr(settings, "AAPANEL_CA_BUNDLE", pem)
    monkeypatch.setattr(settings, "AAPANEL_URL", url)
    c = AaPanelClient()
    assert c.tunnel
    ctx = c._client._transport._pool._ssl_context
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.verify_mode == ssl.CERT_REQUIRED       # пин сохранён, проверка не отключена
    assert ctx.check_hostname is False                # имя (CN=*.aapanel.com) не совпадёт с сервисом


def test_direct_mode_unchanged(monkeypatch):
    monkeypatch.setattr(settings, "AAPANEL_URL", "https://185.201.252.187:18839")
    with pytest.raises(RuntimeError, match="not loopback"):      # fail-closed прямого режима жив
        AaPanelClient()
    monkeypatch.setattr(settings, "AAPANEL_URL", "https://127.0.0.1:8888")
    assert AaPanelClient().tunnel is False


def test_tunnel_connect_error_names_the_sidecar(monkeypatch, pem):
    monkeypatch.setattr(settings, "AAPANEL_TUNNEL", "1")
    monkeypatch.setattr(settings, "AAPANEL_CA_BUNDLE", pem)
    monkeypatch.setattr(settings, "AAPANEL_URL", TUN)
    c = AaPanelClient()

    def boom(*a, **k):
        raise httpx.ConnectError("refused")
    monkeypatch.setattr(c._client, "request", boom)
    monkeypatch.setattr("tenacity.nap.time.sleep", lambda s: None)
    with pytest.raises(RuntimeError, match="aapanel-tunnel"):
        c.ping()


def test_direct_connect_error_has_no_tunnel_hint(monkeypatch):
    monkeypatch.setattr(settings, "AAPANEL_URL", "https://127.0.0.1:8888")
    c = AaPanelClient()

    def boom(*a, **k):
        raise httpx.ConnectError("refused")
    monkeypatch.setattr(c._client, "request", boom)
    monkeypatch.setattr("tenacity.nap.time.sleep", lambda s: None)
    with pytest.raises(RuntimeError) as e:
        c.ping()
    assert "туннел" not in str(e.value)


def test_tunnel_ping_ok_over_mock(monkeypatch, pem):
    monkeypatch.setattr(settings, "AAPANEL_TUNNEL", "1")
    monkeypatch.setattr(settings, "AAPANEL_CA_BUNDLE", pem)
    monkeypatch.setattr(settings, "AAPANEL_URL", TUN)
    c = AaPanelClient()
    seen = []

    def req(method, url, **kw):
        seen.append(url)
        return httpx.Response(200, json=0, request=httpx.Request(method, url))
    monkeypatch.setattr(c._client, "request", req)
    assert c.ping() is True
    assert seen[0].startswith(TUN + "/ajax?action=GetTaskCount")
