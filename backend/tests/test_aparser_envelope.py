"""A-Parser не имеет права молчать: сбой живёт в КОНВЕРТЕ ответа (аудит, F6).

A-Parser отвечает **HTTP 200 даже на отказ** — ошибка сидит в теле:
`{"success":0,"msg":"Auth failed"}` (docs/api/aparser.md). `raise_for_status` такого не видит,
а `_call` возвращал тело как обычный результат. Дальше `_result_string` не находил
`resultString` и отдавал `""` — и по пустому тексту whois-парсер ЧЕСТНО отвечал «ничего не
разобрал»: `{available: None, created: None}`, без единого исключения.

Следствие тяжелее самой находки: `created=None` -> `age_years` не считается -> гейт
`too_young` **не применяется вообще**, а `errors` пуст -> метка «оценён вслепую» молчит.
Машина не отличала «домену 16 лет» от «я не смог спросить» — и штамповала второе как первое.

Отсюда два независимых рубежа, и оба нужны:
  · транспорт — сбой конверта обязан быть ИСКЛЮЧЕНИЕМ (тесты ниже, группа 1);
  · скоринг — домен без возраста не может быть авто-одобрен, даже если исключение кто-то
    проглотит: незнание возраста — это НЕ «возраст в порядке» (группа 2).
"""
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace

import pytest

import app.db as db
from app.models.domain import Domain
from app.services import scoring
from app.integrations.aparser import AParserClient

_CLEAN_FLAGS = {c: False for c in ("adult", "pharma", "casino", "gambling", "spam")}


def _client(body):
    """Клиент, у которого транспорт (request) отдаёт ровно `body`. Подменяем именно
    `request`, а не `_call`: проверяем как раз ТО, что делает `_call` с ответом."""
    c = AParserClient()
    c.request = lambda *a, **k: SimpleNamespace(json=lambda: body)   # noqa: ARG005
    return c


# ---------- 1. транспорт: конверт ----------

def test_call_raises_on_error_envelope():
    """Тот самый живой ответ протухшего пароля. HTTP 200, а внутри — отказ."""
    c = _client({"success": 0, "msg": "Auth failed"})
    with pytest.raises(RuntimeError, match="Auth failed"):
        c._call("ping")


def test_call_passes_success_envelope_through():
    c = _client({"success": 1, "data": "pong"})
    assert c._call("ping") == {"success": 1, "data": "pong"}
    assert c.ping() is True


def test_call_raises_on_body_without_envelope():
    """Тело без `success` — это не «пустой результат», это НЕ ТОТ ответ (прокси, редирект на
    логин, сменившийся API). Молча принять его — снова начать выдавать незнание за знание."""
    c = _client({"data": {"resultString": "x.ru - registered: 1, creation: 10.06.2004"}})
    with pytest.raises(RuntimeError):
        c._call("oneRequest", {"query": "x.ru"})


def test_call_raises_on_non_dict_body():
    c = _client(["не тот формат"])
    with pytest.raises(RuntimeError):
        c._call("info")


def test_empty_result_is_success_not_error():
    """ГРАНИЦА, которую легко перейти: `success:1` с ПУСТЫМ resultString — законный ответ
    («в whois ничего не нашлось»), а не сбой. Если сделать ошибкой и его, воронка начнёт
    считать сбоем каждый неотвеченный домен — обмен одной немоты на другую."""
    c = _client({"success": 1, "data": {"resultString": ""}})
    assert c.whois_probe("nothing.ru") == {"available": None, "created": None}   # без исключения


def test_whois_probe_raises_on_error_envelope():
    """Путь целиком: отказ конверта -> исключение из whois_probe. Раньше здесь молча
    возвращалось {available: None, created: None} — «домен без возраста», и воронка ехала."""
    c = _client({"success": 0, "msg": "Auth failed"})
    with pytest.raises(RuntimeError, match="Auth failed"):
        c.whois_probe("clara-c.ru")


def test_ahrefs_probe_raises_on_error_envelope():
    """Тот же конверт у платного вызова: «капча не решилась» не должна выглядеть как «DR=0»."""
    c = _client({"success": 0, "msg": "Captcha service error"})
    with pytest.raises(RuntimeError, match="Captcha"):
        c.ahrefs_probe("clara-c.ru")


def test_ping_surfaces_reason_for_diag():
    """`/diag` ловит Exception и показывает текст (diagnostics._run_one). Раньше протухший
    пароль давал тихое False — «красный без причины»; теперь причина видна оператору."""
    c = _client({"success": 0, "msg": "Auth failed"})
    with pytest.raises(RuntimeError, match="Auth failed"):
        c.ping()


# ---------- 2. скоринг: незнание возраста != «возраст в порядке» ----------

def _sig(**kw) -> dict:
    """Домен-мечта, у которого НЕТ ровно одного — возраста: история проверена и чиста,
    ссылочная масса за потолком, эхо в индексе есть. Ровно так выглядит bid-домен, чей whois
    не ответил (lane известен из фида, воронка едет дальше)."""
    return {"wayback_checked": True, "prior_flags": dict(_CLEAN_FLAGS),
            "referring_domains": 5000, "indexed_echo": True, "errors": [], **kw}


def test_unknown_age_never_auto_approves():
    """РЕГРЕССИЯ. До фикса: 0.35(история) + 0.27(RD) + 0.08(эхо) = 0.70 == approve_at ->
    `approved`. Возраст при этом не проверен НИКЕМ: гейт too_young не мог сработать —
    сравнивать было не с чем. Машина одобряла домен, о возрасте которого ничего не знает."""
    out = scoring.compute_score(_sig(errors=["whois:RuntimeError"]))
    assert out["score"] >= 0.70, out           # порог реально взят — гард, а не низкий балл
    assert out["status"] == "scored", out      # но авто-одобрения нет: возраст неизвестен


def test_known_age_still_auto_approves():
    """Контроль: гард бьёт ТОЛЬКО по незнанию. Домен с известным возрастом одобряется как и был —
    иначе «фикс» просто заморозил бы весь пул на ручном разборе."""
    out = scoring.compute_score(_sig(age_years=16.0))
    assert out["status"] == "approved", out


def test_blind_reason_names_whois_and_ahrefs():
    """Домен, чей whois упал, обязан приехать к куратору С ПОМЕТКОЙ. Без неё он в инбоксе
    неотличим от честно проверенного — и человек штампует непроверенное как проверенное."""
    d = Domain(domain="mute.ru", wayback_checked=True, prior_flags=dict(_CLEAN_FLAGS),
               score_breakdown={"errors": ["whois:RuntimeError"], "history_evidence": []})
    assert "возраст" in scoring.blind_reason(d)
    assert scoring.bulk_ok(d) is False          # и в пакет одобрения не идёт

    a = Domain(domain="mute2.ru", wayback_checked=True, prior_flags=dict(_CLEAN_FLAGS),
               score_breakdown={"errors": ["ahrefs:RuntimeError"], "history_evidence": []})
    assert scoring.blind_reason(a) is not None
    assert scoring.bulk_ok(a) is False


# ---------- 3. воронка целиком ----------

class _WaybackNoAge:
    """Wayback отработал и историю прочитал (она чиста), но возраста не дал — архив ничего
    не знает о дате рождения домена. Единственным источником возраста остаётся whois."""
    def classify_history(self, domain):
        return {"prior_flags": dict(_CLEAN_FLAGS), "first_seen": None, "age_years": None,
                "wayback_checked": True, "sampled": 5, "evidence": []}


def _clients(whois):
    class _W:
        def whois_probe(self, dom):
            if isinstance(whois, Exception):
                raise whois
            return whois
    class _R:
        def is_listed(self, dom): return False
    class _B:
        def is_blacklisted(self, dom): return False
    class _S:
        def indexed_echo(self, dom): return True
    return {"aparser": _W(), "rkn": _R(), "blacklist": _B(), "searxng": _S(),
            "wayback": _WaybackNoAge()}


def _add(**kw) -> int:
    with db.SessionLocal() as s:
        d = Domain(source="backorder", status="discovered", lane="bid",
                   referring_domains=5000, **kw)
        s.add(d); s.commit(); s.refresh(d)
        return d.id


def test_funnel_whois_down_domain_is_not_auto_approved():
    """РЕГРЕССИЯ, сквозная. A-Parser лежит -> whois бросает -> возраст неизвестен. Домен
    (lane='bid', RD 5000, история чиста) НЕ должен получить `approved` автоматом: он едет
    в инбокс к человеку — с явной пометкой, почему ему нельзя верить на слово."""
    did = _add(domain="clara-c.ru",
               acquire_deadline=datetime.now(timezone.utc) + timedelta(days=5))
    out = scoring.score_domain(did, _clients(RuntimeError("A-Parser oneRequest: Auth failed")))

    assert out["status"] == "scored", out                   # НЕ approved
    assert any(e.startswith("whois:") for e in out["errors"]), out

    with db.SessionLocal() as s:
        d = s.get(Domain, did)
        assert d.status == "scored"
        assert d.age_years is None                          # возраст так и не узнали — и не врём
        assert "возраст" in scoring.blind_reason(d)         # пометка «вслепую» в инбоксе
        assert scoring.bulk_ok(d) is False                  # из пакетного одобрения исключён


def test_funnel_whois_alive_domain_still_auto_approves():
    """Контроль: когда A-Parser отвечает, тот же домен проходит как раньше — до `approved`.
    Гард не должен превращать живую воронку в вечный ручной разбор."""
    created = datetime.now(timezone.utc) - timedelta(days=int(365.25 * 16))
    did = _add(domain="old-bid.ru",
               acquire_deadline=datetime.now(timezone.utc) + timedelta(days=5))
    out = scoring.score_domain(did, _clients({"available": False, "created": created}))

    assert out["status"] == "approved", out
    with db.SessionLocal() as s:
        d = s.get(Domain, did)
        assert d.age_years and d.age_years > 15
        assert scoring.blind_reason(d) is None
        assert scoring.bulk_ok(d) is True


def test_funnel_too_young_still_rejected_when_whois_answers():
    """ПОЧЕМУ гард обязателен. Гейт молодости живёт ТОЛЬКО в T1 воронки и сравнивает с датой
    из whois: балл его не дублирует (юный домен с большим RD набирает ~0.71 и по баллу прошёл
    бы). Значит молчащий whois не «чуть ослаблял» проверку — он снимал её целиком.
    Здесь whois отвечает, и домен-однолетка честно отбраковывается."""
    young = datetime.now(timezone.utc) - timedelta(days=365)
    did = _add(domain="young.ru",
               acquire_deadline=datetime.now(timezone.utc) + timedelta(days=5))
    out = scoring.score_domain(did, _clients({"available": False, "created": young}))
    assert out["status"] == "rejected" and out["reject_reason"] == "too_young", out


def test_funnel_whois_down_not_excluded_from_pool():
    """Что ломается, когда гард срабатывает? НИЧЕГО не теряется: домен не отбракован
    (`rejected`) и не завис в `discovered` — он в инбоксе, `scored`, с живым баллом.
    Человек может одобрить его вручную; пакет — не может. Лежащий A-Parser тормозит
    автопилот, но не выбрасывает домены."""
    did = _add(domain="pool.ru",
               acquire_deadline=datetime.now(timezone.utc) + timedelta(days=5))
    scoring.score_domain(did, _clients(RuntimeError("whois timeout")))
    with db.SessionLocal() as s:
        d = s.get(Domain, did)
        assert d.status == "scored"           # не rejected и не discovered
        assert d.reject_reason is None
        assert d.score and d.score > 0.0
