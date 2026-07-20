"""M1b — Domain/donor scoring. Implements the funnel in docs/DONORS.md on the FREE stack.

Order: pre-filter -> history (Wayback) -> risk (RKN, blacklist) -> indexed_echo (SearXNG)
-> composite score + breakdown -> status approved | scored(manual) | rejected.
`compute_score` is pure (unit-tested below); `score_domain` does the I/O + DB write.
"""
import logging
import math
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.services import scoring_config as cfg
from app.services import whois as whois_router

# Запас после дедлайна дропа, прежде чем считать домен потерянным навсегда.
#
# `delete_date` в фиде backorder — ДАТА без времени ("2026-07-08", см. docs/api/backorder.md),
# и discovery._parse_deadline превращает её в 00:00 UTC дня дропа. Значит уже в 00:01 того же
# дня условие «дедлайн в будущем» ложно — а домен ещё зарегистрирован: реестр освобождает его
# в течение дня. Без этого запаса перепроверка отбраковывала бы дроп РОВНО В ТОТ ДЕНЬ, когда
# его можно ловить, то есть выбрасывала бы самые ценные домены. Запас покрывает и полуночное
# усечение даты, и сдвиг релиза в реестре на сутки.
DROP_GRACE = timedelta(days=2)

# Как часто перепробовать домен, у которого дедлайна НЕТ (витрины reg.ru/sweb дропов дату не
# отдают; у cctld она может не разобраться из имени архива).
#
# Соблазн «спросили один раз — больше не спрашиваем» здесь СМЕРТЕЛЕН: «занят сегодня» без даты
# дропа не говорит НИЧЕГО про то, когда домен освободится. Один шанс = домен никогда не увидит
# собственного дропа и навсегда осядет в discovered (ревью 2026-07-13, Critical 1). Детерминизм
# есть только там, где дата известна — там мы и не переспрашиваем (см. scorable, ветка 2).
# Сутки: дропы происходят ежедневно, а расход сверху ограничен max_whois_per_run.
RECHECK_EVERY = timedelta(days=1)


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, x))


def _jsonable(v):
    """Рекурсивно приводит sig к JSON-совместимому виду для записи в domain_score_log.

    sig несёт РЕАЛЬНЫЕ datetime-объекты (`acquirability_checked_at`, `whois_created`,
    `first_seen` — whois/Wayback отдают их как datetime, не строки), а JSONB на этом
    стеке (и Postgres-адаптер, и sqlite-shim в conftest.py) сериализует через голый
    `json.dumps` без кастомного encoder'а — TypeError на первом же datetime. Не
    мутирует исходный sig: setattr(d, col, ...) чуть выше по коду в score_domain()
    уже забрал из него настоящие datetime-значения для колонок Domain, им нужен
    именно datetime, а не строка."""
    from datetime import date, datetime
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_jsonable(x) for x in v]
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, date):
        return v.isoformat()
    return v


# Чипы воронки в панели: ключ -> подпись. Порядок = порядок проверок в _funnel.
# Ahrefs шестой: он платный (капча за штуку) и при max_ahrefs_per_run=0 помечается skip —
# честнее показать выключенную стадию, чем спрятать её.
FUNNEL_STAGES = [
    {"key": "rd", "label": "RD из фида"},
    {"key": "whois", "label": "whois-возраст"},
    {"key": "risk", "label": "РКН/блэклист"},
    {"key": "echo", "label": "эхо в индексе"},
    {"key": "history", "label": "Wayback-история"},
    {"key": "ahrefs", "label": "Ahrefs (платно)"},
]

# Проверки, чей отказ означает «домен судили ВСЛЕПУЮ». Гарды в _decide не дают авто-approve
# без Wayback — домен уходит в scored, то есть В ИНБОКС К ЧЕЛОВЕКУ, и там неотличим от честно
# проверенного. Человек штампует непроверенное, думая, что машина посмотрела историю.
#
# ИСТОРИИ ЗДЕСЬ НЕТ НАМЕРЕННО: её вердикт считает history_verdict (см. ниже). Раньше она жила
# тут ключом "wayback" — и это был баг (аудит, F2): «вслепую» выводилось из ФАКТА ОШИБКИ, а
# Wayback, не нашедший ни одного снимка, ошибки не бросает (`wayback_checked=False`, errors
# пуст). Домен, чью историю никто не смотрел, приезжал в инбокс с подписью «история чистая».
_BLIND_RU = {
    # whois кормит ДВА гейта воронки: возраст (`too_young`) и занятость (`available` -> лейн,
    # `not_acquirable`). Молчащий A-Parser не ослаблял их, он снимал их целиком: без даты
    # сравнивать не с чем, отказа нет, и домен ехал дальше «как проверенный» (аудит F6).
    # Балл гейт возраста не дублирует — юный домен с большой ссылочной массой набирает ~0.71
    # и порог берёт. Эта формулировка — для случая, когда возраста НЕ дал никто.
    "whois": "возраст НЕ проверен: whois не ответил — гейт «слишком молодой» не применялся, "
             "занятость домена тоже не сверена",
    "ahrefs": "ссылочный профиль НЕ проверен: Ahrefs не ответил",
    "rkn": "РКН НЕ проверен: реестр не ответил",
    "blacklist": "блэклист НЕ проверен",
    "safebrowsing": "Google Safe Browsing НЕ проверен: сервис не ответил",
    "searxng": "эхо в индексе НЕ проверено",
}

# whois упал, но возраст всё-таки известен — из Wayback (`age_source='wayback'`, фолбэк в
# _funnel). Прежний текст здесь ЛГАЛ ровно в том состоянии, где показывался: он утверждал, что
# гейт «слишком молодой» не применялся, — а он применялся (_funnel сравнивает Wayback-возраст с
# min_age_years). Правда в другом: возраст по архиву — это НИЖНЯЯ оценка (первый снимок не
# раньше регистрации), а вот ЗАНЯТОСТЬ домена не сверял никто.
_BLIND_WHOIS_ARCHIVE_AGE = ("возраст по архиву (whois не ответил): гейт «слишком молодой» "
                            "применён по первому снимку, но занятость домена НЕ сверена")

# Категории прошлого домена — по-русски (панель русская; улики показываются куратору).
_CATS_RU = {"adult": "взрослое", "pharma": "фарма", "casino": "казино",
            "gambling": "ставки", "spam": "спам"}


def history_verdict(d) -> str:
    """Что машина РЕАЛЬНО знает об истории домена: 'clean' | 'dirty' | 'unknown'.

    ЕДИНСТВЕННЫЙ источник правды о чистоте истории — и для инбокса, и для пакетного одобрения,
    и для JSON. Три состояния, а не два: «не проверяли» — это НЕ «чисто». Домен становится
    'clean' только там, где Wayback реально прочитал большинство выборки (`wayback_checked`);
    пустой архив, троттлинг archive.org и упавший запрос дают 'unknown' — и такой домен
    исключён из пакета (см. panel._bulk_candidates).

    'dirty' проверяется ПЕРВЫМ: известная грязь главнее незнания.
    """
    pf = d.prior_flags or {}
    if any(pf.get(k) for k in cfg.HARD_REJECT_FLAGS):
        return "dirty"
    if not d.wayback_checked:
        return "unknown"
    return "clean"


def history_evidence(d) -> list[dict]:
    """Снимки, по которым машина судила историю, — готовые к показу (ссылка, дата, категории).

    Вердикт ошибается (это и доказал аудит), поэтому куратор обязан мочь ПЕРЕПРОВЕРИТЬ его
    глазами: улики пишутся в score_breakdown.history_evidence (Задача 1) — здесь они
    превращаются в ссылки на web.archive.org.

    `unread` — снимок скачался, но видимого текста на нём НЕТ (редирект-заглушка, frameset,
    SPA-оболочка; см. wayback.MIN_TEXT_CHARS). Без этой пометки строка улики выглядела бы в
    инбоксе ровно как честно прочитанная и чистая («категорий не найдено»), хотя не прочитано
    вообще ничего. Улики без `chars` — из прогонов ДО этой пометки: догадываться о них нельзя,
    считаем прочитанными (как их и трактовал тогдашний вердикт).
    """
    from app.integrations.wayback import MIN_TEXT_CHARS

    out = []
    for e in (d.score_breakdown or {}).get("history_evidence") or []:
        url, ts = e.get("url") or "", str(e.get("timestamp") or "")
        if not url or len(ts) < 8:   # пустые url/timestamp -> битая ссылка web.archive.org/web//
            continue
        chars = e.get("chars")
        out.append({
            "link": f"https://web.archive.org/web/{ts}/{url}",
            "url": url,
            "when": f"{ts[6:8]}.{ts[4:6]}.{ts[:4]}" if len(ts) >= 8 else ts,
            "cats": ", ".join(_CATS_RU.get(c, c) for c in e.get("cats") or []),
            "chars": chars,
            "unread": isinstance(chars, int) and chars < MIN_TEXT_CHARS,
        })
    return out


def blind_reason(d) -> str | None:
    """Домен оценён при недоступной/несостоявшейся проверке — в пакет одобрения он не идёт.

    История идёт первой строкой: она — главный инвариант проекта («домены берём за чистую
    историю»), и именно она молча выдавала непроверенное за чистое.
    """
    errors = [str(e) for e in ((d.score_breakdown or {}).get("errors") or [])]
    if history_verdict(d) == "unknown":
        if any(e.startswith("wayback:") for e in errors):
            return "история НЕ проверена: Wayback был недоступен"
        if (d.score_breakdown or {}).get("history_evidence"):
            # снимки есть, но прочитать удалось меньшинство (троттлинг archive.org) —
            # вердикт по паре страниц был бы гаданием
            return "история НЕ проверена: прочитано слишком мало снимков"
        # sampled==0 — либо CDX вернул пустой список (архив реально пуст), либо снимки были,
        # но ни одно тело не открылось (троттлинг/5xx archive.org глушатся внутри classify_history,
        # evidence остаётся пустым). Различить эти два случая из score_breakdown нельзя, поэтому
        # формулировка НЕ утверждает «снимков нет» как факт. sampled отсутствует (None) — домен
        # отскорен ДО того, как поле стали писать (иначе не заполнено): и это тоже не «архива нет».
        if (d.score_breakdown or {}).get("sampled") == 0:
            return "история НЕ проверена: снимков нет или ни один не открылся — судить не по чему"
        return "история НЕ проверена: улик нет в базе — перепроверь"
    for e in errors:
        head = e.split(":", 1)[0]
        if head == "whois" and (d.score_breakdown or {}).get("age_source") == "wayback":
            return _BLIND_WHOIS_ARCHIVE_AGE
        if head in _BLIND_RU:
            return _BLIND_RU[head]
    return None


def history_note(d) -> str | None:
    """Информационная пометка о СВЕЖЕСТИ вердикта истории. НЕ блокирует — и это осознанно.

    Побочный эффект правила «не затирать проверенное» (аудит F9/C2): домен, чью историю
    проверили РАНЬШЕ, а сегодня Wayback не ответил, сохраняет `wayback_checked=True` и
    вердикт `clean` — про сегодняшний отказ архива не говорил НИКТО (ошибка живёт в
    `score_breakdown.errors`, куда куратор не смотрит).

    Почему НЕ блокирует (и почему эта пометка живёт рядом с `blind_reason`, а не внутри него):
    вердикт опирается на РЕАЛЬНЫЕ прошлые улики, они сохранены и показываются строкой ниже, а
    авто-approve по-прежнему гардится по `sig` ТЕКУЩЕГО прогона (`_decide`) — машина такой домен
    сама не одобрит. Запирать его от пакетного одобрения из-за ТРАНЗИЕНТНОГО сбоя архива значило
    бы завести ровно ту тихую ловушку, от которой ветка избавлялась: домены, помеченные сетевым
    чихом, копятся навсегда и никем не разбираются.
    """
    if history_verdict(d) == "unknown":
        return None                    # там своё слово скажет blind_reason — не дублируем
    errors = [str(e) for e in ((d.score_breakdown or {}).get("errors") or [])]
    if any(e.startswith("wayback:") for e in errors):
        return "вердикт — из прошлой проверки: сегодня Wayback не ответил"
    return None


def bulk_ok(d) -> bool:
    """Домен годится для ПАКЕТНОГО одобрения и вправе носить подпись «история чистая».

    ОДИН предикат для panel._bulk_candidates (что реально становится `approved`) и для
    строки инбокса (что рисуется как «чистая»): если эти два места переизобретают условие
    порознь, они разъедутся ровно в режиме бага, который это и чинит (аудит F2) — расхождение
    всплывёт молча, когда `history_verdict`/`blind_reason` обрастут новым значением/веткой.

    `dirty_reason` (аудит F9) добавлен СЮДА, а не рядом с пакетом, по тому же правилу: новое
    основание «нельзя» обязано пройти через единый предикат, иначе строка инбокса подписала бы
    «история чистая» домен, который пакет молча пропускает. `history_verdict` ловит грязь ТОЛЬКО
    по `prior_flags`; РКН и блэклист — это отдельные колонки, и до сих пор их здесь не видел никто.
    """
    from app.services.transitions import dirty_reason   # ленивый: transitions зовёт нас в ответ
    return history_verdict(d) == "clean" and not blind_reason(d) and not dirty_reason(d)


def _decide(score: float, sig: dict, approve_at: float, manual_review_at: float) -> str:
    """Pure: score threshold -> status, plus the two invariant downgrade guards below.
    Factored out (2026-07 review, Finding 1) so BOTH `compute_score` (static cfg.DECISION)
    and `score_domain` (runtime /settings thresholds) decide through the same logic — the
    live sliders used to only move preview counters, never the actual stored status."""
    status = ("approved" if score >= approve_at
              else "scored" if score >= manual_review_at
              else "rejected")
    # core invariant (CLAUDE.md): never AUTO-approve a domain whose history we could not
    # verify — a successful Wayback pass is mandatory. If it failed/absent, downgrade to
    # manual review. (Emergent from the weights today, but pinned so reweighting can't break it.)
    if status == "approved" and not sig.get("wayback_checked"):
        status = "scored"
    # risk-guard: если проверка RKN, blacklist или SafeBrowsing упала (ключ сигнала
    # отсутствует, ошибка осела в errors), нельзя подтверждать чистоту автоматом —
    # уводим в ручной `scored`.
    if status == "approved" and any(
            e.startswith(("rkn:", "blacklist:", "safebrowsing:"))
            for e in (sig.get("errors") or [])):
        status = "scored"
    # whois-guard (аудит F6, доведён ревью Задачи 4): whois УПАЛ — гардим по САМОМУ ОТКАЗУ,
    # тем же механизмом, что кормит бейдж «оценён вслепую», а не по `age_years is None`.
    #
    # Почему не по возрасту. Возраст, не добытый whois'ом, ДОБИРАЕТСЯ из Wayback (_funnel:
    # first_seen -> age_years), и это законно: первый снимок не раньше регистрации, значит для
    # гейта «слишком молодой» архивный возраст — консервативная НИЖНЯЯ оценка, она годится.
    # Но упавший whois означает ещё и «мы не знаем, СВОБОДЕН ли домен вообще» (`available`) —
    # а это ВТОРОЙ, независимый гейт воронки (лейн/`not_acquirable`). Его подменить нечем.
    # Поэтому отказ whois снимает право на авто-approve ДАЖЕ когда возраст известен иначе —
    # ровно тот случай, что утекал живьём: clara-c.ru (RD 2219, возраст из архива 16 лет,
    # score 0.87) авто-одобрялся с бейджем «оценён вслепую» на лбу.
    if status == "approved" and any(
            e.startswith("whois:") for e in (sig.get("errors") or [])):
        status = "scored"
    # Страховка на будущее переутяжеление весов: возраст НЕИЗВЕСТЕН вообще (whois молчит И
    # архив пуст) — значит гейт `too_young` не применялся ни разу, сравнивать было не с чем.
    # Балл его не подстраховывает: `age` весит 0.18, и домен без возраста, но с проверенной
    # историей + RD за потолком + эхом набирает ровно 0.70 == approve_at. Сегодня эта ветка
    # недостижима (пустой архив -> wayback_checked=False -> уже сработал гард выше), и это
    # правильно: инвариант должен пережить перенастройку весов, а не зависеть от неё.
    if status == "approved" and sig.get("age_years") is None:
        status = "scored"
    return status


def compute_score(sig: dict, weights: dict | None = None) -> dict:
    """Pure: signals -> {score, status, breakdown}. No I/O. See scoring_config for knobs.

    `weights` — рантайм-веса с /settings (None -> дефолты из scoring_config). Сумма НЕ обязана
    быть 1.0: нормируем на неё, иначе оператор, подвинувший один ползунок, незаметно менял бы
    масштаб всей шкалы 0..1 — и пороги approve/manual начали бы значить не то, что показывают.
    """
    pf = sig.get("prior_flags") or {}

    # --- hard rejects (Stage E) ---
    #
    # Здесь БЫЛИ ещё две ветки отказа, и обе удалены как призраки (аудит 2026-07-14):
    #   · `trademark_risk` (F5) — читался из БД, но НИ ОДИН код его не вычислял: ни расчёта, ни
    #     формы, ни импорта. Значение всегда NULL, ветка мертва. Гейт, который выглядит рабочим
    #     и не работает, опаснее отсутствующего: он врёт куратору, что юр-риск проверен. Считать
    #     его вслепую по докстрингу — ровно та ошибка, что похоронила cctld-источник, поэтому
    #     ветка снята, а колонка `Domain.trademark_risk` оставлена (данные не рушим).
    #   · `topic_switch` (F4) — строгое подмножество категорийного отказа ниже: см. wayback.py,
    #     флаг больше не производится.
    reasons = []
    if sig.get("rkn_listed"):
        reasons.append("rkn_listed")
    if sig.get("blacklisted") is True:
        reasons.append("blacklisted")
    reasons += [f"prior_{c}" for c in cfg.HARD_REJECT_FLAGS if pf.get(c)]
    if reasons:
        return {"score": 0.0, "status": "rejected", "breakdown": {"hard_reject": reasons}}

    # --- composite (Stage F) ---
    n = cfg.NORM
    comp = {
        # spam (как и остальная грязная история) уже отсеян hard-reject'ом выше —
        # уцелевший домен чист: полный балл при проверенной истории, половина при непроверенной.
        "history_cleanliness": 1.0 if sig.get("wayback_checked") else 0.5,
        "authority": _clamp((sig.get("dr") or 0.0) / n["DR_FULL"]),
        "age": _clamp((sig.get("age_years") or 0.0) / n["AGE_FULL"]),
        "rd_proxy": _clamp(math.log10((sig.get("referring_domains") or 0) + 1)
                           / math.log10(n["RD_FULL"] + 1)),
        "indexed_echo": 1.0 if sig.get("indexed_echo") else 0.0,
    }
    w = {k: float(v) for k, v in (weights or cfg.WEIGHTS).items() if k in comp}
    norm = sum(w.values()) or 1.0
    score = round(_clamp(sum(w[k] * comp[k] for k in w) / norm), 4)
    status = _decide(score, sig, cfg.DECISION["approve_at"], cfg.DECISION["manual_review_at"])
    return {"score": score, "status": status,
            "breakdown": {"components": comp, "weights": w}}


def _make_clients() -> dict:
    """Собрать интеграционные клиенты один раз на прогон (переиспользуются между доменами)."""
    from app.integrations.wayback import WaybackClient
    from app.integrations.rkn import RknClient
    from app.integrations.blacklist import BlacklistClient
    from app.integrations.searxng import SearxngClient
    from app.integrations.aparser import AParserClient
    from app.integrations.whois_tci import TciWhoisClient
    return {
        "wayback": WaybackClient(), "rkn": RknClient(), "blacklist": BlacklistClient(),
        "searxng": SearxngClient(), "aparser": AParserClient(), "tci": TciWhoisClient(),
        "_whois_lock": threading.Lock(),
    }


def acquirability_verdict(available, acquire_deadline, now, *, lane) -> str:
    """whois-доступность + дедлайн ловли -> 'free' | 'taken' | 'waiting' | 'unknown'.

    ЕДИНСТВЕННОЕ место, где решается «можно ли ещё купить». Его зовут и воронка (T1, при
    первом скоринге), и перепроверка (recheck_acquirability, потом) — двух версий правды
    здесь быть не должно.

    'waiting' — домен занят СЕЙЧАС, и это нормально: дроп ещё не наступил. Так выглядит
    любой backorder-кандидат до своей delete_date.
    'taken' — занят, и ждать больше нечего: дедлайн с запасом прошёл (домен продлили или
    перехватили) либо свободный домен кто-то зарегистрировал. Для отобранного донора это
    и есть протухание.

    ОСТОРОЖНО: 'taken' стоит дорого — домен уходит в rejected. Поэтому в каждом сомнении
    отвечаем 'unknown'/'waiting', а не 'taken': потерянный ценный дроп хуже лишней проверки.
    Именно поэтому `lane` — обязательный именованный аргумент: с дефолтом None вызывающий,
    забывший его передать, получал бы 'taken' на bid-домене, то есть ровно тот баг.
    """
    from datetime import timezone
    if available is True:
        return "free"
    if available is None:
        return "unknown"
    # available is False — домен ЗАНЯТ сейчас. Навсегда ли — решает дедлайн дропа.
    dl = acquire_deadline
    if dl is not None and dl.tzinfo is None:          # из БД дата может прийти naive
        dl = dl.replace(tzinfo=timezone.utc)
    if dl is None:
        # Без даты дропа судить почти не по чему, а цена ошибки — выброшенный ценный дроп.
        # 'taken' здесь заслуживает ТОЛЬКО lane='free': такой домен обязан быть свободен к
        # регистрации, и раз он занят — его кто-то выкупил. Всё остальное молчит:
        #   bid  — «занят» это НОРМА, домен ждёт своего дропа;
        #   NULL — лейн НЕИЗВЕСТЕН (записи старше коммита 69ef659, сырые витрины), и принимать
        #          незнание за «домен свободного лейна» нельзя. Ровно так на живом боксе утекли
        #          лучшие домены базы: clara-c.ru (score 0.89, RD 2219) и ещё 28 — все lane=NULL.
        return "taken" if lane == "free" else "unknown"
    if now <= dl + DROP_GRACE:
        return "waiting"                             # дроп ещё не наступил или идёт прямо сейчас
    return "taken"                                   # дедлайн с запасом прошёл, а домен занят


def scorable(now):
    """SQL-условие «этот домен МОЖЕТ пройти T1 прямо сейчас» — фильтр выборки score_pending.

    Без него воронка платит whois'ом за ответ, который уже знает. Не-bid домен до своего дропа
    ГАРАНТИРОВАННО занят (реестр освобождающихся на то и реестр), вердикт вернёт `waiting`, домен
    останется discovered — и следующий прогон купит тот же ответ заново. Пока такие домены
    терминально уезжали в rejected (баг с lane=NULL), пул не копился; теперь cctld везёт дедлайн,
    и весь реестр (~9.5 тыс.) законно ждёт дропа неделями. Один `весь пул` выжигал бы
    max_whois_per_run на одних и тех же строках с нулевым продвижением.

    Берём, значит, только тех, у кого есть шанс:
      · lane='bid' — backorder: T1 короткозамкнут лейном, whois нужен ради возраста;
      · дроп НАСТУПИЛ (`deadline <= now`) — сегодня whois впервые может сказать «свободен».
        До дропа не переспрашиваем: ответ известен по ДАТЕ, а не по догадке. F20 (аудит
        2026-07-14): здесь стоял `<= now + DROP_GRACE` — не «наступил с запасом», а «наступит
        В ПРЕДЕЛАХ DROP_GRACE ВПЕРЕДИ», то есть дроп ЗАВТРА/послезавтра уже проходил сюда, хотя
        такой домен гарантированно ещё занят. DROP_GRACE здесь не нужен вообще — это ДРУГАЯ
        граница, чем верхний запас ПОСЛЕ дропа в acquirability_verdict, путать нельзя;
      · дедлайна НЕТ — раз в RECHECK_EVERY. Здесь одним шансом обойтись нельзя: «занят сегодня»
        без даты дропа не говорит ничего про день освобождения, и домен (вся популяция
        reg.ru/sweb) никогда не увидел бы собственного дропа.
    """
    from app.models.domain import Domain
    from sqlalchemy import or_, and_
    return or_(
        Domain.lane == "bid",
        and_(Domain.acquire_deadline.is_not(None), Domain.acquire_deadline <= now),
        and_(Domain.acquire_deadline.is_(None),
             or_(Domain.acquirability_checked_at.is_(None),
                 Domain.acquirability_checked_at < now - RECHECK_EVERY)),
    )


def _deadline_from_whois(existing, free_date, now, lane):
    """Дедлайн выкупа из whois free-date.

    `free_date` — ПРОЕКЦИЯ «освободится, если не продлят» (paid-till + запас реестра),
    она есть у КАЖДОГО занятого .ru-домена (живой факт: у yandex.ru/mail.ru она тоже
    есть, хотя оба продлеваются из года в год) — не путать с гарантированной датой
    дропа. None = «не знаем».

    Пустой дедлайн она заполняет всегда — КРОМЕ bid/free: у обоих «домен занят» есть СВОЙ
    законный терминал, и класть им проекцию значит рисовать надежду там, где машина уже
    знает, что дальше решать нечего:
      bid  — дедлайн из фида (лейн+цена, денежный путь M2), занят = снайпнут конкурентом;
      free — whois уже подтвердил, что домен был свободен, занят = его КУПИЛИ (см.
             acquirability_verdict: `taken` заслуживает именно lane='free'). Возврат вердикта
             для free не зависит от даты (occupied+free всегда taken) — но проекция всё равно
             утекала бы в UI как «освободится X» на терминально недостижимом домене, если
             заполнять её даже в пустое поле (находка повторного ревью, 2026-07-20: исключение
             раньше проверялось ТОЛЬКО при обновлении уже известного дедлайна, а пустой
             `existing is None` уходил в отдельную ветку ДО проверки лейна).
    Целевая популяция — lane=NULL (бездедлайновый пул).

    Уже известный (для НЕ bid/free) дедлайн эта проекция обновляет — только если он ПРОТУХ:
    без этого домен из бездедлайнового пула, получивший проекцию, дождавшийся её и
    ПРОДЛЁННЫЙ владельцем, навсегда застревал бы на мёртвой дате — следующий whois
    честно приносит свежий free_date, а старое правило (`existing is not None`) его
    выбрасывало, вердикт судил по трупу даты и хоронил домен в not_acquirable/rejected
    (находка финального ревью, 2026-07-20)."""
    from datetime import datetime, time, timezone
    if free_date is None or lane in ("bid", "free"):
        return existing
    new = datetime.combine(free_date, time.min, tzinfo=timezone.utc)
    if existing is None:
        return new
    ex = existing if existing.tzinfo else existing.replace(tzinfo=timezone.utc)
    if now > ex + DROP_GRACE and new > ex:      # обновляем ТОЛЬКО просроченную и ТОЛЬКО вперёд
        return new
    return existing


# После скольких сбоев ПОДРЯД safebrowsing_check (A-Parser) перестаём его звать до конца
# прогона. Живой инцидент 2026-07-20: A-Parser упал, а BaseClient.request ретраит каждый
# transport-сбой 3 раза с exponential backoff (~30 с) — T2, задуманный как «средний» по
# цене, платил полный ретрай-шторм НА КАЖДЫЙ домен, доживший до risk-стадии (whois уже
# летал через TCI за 30 мс). Снаружи это выглядело как «воронка снова ходит по кругу всеми
# инструментами разом» — на самом деле один сломанный T2-вызов маскировался под нормальную
# стоимость этапа. Счётчик живёт на самом клиенте (как TciWhoisClient.consecutive_failures),
# клиент создаётся заново на каждый прогон (_make_clients()) — предохранитель не переживает свип.
_APARSER_SAFEBROWSING_LIMIT = 3


def _funnel(d, c, st, sig, whois_budget=None, ahrefs_budget=None, run=None) -> str | None:
    """Ступени дёшево→дорого с ранним выходом. Возвращает reject_reason или None,
    наполняя sig. Приобретаемость — гейт на T1: whois решает free/занят для сырых
    источников (backorder объявляет lane=bid сам). Дорогой Wayback (T3) — только для
    приобретаемых выживших. sig['acquirability_unresolved']=True → оставить domain discovered."""
    from datetime import datetime, timezone
    from app.services import jobs
    now = datetime.now(timezone.utc)

    jobs.report(run, stage="rd")
    # T0 — фид (0 стоимости)
    if d.feed_flags and any(d.feed_flags.get(k) for k in ("rkn", "judicial", "block")):
        return "feed_flag"
    if d.referring_domains is not None and d.referring_domains < st["min_referring_domains"]:
        return "low_rd"

    jobs.report(run, stage="whois")
    # T1 — приобретаемость + возраст (ОДИН whois-вызов, под бюджетом)
    if whois_budget is not None and whois_budget[0] <= 0:
        sig["acquirability_unresolved"] = True     # бюджет whois на прогон исчерпан — оставить discovered
        sig["unresolved_why"] = "budget"
        return None
    age_known = False
    try:
        if whois_budget is not None:
            whois_budget[0] -= 1
        pr = whois_router.probe(d.domain, c)
    except Exception as e:  # noqa: BLE001
        sig["errors"].append(f"whois:{type(e).__name__}")
        if d.lane != "bid":                         # сырому источнику whois нужен для лейна
            sig["acquirability_unresolved"] = True
            sig["unresolved_why"] = "whois_failed"
            return None
        pr = {"available": None, "created": None}   # bid: лейн из источника, продолжаем без возраста

    # whois по этому домену только что состоялся — зафиксируем факт сверки, иначе свежеотскоренный
    # донор уходит в БД с checked_at=NULL, встаёт в самую голову nulls_first-очереди перепроверки,
    # и первый же её клик тратит квоту A-Parser на домен, чей whois-ответ получен пять минут назад
    # (а подсказка «N не сверялись» краснеет сразу после Score).
    if pr.get("available") is not None:
        sig["acquirability_checked_at"] = now
    sig["whois_source"] = pr.get("whois_source")   # чем судили: tci|aparser|aparser_fallback|None

    # free-date (TCI) закрывает пустой дедлайн ДО вердикта приобретаемости — иначе
    # домен без даты дропа ещё один цикл судится по старому правилу (unknown/taken-
    # без-даты). Известный АКТУАЛЬНЫЙ дедлайн (backorder: лейн+цена) не трогаем, а
    # ПРОТУХШИЙ — обновляем вперёд; см. docstring _deadline_from_whois.
    #
    # MINOR 3 (ревью 2026-07-20): пишем в d.acquire_deadline мимо whitelist'а sig,
    # которым ниже по коду setattr() применяет остальные наблюдения — НАМЕРЕННО.
    # Whitelist защищает от отмывания грязи МЕЖДУ ПРОГОНАМИ (рескор без Wayback не должен
    # стирать вчерашний rkn_listed=True). Здесь мутация синхронна с САМИМ whois-ответом
    # этого прогона и идемпотентна (_deadline_from_whois детерминирована по (existing,
    # free_date, now, lane)) — прятать её в sig и applying позже добавило бы шаг без
    # смысла, не безопасности.
    prev_deadline = d.acquire_deadline
    d.acquire_deadline = _deadline_from_whois(d.acquire_deadline, pr.get("free_date"), now, d.lane)
    if d.acquire_deadline != prev_deadline:
        # эта дата — whois-проекция «освободится, если не продлят», не факт из фида
        # (MINOR 4, ревью 2026-07-20): панель обязана подписывать её иначе, чем СРОК ДРОПА.
        sig["deadline_source"] = "whois_projection"

    wc = pr.get("created")
    sig["whois_created"] = wc
    if wc is not None:
        age_known = True
        age = (now - wc).days / 365.25
        sig["age_years"] = round(age, 2)
        sig["age_source"] = "whois"      # ОТКУДА возраст — это не деталь: бейдж пишет по нему

    if d.lane == "bid":
        sig["lane"] = "bid"
        if age_known and age < st["min_age_years"]:
            return "too_young"
    else:
        # сюда попадаем только при lane != "bid" (bid короткозамкнут выше), но передаём явно:
        # вердикт судит bid-домены иначе, и умолчания здесь стоят слишком дорого.
        # Приобретаемость — ПЕРЕД возрастом: домен, перехваченный снайпером сразу после
        # дропа, отвечает whois'ом нового владельца (available=False, created=пару дней
        # назад) — возраст почти всегда < min_age_years, и "too_young" замаскировал бы
        # настоящую причину (домен не наш) под порог, который якобы можно ослабить в
        # /settings. Для taken возраст вообще не смотрим — не наш домен, и точка.
        v = acquirability_verdict(pr.get("available"), d.acquire_deadline, now, lane=d.lane)
        if v == "taken":
            return "not_acquirable"                 # занят, купить нельзя — возраст тут ни при чём
        elif v == "free":
            sig["lane"] = "free"                    # свободен к регистрации — тут возраст уже сигнал качества
            if age_known and age < st["min_age_years"]:
                return "too_young"
        else:
            # waiting — дроп ещё не наступил (перепробуем после даты);
            # unknown — whois ОТВЕТИЛ, но ответ не разобрали (available=None: нестандартный TLD,
            #   пустой ответ A-Parser, страница лимита — исключения при этом НЕТ, и в errors
            #   тоже ничего). И то и другое: оставить discovered.
            # Причину называем ЯВНО: панель не должна угадывать её сниффингом строк в errors —
            # там этой ветки просто не видно, и сообщение «домен занят» было бы ложью о факте,
            # которого мы не устанавливали.
            sig["acquirability_unresolved"] = True
            if v == "waiting":
                sig["unresolved_why"] = "waiting"          # занят, но дроп впереди — норма
            elif pr.get("available") is None:
                sig["unresolved_why"] = "whois_unclear"    # ответ пришёл, но НЕ разобран
            else:
                # whois РАЗОБРАН и сказал «занят», но лейн/дата дропа неизвестны (вся масса
                # cctld/витрин: discovery ставит lane только backorder'у). Занятость установлена,
                # неизвестно ДРУГОЕ — когда домен освободится. Назвать это «не разобрали» значило
                # бы послать оператора чинить несуществующую поломку парсинга A-Parser на .ru.
                sig["unresolved_why"] = "taken_undated"
            return None

    jobs.report(run, stage="risk")
    # T2 — риск (средне): РКН + Spamhaus
    try:
        sig["rkn_listed"] = c["rkn"].is_listed(d.domain)
        if sig["rkn_listed"]:
            return "rkn"
    except Exception as e:  # noqa: BLE001
        sig["errors"].append(f"rkn:{type(e).__name__}")
    try:
        sig["blacklisted"] = c["blacklist"].is_blacklisted(d.domain)
        if sig["blacklisted"] is True:
            return "blacklist"
    except Exception as e:  # noqa: BLE001
        sig["errors"].append(f"blacklist:{type(e).__name__}")
    if sig.get("blacklisted") is None and "blacklisted" in sig:
        sig["errors"].append("blacklist:unavailable")   # транзиент -> risk-guard -> manual

    ap = c["aparser"]
    if getattr(ap, "safebrowsing_failures", 0) >= _APARSER_SAFEBROWSING_LIMIT:
        sig["errors"].append("safebrowsing:circuit_open")  # предохранитель уже сработал в этом прогоне
    else:
        try:
            sig["safebrowsing_flagged"] = ap.safebrowsing_check(d.domain)
            ap.safebrowsing_failures = 0  # канал жив — счётчик сбоев сброшен
            if sig["safebrowsing_flagged"] is True:
                return "safebrowsing"
        except Exception as e:  # noqa: BLE001
            sig["errors"].append(f"safebrowsing:{type(e).__name__}")
            ap.safebrowsing_failures = getattr(ap, "safebrowsing_failures", 0) + 1
            if ap.safebrowsing_failures >= _APARSER_SAFEBROWSING_LIMIT:
                # Срабатывание — ОТДЕЛЬНЫЙ безусловный лог, ровно один раз на прогон, не на
                # каждый из оставшихся доменов (та же находка, что и у предохранителя TCI).
                logging.getLogger(__name__).warning(
                    "A-Parser SafeBrowsing: %d сбоев подряд — предохранитель сработал, "
                    "до конца прогона пропускается", _APARSER_SAFEBROWSING_LIMIT)
        if sig.get("safebrowsing_flagged") is None and "safebrowsing_flagged" in sig:
            sig["errors"].append("safebrowsing:unavailable")  # формат не распознан -> risk-guard

    jobs.report(run, stage="echo")
    # indexed_echo
    try:
        sig["indexed_echo"] = c["searxng"].indexed_echo(d.domain)
    except Exception as e:  # noqa: BLE001
        sig["errors"].append(f"searxng:{type(e).__name__}")

    jobs.report(run, stage="history")
    # T3 — история (дорого): только для приобретаемых выживших
    try:
        hist = c["wayback"].classify_history(d.domain)
        pf = hist.get("prior_flags") or {}
        sig["prior_flags"] = pf
        sig["wayback_checked"] = hist.get("wayback_checked")     # сохраняем ДО возможного выхода
        # чем именно подтверждён вердикт истории — снимки, которые реально смотрели. Кладём
        # ДО возможного выхода в history_dirty: отказ — тоже вердикт, и он тоже ошибается.
        sig["history_evidence"] = hist.get("evidence") or []
        # сколько снимков РЕАЛЬНО скачали в ЭТОМ прогоне — 0 отличает «архив честно пуст» от
        # «этот прогон вообще не оставил числа» (см. blind_reason: домены, отскоренные до
        # появления этого поля, не имеют права носить утверждение о пустом архиве).
        sig["sampled"] = hist.get("sampled")
        sig["first_seen"] = hist.get("first_seen")
        if sig.get("whois_created") is None and hist.get("age_years") is not None:
            sig["age_years"] = hist["age_years"]           # whois приоритетнее; Wayback — фолбэк
            # ...и бейдж обязан сказать об этом ПРАВДУ: гейт молодости ПРИМЕНЁН (ниже, по этому
            # самому числу), а вот занятость домена не сверял никто. Прежний текст «гейт не
            # применялся» был ложью ровно в том состоянии, где показывался (ревью Задачи 4).
            sig["age_source"] = "wayback"
        if any(pf.get(k) for k in cfg.HARD_REJECT_FLAGS):
            return "history_dirty"
    except Exception as e:  # noqa: BLE001
        sig["errors"].append(f"wayback:{type(e).__name__}")

    # непроверяемый по whois возраст всё равно проходит гейт молодости (ПОСЛЕ history_dirty)
    if not age_known and sig.get("age_years") is not None and sig["age_years"] < st["min_age_years"]:
        return "too_young"

    jobs.report(run, stage="ahrefs")
    # T3b — Ahrefs (дорого, капча за деньги): ТОЛЬКО если фид не дал RD (cctld/reg_ru/
    # sweb — у backorder RD уже есть, повторно не проверяем) и бюджет жив.
    # ahrefs_budget=None -> НЕ вызываем (в отличие от whois_budget=None=безлимит — Ahrefs
    # платный, дефолт должен быть "выключено", а не "неограниченно").
    if d.referring_domains is None and ahrefs_budget is not None and ahrefs_budget[0] > 0:
        ahrefs_budget[0] -= 1
        try:
            ah = c["aparser"].ahrefs_probe(d.domain)
            sig["dr"] = ah["dr"]
            sig["ahrefs_backlinks"] = ah["backlinks"]
            if ah["referring_domains"] is not None:
                sig["referring_domains"] = ah["referring_domains"]
        except Exception as e:  # noqa: BLE001
            sig["errors"].append(f"ahrefs:{type(e).__name__}")
    return None


def score_domain(domain_id: int, clients: dict | None = None, whois_budget=None,
                 ahrefs_budget=None, run: int | None = None) -> dict:
    """Полная воронка для одного домена. whois_budget — мутабельный [int] или None (без лимита).
    job — имя прогона в реестре (jobs.py): если задан, _funnel репортит текущую стадию."""
    from datetime import datetime, timezone
    from app.db import SessionLocal
    from app.models.domain import Domain
    from app.models.domain_score_log import DomainScoreLog
    from app.services.settings import get_settings

    st = get_settings()
    with SessionLocal() as db:
        d = db.get(Domain, domain_id)
        if d is None:
            raise ValueError(f"domain {domain_id} not found")
        if d.status not in ("discovered", "scored", "rejected"):
            return {"domain": d.domain, "status": d.status, "skipped": "status"}
        c = clients or _make_clients()
        sig: dict = {"errors": []}
        reject = _funnel(d, c, st, sig, whois_budget, ahrefs_budget, run=run)

        if sig.get("acquirability_unresolved"):
            # приобретаемость не определена (whois сбой/непонятно/бюджет) — статус НЕ трогаем,
            # домен остаётся discovered, следующий прогон перепробьёт (см. спек §D).
            #
            # Но если whois ОТВЕТИЛ по существу («занят»), факт сверки надо запомнить: иначе
            # каждый следующий прогон платит whois'ом за уже полученный ответ, а таких доменов
            # теперь МАССА — cctld везёт дедлайн, и весь реестр (~9.5 тыс.) законно ждёт своего
            # дропа в discovered. Ту же аварию уже лечили в recheck_acquirability (см. ниже).
            #
            # ОСТОРОЖНО, здесь легко ошибиться (и я ошибся — ревью 2026-07-13): штамп НЕ значит
            # «больше не спрашиваем». Он значит «спросили, знаем когда вернуться». Когда дата
            # дропа известна, ответ предсказуем по ДАТЕ, и scorable() ждёт её. Когда даты нет,
            # «занят сегодня» не говорит ничего про завтра — там кулдаун RECHECK_EVERY, а не
            # один шанс, иначе домен никогда не увидит собственного дропа.
            if sig.get("acquirability_checked_at"):
                d.acquirability_checked_at = sig["acquirability_checked_at"]
            # Дедлайн на этом пути уже переписан whois-проекцией (_funnel, T1), а
            # score_breakdown целиком здесь НЕ собирается — домен не оценён, собирать
            # нечего. Но подпись источника даты обязана дойти до панели именно отсюда:
            # домен с проекцией даёт вердикт `waiting` и остаётся discovered, то есть
            # ВСЯ популяция проекций живёт на этом пути. Без этой строки панель
            # подписывала бы проекцию «СРОК ДРОПА» (дата из фида) ровно там, где она
            # проекция и есть. Кладём ОДИН ключ, не трогая остальной breakdown.
            if sig.get("deadline_source"):
                d.score_breakdown = {**(d.score_breakdown or {}),
                                     "deadline_source": sig["deadline_source"]}
            db.add(DomainScoreLog(domain_id=d.id, run_id=run, outcome="unresolved",
                                  reject_reason=None, score=None, sig=_jsonable(sig)))
            db.commit()
            return {"domain": d.domain, "status": d.status, "unresolved": True,
                    "why": sig.get("unresolved_why"), "errors": sig.get("errors", [])}

        if reject:
            result = {"score": 0.0, "status": "rejected", "breakdown": {"funnel_reject": reject}}
        else:
            sig.setdefault("referring_domains", d.referring_domains)
            # F25: тот же приём для DR. Ahrefs зовётся ТОЛЬКО когда фид не дал RD (см. _funnel,
            # T3b) — при рескоре уже приобретённого RD-домена sig["dr"] никогда не наполняется,
            # хотя d.dr в БД уже хранит проверенное значение с прошлого прогона. Без setdefault
            # compute_score считал authority от 0.0, будто Ahrefs вообще не спрашивали — домен с
            # authority=1.0 на первом скоринге терял вес 0.12 на каждом следующем рескоре.
            # float(): `dr` — Numeric, ORM отдаёт его как Decimal при чтении СВЕЖЕЙ строки из БД
            # (`d = db.get(...)` в начале score_domain — новая сессия, не то же значение, что
            # только что вернул Ahrefs) — Decimal/float в compute_score роняет TypeError (тот же
            # приём, что уже применяет api/domains.py при отдаче `dr` наружу).
            sig.setdefault("dr", float(d.dr) if d.dr is not None else None)
            result = compute_score(sig, st.get("weights"))     # веса — рантайм, с /settings
            if "hard_reject" not in result["breakdown"]:
                # Finding 1 (2026-07 review): re-decide with the RUNTIME /settings thresholds
                # (not just cfg.DECISION) so the approve/manual-review sliders actually govern
                # the stored status, not only the /settings preview counters. Hard-rejects
                # (score 0.0) are excluded — they must never be "rescued" by a low threshold.
                result = {**result, "status": _decide(result["score"], sig,
                                                      st["approve_at"], st["manual_review_at"])}

        # СИГНАЛЫ ПИШЕМ ТОЛЬКО ИЗ ПРОВЕРОК, КОТОРЫЕ В ЭТОМ ПРОГОНЕ РЕАЛЬНО ОТРАБОТАЛИ.
        #
        # Раньше здесь стоял безусловный `d.X = sig.get("X")` — и он ОТМЫВАЛ ГРЯЗЬ (ревью
        # Задачи 6, Critical 2). Воронка выходит рано: T0 (low_rd/feed_flag) не зовёт вообще
        # ничего, T1 (too_young/not_acquirable) — только whois. РКН, блэклист и Wayback при
        # таком выходе НЕ ВЫПОЛНЯЛИСЬ, sig о них молчит — и в колонки ложился None. Домен,
        # отклонённый за РКН, после «▶ перепроверить» терял ВСЕ улики (rkn_listed=None,
        # prior_flags=None) и снова становился чистым для политики (services/transitions).
        # То есть кнопка реабилитации работала не «по новым уликам», а ПО ИХ ОТСУТСТВИЮ:
        # достаточно было поднять min_rd на /settings, чтобы перескор стёр память о РКН.
        #
        # Правило ОБЩЕЕ, а не три частных случая (dr/age_years из F25 — тот же корень):
        # ОТСУТСТВИЕ значения — это «не проверяли», и оно не имеет права затирать то, что
        # кто-то проверил. None трактуем так же: «blacklist ответил "недоступен"» не снимает
        # вчерашнее «в блэклисте». Проверка, которая ОТРАБОТАЛА и сказала «чист», кладёт False —
        # и реабилитирует домен, как и задумано (см. test_rescoring_is_the_honest_way_back).
        for col in ("lane", "whois_created", "acquirability_checked_at", "prior_flags",
                    "wayback_checked", "first_seen", "age_years", "rkn_listed", "blacklisted",
                    "indexed_echo", "dr", "referring_domains"):
            v = sig.get(col)
            if v is not None:
                setattr(d, col, v)
        # ЛЕГАСИ-КОЛОНКА. Имя врёт: это «не отклонён», а не «чистая история» — по нему нельзя
        # судить о прошлом домена (JSON-двойник так и делал, аудит F2). Чистоту истории
        # спрашивать ТОЛЬКО у history_verdict(). Колонка доживает до ближайшей миграции.
        d.clean = result["status"] != "rejected"
        d.score = result["score"]
        prev = d.score_breakdown or {}

        def _kept(key):
            """То же правило для УЛИК: снимок, который этот прогон не смотрел, не исчезает.

            Иначе `prior_flags` (мы его только что сохранили) остался бы вердиктом без единого
            подтверждения: инбокс пишет «история грязная — смотри снимки», а смотреть нечего.
            """
            v = sig.get(key)
            return v if v is not None else prev.get(key)

        d.score_breakdown = {**result["breakdown"], "errors": sig.get("errors", []),
                             "ahrefs_backlinks": _kept("ahrefs_backlinks"),
                             "history_evidence": _kept("history_evidence") or [],
                             "sampled": _kept("sampled"),
                             # 'whois' | 'wayback' | None — по нему blind_reason выбирает,
                             # ЧТО именно осталось непроверенным (возраст или только занятость)
                             "age_source": _kept("age_source"),
                             # 'tci' | 'aparser' | 'aparser_fallback' | None — ЧЕМ судили
                             # приобретаемость в этом прогоне (ревью Задачи 1, 2026-07-19):
                             # раньше whois_source терялся в sig и до оператора не доходил.
                             "whois_source": _kept("whois_source"),
                             # 'whois_projection' | None(='feed') — ЧЕМ является текущий
                             # acquire_deadline (MINOR 4, ревью 2026-07-20): проекцией TCI
                             # («освободится, если не продлят») или авторитетной датой из
                             # фида (backorder delete_date / cctld архив). _kept — тот же
                             # приём, что у whois_source: этот прогон мог не трогать
                             # deadline вовсе (whois не звался/bid/дедлайн ещё свежий), а
                             # прошлая классификация не должна теряться молча.
                             "deadline_source": _kept("deadline_source")}
        d.status = result["status"]
        d.reject_reason = reject or ("low_score" if result["status"] == "rejected" else None)
        # F24: когда домен ПОСЛЕДНИЙ РАЗ прошёл воронку до решения — до этой колонки узнать было
        # неоткуда (discovered_at — это discovery, не скоринг; score_breakdown молчит про время).
        # Ставим только здесь: unresolved-возврат выше (whois не разобрал/бюджет исчерпан)
        # оставляет домен discovered — воронка НЕ дошла до решения, значит и не "оценила" его.
        d.scored_at = datetime.now(timezone.utc)
        db.add(DomainScoreLog(
            domain_id=d.id, run_id=run,
            outcome="rejected" if result["status"] == "rejected" else "scored",
            reject_reason=d.reject_reason, score=result["score"], sig=_jsonable(sig)))
        db.commit()
        return {"domain": d.domain, **result, "reject_reason": d.reject_reason,
                "errors": sig.get("errors", [])}


def score_pending(limit: int = 100) -> int:
    """Скорит `discovered` домены. Прогресс и стадии — через jobs.track (см. services/jobs.py).
    Между доменами смотрит стоп-кнопку: «Проверить весь пул» — это часы работы и квоты
    A-Parser, прервать это должно быть можно без рестарта контейнера.

    Возвращает СКОЛЬКО РЕАЛЬНО ПРОШЛО воронку: при отмене — частичное число, не len(rows).
    Оркестратор пишет это в counts свипа — врать ему нельзя."""
    from datetime import datetime, timezone
    from sqlalchemy import select, func, case, and_
    from app.db import SessionLocal
    from app.models.domain import Domain
    from app.services import jobs
    from app.services.settings import get_settings

    st = get_settings()
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        # ЯРУС СРОЧНОСТИ — первым ключом, не RD и не голая дата.
        #
        # RD есть только у backorder; у cctld/витрин он NULL — значит по RD домен, дропающийся
        # СЕГОДНЯ, лёг бы вперемешку с кулдаун-пулом, и при n=5 пул вытеснял бы его НИКОГДА не
        # доскоренным. Но и голая дата ASC неверна: «самая ранняя» — это ПРОТУХШИЙ дедлайн
        # месячной давности, то есть дроп, который мы уже упустили. Он встал бы впереди
        # сегодняшнего и жёг бы полный дорогой путь (whois+РКН+Wayback ≈ 60 с) на покойника —
        # для lane='bid' воронка его даже не отбракует (T1 короткозамкнут лейном).
        expired = and_(Domain.acquire_deadline.is_not(None),
                       Domain.acquire_deadline < now - DROP_GRACE)
        tier = case((Domain.acquire_deadline.is_(None), 2),   # дата неизвестна — кулдаун-пул
                    (expired, 1),                             # окно дропа закрыто — уже упустили
                    else_=0)                                  # окно открыто/впереди — вот они и важны
        rows = db.execute(
            select(Domain.id, Domain.domain).where(Domain.status == "discovered", scorable(now))
            .order_by(tier,
                      Domain.acquire_deadline.asc(),          # внутри яруса — ближайший дроп первым
                      Domain.referring_domains.desc().nulls_last())   # равных по сроку разводит RD
            .limit(limit)
        ).all()
        # ПОЧЕМУ пусто — теперь это ШТАТНОЕ состояние: после scorable() домены, чей дроп ещё
        # впереди, законно ждут своей даты. «Прогнано 0 доменов» без объяснения — ровно та
        # немота, из-за которой оператор решил, что перепроверка сломана. Считаем причину
        # ЗДЕСЬ, пока сессия открыта.
        idle_msg = None
        if not rows:
            # Считаем РАЗДЕЛЬНО: «ждут дропа» (дата известна, она в будущем) и «дата неизвестна»
            # (кулдаун). Свалить их в одно число значило бы обещать дроп там, где о нём никто
            # ничего не знает.
            waiting = db.scalar(select(func.count()).select_from(Domain)
                                .where(Domain.status == "discovered",
                                       Domain.acquire_deadline > now)) or 0
            undated = db.scalar(select(func.count()).select_from(Domain)
                                .where(Domain.status == "discovered",
                                       Domain.acquire_deadline.is_(None))) or 0
            nearest = db.scalar(select(func.min(Domain.acquire_deadline))
                                .where(Domain.status == "discovered",
                                       Domain.acquire_deadline > now))
            if not (waiting or undated):
                idle_msg = "оценивать нечего: найденных доменов нет — сначала «Найти дропы»"
            else:
                parts = []
                if waiting:
                    parts.append(f"{waiting} ждут своего дропа"
                                 + (f" (ближайший — {nearest:%d.%m})" if nearest else ""))
                if undated:
                    parts.append(f"{undated} без даты дропа — вернусь к ним в течение суток")
                idle_msg = "оценивать нечего: " + ", ".join(parts)
    stages = [dict(s) for s in FUNNEL_STAGES]
    if int(st["max_ahrefs_per_run"]) == 0:
        stages[-1]["state"] = "skip"           # платная стадия выключена — так и покажем
    clients = _make_clients()
    whois_budget = [int(st["max_whois_per_run"])]
    ahrefs_budget = [int(st["max_ahrefs_per_run"])]
    total, done = len(rows), 0
    with jobs.track("score", stages=stages) as run:
        for i, (did, name) in enumerate(rows, 1):
            # ПОРЯДОК ВАЖЕН: репорт ДО проверки стопа. done = i-1 — это ровно столько доменов,
            # сколько уже дошли до конца. Проверь стоп раньше репорта — и в реестре останется
            # done от прошлой итерации, то есть «остановлена на 0 / 5» после одного домена.
            jobs.report(run, done=i - 1, total=total, current=name)
            if jobs.cancelled(run):
                raise jobs.Cancelled()
            try:
                score_domain(did, clients, whois_budget, ahrefs_budget, run=run)
            except Exception:  # noqa: BLE001 — падение одного домена не топит батч
                logging.getLogger(__name__).exception("score_domain %s упал", name)
            done = i
        jobs.report(run, done=total, total=total, current="",
                    message=idle_msg or f"прогнано {total} доменов через воронку")
    return done


# статусы, где домен — ЕЩЁ НАШ КАНДИДАТ на покупку и им не владеет другая машина.
# purchasing/purchased НЕ трогаем: там живой заказ, его статусом управляет M2 (иначе
# перепроверка отбраковала бы домен из-под оформленного выкупа).
_RECHECK_STATUSES = ("approved", "scored")


def stale_donors(days: int = 3, db=None) -> int:
    """Сколько отобранных доноров давно (или ни разу) не сверялись с whois. Для подписи кнопки.

    `db` — уже открытая сессия (панель отдаёт свою из DI, чтобы не плодить соединение
    на каждый рендер /domains)."""
    from datetime import datetime, timezone
    from sqlalchemy import select, func, or_
    from app.db import SessionLocal
    from app.models.domain import Domain

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    stmt = select(func.count(Domain.id)).where(
        Domain.status.in_(_RECHECK_STATUSES),
        or_(Domain.acquirability_checked_at.is_(None),
            Domain.acquirability_checked_at < cutoff))
    if db is not None:
        return db.execute(stmt).scalar_one()
    with SessionLocal() as s:
        return s.execute(stmt).scalar_one()


def recheck_acquirability(limit: int = 200) -> dict:
    """Перепроверить whois'ом отобранных доноров: не выкупил ли их кто-то за это время.

    ЗАЧЕМ. Скоринг решает приобретаемость ОДИН раз (T1) и больше к ней не возвращается.
    Но список доноров протухает: домен, одобренный неделю назад, сегодня может быть уже
    зарегистрирован другим — а мы держим его как «готов к выкупу» и однажды поставим на него
    ставку впустую. Отдельного прохода для этого не было; это он.

    Занятый (и ждать нечего) -> rejected/not_acquirable. Свободный / ещё не дропнувшийся —
    остаётся кандидатом, только помечается свежепроверенным. Не определилось (whois молчит,
    сбой) -> НЕ трогаем ни статус, ни отметку: домен остаётся протухшим и попадёт в следующий
    прогон. Денег не тратит, гейтов не касается.

    Бюджет — `max_whois_per_run` с /settings, СВОЙ на прогон (не общий со скорингом: джобы
    single-flight по имени, поэтому Score и Перепроверка могут идти одновременно и взять по
    капу каждый — суммарно до 2× квоты A-Parser). Самые протухшие проверяются первыми.

    Прогресс — сам, через jobs.track: сводка («ЗАНЯТЫ 3») переехала сюда из panel.py и живёт
    в job_run.message, а датирует её job_run.finished_at — штамп времени руками больше не нужен.
    """
    from datetime import datetime, timezone
    from sqlalchemy import select, update
    from app.db import SessionLocal
    from app.models.domain import Domain
    from app.services import jobs
    from app.services.settings import get_settings

    # checked == сколько whois-вызовов реально сделали == расход бюджета. Обычно он же = сумма
    # free+waiting+taken+unknown; расходится ровно на домены, которые между whois и записью
    # успели уйти в выкуп (см. декремент taken по rowcount ниже) — их отбраковки не было.
    out = {"checked": 0, "free": 0, "waiting": 0, "taken": 0, "unknown": 0}
    with jobs.track("recheck", stages=[{"key": "whois", "label": "whois по донорам"}]) as run:
        jobs.report(run, stage="whois")
        budget = int(get_settings()["max_whois_per_run"])
        if budget <= 0:
            # ВНУТРИ track, а не до него: иначе прогон завершался бы, не создав строки реестра,
            # и кнопка «Перепроверить» выглядела бы сломанной — ровно та болезнь, которую лечим.
            jobs.report(run, message="whois-бюджет = 0, проверять нечем (см. /settings)")
            return out
        with SessionLocal() as db:
            ids = db.execute(
                select(Domain.id).where(Domain.status.in_(_RECHECK_STATUSES))
                # протухшие первыми; id — вторичный ключ, иначе порядок внутри NULL-корзины
                # не определён и прогоны могут топтаться по одним и тем же доменам
                .order_by(Domain.acquirability_checked_at.asc().nulls_first(), Domain.id.asc())
                .limit(min(limit, budget))
            ).scalars().all()

        c = _make_clients()
        total = len(ids)
        for i, did in enumerate(ids, 1):
            jobs.report(run, done=i - 1, total=total)         # ДО стопа — см. score_pending
            if jobs.cancelled(run):
                raise jobs.Cancelled()
            with SessionLocal() as db:
                d = db.get(Domain, did)
                if d is None or d.status not in _RECHECK_STATUSES:
                    continue                      # статус увели, пока шли (напр. в выкуп)
                name, deadline, lane = d.domain, d.acquire_deadline, d.lane
            jobs.report(run, current=name)        # репорт ДО вызова: whois идёт секунды

            now = datetime.now(timezone.utc)
            out["checked"] += 1                           # вызов состоялся — бюджет потрачен
            try:
                pr = whois_router.probe(name, c)
            except Exception:  # noqa: BLE001 — падение одного домена не топит батч
                logging.getLogger(__name__).exception("whois-перепроверка %s упала", name)
                out["unknown"] += 1
                continue        # СБОЙ (сеть/A-Parser) — транзиентен. Отметку не ставим: вернёмся.

            # lane обязателен: для bid-домена «занят» — НОРМА (ждёт своего дропа), и без него
            # вердикт отбраковал бы живой дроп.
            v = acquirability_verdict(pr.get("available"), deadline, now, lane=lane)
            out[v] += 1
            if v == "unknown" and pr.get("available") is None:
                continue        # whois ОТВЕТИЛ, но невнятно. Не штампуем — пробуем ещё раз позже.
            # Прочий unknown (bid без дедлайна) whois ОТВЕТИЛ по существу: «занят». Судить не по
            # чему, но ответ ДЕТЕРМИНИРОВАННЫЙ — завтра будет ровно тот же. Такой домен обязан
            # получить отметку, иначе он вечно висит в голове nulls_first-очереди и выедает весь
            # бюджет: если таких доменов больше бюджета (а это ровно авария «фид сменил формат
            # delete_date»), перепроверка никогда не дойдёт до остального списка и молча выродится
            # в no-op. Статус не трогаем — домен остаётся кандидатом; счётчик unknown в сводке
            # покажет оператору, что что-то не так.

            # Атомарно и только из «наших» статусов: между whois-раундтрипом и записью человек
            # мог отправить домен в выкуп (create_order -> purchasing). Голый UPDATE перезатёр бы
            # его нашим rejected и разъехался с живым заказом; rowcount==0 = домен уже не наш.
            with SessionLocal() as db:
                vals = {"acquirability_checked_at": now}
                if v == "taken":
                    vals |= {"status": "rejected", "reject_reason": "not_acquirable"}
                res = db.execute(update(Domain)
                                 .where(Domain.id == did, Domain.status.in_(_RECHECK_STATUSES))
                                 .values(**vals))
                db.commit()
            if v == "taken" and res.rowcount == 0:
                out["taken"] -= 1     # домен успели увести в выкуп — отбраковки НЕ было, не врём

        # Пустой прогон обязан ОБЪЯСНИТЬСЯ. Перепроверка судит только УЖЕ оценённых доноров
        # (scored/approved); пока инбокс пуст, ей нечего делать, и она честно завершается за
        # ~40 мс. Со сводкой «проверено 0: свободны 0, ЗАНЯТЫ 0...» это неотличимо от сломанной
        # кнопки — ровно так оператор и решил, что перепроверка не работает (дебаг 2026-07-13).
        msg = (f"проверено {out['checked']}: свободны {out['free']}, "
               f"ждут дропа {out['waiting']}, ЗАНЯТЫ {out['taken']} (отбракованы), "
               f"не определилось {out['unknown']}") if total else (
            "проверять нечего: нет доменов «на решении» или «одобрен». "
            "Сначала оцени найденные — кнопка «Оценить домены»")
        jobs.report(run, done=total, total=total, current="", message=msg)
    return out


# ============================================================================
# Волновая архитектура (2026-07-20): дёшево->дорого волнами на ВЕСЬ пул, а не
# по-доменно. См. docs/superpowers/specs/2026-07-20-scoring-wave-architecture-design.md.
# ============================================================================

# Границы конкурентности — ХАРДКОД, не /settings (решение пользователя): "если домен
# занят whois — скипаем в этой волне" — сами лимиты волн оператор не крутит.
# history=4 — вежливость к archive.org (проектная ценность, не число для тюнинга).
# ahrefs=2 — капча за штуку, дорого и хрупко к нагрузке.
_CONCURRENCY = {"whois": 12, "risk": 12, "history": 4, "ahrefs": 2}


@dataclass
class FunnelState:
    """Домен на пути через волны — лёгкий снэпшот, НЕ ORM-объект: волны держат его в
    памяти между несколькими вызовами без открытой сессии/транзакции. Финализация
    (commit в БД) происходит ОТДЕЛЬНО, в момент выхода из конвейера (см. _commit_result)."""
    domain_id: int
    domain: str
    lane: str | None
    referring_domains: int | None
    acquire_deadline: "datetime | None"
    feed_flags: dict | None
    sig: dict = field(default_factory=lambda: {"errors": []})
    reject_reason: str | None = None
    unresolved_why: str | None = None
    alive: bool = True


class Budget:
    """Потокобезопасный счётчик бюджета — замена сегодняшнему [int] (безопасен только
    при последовательном доступе). N потоков волны конкурентно зовут .take(); ровно N
    успешных, если бюджет == N — не больше (голый `box[0] -= 1` под конкурентностью мог
    бы пропустить декремент из-за гонки read-modify-write)."""
    def __init__(self, n: int):
        self._n = n
        self._lock = threading.Lock()

    def take(self) -> bool:
        with self._lock:
            if self._n <= 0:
                return False
            self._n -= 1
            return True


class _ListBudget:
    """Адаптер легаси-контракта score_domain() ([int]-список) под протокол Budget.take().
    Мутирует ТОТ ЖЕ список (не копию) — вызывающий код (тесты, ручной вызов из панели)
    видит расход бюджета в своём списке, как и раньше."""
    def __init__(self, box: list):
        self._box = box

    def take(self) -> bool:
        if self._box[0] <= 0:
            return False
        self._box[0] -= 1
        return True


def _run_concurrent(states: list, workers: int, run: "int | None", stage: str, fn) -> None:
    """Гоняет fn(state) на всех ALIVE states пулом `workers` потоков. fn мутирует state
    IN PLACE (sig/reject_reason/unresolved_why/alive) и НЕ касается БД — коммит только
    в _checkpoint, после того как волна целиком завершилась.

    Прогресс: stage репортится ОДИН раз в начале (флип чипа волны в реестре) — done/total
    без stage= на каждом тике (report() с stage= делает лишний SELECT stages на КАЖДЫЙ
    вызов, см. jobs.py:330-334; сотни тиков волны не должны множить это на сотни
    SELECT'ов). Отмена проверяется после КАЖДОГО завершения — как и в сегодняшнем
    последовательном score_pending (по одной проверке на домен), просто теперь через
    as_completed вместо for-цикла.
    """
    from app.services import jobs
    alive = [s for s in states if s.alive]
    if not alive:
        return
    jobs.report(run, stage=stage, done=0, total=len(alive))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(fn, s): s for s in alive}
        done = 0
        for fut in as_completed(futures):
            s = futures[fut]
            try:
                fut.result()
            except Exception:  # noqa: BLE001 — сбой одного домена не топит волну
                logging.getLogger(__name__).exception("%s упал для %s", stage, s.domain)
            done += 1
            jobs.report(run, done=done, total=len(alive))
            if jobs.cancelled(run):
                ex.shutdown(wait=False, cancel_futures=True)
                raise jobs.Cancelled()


def _wave_t0(states: list, st: dict) -> None:
    """T0 — фид, без сети, мгновенно. Без пула: I/O нет, конкурентность не нужна."""
    for s in states:
        if not s.alive:
            continue
        if s.feed_flags and any(s.feed_flags.get(k) for k in ("rkn", "judicial", "block")):
            s.reject_reason = "feed_flag"
            s.alive = False
        elif (s.referring_domains is not None
              and s.referring_domains < st["min_referring_domains"]):
            s.reject_reason = "low_rd"
            s.alive = False


def _whois_one(s: FunnelState, clients: dict, budget, st: dict) -> None:
    """Тело T1 для ОДНОГО домена — вызывается конкурентно из _wave_whois. Прямой перенос
    сегодняшнего _funnel T1 (scoring.py, было строки 478-570), без изменения логики: budget
    вместо мутируемого [int], state вместо ORM Domain + sig."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)

    if budget is not None and not budget.take():
        s.unresolved_why = "budget"
        s.alive = False
        return

    age_known = False
    age = None
    try:
        pr = whois_router.probe(s.domain, clients)
    except Exception as e:  # noqa: BLE001
        s.sig["errors"].append(f"whois:{type(e).__name__}")
        if s.lane != "bid":
            s.unresolved_why = "whois_failed"
            s.alive = False
            return
        pr = {"available": None, "created": None}

    if pr.get("available") is not None:
        s.sig["acquirability_checked_at"] = now
    s.sig["whois_source"] = pr.get("whois_source")

    prev_deadline = s.acquire_deadline
    s.acquire_deadline = _deadline_from_whois(s.acquire_deadline, pr.get("free_date"), now, s.lane)
    if s.acquire_deadline != prev_deadline:
        s.sig["deadline_source"] = "whois_projection"

    wc = pr.get("created")
    s.sig["whois_created"] = wc
    if wc is not None:
        age_known = True
        age = (now - wc).days / 365.25
        s.sig["age_years"] = round(age, 2)
        s.sig["age_source"] = "whois"

    if s.lane == "bid":
        s.sig["lane"] = "bid"
        if age_known and age < st["min_age_years"]:
            s.reject_reason = "too_young"
            s.alive = False
        return

    v = acquirability_verdict(pr.get("available"), s.acquire_deadline, now, lane=s.lane)
    if v == "taken":
        s.reject_reason = "not_acquirable"
        s.alive = False
    elif v == "free":
        s.sig["lane"] = "free"
        if age_known and age < st["min_age_years"]:
            s.reject_reason = "too_young"
            s.alive = False
    else:
        s.unresolved_why = ("waiting" if v == "waiting"
                            else "whois_unclear" if pr.get("available") is None
                            else "taken_undated")
        s.alive = False


def _wave_whois(states: list, clients: dict, budget, st: dict, run) -> None:
    """T1 — приобретаемость + возраст, конкурентно на весь выживший после T0 пул."""
    _run_concurrent(states, _CONCURRENCY["whois"], run, "whois",
                    lambda s: _whois_one(s, clients, budget, st))


if __name__ == "__main__":  # pure-function self-check (no I/O)
    # clean old domain -> manual review at least
    clean = compute_score({"wayback_checked": True, "prior_flags": {},
                           "dr": 4.0, "age_years": 10, "referring_domains": 30,
                           "indexed_echo": True})
    assert clean["status"] in ("approved", "scored"), clean
    # casino history -> hard reject
    dirty = compute_score({"wayback_checked": True, "prior_flags": {"casino": True},
                           "dr": 9.0, "age_years": 15, "referring_domains": 500})
    assert dirty["status"] == "rejected" and dirty["score"] == 0.0, dirty
    # RKN -> hard reject regardless of quality
    rkn = compute_score({"rkn_listed": True, "dr": 6, "age_years": 12,
                         "referring_domains": 200, "wayback_checked": True, "prior_flags": {}})
    assert rkn["status"] == "rejected", rkn
    # empty/unknown -> low score, rejected
    empty = compute_score({})
    assert empty["status"] == "rejected", empty
    # INVARIANT: unverified history never auto-approves, even with huge RD
    unverified = compute_score({"referring_domains": 5000, "wayback_checked": False,
                                "prior_flags": {}})
    assert unverified["status"] != "approved", unverified
    # weights sum to 1.0
    assert abs(sum(cfg.WEIGHTS.values()) - 1.0) < 1e-9
    print("scoring compute_score ok:", clean["score"], dirty["score"], rkn["score"], empty["score"])
