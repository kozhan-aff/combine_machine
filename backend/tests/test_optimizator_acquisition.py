"""Optimizator branch of execute_confirmed_order/confirm_order — idempotency via
check_domain (no domain->order listing exists, unlike backorder.find_order), clean
rejection vs. ambiguous-transport distinction. See design doc "Идемпотентность денег".
"""
import app.db as db
from app.models.domain import Domain, AcquisitionOrder
from app.services import acquisition
from app.integrations.optimizator import OptimizatorError, OptimizatorAmbiguous


def _approved_optimizator(name="free-clean.ru") -> int:
    with db.SessionLocal() as s:
        d = Domain(domain=name, source="cctld", status="approved", lane="free")
        s.add(d)
        s.commit()
        s.refresh(d)
        return d.id


def test_confirm_freezes_price_for_optimizator(monkeypatch):
    monkeypatch.setattr(
        "app.integrations.optimizator.OptimizatorClient.prices",
        lambda self, zone="ru": {"domain": "RU", "price_registration": 179, "price_renewal": 199})
    did = _approved_optimizator()
    oid = acquisition.create_order(did, provider="optimizator")
    result = acquisition.confirm_order(oid)          # bid_rub НЕ передаём — optimizator его не требует
    assert result["confirmed_by_human"] is True
    with db.SessionLocal() as s:
        o = s.get(AcquisitionOrder, oid)
        assert o.cost == 179


def test_execute_registers_when_not_already_ours(monkeypatch):
    monkeypatch.setattr("app.integrations.optimizator.OptimizatorClient.prices",
                        lambda self, zone="ru": {"price_registration": 179})
    monkeypatch.setattr("app.integrations.optimizator.OptimizatorClient.check_domain",
                        lambda self, domain: (_ for _ in ()).throw(OptimizatorError("not found", 404)))
    monkeypatch.setattr("app.integrations.optimizator.OptimizatorClient.register",
                        lambda self, domains: {"order_id": 555})
    did = _approved_optimizator()
    oid = acquisition.create_order(did, provider="optimizator")
    acquisition.confirm_order(oid)
    result = acquisition.execute_confirmed_order(oid)
    assert result["status"] == "ordered"
    assert result["result"]["order_id"] == 555


def test_execute_skips_register_when_already_ours(monkeypatch):
    monkeypatch.setattr("app.integrations.optimizator.OptimizatorClient.prices",
                        lambda self, zone="ru": {"price_registration": 179})
    monkeypatch.setattr("app.integrations.optimizator.OptimizatorClient.check_domain",
                        lambda self, domain: {"data_end": "02.12.2027", "domain": domain.upper()})
    called = {"register": 0}

    def _register(self, domains):
        called["register"] += 1
        return {"order_id": 1}
    monkeypatch.setattr("app.integrations.optimizator.OptimizatorClient.register", _register)
    did = _approved_optimizator()
    oid = acquisition.create_order(did, provider="optimizator")
    acquisition.confirm_order(oid)
    result = acquisition.execute_confirmed_order(oid)
    assert result["status"] == "ordered"
    assert called["register"] == 0                   # НЕ шлём второй reg_domains


def test_execute_clean_rejection_leaves_retry_open(monkeypatch):
    monkeypatch.setattr("app.integrations.optimizator.OptimizatorClient.prices",
                        lambda self, zone="ru": {"price_registration": 179})
    monkeypatch.setattr("app.integrations.optimizator.OptimizatorClient.check_domain",
                        lambda self, domain: (_ for _ in ()).throw(OptimizatorError("not found", 404)))
    monkeypatch.setattr(
        "app.integrations.optimizator.OptimizatorClient.register",
        lambda self, domains: (_ for _ in ()).throw(OptimizatorError("недостаточно средств", 42)))
    did = _approved_optimizator()
    oid = acquisition.create_order(did, provider="optimizator")
    acquisition.confirm_order(oid)
    result = acquisition.execute_confirmed_order(oid)
    assert result["status"] == "failed"
    # ЧИСТЫЙ отказ падает в общий `except Exception` (шаренный с backorder) — он СПРЕДИТ
    # o.result в топ-левел (`**o.result`), а не кладёт под ключ "result" (см. test_m23_fixes.py
    # ::test_retry_from_failed_works, тот же паттерн для backorder). Проверено чтением кода:
    # брифовский `result["result"].get(...)` здесь всегда KeyError — правим тест под факт,
    # не под предположение (см. task-2-report.md, раздел "Расхождения").
    assert result.get("maybe_sent") is not True     # чистый отказ — не ambiguous


def test_execute_ambiguous_send_sets_maybe_sent(monkeypatch):
    monkeypatch.setattr("app.integrations.optimizator.OptimizatorClient.prices",
                        lambda self, zone="ru": {"price_registration": 179})
    monkeypatch.setattr("app.integrations.optimizator.OptimizatorClient.check_domain",
                        lambda self, domain: (_ for _ in ()).throw(OptimizatorError("not found", 404)))
    monkeypatch.setattr(
        "app.integrations.optimizator.OptimizatorClient.register",
        lambda self, domains: (_ for _ in ()).throw(OptimizatorAmbiguous("timed out")))
    did = _approved_optimizator()
    oid = acquisition.create_order(did, provider="optimizator")
    acquisition.confirm_order(oid)
    result = acquisition.execute_confirmed_order(oid)
    assert result["status"] == "failed"
    # Тот же спред-паттерн, что и у backorder AmbiguousSend (структурное зеркало) — не
    # нест под "result", см. комментарий в test_execute_clean_rejection_leaves_retry_open.
    assert result["maybe_sent"] is True


def test_execute_ambiguous_check_domain_blocks_register_and_sets_maybe_sent(monkeypatch):
    """Bug 4a: check_domain НЕ смог ответить (Ambiguous, не чистый OptimizatorError) — это
    НЕ равно "не наш". Слать register() поверх неизвестности рискованно (а вдруг уже наш и
    это повторная попытка) — execute обязан остановиться, пометить maybe_sent=True и НЕ
    звать register() вообще."""
    monkeypatch.setattr("app.integrations.optimizator.OptimizatorClient.prices",
                        lambda self, zone="ru": {"price_registration": 179})
    monkeypatch.setattr(
        "app.integrations.optimizator.OptimizatorClient.check_domain",
        lambda self, domain: (_ for _ in ()).throw(OptimizatorAmbiguous("timed out")))
    called = {"register": 0}

    def _register(self, domains):
        called["register"] += 1
        return {"order_id": 1}
    monkeypatch.setattr("app.integrations.optimizator.OptimizatorClient.register", _register)
    did = _approved_optimizator()
    oid = acquisition.create_order(did, provider="optimizator")
    acquisition.confirm_order(oid)
    result = acquisition.execute_confirmed_order(oid)
    assert result["status"] == "failed"
    assert result["maybe_sent"] is True
    assert called["register"] == 0                   # неизвестность НЕ повод слать деньги


def test_execute_still_gates_on_confirmed_by_human():
    """ГЕЙТ НЕ ТРОНУТ — тот же тест, что уже есть для backorder, повторён для optimizator."""
    did = _approved_optimizator()
    oid = acquisition.create_order(did, provider="optimizator")
    result = acquisition.execute_confirmed_order(oid)     # НЕ подтверждён
    assert "gate" in (result.get("error") or "")
