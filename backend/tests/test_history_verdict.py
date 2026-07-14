"""История домена: «никто не смотрел» ≠ «чисто» (аудит, F2).

Wayback, не нашедший НИ ОДНОГО снимка, честно возвращает `wayback_checked=False` — и НЕ
бросает исключение: `score_breakdown.errors` при этом ПУСТ. Прежний `blind_reason` судил
только по `errors`, поэтому такой домен приезжал в инбокс со score 0.825, подписью
«история чистая» и попадал в пакетное одобрение — гейт курации штамповал непроверенное как
проверенное. Вердикт истории теперь считается ЯВНО (`history_verdict`), а не выводится из
факта «ошибок не было».
"""
from datetime import datetime, timezone, timedelta

import app.db as db
from app.models.domain import Domain
from app.services import scoring

_CLEAN_FLAGS = {c: False for c in ("adult", "pharma", "casino", "gambling", "spam")}


def _add(**kw) -> int:
    with db.SessionLocal() as s:
        d = Domain(source="backorder", **kw)
        s.add(d); s.commit(); s.refresh(d)
        return d.id


def _clients(wayback, created):
    """Воронка целиком на фейках: whois «занят + дата регистрации» (домен создаётся с lane='bid',
    поэтому T1 короткозамкнут лейном и «занят» — норма), РКН/блэклист чисты, эхо есть."""
    class _W:
        def whois_probe(self, dom): return {"available": False, "created": created}
    class _R:
        def is_listed(self, dom): return False
    class _B:
        def is_blacklisted(self, dom): return False
    class _S:
        def indexed_echo(self, dom): return True
    return {"aparser": _W(), "rkn": _R(), "blacklist": _B(), "searxng": _S(), "wayback": wayback}


# ---- вердикт ----

def test_verdict_unknown_when_wayback_saw_nothing():
    """Снимков нет — историю НИКТО не смотрел. Ошибки при этом тоже нет: вот в чём подвох."""
    d = Domain(domain="ghost.ru", wayback_checked=False, prior_flags={},
               score_breakdown={"errors": [], "history_evidence": []})
    assert scoring.history_verdict(d) == "unknown"
    assert "история НЕ проверена" in scoring.blind_reason(d)


def test_verdict_clean_only_when_wayback_really_checked():
    d = Domain(domain="ok.ru", wayback_checked=True, prior_flags=_CLEAN_FLAGS,
               score_breakdown={"errors": []})
    assert scoring.history_verdict(d) == "clean"
    assert scoring.blind_reason(d) is None


def test_verdict_dirty_when_prior_flags_set():
    d = Domain(domain="bad.ru", wayback_checked=True, score_breakdown={"errors": []},
               prior_flags={**_CLEAN_FLAGS, "casino": True})
    assert scoring.history_verdict(d) == "dirty"


def test_verdict_dirty_beats_missing_check():
    """Грязь известна — она главнее незнания: домен с флагом не «unknown», а «dirty».

    Флаг тут `gambling`, а не снятый `topic_switch` (аудит 2026-07-14, F4): вердикт истории
    держится на категориях HARD_REJECT_FLAGS, и проверять его надо тем, что реально живёт.
    """
    d = Domain(domain="bad2.ru", wayback_checked=False, prior_flags={"gambling": True},
               score_breakdown={"errors": []})
    assert scoring.history_verdict(d) == "dirty"


def test_blind_reason_still_names_dead_checks():
    """РКН/блэклист/эхо остались «вслепую» по errors — история их не поглотила."""
    d = Domain(domain="r.ru", wayback_checked=True, prior_flags=_CLEAN_FLAGS,
               score_breakdown={"errors": ["rkn:ConnectError"]})
    assert "РКН" in scoring.blind_reason(d)


# ---- пакетное одобрение ----

def test_unchecked_history_stays_out_of_bulk(client):
    """РЕПРО АУДИТА: score 0.825, errors пуст, снимков не было — домен уходил в пакет как чистый."""
    _add(domain="ghost.ru", status="scored", score=0.825, wayback_checked=False,
         prior_flags={}, score_breakdown={"errors": []})
    _add(domain="ok.ru", status="scored", score=0.825, wayback_checked=True,
         prior_flags=_CLEAN_FLAGS, score_breakdown={"errors": []})
    assert client.get("/domains/bulk-preview?min_score=0.8").json() == {"n": 1, "skipped": 1}
    r = client.post("/domains/bulk-approve", data={"min_score": 0.8}, follow_redirects=False)
    assert r.status_code == 303
    with db.SessionLocal() as s:
        st = {d.domain: d.status for d in s.query(Domain).all()}
    assert st == {"ghost.ru": "scored", "ok.ru": "approved"}   # непроверенный НЕ одобрен пакетом


def test_inbox_warns_instead_of_claiming_clean(client):
    _add(domain="ghost.ru", status="scored", score=0.825, wayback_checked=False,
         prior_flags={}, score_breakdown={"errors": []})
    html = client.get("/domains").text
    assert "история НЕ проверена" in html
    assert "история чистая" not in html          # ложное «чисто» — то самое, что штамповали


# ---- предикат не имеет права раздвоиться (ревью Task 2, «С правками») ----

def test_row_and_bulk_share_one_predicate(client, monkeypatch):
    """Раньше `_bulk_candidates` и подпись «история чистая» в строке инбокса реконструировали
    одно и то же условие НЕЗАВИСИМО (Python в panel.py, Jinja в domains.html). Сегодня они
    совпадают только потому, что `blind_reason` гарантированно возвращает не-None для КАЖДОГО
    'unknown' — свяжись этот факт (новый ранний `return None`, четвёртое значение вердикта),
    и строка сказала бы «история чистая» ровно там, где пакет домен уже не берёт.

    Симулируем эту будущую регрессию монки-патчем `blind_reason` -> всегда None, оставляя
    историю НЕПРОЧИТАННОЙ (`wayback_checked=False`, значит `history_verdict == 'unknown'`).
    `bulk_ok` обязан остаться False (он сверяет ЕЩЁ И history_verdict, не только blind_reason),
    и строка не имеет права нести «история чистая» для домена, которого пакет не берёт.
    """
    _add(domain="ghost2.ru", status="scored", score=0.9, wayback_checked=False,
         prior_flags={}, score_breakdown={"errors": []})
    monkeypatch.setattr(scoring, "blind_reason", lambda d: None)
    # решающая проверка: строка инбокса и пакет обязаны совпасть — ни то ни другое не
    # признаёт этот домен «чистым», хотя blind_reason (искусственно) молчит.
    assert client.get("/domains/bulk-preview?min_score=0.5").json() == {"n": 0, "skipped": 1}
    html = client.get("/domains").text
    assert "история чистая" not in html
    # и предикат в изоляции — тот же вывод, без похода через HTTP/шаблон
    unread = Domain(wayback_checked=False, prior_flags={}, score_breakdown={"errors": []})
    assert scoring.bulk_ok(unread) is False


# ---- улики ----

def test_inbox_shows_wayback_evidence(client):
    """Вердикт ошибается (это и доказал аудит) — куратор обязан мочь перепроверить его глазами:
    те же снимки, по которым судила машина."""
    _add(domain="thin.ru", status="scored", score=0.9, wayback_checked=False, prior_flags={},
         score_breakdown={"errors": [], "history_evidence": [
             {"url": "http://thin.ru/casino/", "timestamp": "20190312101500",
              "cats": ["casino"]}]})
    html = client.get("/domains").text
    assert "web.archive.org/web/20190312101500/http://thin.ru/casino/" in html
    assert "12.03.2019" in html and "казино" in html    # дата и категория — по-русски


def test_inbox_never_labels_a_textless_snapshot_as_clean(client):
    """Снимок-редирект (казино за meta-refresh/`location.href`) скачивается с 200 + text/html,
    но видимого текста на нём НЕТ. В строке улик он не имеет права выглядеть как прочитанный
    и чистый — иначе куратор штампует ровно то, что машина не смотрела (ревью Задачи 3)."""
    _add(domain="stub.ru", status="scored", score=0.87, wayback_checked=False, prior_flags={},
         score_breakdown={"errors": [], "sampled": 0, "history_evidence": [
             {"url": "http://stub.ru/", "timestamp": "20250601000000", "cats": [], "chars": 0}]})
    html = client.get("/domains").text
    assert "без текста — не читан" in html
    assert "история чистая" not in html


# ---- сквозь воронку и наружу в JSON ----

def test_funnel_marks_history_unknown_without_errors(client):
    """Сквозной путь: Wayback вернул «снимков нет» — статус scored, errors ПУСТ, и всё же
    домен не годится в пакет."""
    class _WB:
        def classify_history(self, dom):
            return {"prior_flags": {}, "first_seen": None, "age_years": None,
                    "wayback_checked": False, "sampled": 0, "evidence": []}
    did = _add(domain="ghost.ru", status="discovered", lane="bid", referring_domains=5000)
    old = datetime.now(timezone.utc) - timedelta(days=365 * 12)
    out = scoring.score_domain(did, clients=_clients(_WB(), old))
    assert out["status"] == "scored" and out["errors"] == []
    with db.SessionLocal() as s:
        assert scoring.history_verdict(s.get(Domain, did)) == "unknown"
    assert client.get("/domains/bulk-preview?min_score=0.5").json()["n"] == 0


def test_json_api_reports_history_not_fake_clean(client):
    """JSON-двойник панели отдавал `clean: true` — а это лишь «не отклонён», не чистота истории."""
    _add(domain="ghost.ru", status="scored", score=0.825, clean=True,
         wayback_checked=False, prior_flags={}, score_breakdown={"errors": []})
    row = client.get("/api/domains/").json()[0]
    assert row["history"] == "unknown" and "clean" not in row


def test_stale_verdict_is_named_but_not_locked(client):
    """Правило «не затирать проверенное» (аудит F9/C2) оставило домен, чью историю проверили
    РАНЬШЕ, а сегодня Wayback не ответил, с вердиктом `clean` — и про сегодняшний отказ архива
    не говорил НИКТО (ошибка живёт в score_breakdown.errors, куда куратор не смотрит).

    Пакет его берёт — и это осознанно: вердикт держится на реальных прошлых уликах, авто-approve
    гардится по sig ТЕКУЩЕГО прогона, а запирать домен из-за ТРАНЗИЕНТНОГО сбоя архива значило бы
    завести ту самую тихую ловушку, от которой ветка избавлялась. Но сказать правду в строке —
    обязан."""
    _add(domain="stale.ru", status="scored", score=0.825, wayback_checked=True,
         prior_flags=_CLEAN_FLAGS, score_breakdown={"errors": ["wayback:RuntimeError"]})
    assert client.get("/domains/bulk-preview?min_score=0.8").json() == {"n": 1, "skipped": 0}
    html = client.get("/domains").text
    assert "сегодня Wayback не ответил" in html, "строка молчит о том, что архив сегодня лежал"
