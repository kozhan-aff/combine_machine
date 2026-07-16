"""OptimizatorClient — транспорт (M2, второй канал выкупа). Формат ответа живьём
проверен 2026-07-16 (balance/prices/check_nicd реальным ключом); reg_domains/
check_order/check_domain/renew_domains — по документации (не тратим деньги на тест).
См. docs/superpowers/specs/2026-07-16-optimizator-integration-design.md."""
import httpx
import pytest

from app.integrations.optimizator import OptimizatorClient, OptimizatorError, OptimizatorAmbiguous


def _client(monkeypatch, response_json, status=200):
    """Мокает httpx на уровне request(): OptimizatorClient использует GET-запросы
    через BaseClient.request, кроме register() (тот идёт напрямую через httpx —
    см. Step 3). Один хелпер покрывает все методы, кроме register()."""
    class _Resp:
        def __init__(self):
            self.status_code = status
        def json(self):
            return response_json
        def raise_for_status(self):
            if status >= 400:
                raise httpx.HTTPStatusError("boom", request=None, response=self)

    def fake_request(self, method, url, **kw):
        return _Resp()

    monkeypatch.setattr("app.integrations.base.BaseClient.request", fake_request)
    return OptimizatorClient()


def test_balance_parses_real_live_format(monkeypatch):
    c = _client(monkeypatch, [{"balance": 0}])
    assert c.balance() == 0


def test_prices_parses_real_live_format(monkeypatch):
    c = _client(monkeypatch, [{"domain": "RU", "price_registration": 179, "price_renewal": 199}])
    out = c.prices("ru")
    assert out == {"domain": "RU", "price_registration": 179, "price_renewal": 199}


def test_ping_true_on_success(monkeypatch):
    c = _client(monkeypatch, [{"balance": 0}])
    assert c.ping() is True


def test_check_nicd_false_on_411(monkeypatch):
    c = _client(monkeypatch, [{"error": "Указанная анкета не находится под нашим "
                                        "управлением", "error_id": 411}])
    assert c.check_nicd() is False


def test_check_nicd_true_on_success(monkeypatch):
    c = _client(monkeypatch, [{"nicd": "11/NIC-D"}])
    assert c.check_nicd() is True


def test_check_nicd_reraises_other_error_ids(monkeypatch):
    c = _client(monkeypatch, [{"error": "неизвестный ключ API", "error_id": 401}])
    with pytest.raises(OptimizatorError):
        c.check_nicd()


def test_error_shaped_response_raises_optimizator_error(monkeypatch):
    c = _client(monkeypatch, [{"error": "недостаточно средств", "error_id": 42}])
    with pytest.raises(OptimizatorError) as exc:
        c.balance()
    assert exc.value.error_id == 42


def test_order_status_unwraps_array(monkeypatch):
    c = _client(monkeypatch, [{"order_id": 7, "state": "completed"}])
    assert c.order_status(7) == {"order_id": 7, "state": "completed"}


def test_check_domain_unwraps_array(monkeypatch):
    c = _client(monkeypatch, [{"data_end": "02.12.2016", "domain": "A.RU"}])
    assert c.check_domain("a.ru") == {"data_end": "02.12.2016", "domain": "A.RU"}


def test_register_unwraps_array(monkeypatch):
    def fake_get(self, url, **kw):
        class _R:
            def raise_for_status(self): pass
            def json(self): return [{"order_id": 99}]
        return _R()
    monkeypatch.setattr("httpx.Client.get", fake_get)
    c = OptimizatorClient()
    assert c.register(["a.ru"]) == {"order_id": 99}


def test_register_raises_on_error(monkeypatch):
    def fake_get(self, url, **kw):
        class _R:
            def raise_for_status(self): pass
            def json(self): return [{"error": "нет анкеты под управлением", "error_id": 411}]
        return _R()
    monkeypatch.setattr("httpx.Client.get", fake_get)
    c = OptimizatorClient()
    with pytest.raises(OptimizatorError):
        c.register(["a.ru"])


def test_register_transport_failure_raises_ambiguous(monkeypatch):
    def fake_get(self, url, **kw):
        raise httpx.TimeoutException("timed out")
    monkeypatch.setattr("httpx.Client.get", fake_get)
    c = OptimizatorClient()
    with pytest.raises(OptimizatorAmbiguous):
        c.register(["a.ru"])


# --- Fix Codex Bug 1/2/3: _get()/_unwrap() must surface uncertainty, not swallow it -----------


def test_unwrap_empty_list_raises_ambiguous():
    """[] — ни ошибки, ни данных. Раньше тихо возвращалось {} (успех с пустым результатом);
    для check_domain это ложноположительно читалось бы как "домен наш"."""
    from app.integrations.optimizator import _unwrap
    with pytest.raises(OptimizatorAmbiguous):
        _unwrap([])


def test_unwrap_empty_dict_row_raises_ambiguous():
    """[{}] — распарсили массив, а внутри пусто: тоже не форма успеха, тоже неизвестность."""
    from app.integrations.optimizator import _unwrap
    with pytest.raises(OptimizatorAmbiguous):
        _unwrap([{}])


def test_unwrap_non_dict_row_raises_ambiguous():
    from app.integrations.optimizator import _unwrap
    with pytest.raises(OptimizatorAmbiguous):
        _unwrap(["unexpected string payload"])


def test_get_transport_failure_raises_ambiguous(monkeypatch):
    """_get() — единственный путь, которым идут check_domain/balance/prices/check_nicd/
    check_order (всё, кроме register(), которое обходит BaseClient целиком). Раньше
    httpx.TransportError/HTTPStatusError из self.request() (BaseClient, с ретраями) улетал
    наружу КАК ЕСТЬ — check_domain() физически не мог сдержать обещание докстринга
    "бросает OptimizatorAmbiguous на сбой"."""
    def fake_request(self, method, url, **kw):
        raise httpx.TimeoutException("timed out")
    monkeypatch.setattr("app.integrations.base.BaseClient.request", fake_request)
    c = OptimizatorClient()
    with pytest.raises(OptimizatorAmbiguous):
        c.check_domain("a.ru")


def test_get_still_raises_optimizator_error_unwrapped(monkeypatch):
    """Регрессия: обёртка в _get() не должна перехватывать/перепаковывать OptimizatorError —
    чистый отказ провайдера обязан долетать как есть (не как Ambiguous)."""
    c = _client(monkeypatch, [{"error": "недостаточно средств", "error_id": 42}])
    with pytest.raises(OptimizatorError) as exc:
        c.balance()
    assert exc.value.error_id == 42
