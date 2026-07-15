"""Регрессии Задачи 18 (аудит 2026-07-14, F26+F27+F28).

(а) F26: publish_site должен использовать offer/lang, ЗАФИКСИРОВАННЫЕ на странице в момент
    генерации (content.generate_site), а не пересчитывать «текущий активный оффер сайта» заново
    в момент публикации. До фикса `publish._pick_offer` гонял ТУ ЖЕ query, что и генерация, и
    если набор SiteOffer сайта менялся между шагами (оператор добавил/сменил оффер с меньшим
    Offer.id), страница, написанная про бренд A на английском, публиковалась со ссылкой на
    бренд B и `<html lang="ru">`.
(б) F28: affiliate_link со схемой javascript: отклоняется НА СОЗДАНИИ оффера (panel.py-роут) И
    не рендерится в render_html (content.py) — defense in depth, обе точки, не одна.
"""
import pytest

import app.db as db
from app.config import settings
from app.models.domain import Domain
from app.models.site import Site, Page
from app.models.offer import Offer, SiteOffer


@pytest.fixture(autouse=True)
def _loopback_panel(monkeypatch):
    """Как в test_aapanel_errors.py: клиент aaPanel fail-close'ит на не-loopback URL без
    CA-бандла, а .env оператора несёт контейнерный путь к сертификату — тест не должен
    зависеть ни от того, ни от другого."""
    monkeypatch.setattr(settings, "AAPANEL_URL", "https://127.0.0.1:8888")
    monkeypatch.setattr(settings, "AAPANEL_CA_BUNDLE", "")
    monkeypatch.setattr(settings, "AAPANEL_API_KEY", "testsk")


def _make_site(domain="cc.ru") -> int:
    with db.SessionLocal() as s:
        d = Domain(domain=domain, source="backorder", status="approved")
        s.add(d)
        s.commit()
        s.refresh(d)
        site = Site(domain_id=d.id, status="content", doc_root=f"/www/wwwroot/{domain}")
        s.add(site)
        s.commit()
        s.refresh(site)
        return site.id


def _add_offer(brand, lang, link="https://ex.com/aff") -> int:
    with db.SessionLocal() as s:
        o = Offer(brand=brand, affiliate_link=link, language=lang, active=True)
        s.add(o)
        s.commit()
        s.refresh(o)
        return o.id


# ── (а) страница публикуется под тот оффер/язык, под который писалась ─────────

def test_publish_uses_offer_and_lang_captured_at_generation(monkeypatch):
    """РЕГРЕССИЯ (F26). Воспроизводит реальный сценарий бага: оффер сайта меняется ПОСЛЕ
    генерации, ДО публикации. Страница обязана уехать со ссылкой/языком оффера A (под который
    её реально писал LLM), а не оффера B (который стал «текущим» к моменту публикации)."""
    from app.services import content, publish
    from app.integrations.aapanel import AaPanelClient

    site_id = _make_site()
    offer_a = _add_offer("NordVPN", "en", link="https://ex.com/nord")
    with db.SessionLocal() as s:
        s.add(SiteOffer(site_id=site_id, offer_id=offer_a))
        s.commit()

    # эхо промпта в тело — подтверждает, что писали именно про NordVPN
    monkeypatch.setattr("app.integrations.llm.LlmClient.complete",
                        lambda self, system, prompt, **kw: f"<p>{prompt}</p>")
    assert content.generate_site(site_id, lang="en") == 3

    with db.SessionLocal() as s:
        pages = s.query(Page).filter_by(site_id=site_id).all()
        assert all(p.lang == "en" and p.offer_id == offer_a for p in pages)   # зафиксировано
        for p in pages:
            p.status = "edited"
        s.commit()

    # ПОСЛЕ генерации оператор меняет активный оффер сайта на B (меньший Offer.id — старый
    # _pick_offer/order_by(Offer.id) выбрал бы именно его, будь он вызван заново при публикации)
    offer_b = _add_offer("Surfshark", "ru", link="https://ex.com/surf")
    with db.SessionLocal() as s:
        s.query(SiteOffer).filter_by(site_id=site_id).delete()
        s.add(SiteOffer(site_id=site_id, offer_id=offer_b))
        s.commit()

    written = {}

    def _post(self, path, data=None):
        if "CreateFile" in path:
            return {"status": True, "msg": "Created successfully!"}
        if "SaveFileBody" in path:
            written[data["path"]] = data["data"]
            return {"status": True, "msg": "Saved successfully!"}
        raise AssertionError(path)  # pragma: no cover

    monkeypatch.setattr(AaPanelClient, "_post", _post)
    out = publish.publish_site(site_id)
    assert out["status"] == "published"

    home = written["/www/wwwroot/cc.ru/index.html"]
    assert "<html lang='en'>" in home                 # язык генерации, НЕ 'ru' от оффера B
    assert "ex.com/nord" in home                       # ссылка на оффер A ...
    assert "ex.com/surf" not in home                   # ... а НЕ на оффер B


def test_publish_legacy_page_without_offer_id_falls_back_to_current_offer(monkeypatch):
    """ГРАНИЦА. Legacy-страница (offer_id/lang пусты — создана до миграции 0018) не имеет
    сохранённой истории: publish держит прежнее поведение (текущий активный оффер сайта),
    а не падает и не публикует страницу вовсе без оффера."""
    from app.services import publish
    from app.integrations.aapanel import AaPanelClient

    site_id = _make_site(domain="legacy.ru")
    offer_id = _add_offer("NordVPN", "ru", link="https://ex.com/nord")
    with db.SessionLocal() as s:
        s.add(SiteOffer(site_id=site_id, offer_id=offer_id))
        s.add(Page(site_id=site_id, url_path="/", title="стр", status="edited",
                    body="<p>текст</p>", lang=None, offer_id=None))
        s.commit()

    written = {}

    def _post(self, path, data=None):
        if "CreateFile" in path:
            return {"status": True, "msg": "Created successfully!"}
        if "SaveFileBody" in path:
            written[data["path"]] = data["data"]
            return {"status": True, "msg": "Saved successfully!"}
        raise AssertionError(path)  # pragma: no cover

    monkeypatch.setattr(AaPanelClient, "_post", _post)
    out = publish.publish_site(site_id)
    assert out["status"] == "published"
    home = written["/www/wwwroot/legacy.ru/index.html"]
    assert "ex.com/nord" in home and "<html lang='ru'>" in home


# ── (б) allowlist схем http/https — создание оффера И render_html ─────────────

def test_offer_create_rejects_javascript_scheme(client):
    """РЕГРЕССИЯ (F28). panel.py::offer_create_action проверял только .strip() truthiness —
    javascript: проходил как валидная партнёрская ссылка."""
    r = client.post("/offers/create", data={"brand": "Evil",
                                             "affiliate_link": "javascript:alert(1)"},
                     follow_redirects=False)
    assert r.status_code in (302, 303)
    with db.SessionLocal() as s:
        assert s.query(Offer).filter_by(brand="Evil").first() is None   # оффер НЕ создан


def test_offer_create_accepts_https(client):
    """Контроль: легитимная http(s)-ссылка по-прежнему проходит."""
    r = client.post("/offers/create", data={"brand": "NordVPN",
                                             "affiliate_link": "https://ex.com/aff"},
                     follow_redirects=False)
    assert r.status_code in (302, 303)
    with db.SessionLocal() as s:
        assert s.query(Offer).filter_by(brand="NordVPN").first() is not None


def test_pipeline_create_offer_rejects_javascript_scheme(client):
    """defense in depth, третья точка: JSON API-двойник (`pipeline.py::create_offer`) — ровно
    та «форма обойдена прямым API-вызовом», от которой предостерегает комментарий в panel.py."""
    r = client.post("/api/offers", json={"brand": "Evil", "affiliate_link": "javascript:alert(1)"})
    assert r.status_code == 400
    with db.SessionLocal() as s:
        assert s.query(Offer).filter_by(brand="Evil").first() is None


def test_render_html_never_emits_javascript_href():
    """РЕГРЕССИЯ (F28), defense in depth. Даже если оффер с опасной схемой каким-то образом
    оказался в БД (легаси-данные, будущий источник данных), render_html не должен вставить её
    в href — html.escape() экранирует спецсимволы, но НЕ проверяет схему."""
    from types import SimpleNamespace
    from app.services.content import render_html

    page = SimpleNamespace(title="T", body="<p>x</p>")
    offer = SimpleNamespace(brand="Evil", affiliate_link="javascript:alert(1)", promo_code=None)
    out = render_html(page, offer)
    assert "javascript:" not in out
    assert "<a href=" not in out            # ссылки нет вовсе — не «безопасная замена»
