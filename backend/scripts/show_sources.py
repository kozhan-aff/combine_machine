"""Read-only: текущее scoring_settings.sources_enabled ДО миграции 0015.

0015 безусловно ставит cctld/reg_ru/sweb=false (аудит F21). Запиши вывод ПЕРЕД
git-pull, чтобы вернуть ручной выбор через /settings ПОСЛЕ. Витрины (cctld/reg_ru/
sweb) жгут платный Ahrefs и дают сырьё без RD/лейна — включать осознанно.

Запуск на боксе (PowerShell):
  docker compose run --rm backend python backend/scripts/show_sources.py
"""
import json
from sqlalchemy import select
from app.db import SessionLocal
from app.models.settings import ScoringSettings


def main() -> int:
    with SessionLocal() as db:
        row = db.execute(select(ScoringSettings)).scalars().first()
        if row is None:
            print("scoring_settings пуста — дефолты кода, миграция 0015 ничего не сотрёт.")
            return 0
        print("sources_enabled ДО 0015:", json.dumps(row.sources_enabled, ensure_ascii=False))
        print("После git-pull вернуть нужные тумблеры на /settings вручную.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
