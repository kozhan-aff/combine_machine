"""S20 (аудит 2026-07-18): /diag не должен показывать сырой секрет в тексте ошибки пинга.

httpx кладёт полный URL (с ?api_key=...) в HTTPStatusError.str(); без скраба он утекал в
поле "error" и рендерился на /diag любому, кто откроет страницу. Проверяем, что _run_one
затирает любое настроенное значение секрета."""
from app.config import settings
from app.services import diagnostics


def test_run_one_scrubs_configured_secret_from_error(monkeypatch):
    monkeypatch.setattr(settings, "OPTIMIZATOR_API_KEY", "SUPERSECRET123")

    def boom():
        # имитируем httpx-подобное сообщение с ключом в URL
        raise RuntimeError(
            "Client error '403 Forbidden' for url "
            "'http://box/?a=api&sa=balance&api_key=SUPERSECRET123'")

    res = diagnostics._run_one("optimizator", "Optimizator", "M2", "1", "M1", False, boom)
    assert res["status"] == "fail"
    assert "SUPERSECRET123" not in res["error"]
    assert "***" in res["error"]


def test_run_one_leaves_error_intact_when_no_secret_present(monkeypatch):
    monkeypatch.setattr(settings, "OPTIMIZATOR_API_KEY", "SUPERSECRET123")

    def boom():
        raise RuntimeError("ConnectTimeout: box unreachable")

    res = diagnostics._run_one("optimizator", "Optimizator", "M2", "1", "M1", False, boom)
    assert "ConnectTimeout" in res["error"] and "***" not in res["error"]
