"""/diag v2: пингуются международные интеграции, РФ-проверок нет; выключенный DropCatch не
пингуется (ToS не прочитан, инвариант 6); Web Risk в фоне — только наличие ключа."""
from app.services import diagnostics


def _row(key):
    return next(s for s in diagnostics._spec() if s[0] == key)


def test_spec_keys_v2():
    keys = [s[0] for s in diagnostics._spec()]
    for k in ("ahrefs", "rdap", "webrisk", "dropcatch", "nominet", "registry_mx", "wayback", "aparser", "llm"):
        assert k in keys, k
    for k in ("rkn", "tci", "backorder"):
        assert k not in keys, k


def test_dropcatch_is_not_pinged_while_source_is_off(monkeypatch):
    """1.7: фон обновляет /diag каждые 5 минут — это 288 вызовов GetFileUrl в сутки при
    непрочитанном ToS. Выключенный источник — skip без сети, с честной причиной."""
    from app.integrations.dropcatch import DropCatchClient
    from app.services.settings import update_settings
    calls = []
    monkeypatch.setattr(DropCatchClient, "ping", lambda self: calls.append(1) or True)
    update_settings(sources_enabled={"dropcatch": False, "nominet": True, "mx": True, "emd": True})
    out = diagnostics.run_diagnostics(specs=[_row("dropcatch")])[0]
    assert out["status"] == "skip" and "выключен" in out["error"] and calls == []
    update_settings(sources_enabled={"dropcatch": True, "nominet": True, "mx": True, "emd": True})
    assert diagnostics.run_diagnostics(specs=[_row("dropcatch")])[0]["status"] == "ok" and calls == [1]


def test_webrisk_diag_checks_key_without_lookup(monkeypatch):
    """3.9: настоящий lookup каждые 5 минут съедал бы ~8,6 тыс. из 100 тыс. бесплатных вызовов
    в месяц. Фон проверяет только наличие ключа."""
    from app.config import settings
    from app.integrations.webrisk import WebRiskClient

    def _no_lookup(self, d):
        raise AssertionError("диагностика не вправе тратить lookup Web Risk")
    monkeypatch.setattr(WebRiskClient, "threats", _no_lookup)
    monkeypatch.setattr(settings, "WEBRISK_API_KEY", "k")
    assert diagnostics.run_diagnostics(specs=[_row("webrisk")])[0]["status"] == "ok"
    monkeypatch.setattr(settings, "WEBRISK_API_KEY", "")
    out = diagnostics.run_diagnostics(specs=[_row("webrisk")])[0]
    assert out["status"] == "skip" and out["error"]      # не FAIL: ключ опционален, причина названа


def test_rdap_ping_failure_is_red_not_a_crash(monkeypatch):
    """RdapClient.ping бросает при недоступном IANA — /diag показывает «fail» с причиной, а не 500."""
    from app.integrations.rdap import RdapClient

    def _down(self):
        raise RuntimeError("IANA down")
    monkeypatch.setattr(RdapClient, "ping", _down)
    out = diagnostics.run_diagnostics(specs=[_row("rdap")])[0]
    assert out["status"] == "fail" and "IANA down" in out["error"]


def test_red_banner_only_for_critical_checks(monkeypatch):
    """R2-18: глобальный красный баннер — только от критичных проверок. Лежащий Nominet или
    registry.mx (некритичные источники) виден на /diag, но баннером на всех экранах не горит."""
    from app.services import diag_cache
    monkeypatch.setattr(diag_cache, "_checks", None)          # кэш — модульный глобал: вернётся
    monkeypatch.setattr(diag_cache, "_checked_at", None)      # после теста, в чужой рендер не утечёт
    monkeypatch.setattr(diag_cache, "run_diagnostics", lambda: [
        {"key": "nominet", "label": "Nominet", "status": "fail", "critical": False},
        {"key": "registry_mx", "label": "registry.mx", "status": "fail", "critical": False},
        {"key": "wayback", "label": "Wayback", "status": "fail", "critical": True}])
    diag_cache.refresh()
    assert diag_cache.alert()["down"] == ["Wayback"]
