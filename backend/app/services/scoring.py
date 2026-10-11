"""M1b — Domain/donor scoring. Implements the funnel in docs/DONORS.md on the FREE stack.

Order: t0 (зоны/бренды) -> avail (RDAP/whois) -> risk (Web Risk, Spamhaus с DQS) -> links (Ahrefs
batch) -> history (Wayback + тема) -> deep (анкоры финалистов) -> composite score + breakdown ->
status scored | rejected (`approved` ставит только человек).
`compute_score` is pure (unit-tested below); `score_domain` does the I/O + DB write.
"""
import logging
import math
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from types import SimpleNamespace
from datetime import datetime, timedelta

from app.services import scoring_config as cfg
from app.services import whois as whois_router

# Запас после дедлайна дропа, прежде чем считать домен потерянным навсегда.
#
# Дата дропа у DropCatch — ДАТА без времени (`Drop Date`), discovery кладёт её как 00:00 UTC дня
# дропа. Значит уже в 00:01 того же дня условие «дедлайн в будущем» ложно — а домен ещё
# зарегистрирован: реестр освобождает его в течение дня. Без этого запаса перепроверка отбраковывала бы дроп РОВНО В ТОТ ДЕНЬ, когда
# его можно ловить, то есть выбрасывала бы самые ценные домены. Запас покрывает и полуночное
# усечение даты, и сдвиг релиза в реестре на сутки.
DROP_GRACE = timedelta(days=2)

# Как часто перепробовать домен, у которого дедлайна НЕТ (registry.mx, ручной список и EMD даты
# дропа не несут).
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


# Чипы волн в панели: ключ -> подпись. Порядок = порядок волн в _run_waves (таблица `waves`).
FUNNEL_STAGES = [
    {"key": "t0", "label": "зона и бренд"},
    {"key": "avail", "label": "не занят ли"},
    {"key": "risk", "label": "чёрные списки"},
    {"key": "probe", "label": "возраст"},
    {"key": "links", "label": "ссылки"},
    {"key": "history", "label": "история и тема"},
    {"key": "deep", "label": "спам-ссылки"},
]

# Проверки, чей отказ означает «домен судили ВСЛЕПУЮ». Авто-одобрения нет (Р2): любой домен
# уходит в scored, то есть В ИНБОКС К ЧЕЛОВЕКУ, и без пометки там неотличим от честно
# проверенного. Человек штампует непроверенное, думая, что машина посмотрела историю, — поэтому
# пометка «вслепую» ещё и исключает домен из пакета (bulk_ok).
#
# ИСТОРИИ ЗДЕСЬ НЕТ НАМЕРЕННО: её вердикт считает history_verdict (см. ниже). Раньше она жила
# тут ключом "wayback" — и это был баг (аудит, F2): «вслепую» выводилось из ФАКТА ОШИБКИ, а
# Wayback, не нашедший ни одного снимка, ошибки не бросает (`wayback_checked=False`, errors
# пуст). Домен, чью историю никто не смотрел, приезжал в инбокс с подписью «история чистая».
_BLIND_RU = {
    # RDAP/whois (W2) даёт ДВЕ вещи: занятость (`available` -> лейн, `not_acquirable`) и дату
    # регистрации — вход возраста для гейта `too_young` (W5). Молчащий источник не ослаблял их,
    # он снимал их целиком: без даты сравнивать не с чем, отказа нет, и домен ехал дальше «как
    # проверенный» (аудит F6). Эта формулировка — для случая, когда возраста НЕ дал никто.
    "whois": "НЕ проверено, свободен ли домен и сколько ему лет: RDAP/whois не ответил",
    # префикс `age:unverified` (_history_one): Wayback не ответил, а по RDAP/whois домен моложе порога
    "age": "возраст НЕ проверен: архив не ответил, а по RDAP домен моложе порога",
    "ahrefs": "ссылки НЕ проверены: Ahrefs не ответил",
    "webrisk": "риск НЕ проверен: Web Risk не настроен или не ответил",
    "blacklist": "чёрные списки НЕ проверены",
}

# whois упал, но возраст всё-таки известен — из Wayback (`age_source='wayback'`, волна истории).
# Прежний текст здесь ЛГАЛ ровно в том состоянии, где показывался: он утверждал, что гейт
# «слишком молодой» не применялся, — а он применялся (W5 сравнивает возраст по архиву с
# min_age_years). Правда в другом: возраст по архиву — это НИЖНЯЯ оценка (первый снимок не
# раньше регистрации), а вот ЗАНЯТОСТЬ домена не сверял никто.
_BLIND_WHOIS_ARCHIVE_AGE = ("возраст взят из архива (whois не ответил) — "
                            "свободен ли домен, НЕ проверено")

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


def emd_newreg(d) -> bool:
    """EMD без единого прочитанного снимка (находка R2-14): у новорега пустой архив — норма, а не
    «история НЕ проверена». Пометка «⚠ … ▶ Проверить» звала бы перепроверять то, чего нет; пакет
    EMD всё равно не берёт (балла нет). Снимки есть, но прочитано мало (`history_evidence`), или
    Wayback упал (`wayback:` в errors) — не сюда: тогда история и правда не проверена."""
    bd = d.score_breakdown or {}
    errors = [str(e) for e in (bd.get("errors") or [])]
    return (bool(bd.get("emd")) and bd.get("sampled") == 0 and not bd.get("history_evidence")
            and not any(e.startswith("wayback:") for e in errors))


def blind_reason(d) -> str | None:
    """Домен оценён при недоступной/несостоявшейся проверке — в пакет одобрения он не идёт.

    История идёт первой строкой: она — главный инвариант проекта («домены берём за чистую
    историю»), и именно она молча выдавала непроверенное за чистое.
    """
    errors = [str(e) for e in ((d.score_breakdown or {}).get("errors") or [])]
    # EMD-новорег с пустым архивом (R2-14): «история НЕ проверена» тут не тревога, а норма — строка
    # инбокса скажет «архив пуст — новорег» (emd_newreg); остальные проверки ниже идут как обычно.
    if history_verdict(d) == "unknown" and not emd_newreg(d):
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
        return "история НЕ проверена: снимков в базе нет — проверь заново"
    for e in errors:
        head = e.split(":", 1)[0]
        if head == "whois" and (d.score_breakdown or {}).get("age_source") == "wayback":
            return _BLIND_WHOIS_ARCHIVE_AGE
        if head in _BLIND_RU:
            return _BLIND_RU[head]
    # Возраста не дал никто: ни RDAP/whois (`whois_created`), ни архив (`first_seen`/`age_years`)
    # — гейт «слишком молодой» не применялся ни разу. Раньше это держал гард _decide; авто-
    # одобрения больше нет (Р2), и единственная защита — пакет такой домен не берёт.
    # У EMD возраст не критерий: новорег, гейт «слишком молодой» W5 его не судит (R2-14).
    if (not (d.score_breakdown or {}).get("emd") and d.whois_created is None
            and d.first_seen is None and d.age_years is None):
        return "возраст НЕ проверен: возраста нет ни из RDAP/whois, ни из архива"
    # Анкоры финалистов (W6) не проверены: кап W6, пол остатка units, Ahrefs не ответил, пустой
    # ответ при живых донорах — или домен оценён до W6 (ключа нет). На дропах RD раздут спамом:
    # без анкоров одобрять пакетом нельзя (Р2 — гард переехал сюда из _decide). У EMD ссылок нет.
    bd = d.score_breakdown or {}
    if not bd.get("emd") and bd.get("deep_checked") is not True:
        return "спам-ссылки НЕ проверены: лимит Ahrefs, нет ответа или неизвестен язык сайта"
    return None


def topic_far(d) -> bool:
    """Прошлая тема далека от VPN (инвариант 4; политика Google «expired domain abuse» — это ровно
    смена темы). Такой домен человек одобряет только руками, глядя на тему: пакет его не берёт.
    None («тема не определена») не исключает: незнание — не улика (спека §3.1)."""
    tr = d.topical_relevance
    return tr is not None and float(tr) < cfg.TOPIC_FAR_BELOW


def history_note(d) -> str | None:
    """Информационная пометка о СВЕЖЕСТИ вердикта истории. НЕ блокирует — и это осознанно.

    Побочный эффект правила «не затирать проверенное» (аудит F9/C2): домен, чью историю
    проверили РАНЬШЕ, а сегодня Wayback не ответил, сохраняет `wayback_checked=True` и
    вердикт `clean` — про сегодняшний отказ архива не говорил НИКТО (ошибка живёт в
    `score_breakdown.errors`, куда куратор не смотрит).

    Почему НЕ блокирует (и почему эта пометка живёт рядом с `blind_reason`, а не внутри него):
    вердикт опирается на РЕАЛЬНЫЕ прошлые улики, они сохранены и показываются строкой ниже, а
    сама машина не одобряет ничего (Р2) — решает человек, видя эту пометку. Запирать домен от
    пакетного одобрения из-за ТРАНЗИЕНТНОГО сбоя архива значило
    бы завести ровно ту тихую ловушку, от которой ветка избавлялась: домены, помеченные сетевым
    чихом, копятся навсегда и никем не разбираются.
    """
    if history_verdict(d) == "unknown":
        return None                    # там своё слово скажет blind_reason — не дублируем
    errors = [str(e) for e in ((d.score_breakdown or {}).get("errors") or [])]
    if any(e.startswith("wayback:") for e in errors):
        return "вердикт — из прошлой проверки: сегодня Wayback не ответил"
    return None


def list_hits(d) -> list:
    """Категории списков чистоты (UT1/blocklistproject), в которых числится домен
    (`score_breakdown.list_hits`, пишет волна risk). Пусто — не числится ЛИБО списки не проверялись."""
    return list((d.score_breakdown or {}).get("list_hits") or [])


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

    v2 (Р2 — авто-одобрения нет): всё, что раньше держали гарды `_decide`, держит этот предикат.
    Плюс два основания «только руками»: прошлая тема далека от VPN (`topic_far`) и пустой балл
    (EMD — решение за человеком; SQL пакета `score >= x` его тоже не берёт).
    """
    from app.services.transitions import dirty_reason   # ленивый: transitions зовёт нас в ответ
    # Попадание в список чистоты (мягкий сигнал, жёсткий отказ выключен) — «нет авто-одобрения при
    # ненулевом попадании»: пакет такой домен не берёт, решение за человеком, который видит категорию.
    return (history_verdict(d) == "clean" and not blind_reason(d) and not dirty_reason(d)
            and not topic_far(d) and not list_hits(d) and d.score is not None)


def _decide(score: float, sig: dict, manual_review_at: float) -> str:
    """Pure: score -> 'scored' | 'rejected'. АВТО-ОДОБРЕНИЯ НЕТ (решение оператора Р2,
    инвариант 9 мастер-спеки): `approved` ставит только человек — кнопкой или пакетом.

    Гарды, которые раньше жили здесь (Wayback не проверен, whois/РКН/блэклист упали, возраст
    неизвестен), переехали в bulk_ok через blind_reason: домен с такой дырой доезжает до
    инбокса как `scored` с пометкой «оценён вслепую», и пакет его не берёт. `sig` в сигнатуре
    оставлен: compute_score и _commit_result решают одним вызовом."""
    return "scored" if score >= manual_review_at else "rejected"


def compute_score(sig: dict, weights: dict | None = None) -> dict:
    """Pure: signals -> {score, status, breakdown}. No I/O. v2 — docs/v2/02-…-spec.md §3.2.

    `weights` — рантайм-веса с /settings (None -> scoring_config.WEIGHTS); нормируем на сумму,
    чтобы шкала 0..1 и пороги не «плыли» от сдвига одного ползунка. Сигнал, которого нет
    (W5/W6 не дошли или не смогли), — нейтральные 0.5, а не 0: «не знаем» не равно «плохо».
    Статус — только `scored`/`rejected` (_decide): одобряет человек, а домен с непроверенным
    сигналом пакет не возьмёт (bulk_ok/blind_reason).
    """
    pf = sig.get("prior_flags") or {}
    reasons = []
    if sig.get("blacklisted") is True:
        reasons.append("blacklisted")
    if sig.get("webrisk_threats"):
        reasons.append("webrisk")
    if sig.get("trademark_risk"):
        reasons.append("trademark")
    reasons += [f"prior_{c}" for c in cfg.HARD_REJECT_FLAGS if pf.get(c)]
    if reasons:
        return {"score": 0.0, "status": "rejected", "breakdown": {"hard_reject": reasons}}

    n = cfg.NORM

    def _log(v, full):
        return _clamp(math.log10((v or 0) + 1) / math.log10(full + 1))

    rd = sig.get("referring_domains") or 0
    subnets = sig.get("ref_subnets")
    pbn = subnets is not None and rd >= cfg.PBN_MIN_RD and subnets / rd < cfg.PBN_SUBNET_RATIO
    tr, spam, peak = sig.get("topical_relevance"), sig.get("spam_anchor_ratio"), sig.get("peak_traffic")
    # authority: ранг из domain_ranks (Common Crawl) вместо Ahrefs DR; DR — запасной, пока рангов нет.
    # Нет НИ ТОГО НИ ДРУГОГО — «нет данных» = нейтральные 0.5, а не ноль (ноль означал бы «плохо»).
    if sig.get("rank_authority") is not None:
        auth, auth_src = _clamp(float(sig["rank_authority"])), "rank"
    elif sig.get("dr") is not None:
        auth, auth_src = _clamp(float(sig["dr"]) / n["DR_FULL"]), "dr"
    else:
        auth, auth_src = 0.5, None
    comp = {
        "history_cleanliness": 1.0 if sig.get("wayback_checked") else 0.5,
        "topical_fit": _clamp(float(tr)) if tr is not None else 0.5,
        "age": _clamp((sig.get("age_years") or 0.0) / n["AGE_FULL"]),
        "rd": _log(rd, n["RD_FULL"]) * (0.5 if pbn else 1.0),
        "authority": auth,
        "anchor_quality": 1.0 - _clamp(float(spam)) if spam is not None else 0.5,
        "traffic_history": _log(peak, n["TRAFFIC_FULL"]) if peak is not None else 0.5,
    }
    w = {k: float(v) for k, v in (weights or cfg.WEIGHTS).items() if k in comp}
    norm = sum(w.values()) or 1.0
    score = round(_clamp(sum(w[k] * comp[k] for k in w) / norm), 4)
    status = _decide(score, sig, cfg.DECISION["manual_review_at"])
    return {"score": score, "status": status,
            "breakdown": {"components": comp, "weights": w, "pbn_suspect": pbn, "authority_source": auth_src}}


def _make_clients() -> dict:
    """Собрать интеграционные клиенты один раз на прогон (переиспользуются между доменами).
    Локи — для предохранителей под конкурентностью волн (services/whois.py): счётчики сбоев
    живут на инстансах клиентов и меняются из 12 потоков волны."""
    from app.integrations.wayback import WaybackClient
    from app.integrations.blacklist import BlacklistClient
    from app.integrations.aparser import AParserClient
    from app.integrations.rdap import RdapClient
    from app.integrations.webrisk import WebRiskClient
    from app.integrations.ahrefs import AhrefsClient
    from app.integrations.llm import LlmClassifyClient
    from app.integrations.whois43 import Whois43Client
    from app.config import settings as env
    return {
        "wayback": WaybackClient(), "blacklist": BlacklistClient(), "webrisk": WebRiskClient(),
        "aparser": AParserClient(), "rdap": RdapClient(), "ahrefs": AhrefsClient(),
        "_whois_lock": threading.Lock(), "_rdap_lock": threading.Lock(),
        "_webrisk_lock": threading.Lock(), "llm": LlmClassifyClient(), "_llm_lock": threading.Lock(),
        # зоны без RDAP: прямой whois:43 первичен, A-Parser — резерв за семафором (services/whois.py)
        "whois43": Whois43Client(), "_whois43_lock": threading.Lock(),
        "_whois_sem": threading.BoundedSemaphore(max(1, int(env.WHOIS_APARSER_CONCURRENCY))),
    }


def acquirability_verdict(available, acquire_deadline, now, *, lane) -> str:
    """whois-доступность + дедлайн ловли -> 'free' | 'taken' | 'waiting' | 'unknown'.

    ЕДИНСТВЕННОЕ место, где решается «можно ли ещё купить». Его зовут и воронка (W2, при
    первом скоринге), и перепроверка (recheck_acquirability, потом) — двух версий правды
    здесь быть не должно.

    'waiting' — домен занят СЕЙЧАС, и это нормально: дроп ещё не наступил. Так выглядит
    любой bid-кандидат (DropCatch/Nominet) до даты дропа.
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
        #   NULL — лейн НЕИЗВЕСТЕН (ручной список: лейн определит RDAP), и принимать
        #          незнание за «домен свободного лейна» нельзя. Ровно так на живом боксе утекли
        #          лучшие домены базы: clara-c.ru (score 0.89, RD 2219) и ещё 28 — все lane=NULL.
        return "taken" if lane == "free" else "unknown"
    if drop_window_open(dl, now):
        return "waiting"                             # дроп ещё не наступил или идёт прямо сейчас
    return "taken"                                   # дедлайн с запасом прошёл, а домен занят


def drop_window_open(acquire_deadline, now) -> bool:
    """Окно дропа ещё открыто: даты нет (судить не по чему — считаем открытым) или `now` не позже
    дедлайна с запасом DROP_GRACE. Одна граница на двоих — acquirability_verdict и короткое
    замыкание bid в W2 (`_avail_one`): две копии сравнения разъехались бы на naive-дате из БД или
    на запасе."""
    from datetime import timezone
    if acquire_deadline is None:
        return True
    dl = acquire_deadline
    if dl.tzinfo is None:                            # из БД дата может прийти naive
        dl = dl.replace(tzinfo=timezone.utc)
    return now <= dl + DROP_GRACE


def scorable(now):
    """SQL-условие «этот домен МОЖЕТ пройти W2 прямо сейчас» — фильтр выборки score_pending.

    Без него воронка платит whois'ом за ответ, который уже знает. Не-bid домен до своего дропа
    ГАРАНТИРОВАННО занят, вердикт вернёт `waiting`, домен останется discovered — и следующий
    прогон купит тот же ответ заново: один `весь пул` выжигал бы max_whois_per_run на одних и
    тех же строках с нулевым продвижением.

    Берём, значит, только тех, у кого есть шанс:
      · lane='bid' — DropCatch/Nominet: пока окно дропа открыто, W2 короткозамкнут лейном
        (RDAP/whois нужен ради возраста); окно закрыто — RDAP решает, не упущен ли дроп;
      · дроп НАСТУПИЛ (`deadline <= now`) — сегодня whois впервые может сказать «свободен».
        До дропа не переспрашиваем: ответ известен по ДАТЕ, а не по догадке. F20 (аудит
        2026-07-14): здесь стоял `<= now + DROP_GRACE` — не «наступил с запасом», а «наступит
        В ПРЕДЕЛАХ DROP_GRACE ВПЕРЕДИ», то есть дроп ЗАВТРА/послезавтра уже проходил сюда, хотя
        такой домен гарантированно ещё занят. DROP_GRACE здесь не нужен вообще — это ДРУГАЯ
        граница, чем верхний запас ПОСЛЕ дропа в acquirability_verdict, путать нельзя;
      · дедлайна НЕТ — раз в RECHECK_EVERY. Здесь одним шансом обойтись нельзя: «занят сегодня»
        без даты дропа не говорит ничего про день освобождения, и домен (ручной список без
        лейна) никогда не увидел бы собственного дропа.
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


def score_domain(domain_id: int, clients: dict | None = None, whois_budget=None,
                 links_budget=None, run: int | None = None, deep_budget=None) -> dict:
    """Полная воронка для ОДНОГО домена (кнопка «▶ Проверить»): батч из ОДНОГО FunnelState
    через тот же волновой конвейер, что и score_pending. Капы по умолчанию не действуют (None),
    пол остатка units — действует. Исключение: при `max_deep_per_run == 0` волна W6 выключена и
    для ручной кнопки (Budget(0))."""
    from app.db import SessionLocal
    from app.models.domain import Domain
    from app.services.settings import get_settings

    st = get_settings()
    with SessionLocal() as db:
        d = db.get(Domain, domain_id)
        if d is None:
            raise ValueError(f"domain {domain_id} not found")
        if d.status not in ("discovered", "scored", "rejected"):
            return {"domain": d.domain, "status": d.status, "skipped": "status"}
        links_c, deep_c = cache_from_domain(d)
        state = FunnelState(domain_id=d.id, domain=d.domain, lane=d.lane,
                            referring_domains=d.referring_domains,
                            acquire_deadline=d.acquire_deadline, feed_flags=d.feed_flags,
                            source=d.source, market_lang=d.market_lang,
                            links_cached=links_c, deep_cached=deep_c)

    c = clients or _make_clients()
    if deep_budget is None and int(st["max_deep_per_run"]) == 0:
        deep_budget = Budget(0)     # 0 = W6 выключен и для ручной кнопки «▶»: ≈1,1 тыс. units не тратим
    results = _run_waves([state], c, st, whois_budget, links_budget, run, deep_budget=deep_budget)
    return results[0]


def score_pending(limit: int = 100) -> int:
    """Скорит `discovered` домены. Прогресс и стадии — через jobs.track (см. services/jobs.py).
    Между доменами смотрит стоп-кнопку: «Проверить весь пул» — это часы работы и квоты
    A-Parser, прервать это должно быть можно без рестарта контейнера.

    Возвращает СКОЛЬКО РЕАЛЬНО ПРОШЛО воронку: при отмене — частичное число, не len(rows).
    Оркестратор пишет это в counts свипа — врать ему нельзя."""
    from datetime import datetime, timezone
    from sqlalchemy import select, func, case, and_, or_
    from app.db import SessionLocal
    from app.models.domain import Domain
    from app.services import jobs
    from app.services.settings import get_settings

    st = get_settings()
    now = datetime.now(timezone.utc)
    # Платный гейт решаем ДО выборки (I1): закрыт (нет ключа / остаток ниже пола) — не-EMD всё равно
    # ждут следующего прогона без отметки сверки, и если бы они занимали лимит выборки, каждый свип
    # брал бы тот же набор, а EMD (W4 им не нужна) не получал ни одного слота. Один запрос остатка
    # units на прогон: решение уезжает в `_paid_gate` через clients["_paid_gate"]. Копия словаря —
    # решение не должно пережить прогон в наборе, который кто-то переиспользует.
    clients = dict(_make_clients())
    gate = _paid_gate_closed(clients, st)
    clients["_paid_gate"] = gate
    # Без ключа Ahrefs (решение оператора) гейт НЕ держит домены: W4/W6 просто пропускаются.
    # Закрытым считается только пол остатка units при живом ключе.
    closed = gate is not None and gate[0] != "ahrefs_no_key"
    notes: list[str] = []          # пояснения волн («остаток units ниже пола») — в итог задачи
    if gate is not None:
        notes.append(gate[1])
    with SessionLocal() as db:
        # ЯРУС СРОЧНОСТИ — первым ключом, не RD и не голая дата.
        #
        # RD до W4 неизвестен (его даёт Ahrefs) — значит по RD домен, дропающийся СЕГОДНЯ, лёг
        # бы вперемешку с бездатным пулом, и при n=5 пул вытеснял бы его НИКОГДА не доскоренным.
        # Но и голая дата ASC неверна: «самая ранняя» — это ПРОТУХШИЙ дедлайн месячной давности,
        # то есть дроп, который мы уже упустили. Он встал бы впереди сегодняшнего и занимал бы
        # слот выборки и запрос W2 раньше живых: W2 отбракует его как not_acquirable
        # только на своей волне (I1 whole-branch ревью: bid замыкается лейном, лишь пока окно открыто).
        expired = and_(Domain.acquire_deadline.is_not(None),
                       Domain.acquire_deadline < now - DROP_GRACE)
        tier = case((Domain.acquire_deadline.is_(None), 2),   # дата неизвестна — кулдаун-пул
                    (expired, 1),                             # окно дропа закрыто — уже упустили
                    else_=0)                                  # окно открыто/впереди — вот они и важны
        q = (select(Domain.id, Domain.domain, Domain.lane, Domain.referring_domains,
                    Domain.acquire_deadline, Domain.feed_flags, Domain.source, Domain.market_lang,
                    Domain.dr, Domain.backlinks, Domain.organic_traffic, Domain.score_breakdown,
                    Domain.anchors, Domain.spam_anchor_ratio)
             .where(Domain.status == "discovered", scorable(now))
             .order_by(tier,
                       Domain.acquire_deadline.asc(),         # внутри яруса — ближайший дроп первым
                       Domain.referring_domains.desc().nulls_last()))  # равных по сроку разводит RD
        # R2-10: не-EMD домен без W4 не решается (сверх капа — unresolved links_budget), а W2/W3 за
        # него уже заплачены. Берём таких не больше капа W4; остаток лимита добирают EMD (W4 у них нет).
        cap = 0 if closed else min(limit, int(st["max_links_per_run"]))
        rows = db.execute(q.where(or_(Domain.source.is_(None), Domain.source != "emd"))
                          .limit(cap)).all() if cap else []
        rows += db.execute(q.where(Domain.source == "emd").limit(limit - len(rows))).all()
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
                idle_msg = "проверять нечего: найденных доменов нет — сначала «Найти домены»"
            else:
                parts = []
                if waiting:
                    parts.append(f"{waiting} ждут своего дропа"
                                 + (f" (ближайший — {nearest:%d.%m})" if nearest else ""))
                if undated:
                    parts.append(f"{undated} без даты дропа — вернусь к ним в течение суток")
                idle_msg = "проверять нечего: " + ", ".join(parts)
            if closed:
                # закрытый гейт: «найденных нет» было бы враньём — домены есть, их держит гейт
                held = db.scalar(select(func.count()).select_from(Domain)
                                 .where(Domain.status == "discovered")) or 0
                if held:
                    idle_msg = f"{held} доменов ждут следующей проверки"
    stages = [dict(s) for s in FUNNEL_STAGES]
    if gate is not None and gate[0] == "ahrefs_no_key":
        # ключа Ahrefs нет: платные волны честно помечены «пропущена» (не сломана, а отключена)
        for s in stages:
            if s["key"] in ("links", "deep"):
                s["state"] = "skip"
    # Budget, а не [int]: волна avail конкурентная (12 потоков), голый `box[0] -= 1` под ней — гонка
    whois_budget = Budget(int(st["max_whois_per_run"]))
    links_budget = Budget(int(st["max_links_per_run"]))
    deep_budget = Budget(int(st["max_deep_per_run"]))       # 0 = W6 выключен: анкоры не проверены
    total = len(rows)
    states = []
    for (did, name, lane, rd, deadline, flags, src, lang, dr, bl, tr, bd, anc, sar) in rows:
        links_c, deep_c = cache_from_domain(SimpleNamespace(
            referring_domains=rd, backlinks=bl, dr=dr, organic_traffic=tr, score_breakdown=bd,
            anchors=anc, spam_anchor_ratio=sar))
        states.append(FunnelState(domain_id=did, domain=name, lane=lane, referring_domains=rd,
                                  acquire_deadline=deadline, feed_flags=flags, source=src,
                                  market_lang=lang, links_cached=links_c, deep_cached=deep_c))
    done = 0
    with jobs.track("score", stages=stages) as run:
        if not states:
            msg = " · ".join([*notes, *([idle_msg] if idle_msg else [])])
            jobs.report(run, done=0, total=0, current="", message=msg)
        else:
            try:
                results = _run_waves(states, clients, st, whois_budget, links_budget, run=run,
                                     notes=notes, deep_budget=deep_budget)
            except jobs.Cancelled:
                # _run_waves() на отмене RAISE'ит ДО своего `return results` (см. его тело) —
                # локальный список результатов теряется вместе со стеком, ХОТЯ _checkpoint()
                # внутри уже мог реально закоммитить в БД часть states волной(ами) РАНЬШЕ той,
                # на которой прилетела отмена (t0/avail/risk/links/history пишут в БД сразу по
                # завершении своей волны, до общего возврата). Если считать done=0 в этом
                # случае — контракт «частичное число, не len(rows)» соврёт: репорт покажет
                # «отменено, 0 из N», а в БД у части доменов уже честный терминальный статус.
                # Источник правды — САМА БД: сколько id из ЭТОГО батча реально покинули
                # discovered, не длина потерянного results.
                ids = [s.domain_id for s in states]
                with SessionLocal() as s2:
                    done = s2.execute(
                        select(func.count()).select_from(Domain)
                        .where(Domain.id.in_(ids), Domain.status != "discovered")
                    ).scalar() or 0
                raise
            done = len(results)
            msg = " · ".join([f"проверено доменов: {total}", *notes])
            jobs.report(run, done=total, total=total, current="", message=msg)
        # «Слепые» (Wayback упал/недочитан) возвращаются в очередь сами, без платных волн (S2-07)
        retried = retry_blind_history(max(limit - total, 0) or 25, st, clients, run)
        if retried:
            jobs.report(run, message=" · ".join([msg, f"дочитана история: {retried}"]))
    return done


# Через сколько «слепой» по истории домен дочитывается снова (S2-07): архив лежал — не бьём его
# каждым часовым свипом по одним и тем же доменам.
BLIND_COOLDOWN = timedelta(hours=6)


def _blind_state(d) -> "FunnelState":
    """FunnelState из строки уже оценённого домена для W5-only: сигналы прошлого прогона
    предзаполнены, чтобы пересчёт скора не потерял RD/DR/анкоры и не потянул платные волны."""
    bd = d.score_breakdown or {}
    num = lambda v: float(v) if v is not None else None            # noqa: E731 — Numeric -> float
    sig = {"errors": [e for e in (bd.get("errors") or [])
                      if not str(e).startswith(("wayback:", "age:"))],
           "age_years": num(d.age_years), "age_source": bd.get("age_source"),
           "whois_source": bd.get("whois_source"), "referring_domains": d.referring_domains,
           "dr": num(d.dr), "backlinks": d.backlinks, "organic_traffic": d.organic_traffic,
           "topical_relevance": num(d.topical_relevance), "spam_anchor_ratio": num(d.spam_anchor_ratio),
           "ref_subnets": bd.get("ref_subnets"), "rd_dofollow": bd.get("rd_dofollow"),
           "peak_traffic": bd.get("peak_traffic"), "deep_checked": bd.get("deep_checked"),
           "spam_anchors": bd.get("spam_anchors"), "webrisk_threats": bd.get("webrisk_threats"),
           "list_hits": bd.get("list_hits"), "blacklisted": d.blacklisted}
    return FunnelState(domain_id=d.id, domain=d.domain, lane=d.lane,
                       referring_domains=d.referring_domains, acquire_deadline=d.acquire_deadline,
                       feed_flags=d.feed_flags, sig=sig, source=d.source, market_lang=d.market_lang)


def retry_blind_history(limit: int, st: dict, clients: dict, run: "int | None") -> int:
    """Дочитать историю у `scored`-доменов, оценённых «вслепую» по Wayback (S2-07): `wayback:` в
    errors (архив упал) или прочитано слишком мало снимков. Раньше такие домены не выбирались ни
    свипом (`discovered`), ни перепроверкой (только whois), и разбирались только кнопкой «▶»,
    которая гоняет ВСЕ волны заново — вместе с платными.

    Только W5 (Wayback + тема), без W2/W3/W4/W6: платные результаты уже в строке домена. Cooldown
    `BLIND_COOLDOWN` по `scored_at`; полностью пустой архив не ретраим (ждать нечего). Результат
    коммитит `_commit_result` — тем же путём, что и обычный: чистая полная проверка реабилитирует,
    найденная грязь отклоняет, повторный сбой оставляет домен как был."""
    from datetime import timezone
    from sqlalchemy import or_, select
    from app.db import SessionLocal
    from app.models.domain import Domain
    if limit <= 0 or clients.get("wayback") is None:
        return 0
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        rows = db.execute(
            select(Domain).where(Domain.status == "scored", Domain.wayback_checked.is_not(True),
                                 or_(Domain.scored_at.is_(None), Domain.scored_at < now - BLIND_COOLDOWN))
            .order_by(Domain.scored_at.asc().nulls_first(), Domain.id.asc()).limit(limit * 4)
        ).scalars().all()

        def _worth(d) -> bool:
            bd = d.score_breakdown or {}
            errs = [str(e) for e in (bd.get("errors") or [])]
            return (history_verdict(d) == "unknown" and not emd_newreg(d)
                    and (any(e.startswith("wayback:") for e in errs) or bool(bd.get("history_evidence"))))
        states = [_blind_state(d) for d in rows if _worth(d)][:limit]
    if not states:
        return 0
    _wave_history(states, clients, st, run)
    n = 0
    for s in states:
        _commit_result(s, run, st)
        n += 1
    return n


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

    ЗАЧЕМ. Скоринг решает приобретаемость ОДИН раз (W2) и больше к ней не возвращается.
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
    with jobs.track("recheck", stages=[{"key": "whois", "label": "не заняты ли"}]) as run:
        jobs.report(run, stage="whois")
        budget = int(get_settings()["max_whois_per_run"])
        if budget <= 0:
            # ВНУТРИ track, а не до него: иначе прогон завершался бы, не создав строки реестра,
            # и кнопка «Проверить, не заняты ли» выглядела бы сломанной — ровно та болезнь, которую лечим.
            jobs.report(run, message="лимит проверок whois = 0 — проверять нечем (см. Настройки)")
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
            # бюджет: если таких доменов больше бюджета (а это ровно авария «источник сменил формат
            # даты дропа»), перепроверка никогда не дойдёт до остального списка и молча выродится
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
               f"ждут дропа {out['waiting']}, ЗАНЯТЫ {out['taken']} (отклонены), "
               f"не определилось {out['unknown']}") if total else (
            "проверять нечего: нет доменов «ждёт решения» или «одобрен». "
            "Сначала нажми «Проверить домены»")
        jobs.report(run, done=total, total=total, current="", message=msg)
    return out


# ============================================================================
# Волновая архитектура (2026-07-20): дёшево->дорого волнами на ВЕСЬ пул, а не
# по-доменно. См. docs/superpowers/specs/2026-07-20-scoring-wave-architecture-design.md.
# ============================================================================

# Границы конкурентности — ХАРДКОД, не /settings (решение пользователя): "если домен
# занят whois — скипаем в этой волне" — сами лимиты волн оператор не крутит.
# history=4 — вежливость к archive.org (проектная ценность, не число для тюнинга).
# W4 «ссылки» здесь нет: она идёт пачками ПОСЛЕДОВАТЕЛЬНО (лимит Ahrefs 60 запросов/мин).
# deep=2 — W6 стоит ~1,1 тыс. units на домен: не спешим.
_CONCURRENCY = {"avail": 12, "risk": 12, "probe": 4, "history": 4, "topic": 2, "deep": 2}


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
    source: str | None = None       # v2: list/emd/… — W0 и W4/W6 ведут себя по-разному для EMD
    market_lang: str | None = None  # язык прошлого сайта из БД — запасной для W6, если LLM молчит (R2-2)
    texts: list = field(default_factory=list)    # тексты прочитанных снимков -> тема W5 (в сигнал/БД не идут)
    links_cached: dict | None = None    # уже оплаченный ответ Ahrefs W4 (S2-07): повторно не платим
    deep_cached: dict | None = None     # уже оплаченные анкоры W6 (S2-07)


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


def _mark_failed(s, stage: str, e: BaseException) -> None:
    """Необработанное исключение в потоке волны: домен НЕ остаётся «живым без сигнала» (S2-10) —
    иначе он ехал бы в следующие (платные) волны с пустыми sig/errors. Сигнал в errors, домен
    unresolved (оценится следующим прогоном), а не отклонён."""
    if s.alive:
        s.sig["errors"].append(f"{stage}:{type(e).__name__}")
        s.unresolved_why, s.alive = f"{stage}_failed", False


def _run_concurrent(states: list, workers: int, run: "int | None", stage: str, fn) -> None:
    """Гоняет fn(state) на всех ALIVE states пулом `workers` потоков. fn мутирует state
    IN PLACE (sig/reject_reason/unresolved_why/alive) и НЕ касается БД — коммит только
    в _checkpoint, после того как волна целиком завершилась.

    Прогресс: stage репортится ОДИН раз в начале (флип чипа волны в реестре) — done/total
    без stage= на каждом тике (report() с stage= делает лишний SELECT stages на КАЖДЫЙ
    вызов, см. jobs.py:330-334; сотни тиков волны не должны множить это на сотни
    SELECT'ов).

    Отмена (S2-10) проверяется не только после завершения задачи, но и по таймеру (0.5 с): пул
    без `with`, `shutdown(wait=False, cancel_futures=True)` — не ждёт уже запущенные потоки
    (на Wayback это 15-70 с на поток). Запущенные задачи доживают в фоне и мутируют только свои
    FunnelState, которые после отмены никто не коммитит.
    """
    from concurrent.futures import FIRST_COMPLETED, wait
    from app.services import jobs
    alive = [s for s in states if s.alive]
    if not alive:
        return
    jobs.report(run, stage=stage, done=0, total=len(alive))
    ex = ThreadPoolExecutor(max_workers=workers)
    try:
        pending = {ex.submit(fn, s): s for s in alive}
        done = 0
        while pending:
            finished, _ = wait(pending, timeout=0.5, return_when=FIRST_COMPLETED)
            for fut in finished:
                s = pending.pop(fut)
                try:
                    fut.result()
                except Exception as e:  # noqa: BLE001 — сбой одного домена не топит волну
                    logging.getLogger(__name__).exception("%s упал для %s", stage, s.domain)
                    _mark_failed(s, stage, e)
                done += 1
            if finished:
                jobs.report(run, done=done, total=len(alive))
            if jobs.cancelled(run, cached=True):
                raise jobs.Cancelled()
    finally:
        ex.shutdown(wait=False, cancel_futures=True)


def _wave_t0(states: list, st: dict) -> None:
    """W0 — без сети: флаги фида, белый список зон, чужие VPN-бренды. Зоны режутся здесь ещё раз,
    хотя discovery режет их на входе: ручной список приходит без фильтра (оператор должен
    увидеть причину), а белый список мог сузиться после того, как домен попал в пул."""
    from app.services.domain_filters import brand_hit, tld_match
    for s in states:
        if not s.alive:
            continue
        if s.feed_flags and any(s.feed_flags.get(k) for k in ("rkn", "judicial", "block")):
            s.reject_reason, s.alive = "feed_flag", False
        elif not tld_match(s.domain, st["tld_allowlist"]):
            s.reject_reason, s.alive = "tld_closed", False
        elif brand_hit(s.domain, st["brand_tokens"]):
            s.sig["trademark_risk"] = True
            s.reject_reason, s.alive = "trademark", False
        else:
            # Бренд-проверка РЕАЛЬНО отработала и прошла (отказы флага фида и зоны до неё не дошли):
            # пишем False, иначе колонка, однажды ставшая True, не снимается — `_commit_result` пишет
            # только не-None. Оператор убрал токен из /settings -> перескор — единственный путь назад
            # для `trademark` (он в DIRTY_REASONS). Отказ раньше бренда False не пишет: не проверяли.
            s.sig["trademark_risk"] = False


def _avail_one(s: FunnelState, clients: dict, budget, st: dict) -> None:
    """W2 для ОДНОГО домена: доступность + дата регистрации. RDAP бесплатный и бюджета не тратит;
    кап max_whois_per_run — только на whois:43 через A-Parser (зоны без RDAP).

    Возраст здесь только ЗАПИСЫВАЕТСЯ (`whois_created`, информационно `age_years`), отказа
    `too_young` нет (решение оператора Р5): у перехваченного и снова дропающегося домена RDAP
    показывает дату ПОСЛЕДНЕЙ регистрации — 15 лет истории выглядели бы как 3 года. Возраст для
    решения — старшая из даты RDAP и первого снимка Wayback, отказ — в W5 (history)."""
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)

    rdap = clients.get("rdap")
    via_rdap = rdap is not None and rdap.has_rdap(s.domain)   # бутстрап не бросает (rdap._FALLBACK)
    if not via_rdap and budget is not None and not budget.take():
        s.unresolved_why, s.alive = "budget", False
        return

    try:
        pr = whois_router.probe(s.domain, clients)
        wc = pr.get("created")
        if wc is not None and wc.tzinfo is None:
            wc = wc.replace(tzinfo=timezone.utc)   # наивная дата whois:43 — UTC, иначе TypeError ниже
        age = (now - wc).days / 365.25 if wc is not None else None
    except Exception as e:  # noqa: BLE001 — и сбой канала, и битая дата: домен НЕ идёт дальше «живым без вердикта»
        code = "circuit_open" if isinstance(e, whois_router.CircuitOpen) else type(e).__name__
        s.sig["errors"].append(f"whois:{code}")
        # bid «вслепую» дальше — только пока окно дропа открыто (I1): с закрытым окном без ответа
        # RDAP не отличить упущенный дроп от живого, а впереди платные W4/W6 — ждём прогона.
        if s.lane != "bid" or not drop_window_open(s.acquire_deadline, now):
            s.unresolved_why, s.alive = "whois_failed", False
            return
        pr, wc, age = {"available": None, "status": []}, None, None

    if pr.get("available") is not None:
        s.sig["acquirability_checked_at"] = now
    s.sig["whois_source"] = pr.get("whois_source")
    s.sig["whois_created"] = wc
    if age is not None:
        s.sig["age_years"], s.sig["age_source"] = round(age, 2), "whois"

    # Ручной список без лейна, а RDAP говорит «pending delete»/«redemption period»: это дроп, а не
    # чужой занятый домен. Без лейна он висел бы taken_undated до самого дропа (находка 1.12).
    # Даты дропа у него нет — оценка по статусу (находка R2-11): redemption period — 30 суток выкупа
    # + 5 удаления, pending delete — 5. Без даты bid-домен никогда не закрылся бы обычным путём
    # acquirability_verdict (у bid без даты судить нечем). В sig, а не в state: _commit_result
    # пишет оценку только в ПУСТУЮ колонку — реальную дату дропа она не перебивает.
    rdap_status = set(pr.get("status") or [])
    if s.lane is None and {"pending delete", "redemption period"} & rdap_status:
        s.lane = "bid"
        if s.acquire_deadline is None:
            days = 35 if "redemption period" in rdap_status else 5
            s.sig["acquire_deadline"] = now + timedelta(days=days)
    # Короткое замыкание bid — ТОЛЬКО пока окно дропа открыто (I1 whole-branch ревью). «Занят» у bid
    # до дропа — норма, судить нечего. Но дедлайн с запасом прошёл, а домен занят — дроп упущен
    # (перехватили/продлили): без вердикта он платил бы W4 (25 units) и W6 (~1,1 тыс.) и отнимал слот
    # W6 у живых. Тогда решает acquirability_verdict: занят -> not_acquirable, свободен -> лейн free.
    # Исключение — реестр всё ещё держит домен в удалении: дроп идёт, хоть дата фида и прошла, и
    # отказ `not_acquirable` выбросил бы живой дроп («в сомнении — не taken», см. вердикт).
    if s.lane == "bid" and ({"pending delete", "redemption period"} & rdap_status
                            or drop_window_open(s.acquire_deadline, now)):
        s.sig["lane"] = "bid"
        return

    v = acquirability_verdict(pr.get("available"), s.acquire_deadline, now, lane=s.lane)
    if v == "taken":
        s.reject_reason, s.alive = "not_acquirable", False
    elif v == "free":
        s.sig["lane"] = "free"
    else:
        s.unresolved_why = ("waiting" if v == "waiting"
                            else "whois_unclear" if pr.get("available") is None
                            else "taken_undated")
        s.alive = False


def _wave_avail(states: list, clients: dict, budget, st: dict, run) -> None:
    """W2 — доступность (RDAP, иначе whois:43 через A-Parser), конкурентно на выживших после W0."""
    _run_concurrent(states, _CONCURRENCY["avail"], run, "avail",
                    lambda s: _avail_one(s, clients, budget, st))


def _risk_one(s: FunnelState, clients: dict) -> None:
    """W3 для ОДНОГО домена. Web Risk — коммерчески легальная замена Safe Browsing (тот «for
    non-commercial use only»). Без ключа проверку НЕ делаем и честно пишем «не настроено»: домен
    едет дальше, но «вслепую» — пакет его не возьмёт. Spamhaus DBL — только с платным DQS-ключом:
    бесплатное зеркало для коммерции запрещено (docs/v2/research/metrics-history.md).

    Угроза Web Risk НЕ пишется в колонку `blacklisted` (находка 1.5): та — сигнал Spamhaus, и без
    DQS её никто бы не перепроверил и не снял. Улика живёт в `score_breakdown.webrisk_threats`
    (_commit_result хранит её через _kept) — по ней transitions.dirty_reason держит домен грязным,
    и перескор, на котором Web Risk упал, угрозу не отмывает. Лежащий Web Risk — предохранитель
    «3 сбоя подряд» (whois.guarded): дальше `webrisk:circuit_open` без сети до конца прогона."""
    from app.config import settings
    wr = clients.get("webrisk")
    if wr is None or not wr.configured:
        s.sig["errors"].append("webrisk:not_configured")
    else:
        try:
            threats = whois_router.guarded(wr, "threat_failures", lambda: wr.threats(s.domain),
                                           "Google Web Risk", clients.get("_webrisk_lock"))
        except Exception as e:  # noqa: BLE001 — сбой проверки не приговор домену, но «вслепую»
            code = "circuit_open" if isinstance(e, whois_router.CircuitOpen) else type(e).__name__
            s.sig["errors"].append(f"webrisk:{code}")
        else:
            s.sig["webrisk_threats"] = threats
            if threats:
                s.reject_reason, s.alive = "blacklist", False
                return
    bl = clients.get("blacklist")
    if bl is not None and settings.SPAMHAUS_DQS_KEY:
        try:
            listed = bl.is_blacklisted(s.domain)
        except Exception as e:  # noqa: BLE001
            s.sig["errors"].append(f"blacklist:{type(e).__name__}")
        else:
            if listed is None:
                s.sig["errors"].append("blacklist:unavailable")
            else:
                s.sig["blacklisted"] = listed
                if listed:
                    s.reject_reason, s.alive = "blacklist", False


def _wave_risk(states: list, clients: dict, run, notes: list | None = None) -> None:
    """W3 — Web Risk (+ Spamhaus при DQS), конкурентно на весь выживший после avail пул.

    Без ключа Web Risk (S2-13) каждый домен «слепой» по риску, а пакетное одобрение закрыто: это
    не поломка домена, а настройка — говорим об этом в сообщении задачи, а не только в errors."""
    wr = clients.get("webrisk")
    if notes is not None and any(s.alive for s in states) and (wr is None or not wr.configured):
        note = ("Web Risk не настроен (ключ — в «Ключи и доступы») — риск доменов НЕ проверен, "
                "пакетное одобрение закрыто, решаешь ты")
        if note not in notes:
            notes.append(note)
    _run_concurrent(states, _CONCURRENCY["risk"], run, "risk", lambda s: _risk_one(s, clients))


def _wave_lists(states: list, st: dict) -> None:
    """W3-списки: попадание домена в списки чистоты (UT1/blocklistproject) — языконезависимая проверка
    «домен числился в gambling/adult/phishing/malware/drugs». Идёт ПОСЛЕ Web Risk/Spamhaus на тех, кто
    выжил, одним запросом в БД из основного потока (конкурентный харнесс тут не нужен).

    Сигнал МЯГКИЙ: `sig["list_hits"]` = категории (может быть []), пишется в score_breakdown и закрывает
    пакетное одобрение (bulk_ok). Жёсткий отказ `list_hit` — только при `hard_reject_lists` и только по
    scoring_config.HARD_LIST_CATEGORIES. Списки не загружены (таблица пуста) или БД не ответила —
    сигнал НЕ пишется: «не знаем» не равно «чисто» (прошлый list_hits при этом сохраняет _kept)."""
    from app.services import domain_lists
    alive = [s for s in states if s.alive]
    if not alive:
        return
    try:
        hits = domain_lists.lookup([s.domain for s in alive])
    except Exception as e:  # noqa: BLE001 — сбой чтения списков не приговор домену
        for s in alive:
            s.sig["errors"].append(f"lists:{type(e).__name__}")
        return
    if hits is None:
        return
    hard_on = bool(st.get("hard_reject_lists"))
    for s in alive:
        cats = hits.get(s.domain, [])
        s.sig["list_hits"] = cats
        if hard_on and domain_lists.hard_hits(cats):
            s.reject_reason, s.alive = "list_hit", False


def _wave_ranks(states: list, st: dict) -> None:
    """Ранг домена в графе Common Crawl (+ необязательный бонус Majestic) -> `sig["rank_authority"]` 0..1,
    сводка в `sig["rank"]` (идёт в score_breakdown). Бесплатная замена Ahrefs DR, один запрос в БД на пакет.

    Три исхода (services/domain_ranks.py): ранг есть / проверили, в графе нет (authority 0) / не проверяли
    или ранги не загружены — тогда сигнал НЕ пишется, и compute_score берёт DR Ahrefs либо нейтральные 0.5:
    «не знаем» не равно «плохо». Ничего не отклоняет и не закрывает пакетное одобрение."""
    from app.services import domain_ranks
    alive = [s for s in states if s.alive]
    if not alive:
        return
    try:
        info = domain_ranks.lookup([s.domain for s in alive])
    except Exception as e:  # noqa: BLE001 — сбой чтения рангов не приговор домену
        for s in alive:
            s.sig["errors"].append(f"ranks:{type(e).__name__}")
        return
    if info is None:
        return
    low, full = st.get("rank_pct_low", cfg.RANK_PCT_LOW), st.get("rank_pct_full", cfg.RANK_PCT_FULL)
    maj_on = bool(st.get("rank_majestic"))
    for s in alive:
        val, summary = domain_ranks.authority_from_rank(info.get(s.domain) or {}, low, full, maj_on)
        if val is not None:
            s.sig["rank_authority"], s.sig["rank"] = val, summary


def _topic_one(s: FunnelState, clients: dict, texts: list) -> None:
    """W5-мягкий: язык и тема прошлого сайта по УЖЕ прочитанным снимкам (LLM). Ничего не отклоняет
    и не ослепляет (инвариант 3: жёсткие отказы — только детерминированный классификатор). Сбой,
    непригодный ответ, нет клиента или сработал предохранитель «3 сбоя подряд» (зависший LiteLLM
    держал бы слот волны) -> «тема не определена». EMD: язык — рынка набора (discovery), снимки
    его не перезаписывают (находка 4.4)."""
    from app.services import history_llm
    llm, t = clients.get("llm"), None
    if llm is not None:
        try:
            t = whois_router.guarded(llm, "classify_failures",
                                     lambda: history_llm.classify_topics(s.domain, texts, llm),
                                     "LLM (тема прошлого сайта)", clients.get("_llm_lock"))
        except Exception as e:  # noqa: BLE001 — мягкий сигнал: сбой LLM не отклоняет и не ослепляет
            t = None
            # причина (403 tier / 429 / пустой reasoning) — оператору в сообщение задачи, а не в
            # молчаливое «тема не определена» у всех доменов (S2-01/S6-10); текст уже без секретов
            errs = clients.setdefault("_llm_errors", [])
            msg = str(e)[:200]
            if not isinstance(e, whois_router.CircuitOpen) and msg not in errs:
                errs.append(msg)
    if not t:
        s.sig["topic_unknown"] = True
        return
    if s.source == "emd":
        t = {k: v for k, v in t.items() if k != "market_lang"}
    s.sig.update(t)


def _too_young(s: FunnelState, st: dict) -> bool:
    return (s.source != "emd" and s.sig.get("age_years") is not None
            and s.sig["age_years"] < st["min_age_years"])


def _probe_one(s: FunnelState, clients: dict, st: dict) -> None:
    """Дешёвый ранний выход ДО платной W4 (S2-06): одно CDX-окно «рождение» (кэшируется клиентом —
    потом оно же первое окно полной истории). Возраст — СТАРШАЯ из даты RDAP/whois и первого
    снимка; отказ `too_young` только если возраст обеспечен (первый снимок известен либо пустой
    архив подтверждён вторым запросом, S2-14). Сбой пробы ничего не решает — историю доберёт W5.
    Клиент без `probe` (тестовые наборы) — волна ничего не делает."""
    from app.integrations.wayback import WaybackUnavailable
    probe = getattr(clients.get("wayback"), "probe", None)
    if probe is None:
        return
    try:
        p = probe(s.domain)
    except WaybackUnavailable:
        return                          # архив лёг: решит W5 (wayback_down), здесь не судим
    except Exception:  # noqa: BLE001 — дешёвая проба не имеет права отклонять или ослеплять
        return
    wb_age = p.get("age_years")
    if wb_age is not None and (s.sig.get("age_years") is None or wb_age > s.sig["age_years"]):
        s.sig["age_years"], s.sig["age_source"] = wb_age, "wayback"
    if (wb_age is not None or p.get("archive_empty")) and _too_young(s, st):
        s.reject_reason, s.alive = "too_young", False


def _wave_probe(states: list, clients: dict, st: dict, run) -> None:
    """Дешёвая проба архива — между бесплатными волнами и платной W4."""
    if getattr(clients.get("wayback"), "probe", None) is None:
        return
    _run_concurrent(states, _CONCURRENCY["probe"], run, "probe",
                    lambda s: _probe_one(s, clients, st))


def _history_one(s: FunnelState, clients: dict, st: dict, with_topic: bool = True) -> None:
    """W5 для ОДНОГО домена: Wayback-история + категорийный hard-reject + возраст по старшей из
    двух дат (RDAP/whois из W2 и первый снимок) и гейт `too_young` (Р5) + тема прошлого сайта.

    `with_topic=False` — волна: тема (LLM) считается ОТДЕЛЬНЫМ проходом после Wayback (S2-09), чтобы
    латентность модели не держала один из 4 слотов archive.org. Тексты снимков остаются в
    `s.texts` до этого прохода."""
    from app.integrations.wayback import WaybackUnavailable
    texts: list = []
    try:
        hist = clients["wayback"].classify_history(s.domain)
        texts = hist.get("texts") or []
        pf = hist.get("prior_flags") or {}
        s.sig["prior_flags"] = pf
        s.sig["wayback_checked"] = hist.get("wayback_checked")
        s.sig["history_evidence"] = hist.get("evidence") or []
        s.sig["sampled"] = hist.get("sampled")
        s.sig["first_seen"] = hist.get("first_seen")
        # возраст для решения — СТАРШАЯ из даты RDAP/whois (W2) и первого снимка (Р5): у
        # перехваченного и снова дропающегося домена RDAP показывает ПОСЛЕДНЮЮ регистрацию
        wb_age = hist.get("age_years")
        if wb_age is not None and (s.sig.get("age_years") is None or wb_age > s.sig["age_years"]):
            s.sig["age_years"], s.sig["age_source"] = wb_age, "wayback"
        if any(pf.get(k) for k in cfg.HARD_REJECT_FLAGS):
            # улика есть — отказ, даже если прочитана меньшая часть выборки (S2-04)
            s.reject_reason = "history_dirty"
            s.alive = False
            return
    except Exception as e:  # noqa: BLE001
        s.sig["errors"].append(f"wayback:{type(e).__name__}")
        if isinstance(e, WaybackUnavailable):
            # Архив лежит (предохранитель/Retry-After) — это не «домен прочитан плохо», а «не
            # спрашивали»: домен ждёт следующего прогона, а не оседает вслепую в инбоксе (S2-03).
            s.unresolved_why, s.alive = "wayback_down", False
            return
        # Wayback не ответил (archive.org регулярно отдаёт 429/503) — вторая дата возраста
        # НЕИЗВЕСТНА, а не «молода». Отказ too_young по одной дате RDAP окончателен и потерял бы
        # перехваченный дроп (находка R2-1): гейт не судит, домен идёт дальше «вслепую» — `wayback:`
        # в errors держит его вне пакета (blind_reason).
        #
        # Но если гейт СРАБОТАЛ БЫ (по RDAP/whois домен моложе порога), а проверить второй датой
        # нечем — это отдельная дыра: при ручном перескоре домена, ранее отклонённого
        # `too_young`, колонка wayback_checked осталась True, history_verdict = clean, и без
        # метки молодой домен вернулся бы в пакет по ОТСУТСТВИЮ улик. `age:unverified` держит его
        # вне пакета, отказа по-прежнему не ставит.
        if _too_young(s, st):
            s.sig["errors"].append("age:unverified")
        return

    # Гейт молодости — ЗДЕСЬ, а не в W2 (решение оператора Р5), по старшей из двух дат и ПОСЛЕ
    # history_dirty (грязь — более сильная причина). Первый снимок не раньше регистрации, так что
    # и старшая дата — нижняя оценка возраста: молодым домен объявляется только если молоды обе.
    # EMD — новорег: «молодость» — его суть, гейт его не судит (находка R2-14).
    #
    # S2-14: пустой ответ CDX неотличим от сбоя. Нет возраста по архиву (age_years None) и пустота
    # не подтверждена вторым запросом (`archive_empty`) — вторая дата НЕИЗВЕСТНА, как при упавшем
    # Wayback: отказ не ставим, `age:unverified` держит домен вне пакета.
    if _too_young(s, st):
        if hist.get("age_years") is None and not hist.get("archive_empty"):
            s.sig["errors"].append("age:unverified")
        else:
            s.reject_reason = "too_young"
            s.alive = False

    # Тема — только выжившим и только по проверенной истории: LLM на уже отклонённый домен —
    # пустая трата времени, а по паре прочитанных снимков тему не судят.
    if s.alive and s.sig.get("wayback_checked") and texts:
        if with_topic:
            _topic_one(s, clients, texts)
        else:
            s.texts = texts


def _wave_history(states: list, clients: dict, st: dict, run) -> None:
    """W5 — Wayback-история и тема, конкурентно на весь выживший после links пул. Конкурентность
    жёстко 4 — вежливость к archive.org, некрутящаяся константа (не /settings)."""
    _run_concurrent(states, _CONCURRENCY["history"], run, "history",
                    lambda s: _history_one(s, clients, st, with_topic=False))
    # Тема W5 (LLM) — ОТДЕЛЬНЫМ проходом после Wayback (S2-09): модель не держит слот archive.org
    # и идёт своей конкурентностью; тексты уже в памяти, второго скачивания нет.
    topical = [s for s in states if s.alive and s.texts]
    if topical:
        _run_concurrent(topical, _CONCURRENCY["topic"], run, "history",
                        lambda s: _topic_one(s, clients, s.texts))
    for s in states:
        s.texts = []


_LINKS_BATCH = 100          # batch-analysis: до 100 целей за запрос

# W4 -> sig: поле ответа Ahrefs -> ключ сигнала (он же колонка Domain, кроме подсетей и dofollow,
# которые живут в score_breakdown).
_LINKS_FIELDS = (("domain_rating", "dr"), ("refdomains", "referring_domains"),
                 ("refdomains_dofollow", "rd_dofollow"), ("refips_subnets", "ref_subnets"),
                 ("backlinks", "backlinks"), ("org_traffic", "organic_traffic"))

# Платная волна не дошла до домена (ключа Ahrefs нет, пол остатка units, кап W4, сбой Ahrefs,
# строки домена нет в ответе). Такой домен оценится СЛЕДУЮЩИМ прогоном — поэтому _commit_result не
# ставит ему отметку сверки занятости: `scorable` вернул бы free/NULL-лейн только через
# RECHECK_EVERY после неё (находка 2.7).
_PAID_UNRESOLVED = ("links_budget", "units_floor", "ahrefs_no_key", "ahrefs_failed", "ahrefs_missing")


def _units_below_floor(clients: dict, st: dict) -> str | None:
    """Пол остатка units Ahrefs (решение оператора Р3): автопилот гоняет свип раз в час, капы «на
    прогон» месяц не держат. Перед платными волнами — один бесплатный запрос остатка (в начале
    прогона — `_paid_gate`, перед W6 — ещё раз: W4 уже потратила). Остаток неизвестен (None или
    сбой запроса) или ниже пола -> текст причины для сообщения задачи, волна units не тратит. Пол 0
    — пола нет, остаток не спрашиваем."""
    floor = int(st.get("units_floor") or 0)
    if floor <= 0:
        return None
    try:
        left = clients["ahrefs"].units_left()
    except Exception:  # noqa: BLE001 — остаток не узнать: тратить вслепую нельзя
        left = None
    if left is None:
        return "Ahrefs: остаток единиц неизвестен — платные проверки пропущены"
    if left < floor:
        return (f"Ahrefs: остаток {left:,} ниже минимума {floor:,} — платные проверки пропущены"
                .replace(",", " "))
    return None


def _units_daily_spent(clients: dict, st: dict) -> str | None:
    """Суточный пейсинг units (S2-11, по умолчанию ВЫКЛ: units_daily_cap=0). Пол остатка держит
    только месяц целиком — часовой свип выжигал бы лимит за несколько суток. Потрачено за сутки >=
    лимита -> платные волны ждут завтра. Остаток неизвестен — решает `_units_below_floor` (при
    ненулевом поле) или тут пропускаем: пейсинг без цифры — не повод блокировать."""
    cap = int(st.get("units_daily_cap") or 0)
    if cap <= 0:
        return None
    try:
        left = clients["ahrefs"].units_left()
    except Exception:  # noqa: BLE001
        return None
    if left is None:
        return None
    from app.services.settings import units_spent_today
    spent = units_spent_today(int(left))
    if spent >= cap:
        return (f"Ahrefs: за сутки потрачено {spent:,} единиц при суточном лимите {cap:,} — платные "
                f"проверки ждут завтра".replace(",", " "))
    return None


def _no_ahrefs_key(clients: dict) -> bool:
    """Ключа Ahrefs нет вовсе: платные волны W4/W6 пропускаются (не ошибка и не «слепой» отказ)."""
    ah = clients.get("ahrefs")
    return ah is not None and getattr(ah, "api_key", None) == ""    # у фейков тестов атрибута нет


def _paid_gate_closed(clients: dict, st: dict) -> tuple | None:
    """Закрыт ли платный гейт: `(unresolved_why, текст для сообщения задачи)` или None (открыт).
    Ключа Ahrefs нет — без сети; иначе один запрос остатка units (`_units_below_floor`). Клиента
    Ahrefs в наборе нет (тестовые наборы без W4; `_make_clients` кладёт его всегда) — решать
    нечем, гейт открыт, и скажет сама W4 (`ahrefs_failed`)."""
    ah = clients.get("ahrefs")
    if ah is None:
        return None
    if getattr(ah, "api_key", None) == "":             # у фейков тестов атрибута нет
        return ("ahrefs_no_key", "Ahrefs: ключ не задан — ссылки и спам-ссылки не проверяются, "
                                 "оценка без данных о ссылках")
    note = _units_below_floor(clients, st) or _units_daily_spent(clients, st)
    return ("units_floor", note) if note is not None else None


def _paid_gate(states: list, clients: dict, st: dict, notes: list) -> None:
    """Пойдут ли платные волны — решается ОДИН раз за прогон, сразу после бесплатной W0 (находка
    R2-10). Ключа Ahrefs нет или остаток units неизвестен/ниже пола — не-EMD домен без W4 всё равно
    не решится, а RDAP/whois:43 и Web Risk за него тратились бы впустую на каждом часовом свипе.
    Такие домены сразу unresolved (`ahrefs_no_key` / `units_floor`: без отметки сверки, оценятся
    следующим прогоном), причина — в `notes`. EMD идут дальше: W4 у них нет.

    `score_pending` решает это ЕЩЁ ДО выборки (закрытый гейт -> не-EMD не занимают лимит, иначе EMD
    не получал бы слота) и кладёт решение в `clients["_paid_gate"]` — тогда остаток units тут не
    спрашивается второй раз. Без ключа (одиночный `score_domain`, прямой вызов) — решаем сами."""
    todo = [s for s in states if s.alive and s.source != "emd"]
    if not todo:
        return
    decision = clients["_paid_gate"] if "_paid_gate" in clients else _paid_gate_closed(clients, st)
    if decision is None:
        return
    why, note = decision
    if why == "ahrefs_no_key":
        # Решение оператора (2026-10): ключа Ahrefs не будет — это не «ждём прогона», а режим.
        # Домены идут дальше по бесплатным волнам (история, возраст, риск), W4/W6 пропускаются
        # (`_no_ahrefs_key`), оценка без RD/DR; пакет такой домен не берёт (анкоры не проверены).
        if note not in notes:
            notes.append(note)
        return
    for s in todo:
        s.unresolved_why, s.alive = why, False
    if note not in notes:
        notes.append(note)


def cache_from_domain(d) -> tuple:
    """(links_cached, deep_cached) из строки домена (ORM или Row): что платные W4/W6 уже вернули.
    W4 писала RD И backlinks вместе — оба есть, значит ответ Ahrefs получен (RD из старого фида
    без backlinks кэшем не считается). W6 — только если анкоры реально проверены (`deep_checked`)."""
    bd = d.score_breakdown or {}
    links = None
    if d.referring_domains is not None and d.backlinks is not None:
        links = {"dr": float(d.dr) if d.dr is not None else None,
                 "referring_domains": d.referring_domains, "backlinks": d.backlinks,
                 "organic_traffic": d.organic_traffic, "rd_dofollow": bd.get("rd_dofollow"),
                 "ref_subnets": bd.get("ref_subnets")}
    deep = None
    if bd.get("deep_checked") is True and d.spam_anchor_ratio is not None:
        deep = {"anchors": d.anchors, "spam_anchor_ratio": float(d.spam_anchor_ratio),
                "peak_traffic": bd.get("peak_traffic")}
    return links, deep


def _persist_links(states: list) -> None:
    """Оплаченный ответ W4 — в строку домена СРАЗУ после волны (S2-06): отмена, рестарт воркера
    или упавшая за ней W5 (часы Wayback) не сжигают уже потраченные units — следующий прогон
    возьмёт их из `links_cached`. Статус не трогаем: домен остаётся тем, чем был."""
    from app.db import SessionLocal
    from app.models.domain import Domain
    fresh = [s for s in states if not s.links_cached
             and any(s.sig.get(k) is not None for _, k in _LINKS_FIELDS)]
    if not fresh:
        return
    with SessionLocal() as db:
        for s in fresh:
            d = db.get(Domain, s.domain_id)
            if d is None or d.status not in ("discovered", "scored", "rejected"):
                continue
            for col in ("dr", "referring_domains", "backlinks", "organic_traffic"):
                if s.sig.get(col) is not None:
                    setattr(d, col, s.sig[col])
            bd = dict(d.score_breakdown or {})
            for k in ("ref_subnets", "rd_dofollow"):
                if s.sig.get(k) is not None:
                    bd[k] = s.sig[k]
            d.score_breakdown = bd
        db.commit()


def _wave_links(states: list, clients: dict, st: dict, budget, run, notes: list | None = None) -> None:
    """W4 — ссылочный профиль из Ahrefs batch-analysis. Пачками по 100 и ПОСЛЕДОВАТЕЛЬНО: лимит API
    60 запросов/мин, одна пачка = один запрос, параллелить нечего. EMD пропускает — у новорега
    нечего мерить. Ключ и пол остатка units здесь не проверяются: это уже решил `_paid_gate` в
    начале прогона (R2-10).

    Домен, до которого волна не дошла (кап `max_links_per_run`, сбой Ahrefs, нет строки домена в
    ответе), НЕ судится без ссылок, а остаётся discovered (unresolved) до следующего прогона: без
    RD скор ниже порога, и домен навсегда ушёл бы в low_score (находка 1.8). Упала пачка — следующие
    не шлются: протухший ключ дал бы 401 на каждой. Причина с HTTP-кодом — в `notes`, то есть в
    сообщении задачи (R2-9): в логе скора её оператор не увидит.

    Пустое поле ответа ничего не затирает (находка 4.12): DR из discovery уже лежит в строке
    домена, и `_commit_result` подставит его, только если сигнала `dr` нет.

    Живой факт 2026-10-01: на дропах RD раздут автоматическим SEO-спамом (DR 0 при RD 700+), поэтому
    рядом с RD пишем подсети — compute_score режет `rd` вдвое при подозрении на спам-сетку."""
    from app.services import jobs
    from app.services.domain_filters import canonical_domain
    if _no_ahrefs_key(clients):
        return                          # режим без ключа: W4 пропущена, домены идут дальше (см. _paid_gate)
    todo = [s for s in states if s.alive and s.source != "emd"]
    if not todo:
        return
    jobs.report(run, stage="links", done=0, total=len(todo))
    eligible = []
    for s in todo:
        if s.links_cached:
            # Результат W4 уже оплачен (прошлый прогон / ручной перескор) — второй раз не платим
            # (S2-07). Данные Ahrefs о ссылочной массе за дни не протухают; заново они тянутся,
            # только если сброшены `referring_domains`/`backlinks` в строке домена.
            for src, key in _LINKS_FIELDS:
                v = s.links_cached.get(key)
                if v is not None:
                    s.sig[key] = v
            rd = s.links_cached.get("referring_domains")
            if rd is not None and rd < st["min_referring_domains"]:
                s.reject_reason, s.alive = "low_rd", False
        elif budget is not None and not budget.take():
            s.unresolved_why, s.alive = "links_budget", False
        else:
            eligible.append(s)
    for i in range(0, len(eligible), _LINKS_BATCH):
        chunk = eligible[i:i + _LINKS_BATCH]
        # ключ ответа — каноническое имя (punycode/нижний регистр), как его вернёт Ahrefs
        keys = {s.domain: canonical_domain(s.domain) or s.domain for s in chunk}
        try:
            data = clients["ahrefs"].batch(list(keys.values()))
        except Exception as e:  # noqa: BLE001 — пачка упала: она и все следующие ждут прогона
            rest = eligible[i:]
            for s in rest:
                s.sig["errors"].append(f"ahrefs:{type(e).__name__}")
                s.unresolved_why, s.alive = "ahrefs_failed", False
            if notes is not None:
                # 401/403 — ключ не принят, 400 — кривой запрос: код нужен оператору, не только тип
                code = getattr(getattr(e, "response", None), "status_code", None)
                why = type(e).__name__ + (f" {code}" if code else "")
                notes.append(f"Ahrefs (ссылки): {why} — {len(rest)} доменов ждут следующей проверки")
            return
        for s in chunk:
            row = data.get(keys[s.domain])
            if row is None:
                # Строки домена нет в ответе (аномалия: на несуществующий домен Ahrefs отдаёт нули) —
                # как сбой: без RD скор ушёл бы в low_score навсегда. Домен ждёт следующего прогона.
                s.sig["errors"].append("ahrefs:missing")
                s.unresolved_why, s.alive = "ahrefs_missing", False
                continue
            for src, key in _LINKS_FIELDS:
                if row.get(src) is not None:                # None — «неизвестно», не 0
                    s.sig[key] = row[src]
            rd = row.get("refdomains")
            if rd is not None and rd < st["min_referring_domains"]:
                s.reject_reason, s.alive = "low_rd", False
        jobs.report(run, done=min(i + _LINKS_BATCH, len(eligible)), total=len(eligible))
        if jobs.cancelled(run):
            raise jobs.Cancelled()


def _deep_one(s: FunnelState, clients: dict, st: dict) -> None:
    """W6 для ОДНОГО финалиста: анкоры, затем история трафика — ОТДЕЛЬНЫМИ вызовами (находка 3.7):
    сбой истории не выбрасывает уже оплаченный вердикт по анкорам.

    `deep_checked=True` — только если анкоры реально получены (находка 1.3): пустой список при
    живых донорах — это не «чисто», а «не проверено» (`deep:empty`). Сбой — `deep:<Исключение>`.
    Язык прошлого сайта неизвестен, а отказ держался бы только на правиле скрипта — `deep:lang_unknown`
    (находка R2-2): сбой LLM не отклоняет. Во всех трёх случаях домен идёт в решение, но вне пакета
    («анкоры НЕ проверены»).
    `spam_anchors` — улика для transitions.dirty_reason (находка 1.4): _commit_result хранит её
    через _kept, и перескор, на котором W6 не дошла, спам-домен не отмывает."""
    from app.services import link_signals
    try:
        anchors = clients["ahrefs"].anchors(s.domain)
    except Exception as e:  # noqa: BLE001
        s.sig["errors"].append(f"deep:{type(e).__name__}")
        return
    lang = s.sig.get("market_lang") or s.market_lang   # W5 этого прогона, иначе прошлый прогон (R2-2)
    try:
        # Разбор оплаченного ответа — под try: кривая строка (не dict, нечисловой refdomains) иначе
        # ушла бы в общий except волны — только в лог, без `deep:` в errors, а units уже списаны.
        ratio = link_signals.spam_anchor_ratio(anchors, s.domain, lang)
        script_only = (not lang and ratio is not None and ratio > st["spam_anchor_max"]
                       and (link_signals.spam_anchor_ratio(anchors, s.domain, scripts=False) or 0.0)
                       <= st["spam_anchor_max"])
        top = [{"anchor": str(a.get("anchor") or "")[:200], "refdomains": a.get("refdomains"),
                "is_spam": bool(a.get("is_spam"))} for a in anchors[:10]]
    except Exception as e:  # noqa: BLE001 — «не проверено», не отказ
        s.sig["errors"].append(f"deep:{type(e).__name__}")
        return
    # RD неизвестен в сигнале (W4 пишет его только непустым) — берём из состояния домена (БД/фид).
    # Неизвестен совсем (None) — это «не проверено», а не «доноров нет»: нулём считается только 0.
    rd = s.sig.get("referring_domains")
    rd = s.referring_domains if rd is None else rd
    if ratio is None and rd != 0:
        s.sig["errors"].append("deep:empty")        # доноры есть (или RD неизвестен), анкоров нет — не «чисто»
        return
    if script_only:
        # Язык прошлого сайта неизвестен, а отказ держится ТОЛЬКО на правиле скрипта: японский блог
        # на .com при упавшем LLM ушёл бы в вечную грязь. Не отказ, а «анкоры не проверены».
        s.sig["errors"].append("deep:lang_unknown")
        return
    s.sig["anchors"] = top
    s.sig["spam_anchor_ratio"] = ratio
    s.sig["spam_anchors"] = ratio is not None and ratio > st["spam_anchor_max"]
    s.sig["deep_checked"] = True
    if s.sig["spam_anchors"]:
        s.reject_reason, s.alive = "spam_anchors", False
        return                                      # спам-дроп: историю трафика не покупаем
    try:
        s.sig["peak_traffic"] = link_signals.peak_traffic(clients["ahrefs"].metrics_history(s.domain))
    except Exception as e:  # noqa: BLE001 — вердикт по анкорам уже есть и остаётся
        s.sig["errors"].append(f"deep_history:{type(e).__name__}")


def _wave_deep(states: list, clients: dict, st: dict, budget, run, notes: list | None = None) -> None:
    """W6 — дорогая проверка (~1,1 тыс. units) ТОЛЬКО для тех, кто уже набрал предварительный скор
    ≥ manual_review_at — РАНТАЙМ-порог из /settings (находка 2.8) — и только с проверенной историей
    (`wayback_checked`): на заведомо слабый домен и на домен «вслепую», которого пакет всё равно не
    возьмёт, units не тратим. Лучшие первыми — кап `max_deep_per_run` уходит на тех, кого реально решать. Перед
    волной — пол остатка units (Р3): ниже пола W6 не идёт, анкоры «не проверены». EMD пропускает
    (у новорега нет ссылок)."""
    if _no_ahrefs_key(clients):
        return                          # режим без ключа: W6 пропущена, анкоры «не проверены»
    cands = []
    for s in states:
        if not s.alive or s.source == "emd":
            continue
        s.sig["deep_checked"] = False
        if not s.sig.get("wayback_checked"):
            continue        # история не проверена — домен и так вне пакета, units на него не тратим
        if s.deep_cached:
            # анкоры уже оплачены (S2-07) — берём сохранённое, ~1,1 тыс. units повторно не тратим;
            # порог спама пересчитываем по ТЕКУЩЕЙ настройке
            c = s.deep_cached
            s.sig.update({"anchors": c.get("anchors"), "spam_anchor_ratio": c["spam_anchor_ratio"],
                          "peak_traffic": c.get("peak_traffic"), "deep_checked": True,
                          "spam_anchors": c["spam_anchor_ratio"] > st["spam_anchor_max"]})
            if s.sig["spam_anchors"]:
                s.reject_reason, s.alive = "spam_anchors", False
            continue
        pre = compute_score(dict(s.sig), st.get("weights"))
        if "hard_reject" not in pre["breakdown"] and pre["score"] >= st["manual_review_at"]:
            cands.append((pre["score"], s))
    # Сначала отбор под кап: при max_deep_per_run=0 W6 выключена — запроса units нет и «платные
    # волны пропущены» про выключенную волну не пишем.
    picked = []
    for _, s in sorted(cands, key=lambda x: -x[0]):
        if budget is not None and not budget.take():
            break
        picked.append(s)
    if not picked:
        return
    # СВЕЖИЙ запрос остатка (один на волну, не на домен): W4 уже потратила units, а решение гейта в
    # начале прогона (`_paid_gate`, кэш в clients["_paid_gate"]) устарело — `_units_below_floor` его не читает.
    low = _units_below_floor(clients, st)
    if low:
        if notes is not None:
            notes.append(low)
        return
    _run_concurrent(picked, _CONCURRENCY["deep"], run, "deep", lambda s: _deep_one(s, clients, st))


# Вердикт истории и его улики: колонки (prior_flags, wayback_checked) и ключи score_breakdown
# (history_evidence, sampled). Недочитанная история (wayback_checked=False) не перезаписывает их
# поверх сохранённой грязи — см. `keep_history` в _commit_result (I2).
_HISTORY_VERDICT_KEYS = frozenset({"prior_flags", "wayback_checked", "history_evidence", "sampled"})


def _commit_result(state: FunnelState, run, st: dict) -> dict:
    """Записать итог ОДНОГО FunnelState в БД: волны финализируют домен в момент его выхода из
    конвейера (см. _run_waves), не в конце одной функции.

    Открывает СВОЮ сессию — тот же паттерн, что и раньше: разные domain_id — разные
    строки, конкурентная запись безопасна."""
    from datetime import timezone
    from app.db import SessionLocal
    from app.models.domain import Domain
    from app.models.domain_score_log import DomainScoreLog

    sig, reject = state.sig, state.reject_reason
    with SessionLocal() as db:
        d = db.get(Domain, state.domain_id)
        if d is None or d.status not in ("discovered", "scored", "rejected"):
            return {"domain": state.domain, "status": d.status if d else "gone",
                    "skipped": "status"}

        # Оценка дедлайна домена, которого W2 перевела в bid по статусу RDAP (находка R2-11), —
        # и для решённого, и для unresolved исхода. Только в ПУСТУЮ колонку: реальную дату дропа
        # (её мог записать и параллельный discovery посреди прогона) оценка не перебивает.
        if sig.get("acquire_deadline") is not None and d.acquire_deadline is None:
            d.acquire_deadline = sig["acquire_deadline"]

        if state.unresolved_why is not None:
            # Лейн, который W2 уже определила (bid по статусу RDAP), пишем и здесь: платная W4 идёт
            # ПОСЛЕ W2 и может вернуть unresolved. Дедлайн-оценка выше без лейна оставила бы
            # lane=NULL + будущий дедлайн — `scorable` прятал бы домен до самого дропа. Пол/ключ
            # (`_paid_gate`) решаются ДО W2, у них sig["lane"] нет (тест пинит порядок волн).
            if sig.get("lane") is not None:
                d.lane = sig["lane"]
            if sig.get("acquirability_checked_at") and state.unresolved_why not in _PAID_UNRESOLVED:
                d.acquirability_checked_at = sig["acquirability_checked_at"]
            db.add(DomainScoreLog(domain_id=d.id, run_id=run, outcome="unresolved",
                                  reject_reason=None, score=None, sig=_jsonable(sig)))
            db.commit()
            return {"domain": d.domain, "status": d.status, "unresolved": True,
                    "why": state.unresolved_why, "errors": sig.get("errors", [])}

        if reject:
            result = {"score": 0.0, "status": "rejected", "breakdown": {"funnel_reject": reject}}
        elif state.source == "emd":
            # EMD — новорег: ни ссылок, ни трафика, скору не из чего складываться. Решение — за
            # человеком (спека §3.2): scored без балла, в инбоксе «EMD — решение за тобой», пакет
            # его не берёт (bulk_ok: балл пустой).
            result = {"score": None, "status": "scored", "breakdown": {"emd": True}}
        else:
            # F25 / 4.12: W4 пишет `dr`/`referring_domains` только непустыми — DR из discovery
            # (или с прошлого прогона) лежит в строке домена, и без setdefault compute_score
            # считал бы authority от 0.0, будто Ahrefs вообще не спрашивали. float(): `dr` —
            # Numeric, ORM отдаёт его как Decimal при чтении этой (свежей) строки — Decimal/
            # float в compute_score роняет TypeError.
            sig.setdefault("referring_domains", d.referring_domains)
            sig.setdefault("dr", float(d.dr) if d.dr is not None else None)
            # ранг этого прогона не получен (W-ranks не шла: «вслепую»-повтор, сбой/пустые ранги) — а
            # сводка прошлого прогона в breakdown остаётся (_kept): authority берём из неё же, иначе
            # балл сдвинется на 0.5·w без новых данных и breakdown противоречит сам себе
            if sig.get("rank_authority") is None:
                prev_av = ((d.score_breakdown or {}).get("rank") or {}).get("authority")
                if prev_av is not None:
                    sig["rank_authority"] = float(prev_av)
            result = compute_score(sig, st.get("weights"))
            if "hard_reject" not in result["breakdown"]:
                result = {**result, "status": _decide(result["score"], sig, st["manual_review_at"])}

        # СИГНАЛЫ ПИШЕМ ТОЛЬКО ИЗ ПРОВЕРОК, КОТОРЫЕ В ЭТОМ ПРОГОНЕ РЕАЛЬНО ОТРАБОТАЛИ —
        # НЕ blind overwrite. Воронка выходит рано на разных волнах (W0 не зовёт вообще
        # ничего, W2 — только RDAP/whois); риск/ссылки/Wayback/анкоры при таком выходе не
        # исполнялись, sig о них молчит. Безусловный `setattr` отсюда отмывал бы грязь:
        # домен, отклонённый за блэклист, после рескора терял бы ВСЕ улики (blacklisted=None) и
        # снова становился чистым для политики — кнопка реабилитации сработала бы не «по
        # новым уликам», а по их ОТСУТСТВИЮ. Отсутствие значения — «не проверяли», оно не
        # имеет права затирать то, что кто-то проверил (ревью Задачи 6, Critical 2).
        #
        # То же для ИСТОРИИ, прочитанной не до конца (I2 whole-branch ревью): archive.org троттлит,
        # прочитано меньшинство снимков — classify_history отдаёт prior_flags={} и
        # wayback_checked=False. Это «не знаем», а не «чисто», но значение не None, и цикл ниже
        # записал бы его поверх подтверждённого казино: dirty_reason -> None, «✓ Одобрить» открыта.
        # Путь, где Wayback бросил исключение, грязь сохраняет (этих ключей в sig нет вовсе) — два
        # входа в одну ситуацию обязаны вести себя одинаково. Поэтому при сохранённой грязи
        # вердикт истории и его улики не трогаем; свежая ПОЛНАЯ проверка (wayback_checked=True)
        # реабилитирует, как и раньше. Смотрим состояние строки ДО записи этого прогона.
        keep_history = sig.get("wayback_checked") is False and history_verdict(d) == "dirty"
        for col in ("lane", "whois_created", "acquirability_checked_at", "prior_flags",
                    "wayback_checked", "first_seen", "age_years", "blacklisted",
                    "dr", "referring_domains", "trademark_risk", "backlinks", "organic_traffic",
                    "market_lang", "topic", "topical_relevance", "anchors", "spam_anchor_ratio"):
            v = sig.get(col)
            if keep_history and col in _HISTORY_VERDICT_KEYS:
                continue
            if v is not None:
                setattr(d, col, v)
        if sig.get("topic_unknown"):
            # Исключение из правила «не затирать»: тему ЭТОТ прогон спрашивал, и ответа нет —
            # старая тема рядом с «тема не определена» врала бы (находка 4.5). Близость к VPN НЕ
            # стираем: перескор при лежащем LLM не вправе снять исключение «прошлая тема далека от
            # VPN» (тот же принцип «перескор не отмывает»). Язык не трогаем (у EMD он из набора).
            d.topic = None
        prev = d.score_breakdown or {}

        def _kept(key):
            """То же правило для УЛИК: снимок, который этот прогон не смотрел, не исчезает
            (fallback — ИМЕННО существующий score_breakdown, снятый ДО этого прогона).
            Иначе prior_flags (только что сохранённый выше) остался бы вердиктом без
            единого подтверждения: инбокс пишет «история грязная — смотри снимки», а
            смотреть нечего. Сохранённая грязь при недочитанной истории (I2, `keep_history`) —
            улики прошлой полной проверки остаются при её вердикте, частичные их не подменяют."""
            v = None if keep_history and key in _HISTORY_VERDICT_KEYS else sig.get(key)
            return v if v is not None else prev.get(key)

        d.score_breakdown = {**result["breakdown"], "errors": sig.get("errors", []),
                             "ref_subnets": _kept("ref_subnets"),
                             "rd_dofollow": _kept("rd_dofollow"),
                             "history_evidence": _kept("history_evidence") or [],
                             "sampled": _kept("sampled"),
                             "age_source": _kept("age_source"),
                             "whois_source": _kept("whois_source"),
                             "webrisk_threats": _kept("webrisk_threats"),
                             "list_hits": _kept("list_hits"),
                             "rank": _kept("rank"),
                             "topic_unknown": sig.get("topic_unknown"),
                             "parked_share": _kept("parked_share"),
                             "deep_checked": sig.get("deep_checked"),
                             "spam_anchors": _kept("spam_anchors"),
                             "peak_traffic": _kept("peak_traffic")}
        d.status = result["status"]
        d.reject_reason = reject or ("low_score" if result["status"] == "rejected" else None)
        # «Перескор не отмывает» (S2-08): улики грязи (сохранённые выше `keep_history`/`_kept`,
        # свежие колонки) судит ЕДИНЫЙ предикат dirty_reason — по НОВОМУ reject_reason, чтобы старый
        # отказ не держал домен вечно (реабилитирует только полная чистая проверка). Если грязь
        # осталась, а проверка сломалась и дала «scored», домен остаётся rejected с причиной — и не
        # всплывает в инбоксе «на решении».
        from app.services.transitions import dirty_reason
        dirt = dirty_reason(d)
        if dirt and result["status"] != "rejected":
            result = {**result, "status": "rejected", "score": 0.0,
                      "breakdown": {**result["breakdown"], "funnel_reject": dirt}}
            d.status, d.reject_reason = "rejected", dirt
            d.score_breakdown = {**d.score_breakdown, "funnel_reject": dirt}
        d.clean = result["status"] != "rejected"
        d.score = result["score"]
        # F24: когда домен ПОСЛЕДНИЙ РАЗ прошёл воронку ДО РЕШЕНИЯ — unresolved-возврат
        # выше оставляет домен discovered (воронка НЕ дошла до решения, значит и не
        # "оценила" его), поэтому эта отметка ставится только на пути ниже.
        d.scored_at = datetime.now(timezone.utc)
        db.add(DomainScoreLog(
            domain_id=d.id, run_id=run,
            outcome="rejected" if result["status"] == "rejected" else "scored",
            reject_reason=d.reject_reason, score=result["score"], sig=_jsonable(sig)))
        db.commit()
        return {"domain": d.domain, **result, "reject_reason": d.reject_reason,
                "errors": sig.get("errors", [])}


def _checkpoint(states: list, run, st: dict) -> list:
    """Финализировать в БД тех, кто вышел из конвейера НА ЭТОЙ волне (reject_reason ИЛИ
    unresolved_why выставлены), вернуть список результатов _commit_result. Выжившие
    (state.alive) остаются в states — вызывающий сам передаёт их в следующую волну."""
    out = []
    for s in states:
        if not s.alive:
            out.append(_commit_result(s, run, st))
    return out


def _llm_note(clients: dict, notes: list) -> None:
    """Видимая причина, почему тема W5 не определяется (403 tier / 429 / пустой reasoning)."""
    for msg in clients.get("_llm_errors") or []:
        note = f"LLM-тема: {msg}"
        if note not in notes:
            notes.append(note)


def _wayback_note(states: list, notes: list) -> None:
    """Видимая причина «ждут следующего прогона», если архив лёг (WaybackUnavailable): иначе
    домены тихо уходят unresolved без единого слова в сообщении задачи."""
    n = sum(1 for s in states if s.unresolved_why == "wayback_down")
    notes[:] = [x for x in notes if not x.startswith("archive.org недоступен")]
    if n:
        notes.append(f"archive.org недоступен — {n} доменов ждут следующей проверки")


def _run_waves(states: list, clients: dict, st: dict, whois_budget, links_budget,
               run, notes: list | None = None, deep_budget=None) -> list:
    """Оркестратор: волны по порядку дёшево->дорого, между каждой — checkpoint (коммит
    вышедших, отчёт волновой истории), отмена проверяется между волнами (внутри волны —
    в _run_concurrent). Волны — таблица `waves` (ключ чипа, подпись водопада, функция) в
    порядке FUNNEL_STAGES; цикл один на всех. Выжившие после ПОСЛЕДНЕЙ волны финализируются
    как решённые — см. _commit_result. Возвращает результаты в порядке завершения (порядок
    не важен вызывающим — score_pending считает только длину, score_domain — единственный
    элемент списка). `notes` — пояснения волн к водопаду («остаток units ниже пола»): сообщение
    задачи переписывается после каждой волны, и сказанное волной иначе стёрлось бы; список
    вызывающего (score_pending) — чтобы пояснение дожило и до итогового сообщения. `deep_budget` —
    кап W6 (None — без капа: ручная перепроверка одного домена)."""
    from app.services import jobs

    whois_b = whois_budget if whois_budget is None or hasattr(whois_budget, "take") \
        else _ListBudget(whois_budget)
    links_b = links_budget if links_budget is None or hasattr(links_budget, "take") \
        else _ListBudget(links_budget)
    deep_b = deep_budget if deep_budget is None or hasattr(deep_budget, "take") \
        else _ListBudget(deep_budget)
    notes = [] if notes is None else notes

    # (ключ чипа, подпись в водопаде, волна). Порядок = порядок FUNNEL_STAGES — один источник
    # правды: новая волна добавляется ОДНОЙ строкой здесь и одной в FUNNEL_STAGES.
    waves = [
        # W0 и сразу решение «пойдут ли платные волны» (R2-10) — до того, как W2/W3 потратятся
        ("t0", "зона и бренд", lambda alive: (_wave_t0(alive, st), _paid_gate(alive, clients, st, notes))),
        ("avail", "не занят ли", lambda alive: _wave_avail(alive, clients, whois_b, st, run)),
        ("risk", "чёрные списки", lambda alive: (_wave_risk(alive, clients, run, notes), _wave_lists(alive, st),
                                       _wave_ranks(alive, st))),
        # дешёвая проба архива ДО платной W4 (S2-06): too_young по CDX не должен стоить 25 units
        ("probe", "возраст", lambda alive: _wave_probe(alive, clients, st, run)),
        ("links", "ссылки", lambda alive: (_wave_links(alive, clients, st, links_b, run, notes),
                                           _persist_links(alive))),
        ("history", "история и тема", lambda alive: (_wave_history(alive, clients, st, run),
                                              _llm_note(clients, notes),
                                              _wayback_note(states, notes))),
        ("deep", "спам-ссылки", lambda alive: _wave_deep(alive, clients, st, deep_b, run, notes)),
    ]
    results, waterfall, alive = [], [], list(states)
    for i, (key, label, wave) in enumerate(waves):
        before = len(alive)
        wave(alive)
        if jobs.cancelled(run):
            raise jobs.Cancelled()
        if i < len(waves) - 1:
            results += _checkpoint(alive, run, st)
            alive = [s for s in alive if s.alive]
            after = len(alive)
        else:
            # ПОСЛЕДНЯЯ волна: финализируем ВСЕХ, кто в неё вошёл. _commit_result сам различает
            # отказ / unresolved / скор — вышедшие на ней и выжившие решаются одним путём.
            results += [_commit_result(s, run, st) for s in alive]
            after = sum(1 for s in alive if s.alive)
        # та же подпись и у последней волны: «N решено» считало бы только выживших
        skipped_paid = key in ("links", "deep") and _no_ahrefs_key(clients)
        waterfall.append(f"{label}: пропущено (нет ключа Ahrefs)" if skipped_paid
                         else f"{label}: {before} → {after}")
        jobs.report(run, message=" · ".join(waterfall + notes),
                    stage_key=key, stage_before=before, stage_after=after)
    return results


if __name__ == "__main__":  # pure-function self-check (no I/O)
    # чистый старый домен с хорошими ссылками -> scored (одобряет только человек, Р2)
    clean = compute_score({"wayback_checked": True, "prior_flags": {}, "dr": 20.0,
                           "age_years": 10, "referring_domains": 800, "ref_subnets": 600})
    assert clean["status"] == "scored", clean
    # казино в истории -> жёсткий отказ
    dirty = compute_score({"wayback_checked": True, "prior_flags": {"casino": True},
                           "dr": 9.0, "age_years": 15, "referring_domains": 500})
    assert dirty["status"] == "rejected" and dirty["score"] == 0.0, dirty
    # угроза Web Risk -> жёсткий отказ при любом качестве
    risky = compute_score({"webrisk_threats": ["MALWARE"], "dr": 40.0, "age_years": 12,
                           "referring_domains": 2000, "wayback_checked": True, "prior_flags": {}})
    assert risky["status"] == "rejected", risky
    # пусто/неизвестно -> низкий балл, отказ
    empty = compute_score({})
    assert empty["status"] == "rejected", empty
    # ИНВАРИАНТ (Р2): никакой сигнал не даёт `approved` — даже непроверенная история с огромным RD
    unverified = compute_score({"referring_domains": 5000, "wayback_checked": False,
                                "prior_flags": {}})
    assert unverified["status"] != "approved", unverified
    # веса в сумме 1.0
    assert abs(sum(cfg.WEIGHTS.values()) - 1.0) < 1e-9
    print("scoring compute_score ok:", clean["score"], dirty["score"], risky["score"], empty["score"])
