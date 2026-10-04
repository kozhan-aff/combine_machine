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
  · скоринг — домен, чей whois УПАЛ, не может быть авто-одобрен (группа 2). Гард стоит на самом
    отказе, а не на «возраст неизвестен», и вот почему: возраст, не добытый whois'ом, воронка
    добирает из Wayback — и это законно. Но whois кормит ВТОРОЙ гейт, `available` (свободен ли
    домен вообще), и его подменить нечем. Ровно так утекал clara-c.ru из живого дебага: RD 2219,
    возраст 16 лет ИЗ АРХИВА, score 0.87 -> `approved` — с бейджем «оценён вслепую» на лбу.

СТАБЫ WAYBACK ЗДЕСЬ СТРОЯТСЯ ПО КОНТРАКТУ НАСТОЯЩЕГО КЛИЕНТА, и это не педантизм. Прежняя версия
этих тестов доказывала гард на фейке `wayback_checked=True, age_years=None` — состоянии, которого
WaybackClient не выдаёт НИКОГДА (см. `_Wayback*` ниже). Тесты честно краснели на баге и честно
зеленели на фиксе — но на фикции: гард, который они «доказали», был недостижим ни на одном живом
домене, и настоящая дыра проехала мимо ревью. Фикстура обязана уметь отвечать на вопрос
«производит ли настоящий клиент такое состояние вообще?».
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


def test_ping_surfaces_reason_for_diag():
    """`/diag` ловит Exception и показывает текст (diagnostics._run_one). Раньше протухший
    пароль давал тихое False — «красный без причины»; теперь причина видна оператору."""
    c = _client({"success": 0, "msg": "Auth failed"})
    with pytest.raises(RuntimeError, match="Auth failed"):
        c.ping()


# ---------- 2. скоринг: упавший whois != «домен проверен» ----------

def _sig(**kw) -> dict:
    """Домен-мечта: история проверена и чиста, ссылочная масса за потолком.
    Ровно так выглядит bid-домен, чей whois не ответил (lane известен из фида — воронка едет
    дальше и добирает возраст из архива)."""
    return {"wayback_checked": True, "prior_flags": dict(_CLEAN_FLAGS),
            "referring_domains": 5000, "errors": [], **kw}


def _in_bulk(sig: dict, out: dict, **breakdown) -> bool:
    """Положить домен с этим итогом скоринга в БД как `scored` и спросить пакет: возьмёт ли.
    Авто-одобрения нет (Р2) — правило «whois упал / возраста нет» живёт в bulk_ok/blind_reason,
    его и проверяем, на пороге 0.0, чтобы исключал именно гард, а не балл."""
    from app.api.panel import _bulk_candidates
    with db.SessionLocal() as s:
        d = Domain(domain="bulk-probe.com", source="backorder", status="scored", score=out["score"],
                   wayback_checked=sig.get("wayback_checked"), prior_flags=sig.get("prior_flags"),
                   age_years=sig.get("age_years"),
                   score_breakdown={"errors": sig.get("errors", []), "history_evidence": [],
                                    "deep_checked": True, **breakdown})
        s.add(d); s.commit()
        ok, _ = _bulk_candidates(s, 0.0)
        return d.id in {x.id for x in ok}


def test_whois_down_never_auto_approves_even_with_archive_age():
    """РЕГРЕССИЯ (ревью Задачи 4). Живой clara-c.ru: whois лежит, но Wayback дал возраст 16 лет —
    и до фикса домен набирал 0.87 и уезжал в `approved`. Авто-одобрения теперь нет вовсе (Р2), а
    гард переехал в пакет: занятость домена (`available`) не сверял никто — её из архива не
    добрать, поэтому пакетное одобрение такой домен не берёт."""
    sig = _sig(errors=["whois:RuntimeError"], age_years=16.0, referring_domains=2219)
    out = scoring.compute_score(sig)
    assert out["score"] >= 0.70, out           # балл сильного кандидата — исключает гард, а не балл
    assert out["status"] == "scored", out      # одобряет только человек
    assert _in_bulk(sig, out, age_source="wayback") is False


def test_whois_down_without_any_age_never_auto_approves():
    """Второй достижимый вид того же отказа: архив ПУСТ, возраста нет ни у кого. Сигнал нарочно
    сильный (DR 30, тема 1.0, анкоры чистые, трафик за потолком -> 0.88), чтобы пакет исключал
    домен ПРАВИЛОМ (whois упал, возраста нет), а не низким баллом (находка 1.11)."""
    sig = _sig(errors=["whois:RuntimeError"], dr=30.0, topical_relevance=1.0,
               spam_anchor_ratio=0.0, peak_traffic=5000)
    out = scoring.compute_score(sig)
    assert out["score"] >= 0.85, out
    assert out["status"] == "scored", out
    assert _in_bulk(sig, out) is False


def test_known_age_is_scored_and_lands_in_bulk():
    """Контроль: гард бьёт ТОЛЬКО по отказу whois / пустому возрасту. Домен, чей whois ответил,
    приходит `scored` (одобряет человек, Р2), и пакет его берёт — иначе «фикс» просто заморозил
    бы весь пул на поштучном разборе."""
    sig = _sig(age_years=16.0)
    out = scoring.compute_score(sig)
    assert out["status"] == "scored", out
    assert _in_bulk(sig, out) is True


def test_blind_reason_tells_the_truth_about_archive_age():
    """Бейдж обязан говорить ПРАВДУ о том состоянии, в котором показан. Прежний текст —
    «гейт «слишком молодой» не применялся» — на домене с архивным возрастом был ложью: гейт
    применялся, просто по нижней оценке. Непроверенным осталось ДРУГОЕ — занятость."""
    d = Domain(domain="clara-c.ru", wayback_checked=True, prior_flags=dict(_CLEAN_FLAGS),
               score_breakdown={"errors": ["whois:RuntimeError"], "history_evidence": [],
                                "age_source": "wayback"})
    msg = scoring.blind_reason(d)
    assert "архив" in msg and "занятость" in msg, msg
    assert "не применялся" not in msg, msg      # ложь, от которой и чинили
    assert scoring.bulk_ok(d) is False          # и в пакет одобрения не идёт


def test_blind_reason_names_whois_and_ahrefs():
    """Домен, чей whois упал, обязан приехать к куратору С ПОМЕТКОЙ. Без неё он в инбоксе
    неотличим от честно проверенного — и человек штампует непроверенное как проверенное.
    Здесь возраста не дал НИКТО (age_source пуст) — текст про непроверенный возраст правдив."""
    d = Domain(domain="mute.ru", wayback_checked=True, prior_flags=dict(_CLEAN_FLAGS),
               score_breakdown={"errors": ["whois:RuntimeError"], "history_evidence": []})
    # «занятость» есть только в whois-ветке _BLIND_RU (в общей ветке возраста её нет) — так
    # закреплён ключ "whois", а не просто «возраста нет»
    assert "занятость" in scoring.blind_reason(d)
    assert scoring.bulk_ok(d) is False          # и в пакет одобрения не идёт

    # возраст дан (10 лет): исключать домен должна именно пометка Ahrefs, а не пустой возраст
    a = Domain(domain="mute2.ru", wayback_checked=True, prior_flags=dict(_CLEAN_FLAGS),
               age_years=10.0,
               score_breakdown={"errors": ["ahrefs:RuntimeError"], "history_evidence": []})
    assert "Ahrefs" in scoring.blind_reason(a)
    assert scoring.bulk_ok(a) is False


# ---------- 3. воронка целиком ----------
#
# КОНТРАКТ НАСТОЯЩЕГО WaybackClient.classify_history (integrations/wayback.py) — три состояния,
# и только три:
#   1. снимков нет      -> wayback_checked=False, age_years=None, sampled=0
#   2. покрытие мало    -> wayback_checked=False, age_years=<из первого снимка>, sampled=ok
#   3. прочитано        -> wayback_checked=True,  age_years=<из первого снимка>, sampled=ok
# Возраст считается ИЗ snaps[0] ещё до проверки покрытия => `checked=True` без `age_years`
# невозможно. Именно такой стаб стоял здесь раньше — и «доказывал» гард, недостижимый в бою.

class _WaybackAged:
    """Состояние 3: архив прочитан, история чиста, возраст известен (первый снимок)."""
    def __init__(self, age_years: float = 16.0):
        self.age_years = age_years

    def classify_history(self, domain):
        first = datetime.now(timezone.utc) - timedelta(days=int(365.25 * self.age_years))
        return {"prior_flags": dict(_CLEAN_FLAGS), "first_seen": first,
                "age_years": self.age_years, "wayback_checked": True, "sampled": 5,
                "evidence": [{"url": f"http://{domain}/", "timestamp": "20100101000000",
                              "cats": [], "chars": 900}]}


class _WaybackEmpty:
    """Состояние 1: домен не архивировался. Ни истории, ни возраста."""
    def classify_history(self, domain):
        return {"prior_flags": {}, "first_seen": None, "age_years": None,
                "wayback_checked": False, "sampled": 0, "evidence": []}


def _clients(whois, wayback=None):
    class _W:
        def whois_probe(self, dom):
            if isinstance(whois, Exception):
                raise whois
            return whois
    class _B:
        def is_blacklisted(self, dom): return False
    return {"aparser": _W(), "blacklist": _B(),
            "wayback": wayback or _WaybackAged(),
            "webrisk": type("WR", (), {"configured": True, "threats": lambda self, d: []})(),
            "ahrefs": type("Ah", (), {"units_left": lambda self: 2_000_000,
                                      "batch": lambda self, ds: {d: {} for d in ds},
                                      "anchors": lambda self, d, limit=50: [
                                          {"anchor": d, "refdomains": 10, "is_spam": False}],
                                      "metrics_history": lambda self, d, years=5, today=None: []})()}


def _add(**kw) -> int:
    with db.SessionLocal() as s:
        d = Domain(source="backorder", status="discovered", lane="bid",
                   referring_domains=5000, **kw)
        s.add(d); s.commit(); s.refresh(d)
        return d.id


def test_funnel_whois_down_domain_is_not_auto_approved():
    """РЕГРЕССИЯ, сквозная — тот самый clara-c.ru. A-Parser лежит, но Wayback ЖИВ и отдаёт
    возраст 16 лет: домен (lane='bid', RD 5000, история чиста) берёт порог по баллу. Авто-
    одобрения всё равно нет — whois не сказал, свободен ли домен вообще, и архив за него
    этого не скажет. Домен едет в инбокс к человеку, с честной пометкой."""
    did = _add(domain="clara-c.com",
               acquire_deadline=datetime.now(timezone.utc) + timedelta(days=5))
    out = scoring.score_domain(did, _clients(RuntimeError("A-Parser oneRequest: Auth failed")))

    assert out["score"] >= 0.70, out                        # порог взят — гард, а не низкий балл
    assert out["status"] == "scored", out                   # НЕ approved
    assert any(e.startswith("whois:") for e in out["errors"]), out

    with db.SessionLocal() as s:
        d = s.get(Domain, did)
        assert d.status == "scored"
        assert d.age_years == 16.0                          # возраст ЕСТЬ — он из архива
        assert d.score_breakdown["age_source"] == "wayback"
        msg = scoring.blind_reason(d)                       # пометка «вслепую» в инбоксе
        assert "архив" in msg and "занятость" in msg, msg
        assert scoring.bulk_ok(d) is False                  # из пакетного одобрения исключён


def test_funnel_whois_down_and_empty_archive_is_not_auto_approved():
    """Второй достижимый исход того же отказа: whois лёг И архив пуст. Возраста не знает никто,
    историю подтвердить нечем — тем более не одобряем."""
    did = _add(domain="ghost.com",
               acquire_deadline=datetime.now(timezone.utc) + timedelta(days=5))
    out = scoring.score_domain(did, _clients(RuntimeError("whois timeout"), _WaybackEmpty()))

    assert out["status"] == "scored", out
    with db.SessionLocal() as s:
        d = s.get(Domain, did)
        assert d.age_years is None                          # не узнали — и не врём
        assert scoring.blind_reason(d) is not None
        assert scoring.bulk_ok(d) is False


def test_funnel_whois_alive_domain_is_scored_and_bulk_ok():
    """Контроль: когда A-Parser отвечает, тот же домен проходит воронку до `scored` (одобряет человек, Р2) — без пометки
    «вслепую» и с местом в пакете.
    Гард не должен превращать живую воронку в вечный ручной разбор."""
    created = datetime.now(timezone.utc) - timedelta(days=int(365.25 * 16))
    did = _add(domain="old-bid.com",
               acquire_deadline=datetime.now(timezone.utc) + timedelta(days=5))
    out = scoring.score_domain(did, _clients({"available": False, "created": created}))

    assert out["status"] == "scored", out
    with db.SessionLocal() as s:
        d = s.get(Domain, did)
        assert d.age_years and d.age_years > 15
        assert d.score_breakdown["age_source"] == "whois"   # при равном возрасте остаётся RDAP/whois
        assert scoring.blind_reason(d) is None
        assert scoring.bulk_ok(d) is True


def test_funnel_archive_age_still_gates_too_young():
    """Гейт молодости на архивном возрасте РАБОТАЕТ (потому бейдж и не смеет утверждать
    обратное). whois лежит, Wayback говорит «первый снимок год назад» — домен отбраковывается
    как слишком молодой, при том что RD 5000 дал бы ему проходной балл."""
    did = _add(domain="young-archive.com",
               acquire_deadline=datetime.now(timezone.utc) + timedelta(days=5))
    out = scoring.score_domain(did, _clients(RuntimeError("whois down"), _WaybackAged(1.0)))
    assert out["status"] == "rejected" and out["reject_reason"] == "too_young", out


def test_funnel_too_young_still_rejected_when_whois_answers():
    """Гейт молодости — на СТАРШЕЙ из двух дат (Р5), в волне истории: балл его не дублирует (юный
    домен с большим RD по баллу прошёл бы). whois отвечает «год назад», и архив помнит только
    год — домен-однолетка честно отбраковывается, хоть и уже после Wayback."""
    young = datetime.now(timezone.utc) - timedelta(days=365)
    did = _add(domain="young.com",
               acquire_deadline=datetime.now(timezone.utc) + timedelta(days=5))
    out = scoring.score_domain(did, _clients({"available": False, "created": young}, _WaybackAged(1.0)))
    assert out["status"] == "rejected" and out["reject_reason"] == "too_young", out


def test_funnel_whois_down_not_excluded_from_pool():
    """Что ломается, когда гард срабатывает? НИЧЕГО не теряется: домен не отбракован
    (`rejected`) и не завис в `discovered` — он в инбоксе, `scored`, с живым баллом.
    Человек может одобрить его вручную; пакет — не может. Лежащий A-Parser тормозит
    автопилот, но не выбрасывает домены."""
    did = _add(domain="pool.com",
               acquire_deadline=datetime.now(timezone.utc) + timedelta(days=5))
    scoring.score_domain(did, _clients(RuntimeError("whois timeout")))
    with db.SessionLocal() as s:
        d = s.get(Domain, did)
        assert d.status == "scored"           # не rejected и не discovered
        assert d.reject_reason is None
        assert d.score and d.score > 0.0
