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
    """Пакет — решение человека, но НЕ обход гейта: непроверенное в него не попадает."""
    _add(domain="clean.ru", status="scored", score=0.9, score_breakdown={"errors": []})
    _add(domain="blind.ru", status="scored", score=0.9,
         score_breakdown={"errors": ["wayback:ConnectError"]})
    _add(domain="weak.ru", status="scored", score=0.5, score_breakdown={"errors": []})
    r = client.post("/domains/bulk-approve", data={"min_score": 0.8}, follow_redirects=False)
    assert r.status_code == 303
    with SessionLocal() as db:
        st = {d.domain: d.status for d in db.query(Domain).all()}
    assert st == {"clean.ru": "approved", "blind.ru": "scored", "weak.ru": "scored"}


def test_bulk_preview_counts(client):
    _add(domain="clean.ru", status="scored", score=0.9, score_breakdown={"errors": []})
    _add(domain="blind.ru", status="scored", score=0.9,
         score_breakdown={"errors": ["wayback:ConnectError"]})
    body = client.get("/domains/bulk-preview?min_score=0.8").json()
    assert body == {"n": 1, "blind": 1}


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
    assert "Проверка упала" in html and "timeout" in html


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
