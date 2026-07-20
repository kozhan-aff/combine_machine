"""Инбокс M1: срочность важнее красоты, вслепую не штампуем, пакет обходит только чистых."""
from datetime import datetime, timedelta, timezone

import pytest

from app.db import SessionLocal
from app.models.domain import Domain


def _add(**kw) -> None:
    with SessionLocal() as db:
        db.add(Domain(source="backorder", **kw))
        db.commit()


def test_inbox_sorts_by_drop_deadline_not_score(client):
    """Домен с дропом завтра стоит выше красивого с дропом через месяц — иначе его теряют."""
    soon = datetime.now(timezone.utc) + timedelta(days=2)
    later = datetime.now(timezone.utc) + timedelta(days=30)
    _add(domain="urgent.ru", status="scored", score=0.60, acquire_deadline=soon)
    _add(domain="pretty.ru", status="scored", score=0.95, acquire_deadline=later)
    html = client.get("/domains").text
    assert html.index("urgent.ru") < html.index("pretty.ru")


def test_urgency_marks_only_near_deadlines(client):
    """Срочность — БЛИЗКИЙ дедлайн, а не наличие дедлайна: у backorder-домена он есть всегда,
    и полоса «у всех» не выделяла бы ничего. Заодно регрессия на naive-даты SQLite:
    голое сравнение с now(tz) уронило бы этот роут TypeError'ом."""
    soon = datetime.now(timezone.utc) + timedelta(days=1)
    later = datetime.now(timezone.utc) + timedelta(days=40)
    _add(domain="soon.ru", status="scored", score=0.5, acquire_deadline=soon)
    _add(domain="later.ru", status="scored", score=0.5, acquire_deadline=later)
    r = client.get("/domains")
    assert r.status_code == 200                       # не TypeError на naive-дате
    assert r.text.count('class="urgent"') == 1        # полоса ровно у одного
    assert "дроп: 1" in r.text                        # и счётчик срочных сходится


def test_blind_domain_is_flagged_in_inbox(client):
    _add(domain="blind.ru", status="scored", score=0.9,
         score_breakdown={"errors": ["wayback:ConnectError"]})
    html = client.get("/domains").text
    assert "история НЕ проверена" in html
    assert "/domains/1/score" in html          # кнопка «перепроверить» рядом


def test_bulk_approve_skips_blind_domains(client):
    """Пакет — решение человека, но НЕ обход гейта: непроверенное в него не попадает.

    `clean.ru` несёт wayback_checked=True НЕ для красоты: «чистый» домен без реально
    прочитанной истории — это и был баг F2 (пустой Wayback ошибки не бросает), и фикстура,
    молчавшая об этом поле, ровно его и покрывала собой."""
    _add(domain="clean.ru", status="scored", score=0.9, wayback_checked=True,
         prior_flags={}, score_breakdown={"errors": []})
    _add(domain="blind.ru", status="scored", score=0.9,
         score_breakdown={"errors": ["wayback:ConnectError"]})
    _add(domain="weak.ru", status="scored", score=0.5, wayback_checked=True,
         prior_flags={}, score_breakdown={"errors": []})
    r = client.post("/domains/bulk-approve", data={"min_score": 0.8}, follow_redirects=False)
    assert r.status_code == 303
    with SessionLocal() as db:
        st = {d.domain: d.status for d in db.query(Domain).all()}
    assert st == {"clean.ru": "approved", "blind.ru": "scored", "weak.ru": "scored"}


def test_bulk_preview_counts(client):
    _add(domain="clean.ru", status="scored", score=0.9, wayback_checked=True,
         prior_flags={}, score_breakdown={"errors": []})
    _add(domain="blind.ru", status="scored", score=0.9,
         score_breakdown={"errors": ["wayback:ConnectError"]})
    body = client.get("/domains/bulk-preview?min_score=0.8").json()
    assert body == {"n": 1, "skipped": 1}


def test_empty_inbox_explains_next_step(client):
    html = client.get("/domains").text
    assert "Решать нечего" in html


def test_pool_holds_full_registry(client):
    _add(domain="raw.ru", status="discovered")
    assert "raw.ru" in client.get("/domains/pool").text
    assert "raw.ru" not in client.get("/domains").text   # сырьё в инбокс не лезет


def test_domains_shows_last_run_failure(client):
    """Task 4's review finding: /api/jobs/live только перечисляет ИДУЩИЕ задачи, поэтому упавшая
    задача триггерит поллер's location.reload() — после чего на /domains ничего не показывало
    отказ. last_runs обязан удержать его видимым через перезагрузку."""
    from app.services import jobs
    with pytest.raises(RuntimeError):
        with jobs.track("score"):
            raise RuntimeError("A-Parser timeout")
    html = client.get("/domains").text
    assert "Оценка упала" in html and "timeout" in html


def test_empty_recheck_explains_itself(client):
    """Дебаг 2026-07-13: перепроверка на пустом инбоксе завершалась за 41 мс со сводкой
    «проверено 0: свободны 0, ЗАНЯТЫ 0...» — оператор прочитал это как сломанную кнопку.
    Пустой прогон обязан назвать причину и следующий шаг."""
    from app.services import jobs, scoring
    out = scoring.recheck_acquirability(limit=10)
    assert out["checked"] == 0
    msg = jobs.last("recheck")["message"]
    assert "проверять нечего" in msg and "Оценить домены" in msg


def test_domains_nudges_when_funnel_never_ran(client):
    """Корень «машина не работает»: 5605 доменов найдено, воронку не запускали НИ РАЗУ
    (в реестре score=null), а инбокс пуст — и панель об этом молчала."""
    _add(domain="raw.ru", status="discovered")
    html = client.get("/domains").text
    assert "не запускалась" in html and "Оценить домены" in html


def test_domains_shows_last_run_summary(client):
    """Не только падения: успешный прогон тоже обязан оставить след — иначе «ничего не
    произошло» и «всё сломалось» выглядят одинаково."""
    from app.services import jobs
    with jobs.track("score") as run:
        jobs.report(run, done=3, total=3, message="прогнано 3 доменов через воронку")
    assert "прогнано 3 доменов" in client.get("/domains").text


def test_reject_reasons_split_threshold_from_dirt(client):
    """Разбор обязан различать «отсеял мой порог» и «объективная грязь» — иначе непонятно,
    что вообще можно крутить на /settings."""
    _add(domain="a.ru", status="rejected", reject_reason="low_rd")
    _add(domain="b.ru", status="rejected", reject_reason="low_rd")
    _add(domain="c.ru", status="rejected", reject_reason="history_dirty")
    html = client.get("/domains").text
    assert "Мало доноров" in html and "Грязная история" in html
    assert "режет порог" in html and "не трогать" in html
    assert "настроить пороги" in html


def test_expired_drop_is_not_urgent_and_not_first(client):
    """Ревью 2026-07-13. «Ближайший дедлайн» ASC — это самая РАННЯЯ дата, то есть УПУЩЕННЫЙ дроп.
    Он вставал первой строкой инбокса и метился «срочным» (_urgent: dl <= now+3д, для прошедшей
    даты всегда True) — оператора звали решать судьбу покойника, которого уже не купить."""
    now = datetime.now(timezone.utc)
    _add(domain="dead.ru", status="scored", score=0.95, lane="bid",
         acquire_deadline=now - timedelta(days=30))     # дроп упущен месяц назад
    _add(domain="alive.ru", status="scored", score=0.50, lane="bid",
         acquire_deadline=now + timedelta(days=1))      # дропается завтра — вот он и важен
    html = client.get("/domains").text
    assert html.index("alive.ru") < html.index("dead.ru"), "покойник обогнал живой дроп"
    assert html.count('class="urgent"') == 1            # срочный ровно один — живой
    assert "дроп: 1" in html


def test_expired_domain_is_marked_in_inbox_and_in_ready(client):
    """Покойник уехал вниз и не «срочный», но без метки выглядит обычным кандидатом: его одобрят
    (в т.ч. пакетом) и пойдут ПОКУПАТЬ. Перепроверка вынесет ему not_acquirable — но позже."""
    now = datetime.now(timezone.utc)
    _add(domain="dead.ru", status="scored", score=0.9, lane="bid",
         acquire_deadline=now - timedelta(days=30))
    _add(domain="deadready.ru", status="approved", score=0.9, lane="bid",
         acquire_deadline=now - timedelta(days=30))
    html = client.get("/domains").text
    assert html.count("окно закрыто") == 2      # и в инбоксе, и в «готовы к выкупу»


def test_projection_deadline_is_labelled_honestly(client):
    """Дата из whois free-date — ПРОЕКЦИЯ «освободится, если не продлят» (она есть даже
    у yandex.ru, живая проба 2026-07-20), а дата из фида — подтверждённый дроп. Панель
    обязана подписывать их по-разному: иначе оператор видит «СРОК ДРОПА» на домене,
    который просто продлевают из года в год, и идёт его выкупать."""
    soon = datetime.now(timezone.utc) + timedelta(days=5)
    _add(domain="projected.ru", status="scored", score=0.7, acquire_deadline=soon,
         score_breakdown={"deadline_source": "whois_projection"})
    html = client.get("/domains").text
    assert "ОСВОБОДИТСЯ*" in html
    assert "СРОК ДРОПА" not in html


def test_feed_deadline_keeps_drop_label(client):
    """Обратная сторона: дедлайн из фида (backorder/cctld) остаётся «СРОК ДРОПА» —
    подпись не должна размыться до бессмысленной для ВСЕХ доменов."""
    soon = datetime.now(timezone.utc) + timedelta(days=5)
    _add(domain="fromfeed.ru", status="scored", score=0.7, acquire_deadline=soon)
    html = client.get("/domains").text
    assert "СРОК ДРОПА" in html
    assert "ОСВОБОДИТСЯ*" not in html


def test_projection_never_shows_window_closed(client):
    """«окно закрыто — домен занят» — утверждение о ФАКТЕ, и над проекцией оно ложно.
    Домен, дождавшийся своей free-date и реально дропнувшийся, приходит в инбокс с
    прошедшей датой (при available=True свежего free-date нет, обновлять нечем). Если
    прогон Score отстал больше чем на DROP_GRACE, свободный домен носил бы красную
    метку «занят» на экране, с которого идут покупать (ревью 2026-07-20)."""
    past = datetime.now(timezone.utc) - timedelta(days=10)
    _add(domain="dropped.ru", status="scored", score=0.7, acquire_deadline=past,
         score_breakdown={"deadline_source": "whois_projection"})
    html = client.get("/domains").text
    assert "dropped.ru" in html
    assert "окно закрыто" not in html


def test_expired_feed_deadline_still_shows_window_closed(client):
    """Обратная сторона: для подтверждённой даты из фида метка остаётся — без неё
    оператор одобряет покойника и идёт его выкупать."""
    past = datetime.now(timezone.utc) - timedelta(days=10)
    _add(domain="sniped.ru", status="scored", score=0.7, acquire_deadline=past)
    html = client.get("/domains").text
    assert "окно закрыто" in html


def test_pool_labels_projection_deadline_too(client):
    """Ревью 2026-07-20 (аудит серии TCI-whois): /domains/pool — ОТДЕЛЬНЫЙ шаблон
    (pool.html) от /domains (domains.html), и метка «проекция, не подтверждённый дроп»
    туда не доехала — тултип показывал голое «дедлайн DD.MM» на любой дате, включая
    whois-проекцию. Это экран для расследований, но именно там оператор ищет причину,
    почему домен занят — врать честной датой нельзя и там."""
    soon = datetime.now(timezone.utc) + timedelta(days=5)
    _add(domain="poolprojected.ru", status="scored", score=0.7, acquire_deadline=soon,
         score_breakdown={"deadline_source": "whois_projection"})
    html = client.get("/domains/pool").text
    assert "прогноз whois" in html
    assert "не подтверждённый дроп" in html


def test_pool_keeps_plain_deadline_label_for_feed_date(client):
    """Обратная сторона в пуле: подтверждённая дата из фида остаётся простым «дедлайн»,
    без ложной пометки «прогноз»."""
    soon = datetime.now(timezone.utc) + timedelta(days=5)
    _add(domain="poolfromfeed.ru", status="scored", score=0.7, acquire_deadline=soon)
    html = client.get("/domains/pool").text
    assert "дедлайн" in html
    assert "прогноз whois" not in html
