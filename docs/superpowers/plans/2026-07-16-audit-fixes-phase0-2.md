# План исправлений по аудитам 2026-07-15 — Фазы 0–2

> **Для агентов-исполнителей:** ОБЯЗАТЕЛЬНЫЙ СУБ-НАВЫК — используй
> superpowers:subagent-driven-development (рекомендуется) или
> superpowers:executing-plans, чтобы исполнять этот план задача-за-задачей. Шаги
> размечены чекбоксами (`- [ ]`). Модель-исполнитель — **Sonnet 5**: код в шагах
> дан целиком, домысливать нечего; где нужно предварительно прочитать файл — это
> сказано явным шагом.

**Goal:** Закрыть 1 блокирующую (P0) и важные (P1/P2) регрессии из повторного
аудита `2026-07-15-latest-run-reaudit.md`, чтобы `main` можно было деплоить на
живой PostgreSQL/Cloudflare без краша статуса свипа, ложной классификации
CF-токена, немого проглатывания ошибок и снятого предохранителя провижна.

**Architecture:** Точечные фиксы существующих сервисов и моделей, без нового
пайплайна. Три новые линейные Alembic-миграции (расширение колонки + два набора
колонок-наблюдений). Ноль изменений в хард-гейтах (деньги/редактура) и в контракте
оркестратора «двигать стадии только до гейтов».

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.x, Alembic, Jinja2, PostgreSQL
16. Тесты офлайн: `backend/tests/` (SQLite через `Base.metadata.create_all`,
autouse-фикстура `_no_live_network` режет живую сеть, ловушки — `BaseException`).

## Global Constraints (проверяет combine-reviewer на КАЖДОЙ задаче)

- **Денежный гейт:** заказ провайдеру ТОЛЬКО при `confirmed_by_human=true`;
  оркестратор/сервисы/шедулеры НИКОГДА не зовут `confirm_order` /
  `execute_confirmed_order` / `mark_caught`. Ни одна задача плана этого не касается.
- **Редактурный гейт:** публикация ТОЛЬКО из `Page.status == "edited"`;
  `draft→edited` (`mark_edited`) — только человек. Ни одна задача этого не касается.
- **`integrations/` = только транспорт, логика в `services/`.** Новый CF-эндпоинт
  (Task 5) — метод-транспорт в `integrations/cloudflare.py`, решение о значении —
  в `services/cf_sync.py`.
- **Секреты только в `.env`;** значение секрета никогда не попадает в
  `last_error_safe`/логи (в CF-коде уже соблюдено, не сломать).
- **Тесты герметичны:** живой сети нет; тест проверяет реальное поведение, не мок.
  `pyflakes` чист. Вывод тестов без варнингов.
- **UI на русском; светлая холодная CMS, акцент `#2563c9`;** CSS-классы объявлены
  в `base.html`, контент-шаблоны — только семантика. Использовать УЖЕ существующие
  классы (`.b-warn`, `.led-warn`), новых не плодить.
- **Миграции линейны, идемпотентны;** голова сейчас `0018`. Перед записью
  `down_revision` новой миграции всегда сверься: `.venv/bin/alembic -c backend/alembic.ini heads`.
- **Коммиты:** ветка от `main`, heredoc `git commit -F -` (не `-m` — бэктики ловит
  zsh), трейлер `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
- **Бокс — Windows/PowerShell,** репо `D:\combine_machine`: команды для оператора
  без `~`, без unix-путей, без inline `#`.

## Порядок и приоритет

Строгий: **Task 1 → 2 → 3 (Фаза 0) → 4 → 5 → 6 (Фаза 1) → 7 → 8 (Фаза 2)**.
Фаза 0 — блокеры деплоя, обязана уйти до `git-pull` + миграций на боксе.
**Фаза 3 (F3, семантика `Offer.active`) — гейт-решение пользователя, кода в этом
плане нет.** **Фаза 4 (крупный редизайн) — отдельные спеки через
superpowers:brainstorming, в этот план НЕ входит** (см. «За рамками»).

## File Structure (что и зачем трогаем)

| Файл | Задача | Ответственность изменения |
|---|---|---|
| `backend/app/models/autonomy.py` | 1 | Ширина `status` 16→32 |
| `backend/alembic/versions/0019_*.py` | 1 | `ALTER autonomy_runs.status TYPE VARCHAR(32)` |
| `backend/scripts/check_site_dupes.py` | 2 | read-only диагностика дублей `Site.domain_id` перед 0016 |
| `backend/scripts/show_sources.py` | 3 | read-only снимок тумблеров источников перед 0015 |
| `backend/app/services/cf_legacy.py` | 4 | legacy-токен = `user`, не выводить из ACCOUNT_ID |
| `backend/app/integrations/cloudflare.py` | 5 | новый транспорт `get_universal_ssl` |
| `backend/app/services/cf_sync.py` | 5,6 | Universal SSL своим эндпоинтом; DNS/cert ошибки не глотать |
| `backend/app/models/cloudflare.py` | 6 | поля `dns_error_safe`/`cert_error_safe` на zone-mirror |
| `backend/alembic/versions/0020_*.py` | 6 | +2 колонки наблюдений |
| `backend/app/templates/settings_cloudflare.html` | 6 | led-warn при ошибке DNS/cert зоны |
| `backend/app/models/site.py` | 7 | `Site.last_attempt_at` для ротации провижна |
| `backend/alembic/versions/0021_*.py` | 7 | +колонка `last_attempt_at` |
| `backend/app/services/orchestrator.py` | 7,8 | кап на attempts; сигнал «свип с замечаниями» |
| `backend/app/services/jobs.py` | 8 | терминал `done_warn` + `jobs.finish()` |
| `backend/app/templates/dashboard.html` | 8 | ветка рендера `done_warn` через `.b-warn` |

---

## Setup (один раз, до Task 1)

- [ ] **Ветка от main**

```bash
cd /Users/kozhan/Documents/PROJECTS/combine_machine
git checkout main && git pull --ff-only
git checkout -b fix/audit-2026-07-15-phase0-2
.venv/bin/python -m pytest backend/tests/ -q   # baseline: 588 passed
```

Ожидается: `588 passed`. Если нет — СТОП, база не та.

---

## Task 1 — F0.1 (P0): `autonomy_runs.status` не вмещает `completed_with_errors`

**Почему:** `orchestrator.py:377` пишет статус `"completed_with_errors"` (21 символ)
в колонку `String(16)`. SQLite длину не проверяет (тесты зелёные), PostgreSQL при
первом свипе с упавшей сущностью отклонит INSERT (`StringDataRightTruncation`) →
краш в `finally`, `run_sweep()` не вернёт сводку. **Блокер деплоя.**

**Files:**
- Modify: `backend/app/models/autonomy.py:51`
- Create: `backend/alembic/versions/0019_autonomy_status_width.py`
- Test: `backend/tests/test_autonomy_config.py` (добавить один тест)

**Interfaces:**
- Produces: `AutonomyRun.__table__.c.status.type.length == 32` (Task 8 полагается,
  что статус свипа влезает; для JobRun это отдельная колонка, её не трогаем).

- [ ] **Step 1: Написать падающий тест (герметичный, ловит регрессию без живого PG)**

В `backend/tests/test_autonomy_config.py` добавить:

```python
def test_autonomy_status_column_fits_terminal_statuses():
    # На SQLite VARCHAR-длина не enforce-ится, поэтому проверяем САМУ схему, а не INSERT:
    # колонка обязана вмещать самый длинный терминальный статус свипа.
    from app.models.autonomy import AutonomyRun
    longest = "completed_with_errors"          # 21 символ, orchestrator.py:377
    assert AutonomyRun.__table__.c.status.type.length >= len(longest)
```

- [ ] **Step 2: Прогнать — убедиться, что падает**

Run: `.venv/bin/python -m pytest backend/tests/test_autonomy_config.py::test_autonomy_status_column_fits_terminal_statuses -v`
Expected: FAIL (`16 >= 21` ложно).

- [ ] **Step 3: Расширить колонку в модели**

`backend/app/models/autonomy.py:51`, было:

```python
    status: Mapped[str] = mapped_column(String(16), default="running")   # running | done | failed
```

стало:

```python
    status: Mapped[str] = mapped_column(String(32), default="running")   # running|done|failed|cancelled|completed_with_errors
```

- [ ] **Step 4: Прогнать — тест зелёный**

Run: `.venv/bin/python -m pytest backend/tests/test_autonomy_config.py::test_autonomy_status_column_fits_terminal_statuses -v`
Expected: PASS.

- [ ] **Step 5: Создать миграцию для прода (тесты идут через create_all, но боксу нужен ALTER)**

Сверить голову: `.venv/bin/alembic -c backend/alembic.ini heads` → ожидается `0018`.
Создать `backend/alembic/versions/0019_autonomy_status_width.py`:

```python
"""autonomy_runs.status VARCHAR(16)->(32): вмещает completed_with_errors (аудит 2026-07-15, P0).

VARCHAR(16) отклонял 21-символьный терминальный статус на PostgreSQL (SQLite длину не
проверяет — тесты этого не ловили). Только расширение, данные не трогаются."""
from alembic import op
import sqlalchemy as sa

revision = "0019_autonomy_status_width"
down_revision = "0018_page_lang_offer"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("autonomy_runs", "status",
                    existing_type=sa.String(16), type_=sa.String(32),
                    existing_nullable=False, existing_server_default=sa.text("'running'"))


def downgrade() -> None:
    op.alter_column("autonomy_runs", "status",
                    existing_type=sa.String(32), type_=sa.String(16),
                    existing_nullable=False, existing_server_default=sa.text("'running'"))
```

**Проверить точное имя down_revision:** открой `backend/alembic/versions/0018_page_lang_offer.py`,
возьми значение его `revision = "..."` дословно и подставь в `down_revision` выше (имена
ревизий в этом проекте — полные, а не хеши).

- [ ] **Step 6: Миграционная цепочка линейна**

Run: `.venv/bin/alembic -c backend/alembic.ini heads`
Expected: один head `0019_autonomy_status_width`.

- [ ] **Step 7: Полный сьют + pyflakes**

Run: `.venv/bin/python -m pytest backend/tests/ -q && .venv/bin/python -m pyflakes backend/app backend/tests`
Expected: `589 passed`, pyflakes без вывода.

- [ ] **Step 8: Commit**

```bash
git add backend/app/models/autonomy.py backend/alembic/versions/0019_autonomy_status_width.py backend/tests/test_autonomy_config.py
git commit -F - <<'EOF'
fix(F0.1): autonomy_runs.status вмещает completed_with_errors (P0)

VARCHAR(16) крашил INSERT свипа «с замечаниями» на PostgreSQL (21 символ).
Расширил до VARCHAR(32) + миграция 0019. Тест проверяет схему, а не INSERT
(SQLite длину не enforce-ит).

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
```

---

## Task 2 — F0.2: защита от порчи данных миграцией 0016 (дедуп Site)

**Почему:** `0016_cloudflare_mirrors.py` при дублях `Site.domain_id` выбирает keeper
ТОЛЬКО по числу страниц, теряет site-поля проигравшего, при коллизии `url_path`
оставляет страницу keeper (а не `published>edited>draft`) и удаляет `index_history`
проигравшей. На боксе миграция ещё не применялась (он позади `main`). **Нельзя
переписывать применённую миграцию вслепую** — сперва узнаём, есть ли вообще дубли.

**Основной результат — read-only диагностика.** Если дублей нет, 0016 — no-op,
деплой безопасен. Если есть — СТОП, эскалация на упрочнение keeper (условный
под-шаг ниже, требует реальных данных).

**Files:**
- Create: `backend/scripts/check_site_dupes.py`

- [ ] **Step 1: Диагностический скрипт (read-only, без сети)**

Создать `backend/scripts/check_site_dupes.py`:

```python
"""Read-only: есть ли дубли Site по domain_id ДО применения миграции 0016.

0016 при дублях выбирает keeper по числу страниц и может удалить опубликованную
версию/поля/индекс-историю проигравшего (аудит 2026-07-15, P1). Пусто на выходе =
дедуп 0016 no-op, деплой безопасен. Непусто = СТОП, чинить keeper до миграции.

Запуск на боксе (PowerShell):
  docker compose run --rm backend python backend/scripts/check_site_dupes.py
"""
from sqlalchemy import func, select
from app.db import SessionLocal
from app.models.site import Site
from app.models.page import Page


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
```

- [ ] **Step 2: Скрипт импортируется и не падает на пустой тестовой БД**

Проверка (локально, без бокса):

```bash
docker compose run --rm backend python backend/scripts/check_site_dupes.py
```

Expected: `OK: дублей Site по domain_id нет ...` (dev-БД пуста) ИЛИ список дублей.
Если docker недоступен локально — проверь только импорт:
`.venv/bin/python -c "import backend.scripts.check_site_dupes"` (без вывода = ок).

- [ ] **Step 3 (УСЛОВНЫЙ — только если Step 2 на боксе показал дубли): упрочнить keeper 0016**

СТОП и эскалация человеку. Если решено чинить: сначала прочитать
`backend/alembic/versions/0016_cloudflare_mirrors.py:225-330` целиком (там SQL
выбора keeper через `FIRST_VALUE ... ORDER BY count(pages) DESC`). Затем ОТДЕЛЬНОЙ
forward-миграцией `0020_site_dedup_repair.py` (НЕ переписывать применённую 0016)
пере-слить: keeper по `published>edited>draft` затем по числу страниц; перенести
site-поля (`status/cf_zone_id/origin_ip/doc_root/published_at/ssl_error`) и
`index_history` с проигравшего. Тест — PostgreSQL-fixture с двумя Site на домен
(один published/1 страница, другой draft/3 страницы) и проверкой, что выжил
published. **Этот под-шаг вне обычного потока плана — он зависит от данных бокса и
сдвигает нумерацию последующих миграций (тогда F1.3 → 0021, F2.1 → 0022).**

- [ ] **Step 4: Commit (скрипт диагностики)**

```bash
git add backend/scripts/check_site_dupes.py
git commit -F - <<'EOF'
fix(F0.2): read-only проверка дублей Site перед миграцией 0016

0016 при дублях domain_id может удалить опубликованную версию/index_history.
Скрипт показывает дубли и статусы страниц ДО git-pull на боксе: пусто = 0016
безопасна, непусто = чинить keeper отдельной миграцией.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
```

---

## Task 3 — F0.3: снимок тумблеров источников перед 0015

**Почему:** `0015_source_defaults.py` безусловно возвращает `cctld/reg_ru/sweb` в
`false` (безопасный дефолт). Это стирает ручной выбор оператора, сделанный после
0002. Краша нет — нужен только снимок ДО пула, чтобы вернуть выбор после.

**Files:**
- Create: `backend/scripts/show_sources.py`

- [ ] **Step 1: Read-only снимок текущих тумблеров**

Создать `backend/scripts/show_sources.py`:

```python
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
from app.models.scoring_settings import ScoringSettings


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
```

**Проверить имя модели/поля:** открой `backend/app/models/` и найди класс настроек
скоринга (грепни `sources_enabled`), подставь верные имена класса и модуля в импорт
и в `select(...)`. Если поле называется иначе — используй фактическое.

- [ ] **Step 2: Импорт не падает**

Run: `.venv/bin/python -c "import backend.scripts.show_sources"`
Expected: без вывода.

- [ ] **Step 3: Commit**

```bash
git add backend/scripts/show_sources.py
git commit -F - <<'EOF'
fix(F0.3): снимок sources_enabled перед безусловным сбросом миграцией 0015

0015 гасит cctld/reg_ru/sweb на любой базе. Скрипт печатает текущие тумблеры,
чтобы оператор вернул ручной выбор через /settings после git-pull.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
```

**Фаза 0 завершена.** Оператору: запустить `check_site_dupes.py` и `show_sources.py`
на боксе, затем git-pull + миграции, затем вернуть тумблеры источников.

---

## Task 4 — F1.1: legacy CF-токен классифицируется как `account` по наличию ACCOUNT_ID

**Почему:** `cf_legacy.py:27` выводит `token_kind="account"`, если задан
`CLOUDFLARE_ACCOUNT_ID`. Наличие account ID НИЧЕГО не говорит о владельце токена.
Живой `.env`-токен проверен user-owned (2026-07-11, 20 зон, `/user/tokens/verify`),
но при `token_kind="account"` sync проверяет его через `/accounts/{id}/tokens/verify`
→ риск «токен ошибочен, 0 зон». Минимальный верный фикс: legacy-импорт всегда
`user` (account_id остаётся для фильтрации зон).

**Files:**
- Modify: `backend/app/services/cf_legacy.py:27`
- Test: `backend/tests/test_cf_sync.py` (добавить тест)

**Interfaces:**
- Consumes: `CloudflareConnection(token_kind, owner_cf_account_id, ...)` (без изменений).
- Produces: legacy-connection всегда `token_kind == "user"`, `owner_cf_account_id`
  по-прежнему = `CLOUDFLARE_ACCOUNT_ID` (нужен `cf_sync.list_zones_paginated`).

- [ ] **Step 1: Падающий тест**

В `backend/tests/test_cf_sync.py` добавить:

```python
def test_legacy_import_token_kind_is_user_even_with_account_id(sqlite_db, monkeypatch):
    # Наличие CLOUDFLARE_ACCOUNT_ID НЕ делает токен account-owned (аудит F1.1).
    from app.config import settings
    from app.services import cf_legacy
    from app.models.cloudflare import CloudflareConnection
    from app.db import SessionLocal
    monkeypatch.setattr(settings, "CLOUDFLARE_API_TOKEN", "tok_live_1234", raising=False)
    monkeypatch.setattr(settings, "CLOUDFLARE_ACCOUNT_ID", "acc_hex_dead", raising=False)
    with SessionLocal() as db:
        cid = cf_legacy.import_legacy_connection(db)
        conn = db.get(CloudflareConnection, cid)
        assert conn.token_kind == "user"              # НЕ "account"
        assert conn.owner_cf_account_id == "acc_hex_dead"   # но account_id сохранён для листинга зон
```

**Сверь фикстуру:** имя `sqlite_db` — из `backend/tests/conftest.py` (грепни
`def sqlite_db`); если фикстура зовётся иначе, используй фактическое имя.

- [ ] **Step 2: Прогнать — падает**

Run: `.venv/bin/python -m pytest backend/tests/test_cf_sync.py::test_legacy_import_token_kind_is_user_even_with_account_id -v`
Expected: FAIL (`token_kind == "account"`).

- [ ] **Step 3: Фикс**

`backend/app/services/cf_legacy.py:27`, было:

```python
        token_kind="account" if settings.CLOUDFLARE_ACCOUNT_ID else "user",
```

стало:

```python
        # account_id НЕ доказывает владельца токена: живой .env-токен проверен user-owned
        # (2026-07-11, /user/tokens/verify, 20 зон). account-owned токены заводятся отдельным
        # путём с явным token_kind, не этим legacy-импортом (аудит 2026-07-15, F1.1).
        token_kind="user",
```

- [ ] **Step 4: Прогнать — зелёный**

Run: `.venv/bin/python -m pytest backend/tests/test_cf_sync.py::test_legacy_import_token_kind_is_user_even_with_account_id -v`
Expected: PASS.

- [ ] **Step 5: Полный сьют (ничего не сломали в существующих CF-тестах)**

Run: `.venv/bin/python -m pytest backend/tests/test_cf_sync.py -q`
Expected: все зелёные.

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/cf_legacy.py backend/tests/test_cf_sync.py
git commit -F - <<'EOF'
fix(F1.1): legacy CF-токен всегда user-owned, не выводить из ACCOUNT_ID

Наличие account_id ничего не говорит о владельце токена. Живой .env-токен
проверен user-owned; классификация account вела sync на /accounts/.../verify
и грозила «0 зон». account_id сохранён для фильтрации зон.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
```

---

## Task 5 — F1.2: Universal SSL опрашивается неверным эндпоинтом

**Почему:** `cf_sync.py:18` держит `"universal_ssl"` в `_OBSERVED_SETTINGS` и
опрашивает его как обычный zone-setting `GET /zones/{id}/settings/universal_ssl` —
такого setting_id нет. У Universal SSL отдельный эндпоинт
`GET /zones/{id}/ssl/universal/settings` → `{enabled: bool}`. Сейчас GET падает,
`obs.status="error"`, `m.universal_ssl_status` не выставляется — колонка SSL в UI
навсегда «не проверено».

**Files:**
- Modify: `backend/app/integrations/cloudflare.py` (новый метод рядом с `get_dnssec`, ~строка 265)
- Modify: `backend/app/services/cf_sync.py:17-18` (убрать из `_OBSERVED_SETTINGS`)
  и `_sync_zone_details` (выделенный вызов после цикла settings)
- Test: `backend/tests/test_cf_sync.py` (обновить существующий + добавить negative)

**Interfaces:**
- Produces: `CloudflareClient.get_universal_ssl(zone_id) -> dict` (`{"enabled": bool}`);
  `m.universal_ssl_status` ∈ `"on" | "off" | None`.

- [ ] **Step 1: Обновить существующий тест под новый контракт + добавить negative**

В `backend/tests/test_cf_sync.py` найди `test_universal_ssl_status_recorded_from_setting`
(грепни имя). Его fake-клиент принимает любой `setting_id` и возвращает `"on"` —
после фикса universal_ssl идёт НЕ через `get_zone_setting`. Замени/дополни так,
чтобы fake-клиент имел метод `get_universal_ssl`, а `get_zone_setting` ОТВЕРГАЛ
неизвестный setting_id:

```python
def test_universal_ssl_uses_dedicated_endpoint(sqlite_db):
    # universal_ssl больше НЕ обычный zone-setting: свой эндпоинт возвращает {enabled: bool}.
    from app.services import cf_sync
    from app.models.cloudflare import CloudflareZoneMirror
    from app.db import SessionLocal

    KNOWN = set(cf_sync._OBSERVED_SETTINGS)   # universal_ssl СЮДА больше не входит

    class FakeCF:
        def get_zone_setting(self, zid, sid):
            assert sid in KNOWN, f"universal_ssl не должен идти через settings-эндпоинт: {sid}"
            return {"value": "on", "editable": True}
        def get_universal_ssl(self, zid):
            return {"enabled": True}
        def list_dns_paginated(self, zid): return []
        def list_universal_certificate_packs(self, zid): return []
        def get_dnssec(self, zid): return {"status": "active"}

    with SessionLocal() as db:
        m = CloudflareZoneMirror(cf_zone_id="z1", cloudflare_account_id="a1", name="ex.ru")
        db.add(m); db.commit()
        cf_sync._sync_zone_details(db, FakeCF(), m)
        db.commit()
        assert m.universal_ssl_status == "on"

def test_universal_ssl_not_in_observed_settings():
    from app.services import cf_sync
    assert "universal_ssl" not in cf_sync._OBSERVED_SETTINGS
```

- [ ] **Step 2: Прогнать — падает (метода нет / universal_ssl ещё в списке)**

Run: `.venv/bin/python -m pytest backend/tests/test_cf_sync.py::test_universal_ssl_not_in_observed_settings backend/tests/test_cf_sync.py::test_universal_ssl_uses_dedicated_endpoint -v`
Expected: FAIL.

- [ ] **Step 3: Транспортный метод в integrations/cloudflare.py**

После `get_dnssec` (около строки 265) добавить:

```python
    def get_universal_ssl(self, zone_id: str) -> dict:
        """GET /zones/{zone_id}/ssl/universal/settings — read-only статус Universal SSL.
        У него ОТДЕЛЬНЫЙ эндпоинт (не /settings/universal_ssl), ответ {enabled: bool}."""
        resp = self.request("GET", f"{self.base_url}/zones/{zone_id}/ssl/universal/settings",
                            headers=self._headers())
        return self._result(resp)
```

- [ ] **Step 4: Убрать universal_ssl из общего списка settings**

`backend/app/services/cf_sync.py:17-18`, было:

```python
_OBSERVED_SETTINGS = ("ssl", "always_use_https", "min_tls_version", "tls_1_3", "http3",
                      "0rtt", "development_mode", "universal_ssl")  # per-setting GET, read-only
```

стало:

```python
_OBSERVED_SETTINGS = ("ssl", "always_use_https", "min_tls_version", "tls_1_3", "http3",
                      "0rtt", "development_mode")  # per-setting GET, read-only
# universal_ssl НЕ здесь: у него отдельный эндпоинт /ssl/universal/settings (аудит F1.2)
```

- [ ] **Step 5: Убрать зеркалирование из цикла settings и добавить выделенный вызов**

В `_sync_zone_details` удалить ветку внутри цикла settings (строки 217-218):

```python
            if sid == "universal_ssl":  # зеркалим в zone mirror для SSL-колонки UI
                m.universal_ssl_status = _stringify(s.get("value"))
```

и ПОСЛЕ цикла `for sid in _OBSERVED_SETTINGS:` (перед блоком «cert-паки») добавить
выделенный опрос со своим error-полем (пишем в существующий `last_error_safe`
зоны, чтобы не плодить колонок — универсальный SSL концептуально свойство зоны):

```python
    # Universal SSL — отдельный эндпоинт, не общий settings-цикл (аудит F1.2)
    try:
        u = cf.get_universal_ssl(zid)
        m.universal_ssl_status = "on" if u.get("enabled") else "off"
    except Exception as exc:
        # НЕ затираем прежний статус ошибкой — фиксируем на уровне зоны (см. F1.3)
        m.last_error_safe = _safe(exc)
```

- [ ] **Step 6: Прогнать оба теста — зелёные**

Run: `.venv/bin/python -m pytest backend/tests/test_cf_sync.py -q`
Expected: все зелёные (обновлённый + два новых).

- [ ] **Step 7: pyflakes**

Run: `.venv/bin/python -m pyflakes backend/app backend/tests`
Expected: без вывода. (Проверь, что `_stringify` ещё используется где-то; если стал
мёртвым — оставь, он общий хелпер, но pyflakes на unused import/func не ругается на
module-level def — ничего удалять не нужно.)

- [ ] **Step 8: Commit**

```bash
git add backend/app/integrations/cloudflare.py backend/app/services/cf_sync.py backend/tests/test_cf_sync.py
git commit -F - <<'EOF'
fix(F1.2): Universal SSL опрашивается своим эндпоинтом, не как zone-setting

/zones/{id}/settings/universal_ssl не существует — GET падал, SSL-колонка UI
навсегда «не проверено». Добавил транспорт get_universal_ssl
(/ssl/universal/settings -> {enabled}), убрал из _OBSERVED_SETTINGS, маплю
enabled -> on/off. Тест отвергает роутинг universal_ssl через settings.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
```

---

## Task 6 — F1.3 (P2): ошибки DNS/cert глотаются → протухшее выдаётся за свежее

**Почему:** в `_sync_zone_details` чтения DNS (строки 201-202) и cert-паков
(243-244) обёрнуты в голый `except Exception: pass`. Старые записи остаются как
актуальные, ни `last_error_safe`, ни свежесть не обновляются, зона/аккаунт остаются
`ok`. Противоречит назначению вкладки как read-only «экрана правды».

**Files:**
- Modify: `backend/app/models/cloudflare.py` (класс `CloudflareZoneMirror`: +2 поля)
- Create: `backend/alembic/versions/0020_zone_detail_errors.py`
- Modify: `backend/app/services/cf_sync.py` (`_sync_zone_details`: DNS/cert except)
- Modify: `backend/app/templates/settings_cloudflare.html` (led-warn при ошибке)
- Test: `backend/tests/test_cf_sync.py`

**Interfaces:**
- Produces: `CloudflareZoneMirror.dns_error_safe`, `.cert_error_safe` (`Text`,
  nullable) — None при успехе, safe-текст при ошибке GET.

- [ ] **Step 1: Падающий тест — ошибка DNS фиксируется, не глотается**

В `backend/tests/test_cf_sync.py`:

```python
def test_dns_error_recorded_not_swallowed(sqlite_db):
    from app.services import cf_sync
    from app.models.cloudflare import CloudflareZoneMirror
    from app.db import SessionLocal

    class FakeCF:
        def list_dns_paginated(self, zid): raise RuntimeError("dns boom")
        def get_zone_setting(self, zid, sid): return {"value": "on", "editable": True}
        def get_universal_ssl(self, zid): return {"enabled": True}
        def list_universal_certificate_packs(self, zid): raise RuntimeError("cert boom")
        def get_dnssec(self, zid): return {"status": "active"}

    with SessionLocal() as db:
        m = CloudflareZoneMirror(cf_zone_id="z9", cloudflare_account_id="a1", name="ex.ru")
        db.add(m); db.commit()
        cf_sync._sync_zone_details(db, FakeCF(), m)
        db.commit()
        assert m.dns_error_safe and "dns boom" in m.dns_error_safe
        assert m.cert_error_safe and "cert boom" in m.cert_error_safe
```

- [ ] **Step 2: Прогнать — падает (полей нет)**

Run: `.venv/bin/python -m pytest backend/tests/test_cf_sync.py::test_dns_error_recorded_not_swallowed -v`
Expected: FAIL (`AttributeError` / поля нет).

- [ ] **Step 3: Поля на модель**

В `backend/app/models/cloudflare.py`, класс `CloudflareZoneMirror`, рядом с
`last_error_safe` (после строки с `dnssec_error_safe`) добавить:

```python
    dns_error_safe: Mapped[str | None] = mapped_column(Text)     # ошибка чтения DNS-записей зоны
    cert_error_safe: Mapped[str | None] = mapped_column(Text)    # ошибка чтения cert-паков зоны
```

(`Text` уже импортирован — проверь шапку модуля; если нет, добавь в существующий
`from sqlalchemy import ...`.)

- [ ] **Step 4: Не глотать ошибки в сервисе**

В `_sync_zone_details` заменить голые `except` на пишущие.
DNS-блок (строки 201-202), было:

```python
    except Exception:
        pass  # ошибка DNS одной зоны — соседние зоны/детали не портим
```

стало:

```python
    except Exception as exc:
        m.dns_error_safe = _safe(exc)   # не глотать: соседние GET не портим, но правда видна (F1.3)
    else:
        m.dns_error_safe = None
```

(Внимание: `else` цепляется к `try`, а не к `for`. Существующий блок — это
`try: ...for rec...; for r in ...: ... except: pass`. Помести `else: m.dns_error_safe = None`
между `except` и следующим кодом.)

cert-блок (строки 243-244), было:

```python
    except Exception:
        pass
```

стало:

```python
    except Exception as exc:
        m.cert_error_safe = _safe(exc)   # аудит F1.3
    else:
        m.cert_error_safe = None
```

- [ ] **Step 5: Прогнать — зелёный**

Run: `.venv/bin/python -m pytest backend/tests/test_cf_sync.py::test_dns_error_recorded_not_swallowed -v`
Expected: PASS.

- [ ] **Step 6: Миграция для прода**

Сверить голову (`alembic heads` → `0019...`). Создать
`backend/alembic/versions/0020_zone_detail_errors.py`:

```python
"""zone-mirror: dns_error_safe/cert_error_safe — ошибки DNS/cert не глотать (аудит F1.3)."""
from alembic import op
import sqlalchemy as sa

revision = "0020_zone_detail_errors"
down_revision = "0019_autonomy_status_width"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("cloudflare_zone_mirrors", sa.Column("dns_error_safe", sa.Text(), nullable=True))
    op.add_column("cloudflare_zone_mirrors", sa.Column("cert_error_safe", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("cloudflare_zone_mirrors", "cert_error_safe")
    op.drop_column("cloudflare_zone_mirrors", "dns_error_safe")
```

- [ ] **Step 7: UI — led-warn при ошибке DNS/cert зоны**

В `backend/app/templates/settings_cloudflare.html` в строке зоны (где уже есть
`z.missing_since`/`z.universal_ssl_status`, около строк 79-89) добавить ячейку/бейдж,
следуя СУЩЕСТВУЮЩЕМУ паттерну `led-warn` с `title`:

```html
      <td>{% if z.dns_error_safe or z.cert_error_safe %}<span class="led led-warn"
              title="детали зоны читались с ошибкой (данные могут быть протухшими): {{ z.dns_error_safe or z.cert_error_safe }}"></span>ошибка деталей
          {% else %}<span class="led led-ok"></span>ок{% endif %}</td>
```

Заголовок колонки добавь в `<thead>` той же таблицы (найди строку с `<th>` для
SSL/DNSSEC и вставь рядом `<th>детали</th>`). Классы `led led-warn/led-ok` уже
определены в `base.html` — новых не вводить.

- [ ] **Step 8: Полный сьют + pyflakes + глаз на шаблон**

Run: `.venv/bin/python -m pytest backend/tests/ -q && .venv/bin/python -m pyflakes backend/app backend/tests`
Expected: все зелёные, pyflakes чист.
Шаблон проверить рендером через TestClient+SQLite-харнесс (роут CF-настроек) в
статический HTML — колонка «детали» присутствует, при пустых полях «ок».

- [ ] **Step 9: Commit**

```bash
git add backend/app/models/cloudflare.py backend/alembic/versions/0020_zone_detail_errors.py backend/app/services/cf_sync.py backend/app/templates/settings_cloudflare.html backend/tests/test_cf_sync.py
git commit -F - <<'EOF'
fix(F1.3): ошибки чтения DNS/cert зоны не глотать, показывать в UI

Голый except: pass выдавал протухшие DNS/cert-записи за свежие. Добавил
dns_error_safe/cert_error_safe (миграция 0020), пишу их при ошибке GET и
чищу при успехе, показываю led-warn на вкладке Cloudflare.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
```

---

## Task 7 — F2.1 (P1): `cap_provision` капит successes, а не attempts

**Почему:** `_run_provision(cap)` выбирает ВСЕ `Site.status="provisioning"` без
LIMIT; `awaiting_ns`/`error` делают `continue`, не расходуя кап. При капе 5 и 100
ждущих NS сайтах свип сделает до 100 внешних `provision()` — предохранитель снят.
Нужна честная ротация: кап на attempts + least-recently-tried первым.

**Files:**
- Modify: `backend/app/models/site.py` (`Site.last_attempt_at`)
- Create: `backend/alembic/versions/0021_site_last_attempt.py`
- Modify: `backend/app/services/orchestrator.py` (`_run_provision`)
- Test: `backend/tests/test_sweep_outcomes.py`

**Interfaces:**
- Consumes: `Site.status`, новый `Site.last_attempt_at` (nullable datetime).
- Produces: `_run_provision(cap)` делает не более `cap` внешних `provision()`-попыток
  за прогон, обходя сайты в порядке `last_attempt_at ASC NULLS FIRST`.

- [ ] **Step 1: Падающий тест — кап ограничивает попытки, не успехи**

Прочитай `backend/tests/test_sweep_outcomes.py` (стиль сборки Site/monkeypatch
`provisioning.provision`). Добавь тест: 3 сайта `provisioning`, все возвращают
`awaiting_ns`, `cap=2` → ровно 2 вызова `provision()`.

```python
def test_provision_cap_limits_attempts_not_successes(sqlite_db, monkeypatch):
    from app.services import orchestrator, provisioning
    from app.models.site import Site
    from app.db import SessionLocal

    with SessionLocal() as db:
        for i in range(3):
            db.add(Site(domain_id=1000 + i, status="provisioning"))
        db.commit()

    calls = []
    def fake_provision(sid):
        calls.append(sid)
        return {"status": "awaiting_ns"}      # ни успех, ни отказ — раньше не расходовал кап
    monkeypatch.setattr(provisioning, "provision", fake_provision)
    # покупок нет, чтобы мерить только ветку provisioning:
    monkeypatch.setattr(provisioning, "create_site_for", lambda did: None)

    orchestrator._run_provision(2)
    assert len(calls) == 2, f"кап=2 обязан ограничить попытки; было {len(calls)}"
```

**Сверь имена:** точное имя функции провижна в `orchestrator.py` (в контексте она
`_run_provision`/handler для стадии provision — грепни `provisioning.provision(` и
`def ` рядом со строкой 150). Используй фактическое имя и сигнатуру. Если функция
не экспортируемая или требует иного вызова — вызови через её реальную точку входа.

- [ ] **Step 2: Прогнать — падает (3 вызова вместо 2)**

Run: `.venv/bin/python -m pytest backend/tests/test_sweep_outcomes.py::test_provision_cap_limits_attempts_not_successes -v`
Expected: FAIL (`len(calls) == 3`).

- [ ] **Step 3: Поле last_attempt_at на Site**

В `backend/app/models/site.py` добавить в класс `Site`:

```python
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # ротация провижна (F2.1)
```

(Проверь импорты `datetime`, `DateTime`, `Mapped`, `mapped_column` в шапке —
скорее всего уже есть; если `datetime` не импортирован, добавь `from datetime import datetime`.)

- [ ] **Step 4: Кап на attempts + ротация в orchestrator**

В `_run_provision` изменить выборку `prov_ids` и цикл. Выборка (была без LIMIT):

```python
        prov_ids = [r[0] for r in db.execute(
            select(Site.id).where(Site.status == "provisioning").order_by(Site.id)).all()]
```

стала (least-recently-tried первым, ограничена капом):

```python
        prov_ids = [r[0] for r in db.execute(
            select(Site.id).where(Site.status == "provisioning")
            .order_by(Site.last_attempt_at.asc().nulls_first(), Site.id).limit(cap)).all()]
```

Цикл `for sid in prov_ids:` — заменить условие выхода `if succeeded >= cap: break`
на счётчик попыток и штамп времени. Внутри цикла ПЕРЕД `provisioning.provision(sid)`:

```python
    attempts = 0
    for sid in prov_ids:
        if attempts >= cap:
            break
        attempts += 1
        with SessionLocal() as db2:          # штамп попытки — чтобы следующий свип взял ДРУГИЕ сайты
            s = db2.get(Site, sid)
            if s is not None:
                s.last_attempt_at = _utcnow()
                db2.commit()
        try:
            out = provisioning.provision(sid)
            ...
```

Удалить прежнюю строку `if succeeded >= cap: break` из этого цикла (теперь выход по
`attempts`). Ветка `create_site_for` (покупки) уже ограничена `.limit(cap)` на
выборке `purchased` и своим `if succeeded >= cap: break` — её оставить как есть
(там каждый вызов = успех или ошибка, оба расходуют, ротация не нужна).

**Проверь наличие `_utcnow`/`SessionLocal` в orchestrator.py** (грепни). Если
`_utcnow` нет — используй `datetime.now(timezone.utc)` с нужным импортом; если
переоткрывать сессию накладно, штампуй в той же сессии, что и выборка (тогда вынеси
штамп из цикла: сначала выбрать `prov_ids`, разом проставить `last_attempt_at` для
взятых и закоммитить, потом крутить провижн). Выбери форму, консистентную стилю
файла (в нём выборка и цикл уже в разных `with SessionLocal()`).

- [ ] **Step 5: Прогнать — зелёный**

Run: `.venv/bin/python -m pytest backend/tests/test_sweep_outcomes.py::test_provision_cap_limits_attempts_not_successes -v`
Expected: PASS (ровно 2 вызова).

- [ ] **Step 6: Миграция**

`backend/alembic/versions/0021_site_last_attempt.py` (down_revision =
`0020_zone_detail_errors`):

```python
"""Site.last_attempt_at — кап провижна на attempts + ротация (аудит F2.1)."""
from alembic import op
import sqlalchemy as sa

revision = "0021_site_last_attempt"
down_revision = "0020_zone_detail_errors"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sites", sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("sites", "last_attempt_at")
```

**Сверь имя таблицы:** `Site.__tablename__` (грепни в `models/site.py`) — если не
`"sites"`, подставь фактическое.

- [ ] **Step 7: Полный сьют + pyflakes + линейность миграций**

Run: `.venv/bin/python -m pytest backend/tests/ -q && .venv/bin/python -m pyflakes backend/app backend/tests && .venv/bin/alembic -c backend/alembic.ini heads`
Expected: зелёные, pyflakes чист, один head `0021_site_last_attempt`.

- [ ] **Step 8: Commit**

```bash
git add backend/app/models/site.py backend/alembic/versions/0021_site_last_attempt.py backend/app/services/orchestrator.py backend/tests/test_sweep_outcomes.py
git commit -F - <<'EOF'
fix(F2.1): cap_provision ограничивает попытки, не успехи

awaiting_ns/error не расходовали кап -> 100 ждущих NS сайтов = до 100 внешних
provision() за свип. Ввёл кап на attempts + ротацию по last_attempt_at
(least-recently-tried первым, миграция 0021). Предохранитель вернулся,
starvation не воскрес.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
```

---

## Task 8 — F2.2 (P2): JobRun показывает зелёный `done` при свипе «с замечаниями»

**Почему:** оркестратор ставит `AutonomyRun.status="completed_with_errors"`, но
`with jobs.track("sweep")` выходит без исключения → `_close(run_id, "done")` →
карточка на Пульте зелёная. Журнал `/autopilot` (AutonomyRun) и карточка живых/
последних задач (JobRun) рассинхронены. Класс `.b-warn` в `base.html:105` уже
заведён ровно под это состояние — используем его.

**Files:**
- Modify: `backend/app/services/jobs.py` (терминал `done_warn` + `finish()` + контракт-докстринг)
- Modify: `backend/app/services/orchestrator.py` (сигнал перед выходом из `with`)
- Modify: `backend/app/templates/dashboard.html` (ветка рендера `done_warn`)
- Test: `backend/tests/test_jobs.py` (или где живут тесты реестра — грепни `jobs.track`)

**Interfaces:**
- Consumes: `jobs.track(...) as run` (run_id).
- Produces: `jobs.finish(run_id, status)` — тело track сообщает СВОЙ терминальный
  статус; `"done_warn"` (9 символов, влезает в `JobRun.status` String(16)) закрывает
  прогон как успех-с-замечаниями (чипы гасятся в «пройдено», как у `done`).

- [ ] **Step 1: Падающий тест — тело, вызвавшее finish(run, "done_warn"), закрывает прогон done_warn**

Прочитай `backend/tests/test_jobs.py` (стиль вызова `jobs.track`). Добавь:

```python
def test_track_closes_done_warn_when_body_signals(sqlite_db):
    from app.services import jobs
    from app.models.job import JobRun
    from app.db import SessionLocal

    with jobs.track("sweep") as run:
        jobs.finish(run, "done_warn")         # тело: «прошёл, но с замечаниями»

    with SessionLocal() as db:
        r = db.execute(__import__("sqlalchemy").select(JobRun)
                       .order_by(JobRun.id.desc())).scalars().first()
        assert r.status == "done_warn"
        # чипы всё равно «пройдено» — стадии-то отработали:
        assert all(s.get("state") in ("done", "skip") for s in (r.stages or []))
```

(Импорт `select` оформи как в соседних тестах файла — строка выше ленивая для
компактности; используй принятый в файле стиль.)

- [ ] **Step 2: Прогнать — падает (`finish` нет / статус done)**

Run: `.venv/bin/python -m pytest backend/tests/test_jobs.py::test_track_closes_done_warn_when_body_signals -v`
Expected: FAIL.

- [ ] **Step 3: Канал сигнала + терминал done_warn в jobs.py**

Рядом с `_INFLIGHT`/`_FUTURES` (module-level, ~строка 95) добавить:

```python
# Терминальный исход, заявленный ТЕЛОМ track (сегодня — свип: «с замечаниями»). Тело зовёт
# jobs.finish(run_id, "done_warn") перед нормальным выходом; track подхватывает вместо "done".
_OUTCOME: dict[int, str] = {}
```

Публичная функция (рядом с `report`):

```python
def finish(run_id: int | None, status: str) -> None:
    """Тело track заявляет СВОЙ терминальный статус (напр. свип «с замечаниями» -> done_warn).
    Иначе track закрывает прогон как "done". run_id=None — no-op (вне track)."""
    if run_id is None:
        return
    _OUTCOME[run_id] = status
```

В `track` заменить ветки закрытия (строки 261-267), было:

```python
    except Cancelled:
        _close(run_id, "cancelled")         # остановлен человеком — это не ошибка
    except BaseException as e:              # BaseException: ловушки сети в тестах — тоже финал
        _close(run_id, "failed", f"{type(e).__name__}: {e}"[:200])
        raise
    else:
        _close(run_id, "done")
```

стало:

```python
    except Cancelled:
        _OUTCOME.pop(run_id, None)
        _close(run_id, "cancelled")         # остановлен человеком — это не ошибка
    except BaseException as e:              # BaseException: ловушки сети в тестах — тоже финал
        _OUTCOME.pop(run_id, None)
        _close(run_id, "failed", f"{type(e).__name__}: {e}"[:200])
        raise
    else:
        _close(run_id, _OUTCOME.pop(run_id, "done"))
```

В `_close` расширить ветку гашения чипов (строка 239), было:

```python
    if status == "done":                    # успех — все чипы гасим в «пройдено»
```

стало:

```python
    if status in ("done", "done_warn"):     # успех/с замечаниями — стадии отработали, чипы «пройдено»
```

Обновить ТЕРМИНАЛЬНЫЙ КОНТРАКТ в докстринге модуля (шапка, ~строка 16-21): добавить
строку `status == "done_warn" -> свип прошёл, но отдельные сущности упали (см. F2.2)`.

- [ ] **Step 4: Оркестратор сигналит перед выходом из with**

В `run_sweep`, где выставляется `status = "completed_with_errors"` (строка 377),
сразу после него добавить сигнал реестру (переменная замка — `run`):

```python
                    status = "completed_with_errors"
                    jobs.finish(run, "done_warn")   # JobRun-карточка тоже «с замечаниями», не зелёная (F2.2)
```

(`jobs` уже импортирован в функции — `from app.services import jobs` на строке 321.
`run` — это `with jobs.track(...) as run`.)

- [ ] **Step 5: Прогнать — зелёный**

Run: `.venv/bin/python -m pytest backend/tests/test_jobs.py::test_track_closes_done_warn_when_body_signals -v`
Expected: PASS.

- [ ] **Step 6: Рендер done_warn на Пульте**

В `backend/app/templates/dashboard.html` в блоке последних прогонов (около строк
31-40, где `{% if r.status == 'failed' %}` / `'cancelled'`) добавить ветку ПЕРЕД
общим «успех», используя существующий `.b-warn`:

```html
          {% if r.status == 'failed' %}<span style="color:var(--bad)">упала: {{ r.error }}</span>
          {% elif r.status == 'cancelled' %}остановлена на {{ r.done }} / {{ r.total }}
          {% elif r.status == 'done_warn' %}<span class="badge b-warn" title="{{ r.message }}">с замечаниями</span>
```

(Оставить существующую финальную ветку успеха как есть. Точную вставку сверь по
факту разметки строк 36-37.)

- [ ] **Step 7: Полный сьют + pyflakes**

Run: `.venv/bin/python -m pytest backend/tests/ -q && .venv/bin/python -m pyflakes backend/app backend/tests`
Expected: все зелёные, pyflakes чист. Убедись, что существующие тесты свипа
(`test_sweep_outcomes.py`), проверявшие `AutonomyRun.status=="completed_with_errors"`,
всё ещё зелёные — этот путь мы не меняли, только добавили сигнал JobRun.

- [ ] **Step 8: Commit**

```bash
git add backend/app/services/jobs.py backend/app/services/orchestrator.py backend/app/templates/dashboard.html backend/tests/test_jobs.py
git commit -F - <<'EOF'
fix(F2.2): свип «с замечаниями» больше не зелёный на Пульте

AutonomyRun был completed_with_errors, а JobRun закрывался done -> карточка
зелёная. Добавил терминал done_warn + jobs.finish(): тело track заявляет свой
исход, Пульт рендерит .b-warn «с замечаниями». Хард-гейты и путь AutonomyRun
не тронуты.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
```

---

## Финальное ревью ветки

- [ ] **Whole-branch review** через `combine-reviewer` (opus): собрать пакет
  `git diff main...HEAD`, проверить два хард-гейта, чистоту оркестратора,
  дизайн-контракт (`.b-warn`/`led-warn` — существующие классы), гигиену тестов,
  линейность миграций `0019→0020→0021`.
- [ ] **Полный сьют + pyflakes** финально: `.venv/bin/python -m pytest backend/tests/ -q`
  (ожидается ~594 passed) + pyflakes чист.
- [ ] **superpowers:finishing-a-development-branch** — мерж/PR по выбору пользователя.
- [ ] Оператору на боксе: `check_site_dupes.py` + `show_sources.py` → git-pull →
  миграции (`0019→0021`) → вернуть тумблеры источников на `/settings` → `/diag`.

---

## Фаза 3 — гейт-решение пользователя 🚦 (кода в этом плане нет)

**F3 (P1, условно):** `publish.py:105` `db.get(Offer, p.offer_id)` публикует и
деактивированный оффер (`Offer.active=False`), тогда как `_pick_offer` фильтрует
`active=True`. Это дефект ТОЛЬКО если `active=False` означает «не публиковать».
**Нужно решение пользователя:**
- (а) `active=False` = «не публиковать вообще» → перед публикацией: блок с понятной
  причиной ИЛИ текст без CTA; ИЛИ
- (б) `active=False` = «не выбирать для новой генерации» → текущее поведение
  корректно, но UI обязан это объяснять.

Историческую связь «под какой оффер писался текст» (`p.offer_id`) сохранять в любом
случае — авто-подмена бренда недопустима. После ответа — отдельная мелкая задача.

## За рамками этого плана — Фаза 4 (крупный редизайн, отдельные спеки)

Требуют проектирования через **superpowers:brainstorming**, не транскрипции кода:
- **F4.1** SQL-preview 5k-пула (быстрая победа, без сети).
- **F4.2** `availability_sweep` для `discovered` eligible-now.
- **F4.3** Stage-first bulk-раннер S0→S4 + журнал наблюдений.
- **F4.4** Backorder lifecycle + WHOIS-watcher после проигрыша + UI-pill (упирается
  в `OptimizatorClient=NotImplementedError` → fallback только в ручную очередь).

Источник требований и критериев приёмки — `.codex/audits/2026-07-15-work-plan.md`
и `.codex/audits/2026-07-15-user-observations-validation.md`.
