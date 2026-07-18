"""S14 (аудит 2026-07-18): отрицательный контроль-пробинг Spamhaus НЕ кэшируется на процесс.

Транзиентный сбой резолвера сразу после старта воркера иначе навсегда (до рестарта
контейнера) загонял бы каждый последующий домен в путь «история не проверена». Теперь
кэшируется только успех — следующий вызов после сбоя ретраит (как RknClient)."""
import pytest

from app.integrations import blacklist


@pytest.fixture(autouse=True)
def _reset_control():
    blacklist.BlacklistClient._control_ok = None
    yield
    blacklist.BlacklistClient._control_ok = None


def test_negative_control_is_not_cached_and_retries(monkeypatch):
    c = blacklist.BlacklistClient()
    calls = {"n": 0}

    def resolve(host):
        calls["n"] += 1
        return None if calls["n"] == 1 else "127.0.1.2"    # сбой, затем восстановление

    monkeypatch.setattr(c, "_resolve", resolve)

    with pytest.raises(RuntimeError):        # 1-й вызов: контроль не прошёл -> fail-closed
        c._ensure_control()
    assert blacklist.BlacklistClient._control_ok is not True   # НЕ закэширован как рабочий

    c._ensure_control()                       # 2-й вызов ретраит и проходит (резолвер ожил)
    assert blacklist.BlacklistClient._control_ok is True
    assert calls["n"] == 2                    # оба раза реально спросили резолвер


def test_positive_control_is_cached(monkeypatch):
    c = blacklist.BlacklistClient()
    calls = {"n": 0}

    def resolve(host):
        calls["n"] += 1
        return "127.0.1.2"

    monkeypatch.setattr(c, "_resolve", resolve)
    c._ensure_control()
    c._ensure_control()
    assert calls["n"] == 1                     # успех закэширован — второй раз резолвер не дёргаем
