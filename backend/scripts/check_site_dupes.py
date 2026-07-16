"""Read-only: есть ли дубли Site по domain_id ДО применения миграции 0016.

0016 при дублях выбирает keeper по числу страниц и может удалить опубликованную
версию/поля/индекс-историю проигравшего (аудит 2026-07-15, P1). Пусто на выходе =
дедуп 0016 no-op, деплой безопасен. Непусто = СТОП, чинить keeper до миграции.

Запуск на боксе (PowerShell):
  docker compose run --rm backend python backend/scripts/check_site_dupes.py
"""
from sqlalchemy import func, select
from app.db import SessionLocal
from app.models.site import Site, Page


def main() -> int:
    with SessionLocal() as db:
        dup_domity = [r[0] for r in db.execute(
            select(Site.domain_id).group_by(Site.domain_id)
            .having(func.count(Site.id) > 1)).all()]
        if not dup_domity:
            print("OK: дублей Site по domain_id нет — миграция 0016 безопасна (no-op дедуп).")
            return 0
        print(f"ВНИМАНИЕ: {len(dup_domity)} доменов с дублями Site. НЕ деплоить 0016 как есть:")
        for did in dup_domity:
            sites = db.execute(select(Site).where(Site.domain_id == did)).scalars().all()
            print(f"  domain_id={did}:")
            for s in sites:
                pages = db.execute(select(Page.url_path, Page.status)
                                   .where(Page.site_id == s.id)).all()
                statuses = ", ".join(f"{p or '/'}:{st}" for p, st in pages) or "(нет страниц)"
                print(f"    site#{s.id} status={s.status} cf_zone={s.cf_zone_id} страницы=[{statuses}]")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
