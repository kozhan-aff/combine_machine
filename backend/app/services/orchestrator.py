"""M-оркестратор автономии. Двигает конвейер по включённым «авто»-стадиям до гейтов.

Тонкий диспетчер: НИКАКОЙ новой бизнес-логики — только (1) запрос подходящих сущностей,
(2) вызов существующего безопасного сервиса, (3) учёт. Три человеческих гейта (курация,
деньги, редактура) он НЕ трогает — см. _FORBIDDEN в докстринге run_sweep.
"""
from datetime import datetime, timezone


def _start_run(trigger: str) -> int:
    """Открыть ЖУРНАЛЬНУЮ строку свипа. ЗАМКА ЗДЕСЬ БОЛЬШЕ НЕТ (аудит F17, шаг 4).

    Раньше это был второй, самостоятельный single-flight: INSERT..WHERE NOT EXISTS(свежий
    running) с отсечкой по `started_at`. Он был строго СЛАБЕЕ реестрового:
      · судил по НАЧАЛУ прогона, а не по признакам жизни, — любой свип длиннее 15 минут (а это
        норма: одна стадия `generate` — это LLM по пяти сайтам) сам себя объявлял протухшим и
        пускал второй свип поверх живого;
      · «атомарность» держалась на INSERT..SELECT под READ COMMITTED — то есть на удаче, о чём
        честно предупреждал его собственный докстринг.
    Всё, что он охранял, охраняет частичный уникальный индекс job_run (name) WHERE running —
    настоящий запрет БД, к тому же теперь с сердцебиением (services/jobs.py). Второго мнения о
    том, идёт ли свип, быть не должно: два замка с разными критериями расходятся, и расходились.

    AutonomyRun остаётся тем, чем он полезен: ЖУРНАЛОМ (счётчики по стадиям и ошибки на
    /autopilot) и отметкой времени для throttle шедулера (last_finished_sweep_at).
    """
    from app.db import SessionLocal
    from app.models.autonomy import AutonomyRun

    with SessionLocal() as db:
        r = AutonomyRun(started_at=datetime.now(timezone.utc), trigger=trigger,
                        status="running", counts={}, errors=[])
        db.add(r)
        db.commit()
        return r.id


def _finish_run(run_id: int, status: str, counts: dict, errors: list) -> None:
    from app.db import SessionLocal
    from app.models.autonomy import AutonomyRun

    with SessionLocal() as db:
        r = db.get(AutonomyRun, run_id)
        if r is None:
            return
        r.status = status
        r.finished_at = datetime.now(timezone.utc)
        r.counts = counts
        r.errors = errors
        db.commit()


def last_finished_sweep_at() -> datetime | None:
    """Максимум finished_at завершённых прогонов (для throttle шедулера) или None."""
    from sqlalchemy import select, func
    from app.db import SessionLocal
    from app.models.autonomy import AutonomyRun

    with SessionLocal() as db:
        result = db.scalar(select(func.max(AutonomyRun.finished_at)))
        if result is None:
            return None
        # Ensure tz-aware datetime (SQLAlchemy may return naive from func.max)
        if result.tzinfo is None:
            result = result.replace(tzinfo=timezone.utc)
        return result


# --- стадии: каждая = запрос кандидатов + вызов существующего сервиса + учёт ---------
# handler(cap) -> (сделано:int, ошибки:list[str][, доп.счётчики:dict]). cap=None только у
# discovery. Третий элемент опционален: стадия вправе рассказать оператору не только «сделано
# N», но и «пропущено M и вот почему» — это не ошибка и не должно ею притворяться.

def _stage_discovery(cap):
    from app.services import discovery
    return discovery.run_discovery(), []


def _stage_score(cap):
    from app.services import scoring
    return scoring.score_pending(limit=cap), []


def _stage_queue(cap):
    """approved-домены (у них по определению нет открытого заказа) -> create_order, до капа.

    ГРЯЗЬ ОТСЕИВАЕТСЯ В ВЫБОРКЕ, ДО КАПА, — и это не косметика (ревью Задачи 6, Critical 3).
    Легаси-домены (отмытые кнопкой «↩ вернуть в approved» до фикса F9) из `approved` больше НЕ
    УХОДЯТ: политика их отвергает, статус им никто не двигает. Они сидят в голове id-порядка
    вечно — и `LIMIT cap` по сырому `approved` выедали ВЕСЬ кап каждый свип: 10 грязных при
    cap_queue=10, и чистый домен не попадал в очередь НИКОГДА. Автопилотный выкуп вставал
    намертво, а не «шумел».

    Число отсеянных возвращаем ОТДЕЛЬНЫМ счётчиком: «пропустили N грязных» — это факт о
    состоянии базы, который оператор обязан видеть, а не тишина и не строка в ошибках (ошибка
    у стадии значит «сломалось», а здесь сработала защита).
    """
    from sqlalchemy import select
    from app.db import SessionLocal
    from app.models.domain import Domain
    from app.services import acquisition
    from app.services.transitions import dirty_reason

    done, errs, dirty = 0, [], 0
    with SessionLocal() as db:
        # без limit(cap) в SQL: кап применяется к ЧИСТЫМ кандидатам, иначе грязь его и съест.
        # `approved` — курируемый набор (десятки строк), полная выборка здесь дешёвая.
        rows = db.execute(
            select(Domain).where(Domain.status == "approved").order_by(Domain.id)).scalars().all()
        ids = []
        for d in rows:
            if dirty_reason(d) is not None:
                dirty += 1
            elif len(ids) < cap:
                ids.append(d.id)
    for did in ids:
        try:
            acquisition.create_order(did)      # деньги НЕ тратит — только заявка pending_confirm
            done += 1
        except Exception as e:  # noqa: BLE001
            # сюда попадает и отказ политики (домен стал грязным между выборкой и заявкой) —
            # стадию это не роняет: остальные домены свип обработает.
            errs.append(f"domain#{did}: {type(e).__name__}: {e}")
    return done, errs, {"queue_dirty": dirty} if dirty else {}


def _stage_provision(cap):
    """Две под-операции под общим капом: (а) purchased без сайта -> create_site_for;
    (б) сайт в provisioning -> provision (идемпотентен, awaiting_ns = норм, повторим).

    КАП НА ПОПЫТКИ, НЕ НА УСПЕХИ (аудит F2.1, правит регресс из F19). Пункт (б) раньше бил
    капом по `succeeded`: `awaiting_ns`/`error` бюджет не тратили и цикл не останавливали —
    при капе 5 и 100 вечно-`awaiting_ns` сайтах свип делал до 100 внешних `provision()` за
    прогон (предохранитель на квоту/нагрузку по факту снят). Возврат к `LIMIT(cap)` в SQL
    без ротации воскресил бы старый баг F19 (одни и те же первые по id сайты навсегда едят
    кап) — поэтому `LIMIT(cap)` теперь идёт ВМЕСТЕ с `ORDER BY last_attempt_at ASC NULLS
    FIRST, id`: сайт, чью попытку потратили в этом свипе, штампуется и уходит в хвост
    очереди — следующий свип берёт ДРУГИЕ сайты. Фейрнесс F19 больше не гарантирован ВНУТРИ
    одного свипа (кап=1 при вечно-`awaiting_ns` первом сайте не дотянется до второго в ТОТ ЖЕ
    прогон), но вечного голода нет — ротация распределяет попытки МЕЖДУ свипами, и это и есть
    цена настоящего предохранителя: попытки — конечный, охраняемый ресурс, а не успехи.
    Штамп `last_attempt_at` пишется сразу после выборки, в ТОЙ ЖЕ сессии — иначе гонка
    (панель + автопилотный свип, РАЗНЫЕ ПРОЦЕССЫ) могла бы взять один и тот же сайт дважды
    до того, как первая попытка успеет отметиться.
    """
    from sqlalchemy import select, update
    from app.db import SessionLocal
    from app.models.domain import Domain
    from app.models.site import Site
    from app.services import provisioning

    succeeded, errs, awaiting, ssl_failed = 0, [], 0, 0
    with SessionLocal() as db:
        purchased = [r[0] for r in db.execute(
            select(Domain.id).where(Domain.status == "purchased",
                                    ~Domain.id.in_(select(Site.domain_id)))
            .order_by(Domain.id).limit(cap)).all()]
        prov_ids = [r[0] for r in db.execute(
            select(Site.id).where(Site.status == "provisioning")
            .order_by(Site.last_attempt_at.asc().nulls_first(), Site.id).limit(cap)).all()]
        if prov_ids:
            db.execute(update(Site).where(Site.id.in_(prov_ids))
                       .values(last_attempt_at=datetime.now(timezone.utc)))
            db.commit()
    for did in purchased:
        if succeeded >= cap:
            break
        try:
            provisioning.create_site_for(did)
            succeeded += 1
        except Exception as e:  # noqa: BLE001
            errs.append(f"domain#{did}: {type(e).__name__}: {e}")
    for sid in prov_ids:
        try:
            out = provisioning.provision(sid)
            st = out.get("status") if isinstance(out, dict) else None
            if st == "awaiting_ns":
                # ждёт человека (NS у регистратора) — не успех и не отказ; кап и `errors` не
                # трогаем, продолжаем цикл дальше по списку (см. докстринг выше — фейрнесс).
                awaiting += 1
                continue
            if st == "error":
                # раньше тихо считалось `done += 1` (напр. VPS_ORIGIN_IP не задан) — это отказ,
                # а не успех: оператор обязан увидеть его в ошибках свипа, не в идеальной сводке.
                errs.append(f"site#{sid}: {out.get('error', 'провижн вернул ошибку')}")
                continue
            succeeded += 1
            # vhost поднят (провижн не упал), а SSL-режим Cloudflare не переключился. Считать
            # это чистым успехом — врать в отчёте свипа: HTTPS под вопросом, а сводка идеальна.
            # След есть в БД и на карточке сайта, но автопилотный прогон обязан сказать вслух.
            if out.get("ssl_error"):
                ssl_failed += 1
        except Exception as e:  # noqa: BLE001
            errs.append(f"site#{sid}: {type(e).__name__}: {e}")
    extra = {}
    if awaiting:
        extra["provision_awaiting"] = awaiting
    if ssl_failed:
        extra["ssl_failed"] = ssl_failed
    return succeeded, errs, extra


def _stage_generate(cap):
    """Сайты status=content, где страниц МЕНЬШЕ ожидаемого -> generate_site(use_competitor=True).

    Раньше селектор был «у сайта вообще нет страниц» (аудит F19, пункт Б): `scaffold()` даёт
    фиксированный набор страниц (3 спеки) за вызов, но если хотя бы одна LLM-генерация
    вернула пустое тело (`if not body.strip(): continue` в content.py — не крах батча, а
    пропуск ОДНОЙ страницы), у сайта появляется >=1 Page при < ожидаемого, и старый селектор
    ("нет ни одной") больше НИКОГДА его не выбирал — сайт застревал с недостающими страницами
    навсегда. `generate_site()` для КАЖДОЙ спеки сам проверяет «уже есть» (`Page.url_path`) и
    пропускает существующие — повторный вызов на сайте с частью страниц ДОЗАПОЛНЯЕТ
    недостающие, а не дублирует (а гонку двух процессов на одном пути дополнительно ловит
    `uq_page_per_path`, миграция 0014 — см. content.generate_site/IntegrityError)."""
    from sqlalchemy import select, func
    from app.db import SessionLocal
    from app.models.site import Site, Page
    from app.services import content

    expected = len(content.scaffold(""))   # число страниц/сайт — фиксировано scaffold(), не зависит от бренда
    done, errs = 0, []
    with SessionLocal() as db:
        page_counts = (
            select(Page.site_id, func.count(Page.id).label("n"))
            .group_by(Page.site_id).subquery())
        ids = [r[0] for r in db.execute(
            select(Site.id)
            .outerjoin(page_counts, page_counts.c.site_id == Site.id)
            .where(Site.status == "content",
                   func.coalesce(page_counts.c.n, 0) < expected)
            .order_by(Site.id).limit(cap)).all()]
    for sid in ids:
        try:
            content.generate_site(sid, use_competitor=True)
            done += 1
        except Exception as e:  # noqa: BLE001
            errs.append(f"site#{sid}: {type(e).__name__}: {e}")
    return done, errs


def _stage_publish(cap):
    """Сайты с ≥1 edited-страницей -> publish_site (публикует все edited; гейт держится в сервисе)."""
    from sqlalchemy import select
    from app.db import SessionLocal
    from app.models.site import Site, Page
    from app.services import publish

    done, errs = 0, []
    with SessionLocal() as db:
        ids = [r[0] for r in db.execute(
            select(Site.id).where(Site.id.in_(
                select(Page.site_id).where(Page.status == "edited")))
            .order_by(Site.id).limit(cap)).all()]
    for sid in ids:
        try:
            publish.publish_site(sid)
            done += 1
        except Exception as e:  # noqa: BLE001
            errs.append(f"site#{sid}: {type(e).__name__}: {e}")
    return done, errs


def _stage_check_index(cap):
    """Сайты с published-страницами -> check_index (site: через SearXNG).

    Страницы, про которые проверка ничего не выяснила (движки SearXNG не ответили — CAPTCHA/
    лимит), считаем ОТДЕЛЬНО: сайт тут ни при чём, сломан поисковик, и молчаливое «сделано N»
    выдало бы незнание за проделанную работу. Прецедент — `queue_dirty`/`ssl_failed`.
    """
    from sqlalchemy import select
    from app.db import SessionLocal
    from app.models.site import Site, Page
    from app.services import publish

    done, errs, blind = 0, [], 0
    with SessionLocal() as db:
        ids = [r[0] for r in db.execute(
            select(Site.id).where(Site.id.in_(
                select(Page.site_id).where(Page.status == "published")))
            .order_by(Site.id).limit(cap)).all()]
    for sid in ids:
        try:
            out = publish.check_index(sid)
            done += 1
            blind += sum(1 for st in (out.get("pages") or {}).values() if st == "unknown")
        except Exception as e:  # noqa: BLE001
            errs.append(f"site#{sid}: {type(e).__name__}: {e}")
    return done, errs, {"index_unknown": blind} if blind else {}


# порядок конвейера — единственный источник истины оркестратора
STAGES = [
    ("discovery", "auto_discovery", None, _stage_discovery),
    ("score", "auto_score", "cap_score", _stage_score),
    ("queue", "auto_queue", "cap_queue", _stage_queue),
    ("provision", "auto_provision", "cap_provision", _stage_provision),
    ("generate", "auto_generate", "cap_generate", _stage_generate),
    ("publish", "auto_publish", "cap_publish", _stage_publish),
    ("check_index", "auto_check_index", "cap_check_index", _stage_check_index),
]

STAGE_RU = {"discovery": "поиск", "score": "скоринг", "queue": "очередь",
            "provision": "провижн", "generate": "контент", "publish": "публикация",
            "check_index": "индексация"}

# подписи строки «по стадиям» в журнале свипов (autopilot.html). Ключи счётчиков — не только
# стадии: `queue_dirty` рассказывает, сколько грязных доменов стадия обошла стороной,
# `ssl_failed` — у скольких сайтов vhost поднят, а SSL-режим Cloudflare не переключился,
# `index_unknown` — про сколько страниц проверка индексации ничего не выяснила (движки молчат),
# `provision_awaiting` — сколько сайтов ждут смены NS у регистратора (не успех, не отказ — F19).
COUNT_RU = {**STAGE_RU, "queue_dirty": "грязь пропущена", "ssl_failed": "SSL не переключился",
            "index_unknown": "индекс не выяснен", "provision_awaiting": "провижн: ждёт NS"}


def run_sweep(trigger: str = "cron", respect_master: bool = True) -> dict:
    """Прогнать включённые авто-стадии до гейтов. respect_master=False у ручного запуска.

    ЖЁСТКО: зовёт ТОЛЬКО безопасные сервисы из STAGES. НИКОГДА — confirm_order/
    execute_confirmed_order/mark_caught (деньги) и mark_edited (редактура): эти три гейта
    двигает только человек через роуты панели. Ошибка одной сущности не топит стадию/свип.

    Прогресс пишет сам (jobs.track) — именно поэтому свип из воркера теперь виден Пульту.
    Выключенные тумблером стадии показываем как skip, а не прячем: «стадия отключена» и
    «стадия сломалась» — разные вещи, и оператор обязан их различать.
    """
    from app.services import jobs
    from app.services.autonomy import get_autonomy

    cfg = get_autonomy()
    if respect_master and not cfg["autopilot_on"]:
        return {"skipped": "autopilot_off"}

    enabled = [s for s in STAGES if cfg[s[1]]]
    stages = [{"key": k, "label": STAGE_RU[k],
               "state": "pending" if cfg[flag] else "skip"} for k, flag, _, _ in STAGES]
    total = len(enabled)
    counts, errors, status = {}, [], "done"
    run_id = None
    try:
        # ЕДИНСТВЕННЫЙ замок свипа (шаг 4 F17): его держит реестр — уникальным индексом и
        # сердцебиением. Занято -> AlreadyRunning, и журнальной строки НЕ ЗАВОДИМ: свипа не
        # было. Раньше здесь писался прогон со статусом done — он попадал в журнал как
        # состоявшийся И двигал last_finished_sweep_at, из-за чего шедулер откладывал
        # СЛЕДУЮЩИЙ свип, приняв несостоявшийся за только что отработавший.
        with jobs.track("sweep", trigger="auto" if trigger == "cron" else "manual",
                        stages=stages) as run:
            run_id = _start_run(trigger)
            try:
                for i, (key, _flag, cap_attr, handler) in enumerate(enabled):
                    # Между стадиями (не внутри — стадия атомарна для нас) спрашиваем реестр:
                    # нажали ли «стоп» и НАШ ЛИ ЕЩЁ ЗАМОК. Второе — фенсинг: если нас сочли
                    # трупом и отдали замок другому процессу, продолжать = гнать конвейер
                    # ВТОРЫМ (дубли страниц, двойной счёт LLM). Тот же контракт, по которому
                    # между доменами останавливается score_pending, — новой логики нет.
                    if jobs.cancelled(run):
                        raise jobs.Cancelled()
                    jobs.report(run, done=i, total=total, stage=key, current=STAGE_RU[key])
                    cap = cfg[cap_attr] if cap_attr else None
                    try:
                        # третий элемент — доп.счётчики стадии (см. контракт handler выше);
                        # у стадий без него распаковка даёт пустой хвост.
                        n, errs, *extra = handler(cap)
                        counts[key] = n
                        if extra:
                            counts.update(extra[0])
                        errors += [f"{key}: {e}" for e in errs]
                    except jobs.AlreadyRunning:
                        # ЗАМОК СРАБОТАЛ ШТАТНО, а не сломался: оператор прямо сейчас гоняет свой
                        # score/discovery, и второй прогон поверх — ровно то, что мы запрещали
                        # (двое жгут квоту A-Parser). Пропустить стадию и сказать об этом честно;
                        # красить весь свип в failed = кричать волком на собственную защиту.
                        errors.append(f"{key}: пропущена — занята ручным прогоном")
                    except Exception as e:  # noqa: BLE001 — стадия целиком упала (не одна сущность)
                        errors.append(f"{key}: {type(e).__name__}: {e}")
                        status = "failed"
                if status == "done" and errors:
                    # НИ ОДНА стадия не упала целиком (иначе status уже "failed"), но внутри
                    # стадий упали отдельные СУЩНОСТИ (errors непустой) — «всё прошло идеально»
                    # и «часть сущностей упала» раньше были неразличимы в итоговом статусе
                    # свипа (аудит F19, пункт В). Честный статус: не done, но и не failed —
                    # свип не оборвался, он завершился С ЗАМЕЧАНИЯМИ.
                    status = "completed_with_errors"
                jobs.report(run, done=total, total=total, current="",
                            message=f"стадий пройдено: {total}" + (f" · ошибок: {len(errors)}" if errors else ""))
            except jobs.Cancelled:
                status = "cancelled"
                errors.append("свип остановлен — стоп-кнопка или потерянный замок")
                raise                      # track закроет прогон как cancelled, не как failed
            except BaseException:          # noqa: BLE001 — оборвался сам свип, а не стадия
                status = "failed"
                raise
            finally:
                # ЖУРНАЛ ПИШЕМ ВСЕГДА, чем бы всё ни кончилось: свип, оборвавшийся на середине,
                # обязан оставить в /autopilot то, что успел сделать, — иначе счётчики врут
                # молчанием. Частичные counts честнее пустого места.
                _finish_run(run_id, status, counts, errors)
    except jobs.AlreadyRunning:
        # а ЭТОТ AlreadyRunning — от самого track("sweep"): свип уже идёт в другом процессе.
        return {"skipped": "already_running"}
    return {"run_id": run_id, "status": status, "counts": counts, "errors": errors}
