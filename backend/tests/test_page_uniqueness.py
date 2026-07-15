"""Одна страница на путь сайта — инвариант БД, а не удача SELECT'а (аудит F17, шаг 5).

`content.generate_site` спрашивает «страница с таким путём уже есть?» SELECT'ом, а вставляет
отдельным COMMIT'ом. Между ними — окно, и в нём живёт ВТОРОЙ писатель: кнопка «сгенерировать»
в панели и стадия `generate` автопилотного свипа, крутящаяся В ДРУГОМ ПРОЦЕССЕ (worker). Под
READ COMMITTED чужая незакоммиченная строка не видна: оба честно видят «страницы нет» и оба её
создают. Две строки на один url_path рендерятся В ОДИН ФАЙЛ (publish._target_path: docroot +
url_path + /index.html) — публикуется записанная последней: человек редактирует одну, а в
интернет уходит другая, и панель показывает обе как настоящие.

Поэтому «одна страница на путь» переезжает в БД (уникальный индекс uq_page_per_path — модель +
миграция 0014), а generate_site учится ПРОИГРЫВАТЬ гонку: ловит IntegrityError, откатывает ВЕСЬ
батч и говорит человеку словами, а не роняет SQL-трейс в сводку свипа.
"""
import pathlib

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

import app.db as db
from app.integrations.llm import LlmClient
from app.models.domain import Domain
from app.models.site import Page, Site
from app.services import content


def _site(niche="VPN") -> int:
    with db.SessionLocal() as s:
        d = Domain(domain="drop.ru", source="backorder", status="approved")
        s.add(d)
        s.commit()
        s.refresh(d)
        site = Site(domain_id=d.id, status="content", niche=niche)
        s.add(site)
        s.commit()
        s.refresh(site)
        return site.id


def _pages(site_id: int) -> list[Page]:
    with db.SessionLocal() as s:
        return list(s.execute(select(Page).where(Page.site_id == site_id)
                              .order_by(Page.url_path)).scalars().all())


def test_db_refuses_a_second_page_per_path(sqlite_db):
    """САМ ИНВАРИАНТ: вторая строка на тот же (site_id, url_path) в базу не ложится.

    Вставляем ровно то, что коммитит проигравшая гонку транзакция (её SELECT чужой страницы не
    увидел, поэтому дошла до INSERT). До индекса база принимала её молча — и один путь получал
    две строки, то есть два разных тела на один файл в интернете.
    """
    sid = _site()
    with db.SessionLocal() as s:
        s.add(Page(site_id=sid, url_path="/", title="первая", status="draft", body="<p>a</p>"))
        s.commit()
    with db.SessionLocal() as s:
        s.add(Page(site_id=sid, url_path="/", title="вторая", status="draft", body="<p>b</p>"))
        with pytest.raises(IntegrityError):
            s.commit()
    assert len(_pages(sid)) == 1                          # вторая страница не существует


def test_generate_site_loses_the_generation_race(sqlite_db, monkeypatch):
    """Гонка двух генераций (кнопка панели + свип воркера): проигравший откатывает ВЕСЬ свой батч
    и поднимает ValueError русским текстом — не плодит вторую строку и не роняет SQL в сводку.

    Момент гонки воспроизводим точно (как в test_order_uniqueness): чужой процесс коммитит
    страницу "/" ПОСЛЕ того, как наш SELECT «страница есть?» вернул None (мы её не видели), и ДО
    нашего COMMIT'а. Хук — первый llm.complete(): это и есть точка внутри цикла между проверкой
    существования и вставкой. Победитель делает ровно то, что сделал бы настоящий второй процесс:
    кладёт страницу тем же путём своим отдельным коммитом.
    """
    sid = _site()
    other: dict = {}

    def racing_complete(self, system, prompt, **kw):
        if not other:                                    # ровно один раз — это гонка, а не цикл
            with db.SessionLocal() as o:                 # ДРУГОЙ процесс: свой сеанс, свой коммит
                p = Page(site_id=sid, url_path="/", title="чужая",
                         status="draft", body="<p>их черновик</p>")
                o.add(p)
                o.commit()
                o.refresh(p)
                other["id"] = p.id
        return "<h2>наш черновик</h2><p>текст</p>"

    monkeypatch.setattr(LlmClient, "complete", racing_complete)

    with pytest.raises(ValueError, match="другой прогон"):
        content.generate_site(sid)

    pages = _pages(sid)
    assert [p.id for p in pages] == [other["id"]], "проигравший обязан НЕ оставить ни одной своей строки"
    assert [p.url_path for p in pages] == ["/"], \
        "весь батч (/, /vs, /setup) откатился целиком, а не досыпал остаток поверх чужой генерации"


def test_migration_deletes_index_history_before_pages(sqlite_db):
    """Миграция 0014 обязана удалить index_history проигравших дублей ДО самих страниц.

    Тесты миграций не гоняют (харнесс — create_all, FK-энфорсмент выключен), поэтому сторожим
    ТЕКСТОМ — тот же приём, что test_order_uniqueness::test_index_predicate_matches_the_migration.
    FK index_history.page_id -> pages.id объявлен без ondelete (= NO ACTION); PostgreSQL энфорсит
    его и на DELETE родителя с детьми поднимает ForeignKeyViolation, откатывая ВСЮ транзакцию
    миграции и обрывая git-pull-деплой. А дети у проигравшей published-страницы есть штатно
    (check_index пишет IndexHistory каждой published-странице). Значит: детей — раньше родителя.
    """
    up = (pathlib.Path(__file__).resolve().parents[1]
          / "alembic" / "versions" / "0014_page_uniqueness.py").read_text(encoding="utf-8")
    up = up[up.index("def upgrade"):up.index("def downgrade")]
    ih = up.find("DELETE FROM index_history")
    pg = up.find("DELETE FROM pages")
    assert ih != -1, "миграция не чистит index_history проигравших дублей — FK уронит деплой на боксе"
    assert 0 <= ih < pg, "index_history надо удалить ДО pages (FK: дети раньше родителя)"
