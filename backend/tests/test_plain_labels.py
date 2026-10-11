"""Страж простых подписей: на экране панели нет внутренних слов машины.

Панелью пользуется оператор, не разработчик. Коды модулей (M1…M6) и волн (W0…W6), сырые статусы
(`scored`, `approved`…), «провижн», «свип», «гейт» и подобное — язык кода, а не экрана. Этот тест
рендерит экраны на базе, где занято КАЖДОЕ состояние (все статусы доменов и заказов, все причины
отказа, «вслепую», грязь, пустые и непустые списки), и ищет стоп-слова в ВИДИМОМ тексте: теги и
атрибуты вырезаны, `<script>`, `<style>` и `<code>` исключены (в `<code>` живут имена ключей — по
ним ищут в логах).

Новый экран под стражу — одна строка в SCREENS. Сид уже заводит сайты №1–№4 и страницы №1–№6:
`/sites/1`, `/pages/1` и т.п. можно дописывать как есть.
"""
import re
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit

import pytest

import app.db as db
from app.models.domain import AcquisitionOrder, Domain
from app.models.job import JobRun
from app.models.offer import Offer
from app.models.site import Page, Site

# Экраны под стражей: (адрес, что это). Часть 2 прохода дописывает сюда свои экраны.
SCREENS = [
    "/",                                  # Пульт
    "/domains",                           # Домены
    "/domains?lang=pl",                   # Домены: фильтр по языку (пакет скрыт)
    "/domains/pool",                      # Весь список
    "/domains/pool?show_all=1",           # Весь список: вместе с занятыми
    "/domains/pool?status=rejected",      # Весь список: отклонённые со всеми причинами
    "/queue",                             # Покупка
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


class _Visible(HTMLParser):
    """Текст, который видит человек: без тегов, атрибутов и содержимого script/style/code."""
    _SKIP = {"script", "style", "code"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._depth, self.parts = 0, []

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._depth += 1

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._depth:
            self._depth -= 1

    def handle_data(self, data):
        if not self._depth:
            self.parts.append(data)


def visible_text(html: str) -> str:
    p = _Visible()
    p.feed(html)
    p.close()
    return re.sub(r"\s+", " ", " ".join(p.parts))


def stop_hits(text: str) -> list[str]:
    """Найденные стоп-слова с куском текста вокруг — чтобы по сообщению теста было видно, где."""
    out = []
    for rx, what in STOP:
        for m in rx.finditer(text):
            out.append(f"{what}: …{text[max(0, m.start() - 40):m.end() + 40]}…")
    return out


# --- сид: каждое состояние экранов части 1 ----------------------------------------------------

NOW = datetime.now(timezone.utc)
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


def seed_panel(monkeypatch) -> None:
    """Заполнить базу так, чтобы экраны отрисовали все свои ветки. Имена доменов нейтральные:
    стоп-слово в имени домена было бы ложной находкой."""
    from app.integrations import registrar
    from app.services import heartbeat
    from app.services.scoring import FUNNEL_STAGES
    monkeypatch.setattr(registrar, "get_registrar", lambda: _Registrar())
    soon, past = NOW + timedelta(days=2), NOW - timedelta(days=30)
    clean = dict(wayback_checked=True, prior_flags={}, age_years=9.0, score_breakdown=CHECKED)
    with db.SessionLocal() as s:
        s.add(Offer(brand="TestVPN", affiliate_link="https://example.com/a", promo_code="T10",
                    promo_terms="-10%", active=True))

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

        def site(name, status, pages=()):
            st = Site(domain_id=dom(name, status="purchased"), status=status)
            s.add(st)
            s.flush()
            for path, pstatus, extra in pages:
                s.add(Page(site_id=st.id, url_path=path, title=path, body="<p>text</p>", status=pstatus, **extra))

        site("site-one.com", "provisioning")                                          # сайт №1
        site("site-two.com", "content")                                               # №2: текстов нет
        site("site-three.com", "content", [("/", "draft", {}), ("/vpn", "edited", {})])
        site("site-four.com", "published", [
            ("/", "published", {"index_status": "indexed", "index_checked_at": NOW}),
            ("/a", "published", {}),                                                   # не проверяли
            ("/b", "published", {"index_checked_at": NOW}),                            # проверить не удалось
            ("/c", "published", {"index_status": "not_indexed", "index_checked_at": NOW})])

        # итоги запусков — словами самой машины: подписи шагов берём из её словаря
        waterfall = " · ".join(f"{st['label']}: 10 → 5" for st in FUNNEL_STAGES)
        for name, status, kw in (
                ("discovery", "done", {"message": "найдено 12, новых 3"}),
                ("score", "failed", {"error": "RuntimeError: A-Parser timeout", "message": waterfall}),
                ("recheck", "cancelled", {"done": 3, "total": 10}),
                ("generate", "done", {"message": "написано страниц: 4"}),
                ("edit", "done_warn", {"message": "вычитано 3, одобрено 2"}),
                ("sweep", "done", {"message": "шагов пройдено: 9"})):
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
    html = ('<p title="код: low_rd, статус scored">занят <code>AHREFS_API_KEY scored</code></p>'
            '<script>const M1 = "provision";</script><style>.b-approved{}</style>')
    assert visible_text(html).strip() == "занят"


@pytest.mark.parametrize("url", SCREENS)
def test_screen_has_no_internal_words(client, monkeypatch, url):
    seed_panel(monkeypatch)
    r = client.get(url)
    assert r.status_code == 200
    assert stop_hits(visible_text(r.text)) == []


@pytest.mark.parametrize("url", SCREENS)
def test_empty_screen_has_no_internal_words(client, url):
    """Пустая база — свои тексты («Сайтов пока нет», «Решать нечего»): их тоже читает человек."""
    r = client.get(url)
    assert r.status_code == 200
    assert stop_hits(visible_text(r.text)) == []


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
    for words in ("✓ Купить за 2.79 USD", "✓ Купить не дороже", "✓ Зафиксировать цену", "✓ Ставка до (USD)",
                  "▶ Отправить заказ", "↻ Повторить", "✓ Домен получен", "✗ Отменить", "просрочено",
                  "отправка оборвалась — исход неизвестен", "заказ уходит провайдеру", "покупать нельзя"):
        assert words in queue, words
    assert queue.count("✓ Купить ") >= 2 + 2     # backorder и optimizator — «Купить», плюс две с суммой/пределом


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


def test_shared_dictionaries_are_plain():
    """Словари подписей расходятся по всем экранам (и по карточке задачи, которая живёт в
    <script> и под стража экранов не попадает) — проверяем их у источника."""
    from pathlib import Path

    import app
    from app.services import labels, orchestrator, scoring
    words = []
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
    assert (job["discovery"], job["score"], job["recheck"]) == (
        "Найти домены", "Проверить домены", "Проверить, не заняты ли")
    assert orchestrator.STAGE_RU["discovery"] == job["discovery"].lower()
    assert orchestrator.STAGE_RU["score"] == job["score"].lower()
