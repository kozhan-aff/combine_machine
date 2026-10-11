"""Страж простых подписей: на экране панели нет внутренних слов машины.

Панелью пользуется оператор, не разработчик. Коды модулей (M1…M6) и волн (W0…W6), сырые статусы
(`scored`, `approved`…), «провижн», «свип», «гейт» и подобное — язык кода, а не экрана. Этот тест
рендерит экраны на базе, где занято КАЖДОЕ состояние (все статусы доменов и заказов, все причины
отказа, «вслепую», грязь, пустые и непустые списки), и ищет стоп-слова в ВИДИМОМ тексте: теги и
атрибуты вырезаны, `<script>`, `<style>` и `<code>` исключены (в `<code>` живут имена ключей — по
ним ищут в логах).

Под стражей все экраны панели. Новый экран — одна строка в SCREENS (и его состояния — в сид).
Сид заводит сайты №1–№4 и страницы №1–№13 во всех состояниях карточки сайта и редактора.

Кроме экранов сторожим то, что на экран приезжает из кода: флеш-сообщения действий, общие словари
подписей и вообще все русские строки сервисов (`test_python_messages_are_plain`) — причина отказа,
написанная в сервисе, доезжает до оператора флешем, строкой журнала или колонкой «результат».
"""
import ast
import re
import time
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit

import pytest

import app.db as db
from app.models.autonomy import AutonomyRun
from app.models.cloudflare import (CloudflareAccount, CloudflareConnection, CloudflareConnectionAccount,
                                   CloudflareZoneMirror)
from app.models.domain import AcquisitionOrder, Domain
from app.models.domain_list import DomainList
from app.models.job import JobRun
from app.models.offer import Offer, SiteOffer
from app.models.research import SiteResearch
from app.models.site import Page, Site

N_PAGES = 13          # столько страниц заводит сид: редактор каждой — отдельный экран под стражей

# Экраны под стражей.
SCREENS = [
    "/",                                  # Пульт
    "/domains",                           # Домены
    "/domains?lang=pl",                   # Домены: фильтр по языку (пакет скрыт)
    "/domains/pool",                      # Весь список
    "/domains/pool?show_all=1",           # Весь список: вместе с занятыми
    "/domains/pool?status=rejected",      # Весь список: отклонённые со всеми причинами
    "/queue",                             # Покупка
    "/sites/1",                           # карточка сайта: ещё не поднят, ждёт NS, оффера нет
    "/sites/2",                           # поднят с ошибкой HTTPS, конкурентов не изучали, текстов нет
    "/sites/3",                           # пишутся тексты: разбор конкурентов, все вердикты критика
    "/sites/4",                           # опубликован: индексация во всех состояниях, оффер выключен
    *[f"/pages/{i}" for i in range(1, N_PAGES + 1)],     # редактор страницы
    "/offers",                            # Офферы
    "/guides",                            # Правила письма
    "/guides/digest/style.md",            # выжимка одного файла правил
    "/settings",                          # Настройки: пороги и источники
    "/settings/keys",                     # Ключи и доступы
    "/settings/keys?msg=ok",              # …сразу после сохранения
    "/settings/cloudflare",               # Cloudflare
    "/autopilot",                         # Автопилот
    "/diag",                              # Диагностика
]

# (регулярка, что это). Коды модулей и волн — с учётом регистра (иначе ловили бы домен m1.com);
# слова — без учёта регистра.
STOP = [
    (re.compile(r"\b[MМ][1-6]\b"), "код модуля M1…M6"),
    (re.compile(r"\bW[0-6]\b"), "код волны W0…W6"),
    (re.compile(r"\b(draft|edited|published|approved|purchased|scored)\b", re.I), "сырой статус"),
    (re.compile(r"provision", re.I), "provision"),
    (re.compile(r"провижн", re.I), "провижн"),
    (re.compile(r"свип", re.I), "свип"),
    (re.compile(r"\bкап за\b", re.I), "кап за"),
    (re.compile(r"гейт", re.I), "гейт"),
    (re.compile(r"скоринг", re.I), "скоринг"),
    (re.compile(r"воркер", re.I), "воркер"),
    (re.compile(r"дефолт", re.I), "дефолт"),
    (re.compile(r"\bvhost\b", re.I), "vhost"),
    (re.compile(r"\bdocroot\b", re.I), "docroot"),
]

# Только для экранов: остальные внутренние слова из правил подписей. В строках кода их не ищем —
# там они законны (задание модели, журнал), а до экрана доезжают лишь тексты, которые сторожит STOP.
SCREEN_STOP = STOP + [(re.compile(rx, re.I), what) for rx, what in (
    (r"досье", "досье"), (r"шедулер", "шедулер"), (r"throttle", "throttle"), (r"bulk", "bulk-pull"),
    (r"\bфид(а|е|у|ом|ы)?\b", "фид"), (r"\bкап(а|у|ом|ы|ов)?\b", "кап"), (r"disclosure", "disclosure"),
    (r"footprint", "footprint"), (r"идемпотент", "идемпотентно"), (r"read-only", "read-only"),
    (r"\bsync\b", "sync"), (r"\bdrift\b", "drift"), (r"инбокс", "инбокс"))]
# Аббревиатуры на экране — только в скобках после слов: «ссылающихся сайтов (RD)». С учётом регистра:
# бейдж источника `emd` и «Domain Rating» — не они.
SCREEN_STOP.append((re.compile(r"\b(RD|DR|EMD)\b"), "аббревиатура без скобок"))
_BRACKETED = re.compile(r"\((RD|DR|EMD)\)")
HINT_ATTRS = ("title", "placeholder", "onsubmit")       # подсказки и диалоги — такой же текст для человека


class _Visible(HTMLParser):
    """Текст, который видит человек: без тегов и без содержимого script/style/code. Вырезаются только
    ЗАКРЫТЫЕ пары: незакрытый <code> не прячет от стража остаток страницы. Отдельно собираются тексты
    подсказок и диалогов (HINT_ATTRS) — их человек читает так же, как подписи."""
    _SKIP = {"script", "style", "code"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.hints = [], []
        self._open, self._held = [], []      # открытые вырезаемые теги и текст внутри них — до закрытия

    def handle_starttag(self, tag, attrs):
        self.hints += [v for k, v in attrs if k in HINT_ATTRS and v]
        if tag in self._SKIP:
            self._open.append(tag)

    def handle_endtag(self, tag):
        if tag in self._open:
            while self._open.pop() != tag:
                pass
            if not self._open:
                self._held.clear()

    def handle_data(self, data):
        (self._held if self._open else self.parts).append(data)

    def close(self):
        super().close()
        self.parts += self._held             # пара так и не закрылась — это видимый текст
        self._held = []


def _parsed(html: str) -> _Visible:
    p = _Visible()
    p.feed(html)
    p.close()
    return p


def visible_text(html: str) -> str:
    return re.sub(r"\s+", " ", " ".join(_parsed(html).parts))


def hint_texts(html: str) -> list[str]:
    """Тексты подсказок, плейсхолдеров и диалогов подтверждения."""
    return _parsed(html).hints


def stop_hits(text: str, screen: bool = False) -> list[str]:
    """Найденные стоп-слова с куском текста вокруг — чтобы по сообщению теста было видно, где.
    `screen` — текст экрана: к нему список строже (SCREEN_STOP), аббревиатуры допустимы только в скобках."""
    out = []
    if screen:
        text = _BRACKETED.sub("", text)
    for rx, what in (SCREEN_STOP if screen else STOP):
        for m in rx.finditer(text):
            out.append(f"{what}: …{text[max(0, m.start() - 40):m.end() + 40]}…")
    return out


def screen_hits(html: str) -> list[str]:
    """Стоп-слова экрана: в видимом тексте и в подсказках, плейсхолдерах, диалогах подтверждения."""
    out = stop_hits(visible_text(html), screen=True)
    for text in hint_texts(html):
        out += [f"[подсказка] {hit}" for hit in stop_hits(text, screen=True)]
    return out


# --- сид: каждое состояние экранов -------------------------------------------------------------

def _now() -> datetime:
    """«Сейчас» берётся в момент теста, а не при импорте модуля: к концу долгого прогона «свежая»
    отметка из импорта успела бы устареть (сердцебиение, срок подтверждения, снимок диагностики)."""
    return datetime.now(timezone.utc)


CHECKED = {"errors": [], "deep_checked": True, "history_evidence": [
    {"ts": "20190101000000", "url": "http://example.com/", "cats": []}]}
REJECT_REASONS = ["low_rd", "feed_flag", "too_young", "rkn", "blacklist", "history_dirty", "low_score",
                  "not_acquirable", "safebrowsing", "tld_closed", "trademark", "spam_anchors",
                  "legacy_ru", "list_hit", None]


class _Registrar:
    """Настроенный регистратор без сети: одному домену называет цену, другому — нет."""
    name, configured = "namesilo", True

    def check_many(self, names):
        return {n: {"status": "available", "price": 2.79, "premium": 0} for n in names if "quote" in n}

    def balance(self):
        from app.integrations import registrar
        return registrar.Money(100.0, "USD")


DEPLOY_OK = {"branch": "main", "hash": "abc1234", "subject": "fix", "date": "2026-10-11", "dirty": False,
             "ahead": 0, "behind": 0, "detached": False}


def _seed_diag(monkeypatch, **deploy_state) -> None:
    """Диагностика без сети: каждая настоящая роль сервиса — в каждом статусе, словами самой проверки
    (`_run_one` и `run_diagnostics` зовём настоящие, подменяем только сам поход в сеть)."""
    from app.services import deploy, diag_cache, diagnostics
    NOW = _now()

    def down():
        raise RuntimeError("connection refused")

    rows = []
    for key, label, role, _cred, module, critical, _fn in diagnostics._spec():
        rows.append(diagnostics._run_one(key, label, role, "1", module, critical, lambda: True))
        rows.append(diagnostics._run_one(key, label, role, "1", module, critical, down))
        rows.append(diagnostics._run_one(key, label, role, "", module, critical, down))      # ключа нет
    monkeypatch.setattr(diagnostics, "PING_TIMEOUT", 0.05)
    rows += diagnostics.run_diagnostics([("slow", "Slow", "медленный сервис", "1", "M1", False,
                                          lambda: time.sleep(0.2))])
    monkeypatch.setattr(diag_cache, "_checks", rows)
    monkeypatch.setattr(diag_cache, "_checked_at", NOW)
    monkeypatch.setattr(deploy, "deploy_status", lambda: {**DEPLOY_OK, **deploy_state})


@pytest.fixture(autouse=True)
def _offline_screens(tmp_path, monkeypatch):
    """Экраны без сети и без чужих файлов: пустой снимок диагностики (иначе /diag пошёл бы опрашивать
    сервисы) и своя папка правил письма (иначе экран читал бы настоящие файлы оператора)."""
    from app.config import settings
    from app.services import deploy, diag_cache, guides
    NOW = _now()
    monkeypatch.setattr(diag_cache, "_checks", [])
    monkeypatch.setattr(diag_cache, "_checked_at", NOW)
    monkeypatch.setattr(deploy, "deploy_status", lambda: dict(DEPLOY_OK))
    folder = tmp_path / "guides"
    folder.mkdir()
    monkeypatch.setattr(settings, "CONTENT_GUIDES_DIR", str(folder))
    for name in ("style.md", "tone.txt", "moved.md", "unused.md"):
        (folder / name).write_text("Пиши коротко и по делу.\n", encoding="utf-8")
    guides.save_digest("style.md", "— пиши коротко")           # выжимка есть, правлена оператором
    guides.save_digest("moved.md", "— старое правило")
    (folder / "moved.md").write_text("Файл переписали после сжатия.\n", encoding="utf-8")   # выжимка устарела
    guides.set_role("unused.md", "skip")                         # «не использовать»
    return folder


def seed_panel(monkeypatch) -> None:
    """Заполнить базу так, чтобы экраны отрисовали все свои ветки. Имена доменов нейтральные:
    стоп-слово в имени домена было бы ложной находкой."""
    from app.integrations import registrar
    from app.services import content_critic, heartbeat, research
    from app.services.scoring import FUNNEL_STAGES
    monkeypatch.setattr(registrar, "get_registrar", lambda: _Registrar())
    _seed_diag(monkeypatch)
    NOW = _now()
    soon, past = NOW + timedelta(days=2), NOW - timedelta(days=30)
    clean = dict(wayback_checked=True, prior_flags={}, age_years=9.0, score_breakdown=CHECKED)
    with db.SessionLocal() as s:
        own = Offer(brand="TestVPN", affiliate_link="https://example.com/a", promo_code="T10",
                    promo_terms="-10%", country="PL", language="pl", active=True)
        off = Offer(brand="OldVPN", affiliate_link="https://example.com/old", promo_code="OLD", active=False)
        plain = Offer(brand="PlainVPN", affiliate_link="https://example.com/p", active=True)
        s.add_all([own, off, plain])
        s.flush()

        def dom(name, **kw):
            d = Domain(domain=name, **{"source": "nominet", **kw})
            s.add(d)
            s.flush()
            return d.id

        dom("found-a.com")                                                            # найден
        # ждут решения: чистый срочный, «вслепую» всех видов, новый из ключевых слов, далёкая тема,
        # список чистоты, грязный, упущенный дроп, зона не из списка
        dom("wait-b.com", status="scored", score=0.82, dr=33, referring_domains=410, lane="bid",
            market_lang="pl", topic="VPN and privacy blog", topical_relevance=0.8,
            acquire_deadline=soon, acquire_price=20, **clean)
        dom("blind-c.com", status="scored", score=0.9, score_breakdown={"errors": ["wayback:ConnectError"]})
        dom("whois-d.com", status="scored", score=0.7, wayback_checked=True, prior_flags={}, age_years=8.0,
            score_breakdown={"errors": ["whois:RuntimeError"], "history_evidence": [], "age_source": "wayback"})
        dom("ahrefs-d2.com", status="scored", score=0.7, wayback_checked=True, prior_flags={}, age_years=8.0,
            score_breakdown={"errors": ["ahrefs:Timeout"], "history_evidence": []})
        dom("webrisk-d3.com", status="scored", score=0.7, wayback_checked=True, prior_flags={}, age_years=8.0,
            score_breakdown={"errors": ["webrisk:unconfigured"], "history_evidence": []})
        dom("lists-d4.com", status="scored", score=0.7, wayback_checked=True, prior_flags={}, age_years=8.0,
            score_breakdown={"errors": ["blacklist:unavailable"], "history_evidence": []})
        dom("links-e.com", status="scored", score=0.8, wayback_checked=True, prior_flags={}, age_years=9.0,
            score_breakdown={"errors": [], "deep_checked": False})
        dom("mejorvpn-f.com", source="emd", status="scored", score=None, market_lang="es",
            score_breakdown={"emd": True, "errors": [], "sampled": 0, "history_evidence": []})
        dom("far-g.com", status="scored", score=0.6, topic="Cooking recipes", topical_relevance=0.1,
            market_lang="pl", **clean)
        dom("list-h.com", source="list", status="scored", score=0.6, acquire_deadline=soon,
            wayback_checked=True, prior_flags={}, age_years=9.0,
            score_breakdown={**CHECKED, "list_hits": ["casino"]})
        dom("dirty-i.com", status="scored", score=0.95, rkn_listed=True, wayback_checked=True)
        dom("gone-j.com", status="scored", score=0.9, lane="bid", acquire_deadline=past, **clean)
        dom("zone-k.ru", status="scored", score=0.9, **clean)
        dom("note-k2.com", status="scored", score=0.7, wayback_checked=True, prior_flags={}, age_years=9.0,
            score_breakdown={"errors": ["wayback:ReadTimeout"], "deep_checked": True})
        dom("casino-k3.com", status="scored", score=0.5, wayback_checked=True, prior_flags={"casino": True},
            blacklisted=True)
        # одобрены: чистый, «отмытый» грязный, упущенный
        dom("ok-l.com", status="approved", score=0.9, lane="free", acquire_price=12,
            acquire_deadline=soon, topic="VPN deals", topical_relevance=0.9, **clean)
        dom("washed-m.com", status="approved", score=0.9, reject_reason="rkn", rkn_listed=True)
        dom("late-m2.com", status="approved", score=0.9, lane="bid", acquire_deadline=past, **clean)
        dom("bought-n.com", status="purchased")                                       # куплен, сайта нет
        for i, reason in enumerate(REJECT_REASONS):                                   # все причины отказа
            dom(f"no-{i}.com", status="rejected", reject_reason=reason, score=0.3)
        dom("weak-o.ru", status="rejected", reject_reason="low_score", score=0.3)     # зона не из списка

        def order(name, provider, status="pending_confirm", source="nominet", ask=None, dirty=False, **kw):
            did = dom(name, status="purchasing", source=source, acquire_price=ask,
                      **({"reject_reason": "rkn", "rkn_listed": True} if dirty else {}))
            s.add(AcquisitionOrder(domain_id=did, provider=provider, status=status, **kw))

        order("bo-p.ru", "backorder")                                                  # ставка backorder
        order("auction-q.com", "registrar", source="namesilo_auction", ask=20.0)        # аукцион
        order("quote-r.com", "registrar")                                              # цена известна
        order("silent-s.com", "registrar")                                             # цены нет
        order("opt-t.ru", "optimizator")                                               # фикс. цена
        order("sure-u.ru", "backorder", confirmed_by_human=True, confirmed_at=NOW, cost=400)
        order("old-v.ru", "backorder", confirmed_by_human=True, confirmed_at=NOW - timedelta(days=3), cost=400)
        order("retry-w.ru", "backorder", status="failed", confirmed_by_human=True, confirmed_at=NOW,
              cost=400, result={"maybe_sent": True, "error": "связь оборвалась"})
        order("lost-x.ru", "backorder", status="failed", result={"maybe_sent": True})
        order("going-y.ru", "backorder", status="ordering", confirmed_by_human=True, confirmed_at=NOW,
              claimed_at=NOW)
        order("stuck-z.ru", "backorder", status="ordering", confirmed_by_human=True, confirmed_at=NOW,
              claimed_at=NOW - timedelta(hours=5))
        order("sent-aa.ru", "backorder", status="ordered", confirmed_by_human=True, confirmed_at=NOW,
              cost=400, result={"clear_status": "В работе", "auction_ceiling": 40.0})
        order("got-ab.ru", "backorder", status="caught", confirmed_by_human=True, confirmed_at=NOW, cost=400)
        order("off-ac.ru", "backorder", status="cancelled")
        order("bad-ad.ru", "backorder", dirty=True)                                    # грязный в покупке

        def page(site_id, path, pstatus, notes=None, **extra):
            p = Page(site_id=site_id, url_path=path, title=path, body="<p>text</p>", status=pstatus, **extra)
            if notes is not None:            # вердикт критика к нынешнему тексту: с отпечатком, как пишет критик
                p.critic_notes = {**notes, "fp": notes.get("fp") or content_critic.fingerprint(p.title, p.body)}
                p.critic_checked_at = NOW
            s.add(p)
            s.flush()

        def site(name, status, pages=(), **kw):
            st = Site(domain_id=dom(name, status="purchased"), status=status, **kw)
            s.add(st)
            s.flush()
            for path, pstatus, extra in pages:
                page(st.id, path, pstatus, **extra)
            return st.id

        # №1: ещё не поднят — зона ждёт NS у регистратора, оффер не выбран
        site("site-one.com", "provisioning", provision_step="await_ns",
             cf_name_servers=["a.ns.example", "b.ns.example"], ns_waiting_since=NOW)
        # №2: поднят, но HTTPS в Cloudflare не встал; оффер только привязан; конкурентов не изучали
        two = site("site-two.com", "content", provision_step="done", origin_https="none",
                   ssl_error="RuntimeError: Cloudflare 403: Authentication error")
        s.add(SiteOffer(site_id=two, offer_id=plain.id))
        # №3: тексты написаны под прежний оффер; страницы №1–№2 и ниже №7–№12 — все вердикты критика
        three = site("site-three.com", "content", [("/", "draft", {"offer_id": plain.id}), ("/vpn", "edited", {})],
                     provision_step="done", origin_https="ok", offer_id=own.id)
        s.add_all([SiteOffer(site_id=three, offer_id=own.id), SiteOffer(site_id=three, offer_id=plain.id)])
        # №4: опубликован; оффер сайта выключен; индексация — все четыре состояния
        four = site("site-four.com", "published", [
            ("/", "published", {"index_status": "indexed", "index_checked_at": NOW, "offer_id": off.id}),
            ("/a", "published", {}),                                                   # не проверяли
            ("/b", "published", {"index_checked_at": NOW}),                            # проверить не удалось
            ("/c", "published", {"index_status": "not_indexed", "index_checked_at": NOW})],
            provision_step="done", origin_https="origin_ca", cf_zone_id="zone4", offer_id=off.id)
        passed = {"pass": True, "issues": [], "code": [], "model": [], "round": 0}
        page(three, "/c-pass", "draft", passed, critic_score=0.9)
        page(three, "/c-note", "draft", {**passed, "note": content_critic._MANUAL})
        page(three, "/c-issues", "draft", critic_score=0.4, notes={
            "pass": False, "issues": ["объём 300 слов, нужно 1500–2200", "вода"], "round": 1,
            "code": ["объём 300 слов, нужно 1500–2200", content_critic._NO_DOSSIER], "model": ["вода"],
            "advice": ["добавь таблицу цен"]})
        page(three, "/c-error", "draft", {"pass": False, "issues": ["критик не ответил"], "code": [],
                                          "model": ["критик не ответил"], "error": "ReadTimeout"})
        page(three, "/c-stale", "draft", {**passed, "fp": "0" * 16})                  # текст правили после вычитки
        page(three, "/c-old", "draft", critic_checked_at=NOW)                         # вычитывали, вердикта нет
        page(four, "/rewritten", "draft", published_at=NOW)                           # переписана, на сайте прежняя
        assert s.query(Page).count() == N_PAGES

        for i, kind in enumerate(("review", "comparison", "howto", "market")):        # разбор конкурентов
            s.add(SiteResearch(site_id=three, kind=kind, query=f"q {kind}", rank=i + 1, url=f"https://c{i}.example/",
                               domain=f"c{i}.example", words=900, headings=[["h2", "Price"]],
                               screenshot_path="/r/1.png" if i == 0 else None,
                               note="страница не открылась" if i == 3 else None))

        # журнал автопилота: все статусы прохода, все счётчики, ошибки словами сервисов
        from app.services.orchestrator import COUNT_RU, STAGE_RU
        for status, trigger, counts, errors in (
                ("done", "cron", dict.fromkeys(COUNT_RU, 1), []),
                ("completed_with_errors", "manual", {"score": 2, "что-то новое": 1},
                 ["сайт #2: конкуренты не найдены — " + research._EMPTY_REASON]),
                ("failed", "cron", {}, [f"{STAGE_RU['provision']}: RuntimeError: aaPanel недоступен"]),
                ("cancelled", "cron", {}, ["проход остановлен — кнопкой «Остановить» или его перехватил другой процесс"]),
                ("running", "manual", {}, [])):
            s.add(AutonomyRun(started_at=NOW, finished_at=NOW, trigger=trigger, status=status, counts=counts,
                              errors=errors))

        # Cloudflare: подключения во всех статусах, зоны во всех состояниях
        conns = [CloudflareConnection(label=f"conn-{st}", secret_ref="env:CLOUDFLARE_API_TOKEN", token_kind="user",
                                      status=st, last_error_safe=err)
                 for st, err in (("ok", None), ("error", "401 Unauthorized"), ("warn", "token expires soon"),
                                 ("unverified", None))]
        s.add_all(conns)
        s.flush()
        s.add(CloudflareConnectionAccount(connection_id=conns[0].id, cloudflare_account_id="acc1", status="ok",
                                          capabilities_json={"zones_read": "allowed", "dns_write": "denied",
                                                             "ssl_read": "unknown"}))
        s.add(CloudflareAccount(cf_account_id="acc1", name="Main account"))
        s.add(CloudflareZoneMirror(cloudflare_account_id="acc1", cf_zone_id="zone4", name="site-four.com",
                                   status="active", universal_ssl_status="active", origin_tls_status="ready",
                                   name_servers_json=["a.ns.example"], zones_seen_at=NOW))
        s.add(CloudflareZoneMirror(cloudflare_account_id="acc-x", cf_zone_id="z2", name="wait.com", status="pending",
                                   missing_since=NOW, origin_tls_status="failed", dns_error_safe="timeout"))
        for i, st in enumerate(("initializing", "moved", "deleted", "unknown")):
            s.add(CloudflareZoneMirror(cloudflare_account_id="acc1", cf_zone_id=f"zs{i}", name=f"zone-{i}.com",
                                       status=st))
        s.add(DomainList(domain="casino-k3.com", category="gambling", source="ut1"))

        # итоги запусков — словами самой машины: подписи шагов берём из её словаря
        waterfall = " · ".join(f"{st['label']}: 10 → 5" for st in FUNNEL_STAGES)
        for name, status, kw in (
                ("discovery", "done", {"message": "найдено 12, новых 3"}),
                ("score", "failed", {"error": "RuntimeError: A-Parser timeout", "message": waterfall}),
                ("recheck", "cancelled", {"done": 3, "total": 10}),
                ("generate", "done", {"message": "написано страниц: 4"}),
                ("edit", "done_warn", {"message": "вычитано 3, одобрено 2"}),
                ("sweep", "done", {"message": "шагов пройдено: 9"}),
                ("research", "done_warn", {"message": research._EMPTY_REASON}),
                ("domain_lists", "failed", {"error": "HTTPError: 503"}),
                ("domain_ranks", "done_warn", {"message": "Common Crawl: 3 из 40"}),
                ("guides_digest", "done", {"message": "сжато файлов: 2"})):
            s.add(JobRun(name=name, status=status, started_at=NOW, updated_at=NOW, finished_at=NOW, **kw))
        s.commit()
    heartbeat.beat(False)                                                             # фоновый процесс отозвался


# --- сам страж ---------------------------------------------------------------------------------

def test_guard_is_not_blind():
    """Страж обязан ловить то, ради чего заведён, и не трогать то, что исключено по правилам."""
    assert stop_hits("Домены · M1") and stop_hits("волна W4") and stop_hits("статус approved")
    assert stop_hits("кап за свип") and stop_hits("денежный гейт") and stop_hits("запусти Provision")
    assert stop_hits("воркер: жив") and stop_hits("сбросить к дефолтам") and stop_hits("vhost + docroot")
    assert not stop_hits("капать за шиворот") and not stop_hits("домен m1.com куплен, оценка 0.8")
    for word in ("досье конкурентов", "кап whois", "капа нет — bulk-pull всего фида", "чистый footprint",
                 "обязательный disclosure", "идемпотентно", "read-only sync", "общий drift", "инбокс", "шедулер"):
        assert stop_hits(word, screen=True) and not stop_hits(word), word      # строже — только для экранов
    assert not stop_hits("капля, фидер, синхронно", screen=True)
    html = ('<p title="код: low_rd, статус scored">занят <code>AHREFS_API_KEY scored</code></p>'
            '<script>const M1 = "provision";</script><style>.b-approved{}</style>')
    assert visible_text(html).strip() == "занят"
    # подсказка — тоже текст для человека: сырой статус в `title` страж видит
    assert hint_texts(html) == ["код: low_rd, статус scored"] and screen_hits(html)
    assert screen_hits('<input placeholder="кап за свип">') and screen_hits('<form onsubmit="return confirm(\'гейт\')">')
    assert not screen_hits('<th title="Сколько сайтов ссылается на домен (RD).">ссылающихся сайтов</th>')
    assert screen_hits('<th title="RD из Ahrefs">ссылки</th>') and screen_hits("<th>DR</th>") and screen_hits("<b>EMD</b>")
    assert not screen_hits("<span>emd</span> Domain Rating by Ahrefs, из ключевых слов (EMD)")
    # вырезаются только закрытые пары: незакрытый <code> не прячет остаток страницы
    broken = "<p>до <code>AHREFS_API_KEY</code> после</p><p><code>ключ без закрытия</p><p>дальше гейт</p>"
    assert "AHREFS_API_KEY" not in visible_text(broken) and "дальше гейт" in visible_text(broken)
    assert screen_hits(broken)


@pytest.mark.parametrize("url", SCREENS)
def test_screen_has_no_internal_words(client, monkeypatch, url):
    seed_panel(monkeypatch)
    r = client.get(url)
    assert r.status_code == 200
    assert screen_hits(r.text) == []


@pytest.mark.parametrize("url", SCREENS)
def test_empty_screen_has_no_internal_words(client, url):
    """Пустая база — свои тексты («Сайтов пока нет», «Решать нечего»): их тоже читает человек."""
    r = client.get(url)
    assert r.status_code == 200
    assert screen_hits(r.text) == []


def test_seed_really_fills_every_branch(client, monkeypatch):
    """Страж чего-то стоит, только пока сид занимает состояния: без этой проверки он молча
    проверял бы пустые таблицы."""
    seed_panel(monkeypatch)
    inbox, pool, queue = (visible_text(client.get(u).text) for u in
                          ("/domains", "/domains/pool?status=rejected", "/queue"))
    for words in ("история НЕ проверена", "спам-ссылки НЕ проверены", "чёрные списки НЕ проверены",
                  "риск НЕ проверен", "ссылки НЕ проверены", "вердикт — из прошлой проверки",
                  "свободен ли домен, НЕ проверено", "покупать нельзя — грязный", "дроп прошёл",
                  "зоны нет в списке", "архив пуст — домен новый", "прошлая тема далека от VPN",
                  "в списках чистоты", "🛒 Уже купил сам", "＋ К покупке"):
        assert words in inbox, words
    from app.services.labels import REJECT_RU
    for label in REJECT_RU.values():
        assert label in pool, label
    for words in ("✓ Купить за 2.79 USD", "✓ Купить не дороже", "✓ Подтвердить ставку", "✓ Ставка до (USD)",
                  "▶ Отправить заказ", "↻ Повторить", "✓ Домен получен", "✗ Отменить", "просрочено",
                  "отправка оборвалась — исход неизвестен", "заказ уходит провайдеру", "покупать нельзя"):
        assert words in queue, words
    # кнопка, которая только подтверждает, не зовётся «Купить»: заказ уходит следующей — «Отправить заказ»
    assert queue.count("✓ Зафиксировать цену") >= 2            # регистратор без цены и optimizator
    assert queue.count("✓ Купить") == queue.count("✓ Купить за ") + queue.count("✓ Купить не дороже") >= 2


def test_seed_fills_site_editor_and_system_screens(client, monkeypatch):
    """То же для карточки сайта, редактора, офферов и экранов «Система»: каждая ветка отрисована."""
    from app.config import settings
    from app.services.orchestrator import COUNT_RU
    seed_panel(monkeypatch)
    monkeypatch.setattr(settings, "GITHUB_TOKEN", "t0ken")       # с токеном кнопки обновления включены

    def see(url):
        r = client.get(url)
        assert r.status_code == 200, url
        return visible_text(r.text)

    expected = {
        "/sites/1": ("поднимается", "Выбери бренд", "Сейчас: ждёт NS у регистратора", "Пропиши у регистратора домена",
                     "▶ Поднять сайт", "Страниц нет — нажми «Написать тексты» на шаге 5"),
        "/sites/2": ("пишутся тексты", "Привязано:", "HTTPS в Cloudflare настроился не полностью", "↻ Поднять заново",
                     "Cloudflare в режиме flexible", "Конкурентов ещё не изучали", "Последняя попытка",
                     "▶ Изучить конкурентов", "▶ Написать тексты"),
        "/sites/3": ("Пишем про:", "ещё привязаны: PlainVPN", "страницы написаны под PlainVPN",
                     "обзоры: 1, сравнения: 1, инструкции: 1, рынок: 1", "⟳ Изучить заново", "✎ Переписать тексты",
                     "Cloudflare в режиме full", "✓ Вычитать критиком", "▶ Опубликовать (1)", "прошла", "одобряешь ты",
                     "2 замеч.", "не проверена", "устарел", "черновик", "вычитано", "✎ Вычитать", "✎ Открыть"),
        "/sites/4": ("опубликован", "выключен", "оффер выключен", "на сайте прежняя версия", "на сайте",
                     "Последняя проверка ничего не выяснила", "не проверялось", "в индексе", "не в индексе",
                     "Cloudflare в режиме strict", "▶ Проверить индексацию"),
        "/pages/1": ("💾 Сохранить", "✓ Одобрить", "🔍 Вычитать критиком", "← Назад к сайту",
                     "«Сохранить» оставляет черновик, «Одобрить» делает страницу готовой к публикации"),
        "/pages/7": ("критик: прошла", "90/100"),
        "/pages/8": ("критик: прошла", "одобряет человек"),
        "/pages/9": ("критик: замечания", "40/100", "круг 1 из", "проверки кодом:", "редактор-модель:",
                     "пожелания (публикации не мешают):", "добавь таблицу цен"),
        "/pages/10": ("критик: не проверена",),
        "/pages/11": ("вердикт устарел",),
        "/pages/12": ("вердикта нет",),
        "/offers": ("включён", "выключен", "⚠ условия не заданы", "⏻ Включить", "⏻ Выключить", "＋ Добавить оффер",
                    "Резервный адрес"),
        "/guides": ("style.md", "правлена", "устарела", "не использовать", "Сжать правила", "✕ Удалить"),
        "/settings": ("Минимум ссылающихся сайтов", "Оценка, с которой домен сильный", "Лимит проверок whois за проход",
                      "Веса оценки", "Откуда берём домены", "последняя загрузка: ошибка — HTTPError: 503",
                      "последняя загрузка: с замечаниями", "ut1/gambling: 1", "Вернуть стандартные"),
        "/settings/keys?msg=ok": ("Поиск и проверка доменов", "Покупка", "Хостинг и DNS", "Тексты и индексация",
                                  "Прочее", "Проверить связь", "Из .env"),
        "/settings/cloudflare": ("⟳ Обновить из Cloudflare", "работает", "ошибка", "предупреждение", "не проверен",
                                 "ждёт NS", "пропала", "создаётся", "перенесена", "удалена", "готов",
                                 "с ошибкой", "Main account"),
        "/autopilot": ("▶ Запустить проход", "Главный выключатель", "лимит за проход:", "Последние проходы", "готово",
                       "с замечаниями", "ошибка", "остановлен", "идёт", "вручную", "по расписанию",
                       *[f"{label}:1" for label in COUNT_RU.values()],
                       *[f"{name} — " for name in ("Найти домены", "Проверить домены", "Поставить к покупке",
                                                    "Поднять сайт", "Изучить конкурентов", "Написать тексты",
                                                    "Вычитка", "Опубликовать", "Проверить индексацию")]),
        "/diag": ("фоновый процесс: работает", "↻ Проверить связь", "Проверить обновления", "⇩ Обновить программу",
                  "⚠ Обновить принудительно", "✓ версия свежая", "не работают обязательные:", "Поиск и проверка доменов", "Покупка", "Хостинг и DNS",
                  "Тексты и индексация", "Прочее", "не настроен", "не работает (необязателен)", "не ответил за",
                  "ключ не задан", "источник выключен в Настройках"),
    }
    for url, words in expected.items():
        text = see(url)
        for w in words:
            assert w in text, (url, w)


DEPLOY_STATES = [{"error": "git не найден"}, {"detached": True}, {"dirty": True}, {"ahead": 1, "behind": 2},
                 {"behind": 2}, {"ahead": 1}, {}]


@pytest.mark.parametrize("state", DEPLOY_STATES, ids=lambda s: "-".join(s) or "fresh")
@pytest.mark.parametrize("token", ["", "t0ken"], ids=["no-token", "token"])
def test_diag_update_block_is_plain_in_every_state(client, monkeypatch, state, token):
    """Строка версии и блок обновления: все состояния программы на сервере, с токеном GitHub и без."""
    from app.config import settings
    seed_panel(monkeypatch)
    _seed_diag(monkeypatch, **state)
    monkeypatch.setattr(settings, "GITHUB_TOKEN", token)
    r = client.get("/diag")
    assert r.status_code == 200
    text = visible_text(r.text)
    assert screen_hits(r.text) == []
    assert ("⇩ Обновить программу" in text and "⚠ Обновить принудительно" in text) if token else "Кнопки выключены" in text


def test_guides_screen_without_folder_is_plain(client, monkeypatch, tmp_path):
    from app.config import settings
    monkeypatch.setattr(settings, "CONTENT_GUIDES_DIR", str(tmp_path / "nowhere"))
    html = client.get("/guides").text
    assert "Папка правил не найдена" in visible_text(html) and screen_hits(html) == []


class _Forms(HTMLParser):
    """Формы экрана: адрес, текст диалога (`onsubmit`) и кнопки с их подсказками."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.forms, self._form, self._button = [], None, None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form":
            self._form = {"action": a.get("action", ""), "onsubmit": a.get("onsubmit") or "", "buttons": []}
            self.forms.append(self._form)
        elif tag == "button" and self._form is not None:
            self._button = {"text": "", "title": a.get("title") or ""}
            self._form["buttons"].append(self._button)

    def handle_endtag(self, tag):
        if tag == "form":
            self._form = None
        elif tag == "button":
            self._button = None

    def handle_data(self, data):
        if self._button is not None:
            self._button["text"] += data


def test_money_dialogs_name_domain_amount_and_charge(client, monkeypatch):
    """Последнее, что человек читает перед деньгами. У каждой денежной формы «Покупки» диалог называет
    домен, говорит, откуда сумма (число из формы, цена с кнопки или «узнаем сейчас»), и когда спишутся
    деньги: сразу — либо отдельной кнопкой «Отправить заказ», если форма только подтверждает."""
    seed_panel(monkeypatch)
    with db.SessionLocal() as s:
        orders = {s.get(Domain, o.domain_id).domain: o.id for o in s.query(AcquisitionOrder).all()}
    parser = _Forms()
    parser.feed(client.get("/queue").text)

    def form(domain, kind, button):
        found = [(f, b) for f in parser.forms if f["action"] == f"/queue/{orders[domain]}/{kind}"
                 for b in f["buttons"] if b["text"].strip().startswith(button)]
        assert len(found) == 1, (domain, kind, button)
        return found[0]

    send = "нажмёшь «Отправить заказ»"
    cases = [   # домен, действие, кнопка, откуда сумма, что сказано о списании
        ("bo-p.ru", "confirm", "✓ Подтвердить ставку",                    # ставка backorder: сумма из списка
         "this.bid_rub.options[this.bid_rub.selectedIndex].text", ("спишутся", send)),
        ("auction-q.com", "confirm", "✓ Ставка до (USD)",                  # аукцион: потолок из поля
         "this.bid_rub.value", ("спишется не больше этой суммы плюс год продления", send)),
        ("quote-r.com", "buy", "✓ Купить за 2.79 USD",                     # один клик, цена известна
         "за 2.79 USD", ("спишется с баланса сразу",)),
        ("silent-s.com", "buy", "✓ Купить не дороже",                      # один клик, предел задаёт человек
         "this.max_price.value", ("спишется с баланса сразу",)),
        ("silent-s.com", "confirm", "✓ Зафиксировать цену",                # регистратор: только подтвердить
         "сумму узнаем сейчас", ("спишутся", send)),
        ("opt-t.ru", "confirm", "✓ Зафиксировать цену",                    # optimizator: только подтвердить
         "сумму узнаем у провайдера сейчас", ("спишутся", send)),
    ]
    for domain, kind, button, amount, charge in cases:
        f, _b = form(domain, kind, button)
        text = f["onsubmit"]
        assert text.startswith("return confirm(") and domain in text, (button, text)
        low = text.lower()
        assert amount.lower() in low, (button, text)
        for words in charge:
            assert words.lower() in low, (button, words, text)
        if send in charge:                       # форма только подтверждает — «Купить» в диалоге было бы обещанием
            assert "купить" not in low, (button, text)
    # «Отправить заказ» и «Повторить» диалога не имеют (заказ уже подтверждён человеком): о списании
    # говорит подсказка самой кнопки
    f, b = form("sure-u.ru", "execute", "▶ Отправить заказ")
    assert not f["onsubmit"] and "спишутся с баланса" in b["title"]
    f, b = form("retry-w.ru", "execute", "↻ Повторить")
    assert not f["onsubmit"] and "дважды не заплатим" in b["title"]
    head = visible_text(client.get("/queue").text)
    assert "Деньги тратятся только после твоего клика — «Купить», «Отправить заказ» или «Повторить»." in head


def test_confirm_flash_calls_a_fixed_price_a_sum_not_a_bid(client, monkeypatch):
    """Флеш после подтверждения. «Ставка» — только там, где её задал человек (тариф backorder, потолок
    аукциона). Фиксированную цену optimizator и обычной регистрации ставкой не зовём и года продления ей
    не приписываем: ни торга, ни продления там нет."""
    from app.services import acquisition
    cases = [   # что прислала форма, что ответил сервис, что обязано быть во флеше, чего быть не должно
        ({"bid_rub": "190"}, {"bid_rub": 190.0, "currency": "RUB"}, "подтверждён, ставка 190 ₽.", ()),
        ({}, {"bid_rub": 590.0, "currency": "RUB"}, "подтверждён, сумма 590 ₽.", ("ставк",)),
        ({"bid_rub": "20"}, {"bid_rub": 22.99, "currency": "USD"},
         "подтверждён, к списанию до 22.99 USD (ставка + год продления).", ()),
        ({}, {"bid_rub": 2.79, "currency": "USD"}, "подтверждён, к списанию не больше 2.79 USD.",
         ("ставк", "продлен")),
        ({}, {"bid_rub": None, "currency": None}, "Заказ #7 подтверждён. Теперь", ("ставк", "сумма", "списан")),
    ]
    for data, answer, must, never in cases:
        monkeypatch.setattr(acquisition, "confirm_order", lambda order_id, bid=None, answer=answer: answer)
        text = _flash(client.post("/queue/7/confirm", data=data, follow_redirects=False))
        assert must in text and "Теперь нажми «Отправить заказ»." in text, text
        for word in never:
            assert word not in text, (word, text)


def _flash(resp) -> str:
    q = parse_qs(urlsplit(resp.headers["location"]).query)
    return " ".join(q.get("msg", []) + q.get("err", []))


def test_flash_messages_of_work_screens_are_plain(client, monkeypatch):
    """Флеш-сообщение — тот же экран: после клика оператор читает именно его."""
    from app.services import scoring
    seed_panel(monkeypatch)
    with db.SessionLocal() as s:
        ids = {d.domain: d.id for d in s.query(Domain).all()}
        orders = {s.get(Domain, o.domain_id).domain: o.id for o in s.query(AcquisitionOrder).all()}

    def post(url, **data):
        return _flash(client.post(url, data=data, follow_redirects=False))

    seen = [
        post("/domains/bulk-approve", min_score="0.5"),
        post(f"/domains/{ids['ok-l.com']}/queue", provider="backorder"),            # поставлен к покупке
        post(f"/domains/{ids['washed-m.com']}/queue"),                              # отказ: грязный
        post(f"/domains/{ids['washed-m.com']}/set-status", status="purchased"),     # отказ: грязный
        post(f"/domains/{ids['weak-o.ru']}/set-status", status="approved"),         # отказ: зона
        post(f"/domains/{ids['found-a.com']}/set-status", status="purchased"),      # отказ: не тот статус
        post(f"/domains/{ids['bought-n.com']}/make-site"),
        post("/domains/add-list", domains="one-new.com, two-new.com"),
        post(f"/queue/{orders['bo-p.ru']}/execute"),                                # отказ: не подтверждён
        post(f"/queue/{orders['bo-p.ru']}/confirm", bid_rub="190"),
        post(f"/queue/{orders['sent-aa.ru']}/cancel"),                              # отказ: уже отправлен
        post(f"/queue/{orders['retry-w.ru']}/cancel"),                              # отказ: исход неизвестен
        post(f"/queue/{orders['off-ac.ru']}/caught"),                               # отказ: не отправлен
        post(f"/queue/{orders['bo-p.ru']}/cancel"),
        post("/queue/poll"),
    ]
    for why in ("waiting", "whois_failed", "whois_unclear", "taken_undated", "budget", "ahrefs_failed",
                "units_floor", "ahrefs_missing", "ahrefs_no_key", "что-то новое"):
        monkeypatch.setattr(scoring, "score_domain", lambda domain_id, why=why: {
            "domain": "found-a.com", "status": "discovered", "unresolved": True, "why": why})
        seen.append(post(f"/domains/{ids['found-a.com']}/score"))
    for out in ({"domain": "found-a.com", "status": "scored", "score": 0.71},
                {"domain": "found-a.com", "status": "rejected", "score": 0.0, "reject_reason": "low_rd"}):
        monkeypatch.setattr(scoring, "score_domain", lambda domain_id, out=out: out)
        seen.append(post(f"/domains/{ids['found-a.com']}/score"))
    assert all(seen), seen                       # каждое действие что-то сказало
    for text in seen:
        assert stop_hits(text) == [], text
    assert "found-a.com проверен: отклонён (мало ссылающихся сайтов), оценка 0.0" in seen[-1]
    # у домена из ключевых слов оценки нет — флеш о ней молчит, а не пишет «оценка None»
    monkeypatch.setattr(scoring, "score_domain", lambda domain_id: {"domain": "found-a.com", "status": "scored",
                                                                    "score": None})
    said = post(f"/domains/{ids['found-a.com']}/score")
    assert said == "found-a.com проверен: ждёт решения" and "None" not in said


def _said(resp) -> str:
    """Что действие сказало оператору: флеш редиректа либо видимый текст страницы с ошибкой формы."""
    return _flash(resp) if resp.status_code == 303 else visible_text(resp.text)


def test_flash_messages_of_site_and_system_screens_are_plain(client, monkeypatch):
    """Действия карточки сайта, редактора, офферов, настроек, автопилота и диагностики: каждое что-то
    говорит оператору — и говорит простыми словами. Отказы берём настоящие (сервис отвечает сам);
    подменяем только то, что ушло бы в сеть или в фон."""
    from app.config import settings
    from app.services import (content_critic, deploy, diag_cache, jobs, provisioning, publish)
    seed_panel(monkeypatch)
    monkeypatch.setattr(settings, "PANEL_USER", "")
    monkeypatch.setattr(settings, "PANEL_PASS", "")
    monkeypatch.setattr("app.api.panel._require_cf_write", lambda request: None)
    with db.SessionLocal() as s:
        s.add(Page(site_id=1, url_path="/early", title="t", body="<p>text</p>", status="edited"))   # сайт не поднят
        s.commit()
    seen = []

    def post(url, **data):
        seen.append(_said(client.post(url, data=data, follow_redirects=False)))
        return seen[-1]

    def stub(target, name, *results):
        """Подменить target.name: по очереди отдаёт results (исключение — бросает)."""
        queue = list(results)

        def fake(*a, **kw):
            out = queue.pop(0)
            if isinstance(out, Exception):
                raise out
            return out
        monkeypatch.setattr(target, name, fake)
        return len(queue)

    # офферы
    post("/offers/create", brand="NewVPN", affiliate_link="javascript:alert(1)")
    post("/offers/create", brand="NewVPN", affiliate_link="https://x.example/a", promo_code="X")     # без условий
    post("/offers/create", brand="NewVPN", affiliate_link="https://x.example/a")
    post("/offers/1/update", affiliate_link="https://x.example/b")
    post("/offers/999/update", affiliate_link="https://x.example/b")
    for url in ("ftp://x.example", "https://r.example/", ""):
        post("/offers/reserve-url", reserve_offer_url=url)
    post("/sites/3/attach-offer", offer_id="2")                   # выключенный оффер
    post("/sites/3/attach-offer", offer_id="1")
    # поднять сайт: каждый исход сервиса
    warn = provisioning.NOT_OURS
    for _ in range(stub(provisioning, "provision",
                        {"status": "awaiting_ns", "hint": "пропиши у регистратора NS Cloudflare: a.ns.example"},
                        {"status": "error"}, {"status": "error", "error": "зона Cloudflare в статусе «moved»"},
                        {"status": "provisioned", "ssl_error": "RuntimeError: 403"},
                        {"status": "provisioned", "origin_https": "origin_ca"},
                        {"status": "provisioned", "origin_https": "ok", "warnings": [warn]},
                        {"status": "provisioned", "origin_https": "none"}, RuntimeError("aaPanel недоступен"))):
        post("/sites/1/provision")
    # тексты, конкуренты, вычитка: отказы настоящие, запуск фоновой задачи подменён
    post("/sites/999/generate")
    post("/sites/1/generate")                                     # сайт ещё не поднят
    post("/sites/4/generate")                                     # оффер сайта выключен
    post("/sites/2/generate")                                     # конкурентов не изучали
    post("/sites/1/research")                                     # оффера нет
    post("/sites/1/edit")                                         # черновиков нет
    for taken in (True, False):                                   # задача принята / такая уже идёт
        monkeypatch.setattr(jobs, "spawn", lambda name, target, taken=taken: taken)
        for url in ("/sites/3/generate", "/sites/3/rewrite", "/sites/3/research", "/sites/3/edit",
                    "/guides/digest", "/autopilot/run", "/settings/lists/refresh", "/settings/ranks/refresh",
                    "/settings/cloudflare/sync"):
            post(url)
    seen[:] = [x for x in seen if x]          # принятая задача без флеша — её показывает карточка задачи
    # редактор страницы
    post("/pages/1/draft", body="<p>Правка без одобрения.</p>")
    post("/pages/1/save", body="")                                # пустой текст одобрить нельзя
    post("/pages/1/save", body="<p>" + "Достаточно длинный вычитанный текст страницы. " * 5 + "</p>")
    verdict = {"error": None, "pass": True, "note": None}
    for auto in (True, False):
        monkeypatch.setattr("app.services.autonomy.get_autonomy", lambda auto=auto: {"auto_edit": auto})
        stub(content_critic, "critique_page", dict(verdict))
        post("/pages/7/critique")
    for _ in range(stub(content_critic, "critique_page", {**verdict, "error": "ReadTimeout"},
                        {**verdict, "pass": False}, {**verdict, "note": content_critic._MANUAL},
                        {**verdict, "note": content_critic._REFUSED}, ValueError("страница #7 уже одобрена"),
                        RuntimeError("шлюз модели не ответил"))):
        post("/pages/7/critique")
    # публикация и индексация
    post("/sites/2/publish")                                      # вычитанных страниц нет
    post("/sites/1/publish")                                      # сайт не поднят
    for _ in range(stub(publish, "publish_site",
                        {"status": "partial", "pages": ["/vpn"], "failed": {"/": "HTTP 502"},
                         "unverified": {"/a": "отдаётся не записанная версия (заглушка панели, старый кэш или не та папка сайта)"},
                         "warnings": ["IndexNow: 403"]},
                        {"status": "failed", "pages": [], "failed": {"/": "HTTP 502"}},
                        {"status": "published", "pages": ["/", "/vpn"]}, RuntimeError("aaPanel недоступен"))):
        post("/sites/3/publish")
    for _ in range(stub(publish, "check_index", {"pages": {}},
                        {"pages": {"/": "indexed", "/a": "not_indexed", "/b": "unknown"},
                         "sources": {"/": "gsc", "/a": "searxng"}, "gsc_note": "403"}, RuntimeError("таймаут"))):
        post("/sites/4/check-index")
    # обновление программы: отказы сервиса настоящие, сам git подменён
    monkeypatch.setattr(settings, "GITHUB_TOKEN", "")
    post("/admin/check-updates")
    post("/admin/pull")
    post("/admin/force-pull")
    done = {"ok": True, "old": "a1", "new": "b2", "subject": "fix", "alembic_warn": ""}
    for _ in range(stub(deploy, "git_pull", deploy._busy_error(["score", "sweep", "новая задача"]),
                        {**done, "ok": False, "alembic_warn": "обновление базы не завершилось за 120 с"},
                        {**done, "ok": False}, {**done, "new": "a1"},
                        {**done, "needs_rebuild": True, "compose_hint": deploy.COMPOSE_HINT})):
        post("/admin/pull")
    stub(deploy, "git_force_pull", {**done, "forced": True})
    post("/admin/force-pull")
    monkeypatch.setattr(diag_cache, "refresh", lambda force=False: [])
    post("/diag/refresh")
    # настройки, ключи, автопилот, правила письма
    base = {"min_referring_domains": "1", "min_age_years": "3", "approve_at": "0.7", "manual_review_at": "0.4"}
    post("/settings/save", **base)
    post("/settings/save", **base, v2_lists="1", emd_sets="{не список")
    post("/settings/save", **base, v2_lists="1", rank_pct_low="0.9", rank_pct_full="0.1")
    post("/settings/reset")
    post("/settings/keys", v_LLM_MODEL="mistral")                 # панель без пароля — ключи не меняем
    post("/autopilot/settings", sweep_interval_min="60")
    post("/guides/upload")                                        # файл не выбран
    post("/guides/role", rel="style.md", role="никому")
    post("/guides/role", rel="style.md", role="critic")
    post("/guides/delete", rel="нет-такого.md")
    post("/guides/digest/style.md", text="")
    post("/guides/digest/style.md", text="— пиши коротко")

    assert len(seen) >= 75 and all(seen), seen
    for text in seen:
        assert stop_hits(text) == [], text
    # сервисные отказы, которые на экран попадают и без клика: журнал автопилота, карточка задачи
    for text in (provisioning.NOT_OURS, deploy.COMPOSE_HINT, content_critic._NO_DOSSIER):
        assert stop_hits(text) == [], text
    told = " | ".join(seen)
    for words in ("Сайт поднят: домен заведён в Cloudflare и на сервере", "ещё не поднят", "сначала «Поднять сайт»",
                  "Сначала изучи конкурентов (шаг 4)", "привяжи включённый", "Тексты пишутся в фоне",
                  "Конкурентов изучаем в фоне", "Страница одобрена — можно публиковать", "Публиковать нечего",
                  "Связь проверена заново", "Стандартные настройки возвращены", "Проход уже идёт",
                  "«Обновить из Cloudflare» уже идёт", "идут задачи: «Проверить домены», «Проход автопилота», «новая задача»",
                  "но БАЗА НЕ ОБНОВИЛАСЬ", "Обновлено принудительно", "Уже свежая версия", "Резервный адрес сохранён"):
        assert words in told, words


def _russian_literals(path):
    """Русские строки модуля, которые может увидеть оператор: без докстрингов и без самопроверок под
    `if __name__ == "__main__"`."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    skip = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                skip.add(id(first.value))
        if isinstance(node, ast.If) and "__main__" in ast.dump(node.test):
            skip.update(id(n) for n in ast.walk(node))
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip
                and re.search("[а-яё]", node.value, re.I)):
            yield node.lineno, node.value


def test_python_messages_are_plain():
    """Причина отказа, написанная в сервисе, доезжает до экрана — флешем, строкой журнала автопилота,
    сообщением задачи, колонкой «результат». Все русские строки роутов, сервисов и интеграций — без
    внутренних слов (журналы фонового процесса в `workers/` оператор на экране не видит)."""
    from pathlib import Path

    import app
    root = Path(app.__file__).parent
    files = [p for part in ("api", "services", "integrations") for p in sorted((root / part).glob("*.py"))]
    assert len(files) > 40
    found, n = [], 0
    for p in files:
        for line, text in _russian_literals(p):
            n += 1
            found += [f"{p.name}:{line}: {hit}" for hit in stop_hits(text)]
    assert n > 1000                              # строки действительно прочитаны
    assert found == []


def test_literal_scanner_is_not_blind(tmp_path):
    src = tmp_path / "m.py"
    src.write_text('"""докстринг про гейт"""\nA = "сначала provision"\nB = "plain ascii draft"\n'
                   'def f():\n    "докстринг: свип"\n    return f"кап за {1}"\n'
                   'if __name__ == "__main__":\n    assert A, "воркер"\n', encoding="utf-8")
    assert [text for _line, text in _russian_literals(src)] == ["сначала provision", "кап за "]


def test_shared_dictionaries_are_plain():
    """Словари подписей расходятся по всем экранам (и по карточке задачи, которая живёт в
    <script> и под стража экранов не попадает) — проверяем их у источника."""
    from pathlib import Path

    import app
    from app.services import api_keys, diagnostics, labels, orchestrator, scoring
    words = []
    # экран ключей и диагностика: названия групп, подписи полей, справка «где взять», роли сервисов
    for _gid, title, note, fields in api_keys.GROUPS:
        words += [title, note] + [x for f in fields for x in (f.label, f.hint)]
    assert [t for _g, t, _n, _f in api_keys.GROUPS] == [
        "Поиск и проверка доменов", "Покупка", "Хостинг и DNS", "Тексты и индексация", "Прочее"]
    words += [role for _k, _label, role, *_ in diagnostics._spec()] + list(diagnostics._SKIP_WHY.values())
    for d in (labels.STATUS_RU, labels.SITE_STATUS_RU, labels.REJECT_RU, labels.SOURCE_RU, labels.LANE_RU,
              labels.INDEX_RU, orchestrator.STAGE_RU, orchestrator.COUNT_RU, scoring._BLIND_RU):
        words += list(d.values())
    words += [s["label"] for s in scoring.FUNNEL_STAGES] + [scoring._BLIND_WHOIS_ARCHIVE_AGE]
    base = (Path(app.__file__).parent / "templates" / "base.html").read_text(encoding="utf-8")
    job_ru = re.search(r"const JOB_RU = \{(.*?)\};", base, re.S).group(1)
    names = re.findall(r"(\w+):'([^']*)'", job_ru)
    assert len(names) >= 11                      # словарь задач разобран, а не пуст
    words += [ru for _key, ru in names]
    for w in words:
        assert stop_hits(w) == [], w
    # одно действие — одно название: задача на Пульте зовётся как кнопка, которой её запускают
    job = dict(names)
    assert labels.JOB_RU == job                  # словарь задач для текстов из кода — тот же, что у карточки
    assert (job["discovery"], job["score"], job["recheck"]) == (
        "Найти домены", "Проверить домены", "Проверить, не заняты ли")
    assert orchestrator.STAGE_RU["discovery"] == job["discovery"].lower()
    assert orchestrator.STAGE_RU["score"] == job["score"].lower()
