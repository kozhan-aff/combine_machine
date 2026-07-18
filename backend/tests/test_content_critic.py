"""Критик редактуры (Спека 4, 2026-07-18) — advisory-оценка черновика. НЕ гейт:
mark_edited работает независимо от critic_score/critic_notes. Формат ответа LLM НЕ
проверен вживую (бокс недоступен в этой итерации) — парсер обязан быть defensive."""
from app.services.content_critic import _parse_critique


def test_parse_critique_reads_score_and_issues():
    text = "БАЛЛ: 72\n- нет конкретных цифр по тарифам\n- слабое disclosure"
    out = _parse_critique(text)
    assert out["score"] == 0.72
    assert out["issues"] == ["нет конкретных цифр по тарифам", "слабое disclosure"]


def test_parse_critique_handles_missing_score_gracefully():
    text = "тут какой-то мусор без баллов"
    out = _parse_critique(text)
    assert out["score"] is None
    assert out["issues"] == []


def test_parse_critique_handles_empty_text():
    out = _parse_critique("")
    assert out["score"] is None
    assert out["issues"] == []


def test_parse_critique_clamps_out_of_range_score():
    # LLM может ошибиться и написать 150 вместо 0-100 — не позволяем score вылезти за [0, 1]
    out = _parse_critique("БАЛЛ: 150\n- всё отлично")
    assert out["score"] == 1.0


import app.db as db
from app.models.offer import Offer
from app.models.site import Site, Page
from app.models.domain import Domain


def _seed_page(body="текст черновика", lang="ru", with_offer=True) -> int:
    with db.SessionLocal() as s:
        d = Domain(domain="crit.ru", source="backorder", status="purchased")
        s.add(d); s.commit(); s.refresh(d)
        site = Site(domain_id=d.id, status="content")
        s.add(site); s.commit(); s.refresh(site)
        offer_id = None
        if with_offer:
            o = Offer(brand="NordVPN", affiliate_link="https://ref/nord")
            s.add(o); s.commit(); s.refresh(o)
            offer_id = o.id
        p = Page(site_id=site.id, url_path="/", title="t", status="draft",
                body=body, lang=lang, offer_id=offer_id)
        s.add(p); s.commit(); s.refresh(p)
        return p.id


def test_critique_page_writes_score_and_notes(monkeypatch):
    from app.services import content_critic
    monkeypatch.setattr(
        "app.integrations.llm.LlmClient.complete",
        lambda self, system, prompt, **kw: "БАЛЛ: 60\n- маловато конкретики")
    pid = _seed_page()
    out = content_critic.critique_page(pid)
    assert out["score"] == 0.6
    assert out["issues"] == ["маловато конкретики"]
    assert out["error"] is None
    with db.SessionLocal() as s:
        p = s.get(Page, pid)
        assert p.critic_score == 0.6
        assert p.critic_notes == {"issues": ["маловато конкретики"]}
        assert p.critic_checked_at is not None
        assert p.status == "draft"          # ГЕЙТ НЕ ТРОНУТ


def test_critique_page_handles_empty_llm_response(monkeypatch):
    """LlmClient.complete уже возвращает "" на фильтр/blocked (см. integrations/llm.py)
    — критик обязан честно сказать "не смог оценить", а не подставить 0 как результат."""
    from app.services import content_critic
    monkeypatch.setattr(
        "app.integrations.llm.LlmClient.complete",
        lambda self, system, prompt, **kw: "")
    pid = _seed_page()
    out = content_critic.critique_page(pid)
    assert out["score"] is None
    assert out["error"] is not None
    with db.SessionLocal() as s:
        p = s.get(Page, pid)
        assert p.critic_score is None
        assert p.critic_checked_at is not None   # факт ПОПЫТКИ зафиксирован
        assert p.status == "draft"


def test_critique_page_raises_on_missing_page():
    from app.services import content_critic
    import pytest
    with pytest.raises(ValueError):
        content_critic.critique_page(999999)
