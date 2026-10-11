"""Инбокс v2: язык, тема, DR с атрибуцией Ahrefs, EMD, фильтр по языку без «невидимого» пакета,
пакет от «порога сильного кандидата», легенда отказов v2, раскладка прогона."""
import re

import app.db as db
from app.models.domain import Domain
from app.models.domain_score_log import DomainScoreLog
from app.services import jobs

CHECKED = {"errors": [], "deep_checked": True}


def _add(**kw):
    with db.SessionLocal() as s:
        d = Domain(**kw)
        s.add(d)
        s.commit()
        return d.id


def test_inbox_shows_lang_topic_dr_attribution_and_emd(client):
    _add(domain="polski-blog.com", source="nominet", status="scored", score=0.6, dr=12,
         market_lang="pl", topic="VPN and privacy blog", topical_relevance=0.8, score_breakdown=CHECKED)
    _add(domain="mejorvpn.com", source="emd", status="scored", score=None, market_lang="es",
         score_breakdown={"emd": True, "errors": []})
    html = client.get("/domains").text
    assert "polski-blog.com" in html and "VPN and privacy blog" in html and "близость к VPN <b>80%</b>" in html
    assert "Domain Rating by Ahrefs" in html and "домен из ключевых слов — реши сам" in html
    assert 'title="источник: Nominet">uk</span>' in html
    assert 'title="источник: из ключевых слов (EMD)">emd</span>' in html


def test_inbox_lang_filter_hides_bulk_and_counts_before_filter(client):
    """1.6: на /domains?lang=pl оператор видит только польские домены, а пакет взял бы все языки —
    поэтому при выбранном языке форма пакета скрыта. Счётчик «на решении» — до фильтра."""
    _add(domain="pl-site.com", source="nominet", status="scored", score=0.9, market_lang="pl",
         score_breakdown=CHECKED)
    _add(domain="es-site.com", source="nominet", status="scored", score=0.9, market_lang="es",
         score_breakdown=CHECKED)
    html = client.get("/domains?lang=pl").text
    assert "pl-site.com" in html and "es-site.com" not in html
    assert 'action="/domains/bulk-approve"' not in html and "одобрить пакетом нельзя" in html
    assert '<div class="v">2</div><div class="k">ждут решения</div>' in html
    assert 'class="chip on" href="/domains?lang=pl"' in html
    assert 'action="/domains/bulk-approve"' in client.get("/domains").text


def test_inbox_lang_filter_empty_state_keeps_the_filter(client):
    """6.3: пусто по языку — не «Решать нечего», а «по языку X ничего нет», и переключатель
    языков на месте (он стоит ДО ветки пустого инбокса — иначе вернуться некуда)."""
    _add(domain="pl-site.com", source="nominet", status="scored", score=0.9, market_lang="pl",
         score_breakdown=CHECKED)
    html = client.get("/domains?lang=de").text
    assert "По языку de ничего нет" in html and "Решать нечего" not in html
    assert 'href="/domains?lang=pl"' in html


def test_bulk_default_is_the_strong_candidate_threshold(client):
    """Р2: «пакет от скора» по умолчанию = approve_at («порог сильного кандидата»), а не зашитые 0.80."""
    from app.services.settings import update_settings
    update_settings(approve_at=0.65)
    _add(domain="a.com", source="nominet", status="scored", score=0.9, score_breakdown=CHECKED)
    assert 'name="min_score" value="0.65"' in client.get("/domains").text


def test_far_topic_is_marked_and_clean_history_still_named_clean(client):
    """Инвариант 4 + Р2: прошлая тема далека от VPN — пометка в строке; пакет такой домен не берёт,
    но история у него ЧИСТАЯ — строка не вправе писать «история не подтверждена». Тема видна и
    в «Готовы к выкупу», откуда идут покупать."""
    _add(domain="far.com", source="nominet", status="scored", score=0.8, wayback_checked=True,
         prior_flags={}, age_years=9.0, topic="casino reviews", topical_relevance=0.1,
         score_breakdown=CHECKED)
    _add(domain="ready.com", source="nominet", status="approved", score=0.8, topic="VPN deals",
         topical_relevance=0.9, score_breakdown=CHECKED)
    html = client.get("/domains").text
    assert "прошлая тема далека от VPN" in html
    assert "история чистая" in html and "история не подтверждена" not in html
    assert client.get("/domains/bulk-preview?min_score=0.5").json() == {"n": 0, "skipped": 1}
    ready = html[html.index("Готовы к покупке"):]
    assert "тема: VPN deals" in ready


def test_reject_legend_groups_v2_codes(client):
    """6.4: спам-анкоры и легаси Safe Browsing/РКН — грязь; чужая зона, бренд, архив РФ — «нельзя»;
    `low_dr` не производит никто — в легенде его нет."""
    for i, code in enumerate(("spam_anchors", "safebrowsing", "rkn", "tld_closed", "trademark",
                              "legacy_ru", "low_rd")):
        _add(domain=f"r{i}.com", status="rejected", reject_reason=code)
    html = client.get("/domains").text
    for code, kind in (("spam_anchors", "dirt"), ("safebrowsing", "dirt"), ("rkn", "dirt"),
                       ("tld_closed", "taken"), ("trademark", "taken"), ("legacy_ru", "taken"),
                       ("low_rd", "thr")):
        # код причины — в подсказке строки (на экране — фраза из общего словаря)
        assert re.search(rf'title="код причины: {code}">[^<]+</div>\s*<div class="why-bar"><i class="k-{kind}"', html), code
    assert "Низкий DR" not in html


def test_funnel_tally_counts_spam_anchors_and_too_young_as_reached_wayback(client):
    """4.10 + Р5: `spam_anchors` (W6) и `too_young` (W5) рождаются ПОСЛЕ Wayback — в «решено дёшево»
    их не записать."""
    did = _add(domain="tally.com", status="discovered")
    with jobs.track("score") as run:
        with db.SessionLocal() as s:
            for reason in ("spam_anchors", "too_young", "tld_closed"):
                s.add(DomainScoreLog(domain_id=did, run_id=run, outcome="rejected",
                                     reject_reason=reason, score=None, sig={}))
            s.commit()
        t = client.get("/api/jobs/live").json()["jobs"][0]["tally"]
        assert t["reached_wayback"] == 2 and t["before_wayback"] == 1


def test_autopilot_score_stage_says_the_human_approves(client):
    """R2-5 + Р2: стадия «Проверка» автопилота не обещает «сильные и чистые уйдут в approved» —
    машина ставит максимум scored, одобряет человек."""
    html = client.get("/autopilot").text
    assert "уйдут в одобренные" not in html and "Прошедшие ждут твоего решения — одобряешь ты" in html


def test_emd_with_empty_archive_is_newreg_not_blind(client):
    """R2-14: у EMD-новорега пустой архив — норма. Вместо «⚠ история НЕ проверена … ▶ перепроверить»
    строка говорит нейтрально «архив пуст — новорег» (пакет EMD и так не берёт); возраст у новорега
    не критерий. Упал Wayback — это по-прежнему «история НЕ проверена»."""
    from app.services import scoring
    _add(domain="mejorvpn.com", source="emd", status="scored", score=None, market_lang="es",
         score_breakdown={"emd": True, "errors": [], "sampled": 0, "history_evidence": []})
    html = client.get("/domains").text
    assert "архив пуст — домен новый" in html
    assert "история НЕ проверена" not in html and "возраст НЕ проверен" not in html
    down = Domain(domain="vpngratis.com", score_breakdown={"emd": True, "errors": ["wayback:ReadTimeout"],
                                                          "sampled": 0})
    assert scoring.blind_reason(down) == "история НЕ проверена: Wayback был недоступен"


def test_bulk_skips_domains_outside_the_zone_allowlist(client):
    """R2-19: зона вне белого списка — в approved домен не вернуть даже руками (transitions), и пакет
    его не берёт (иначе падал бы отказом политики), а считает в «пропущено». Зону добавили — берёт."""
    from app.services.settings import update_settings
    for name in ("v1-left.ru", "fresh.com"):
        _add(domain=name, source="nominet", status="scored", score=0.9, wayback_checked=True,
             prior_flags={}, age_years=9.0, score_breakdown=CHECKED)
    assert client.get("/domains/bulk-preview?min_score=0.5").json() == {"n": 1, "skipped": 1}
    # строка инбокса судит ТЕМ ЖЕ предикатом, что пакет: для закрытой зоны — ни голого «история чистая»,
    # ни кнопки «✓ одобрить» (политика её отвергнет), а «зона не в белом списке»
    html = client.get("/domains").text
    rows = {m.group(1): m.group(0) for m in
            re.finditer(r"<tr[^>]*>(?:(?!</tr>).)*?(v1-left\.ru|fresh\.com).*?</tr>", html, re.S)}
    assert "история чистая" not in rows["v1-left.ru"] and "✓ Одобрить</button>" not in rows["v1-left.ru"]
    assert "зоны нет в списке" in rows["v1-left.ru"]
    assert "✓ Одобрить</button>" in rows["fresh.com"] and "история чистая" in rows["fresh.com"]
    # и в реестре scored-строка закрытой зоны не получает «✓ одобрить»
    pool = client.get("/domains/pool?status=scored").text
    assert "зоны нет в списке" in pool and pool.count("✓ Одобрить</button>") == 1
    update_settings(tld_allowlist=["com", "ru"])
    assert client.get("/domains/bulk-preview?min_score=0.5").json() == {"n": 2, "skipped": 0}


def test_pool_does_not_offer_return_for_closed_zone(client):
    """R2-19: отклонённый ПОРОГОМ домен вне белого списка зон (.ru v1) — «↩ вернуть в approved» не
    предлагается: политика (transitions.zone_closed) его не пустит, а кнопка, которая не может
    сработать, — ложное предложение. Причина названа, перескор остаётся."""
    _add(domain="weak.ru", status="rejected", reject_reason="low_score", score=0.3)
    html = client.get("/domains/pool?status=rejected").text
    assert "↩ Вернуть в одобренные" not in html and "зоны нет в списке — не вернуть" in html
    assert "▶ Проверить</button>" in html


def test_source_badges_are_explicit_in_inbox_and_pool(client):
    """6.2 + замечание ревью: бейдж каждого источника v2 — из явной карты, и в инбоксе, и в реестре
    (не срез подписи и не «?»)."""
    sources = {"dropcatch": "dc", "nominet": "uk", "mx": "mx", "emd": "emd", "list": "руч"}
    for src in sources:
        _add(domain=f"{src}-site.com", source=src, status="scored", score=0.5, score_breakdown=CHECKED)
    inbox = client.get("/domains").text
    pool = client.get("/domains/pool").text
    for src, badge in sources.items():
        for html in (inbox, pool):
            assert re.search(rf'title="источник: [^"]*"\s*>{badge}</span>{src}-site\.com', html), (src, badge)


def test_emd_score_none_renders_dash_in_inbox_and_pool(client):
    """EMD живёт без балла (score=None): строки инбокса и реестра обязаны отрисоваться «—», а не упасть
    на форматировании None."""
    _add(domain="mejorvpn.com", source="emd", status="scored", score=None, market_lang="es",
         score_breakdown={"emd": True, "errors": []})
    inbox = client.get("/domains")
    assert inbox.status_code == 200 and "оценка <b>—</b>" in inbox.text
    pool = client.get("/domains/pool")
    assert pool.status_code == 200 and "mejorvpn.com" in pool.text


def test_dr_is_attributed_to_ahrefs_in_inbox_and_pool(client):
    """Лицензия Ahrefs: везде, где показан DR, рядом подпись «Domain Rating by Ahrefs»."""
    _add(domain="drsite.com", source="nominet", status="scored", score=0.6, dr=33, score_breakdown=CHECKED)
    assert "рейтинг Ahrefs <b>33</b>" in client.get("/domains").text
    pool = client.get("/domains/pool").text
    assert '<td class="num">33</td>' in pool
    assert 'title="Рейтинг домена по Ahrefs (DR) — Domain Rating by Ahrefs.">рейтинг<br>Ahrefs</th>' in pool
    assert ">Domain Rating by Ahrefs</a>" in pool


def test_bulk_default_follows_the_setting_both_ways(client):
    """Станция /settings обещает: approve_at — значение по умолчанию «пакета от скора». Меняем настройку —
    меняется и поле инбокса (не зашитые 0.80)."""
    from app.services.settings import update_settings
    _add(domain="a.com", source="nominet", status="scored", score=0.9, score_breakdown=CHECKED)
    update_settings(approve_at=0.7)
    assert 'name="min_score" value="0.70"' in client.get("/domains").text
    update_settings(approve_at=0.9)
    html = client.get("/domains").text
    assert 'name="min_score" value="0.90"' in html and 'value="0.80"' not in html


def test_no_auto_approve_wording_in_panel_templates(client):
    """Инвариант 9: машина не одобряет. Ни легенда реестра, ни инбокс, ни автопилот не говорят
    «авто-одобрение / одобряется автоматически»."""
    _add(domain="a.com", source="nominet", status="scored", score=0.9, score_breakdown=CHECKED)
    for url in ("/domains", "/domains/pool", "/autopilot", "/queue"):
        html = client.get(url).text.lower()
        for bad in ("авто-одобр", "автоматически одобр", "auto-approve", "авто-reject", "авто-approve"):
            assert bad not in html, (url, bad)


def test_bulk_approve_empty_threshold_falls_back_to_approve_at(client):
    """M1: очищенное поле + «✓ Одобрить пакет» применяют approve_at из /settings, а не зашитые 0.8
    (при approve_at=0.9 раньше брало бы НИЖЕ видимого оператору порога). Тот же дефолт у preview."""
    from app.services.settings import update_settings
    update_settings(approve_at=0.9)
    _add(domain="mid.com", source="nominet", status="scored", score=0.85, wayback_checked=True,
         prior_flags={}, age_years=9.0, score_breakdown=CHECKED)
    _add(domain="top.com", source="nominet", status="scored", score=0.95, wayback_checked=True,
         prior_flags={}, age_years=9.0, score_breakdown=CHECKED)
    assert client.get("/domains/bulk-preview").json() == {"n": 1, "skipped": 0}   # 0.8 дало бы n=2
    r = client.post("/domains/bulk-approve", data={"min_score": ""}, follow_redirects=False)
    assert r.status_code == 303
    with db.SessionLocal() as s:
        st = {d.domain: d.status for d in s.query(Domain).all()}
    assert st == {"mid.com": "scored", "top.com": "approved"}


def test_long_topic_is_truncated_with_full_text_in_title(client):
    """M2: тема до 120 символов не растягивает nowrap-ячейку — на экране 48, целиком в title."""
    topic = "очень длинная тема прошлого сайта " * 4
    _add(domain="longtopic.com", source="nominet", status="scored", score=0.6, topic=topic,
         topical_relevance=0.8, score_breakdown=CHECKED)
    html = client.get("/domains").text
    assert f'title="Тема прошлого сайта: {topic}. ' in html     # полная тема — в title
    assert "тема: очень длинная тема прошлого сайта" in html and "..." in html.split("тема: ", 1)[1][:80]
    assert f"тема: {topic}" not in html


def test_list_source_deadline_is_labelled_as_estimate(client):
    """M3: у ручного списка (source='list') дата — верхняя граница окна по статусу RDAP, а не дата из
    источника: подпись «ОЦЕНКА ДРОПА»; у остальных источников — «СРОК ДРОПА»."""
    from datetime import datetime, timedelta, timezone
    soon = datetime.now(timezone.utc) + timedelta(days=5)
    _add(domain="manual-est.com", source="list", status="scored", score=0.6, acquire_deadline=soon,
         score_breakdown=CHECKED)
    html = client.get("/domains").text
    assert "ДРОП, ПРИМЕРНО" in html and "ДАТА ДРОПА" not in html
    assert "Настоящий дроп может быть раньше" in html
    _add(domain="feed.com", source="nominet", status="scored", score=0.6, acquire_deadline=soon,
         score_breakdown=CHECKED)
    html = client.get("/domains").text
    assert html.count("ДРОП, ПРИМЕРНО") == 1 and html.count("ДАТА ДРОПА") == 1


def test_bulk_preview_empty_or_garbage_threshold_falls_back_to_approve_at(client):
    """Финальное ревью (minor «г»): очищенное поле шлёт `?min_score=` (domains.html), мусор — `abc`.
    Раньше float-параметр давал 422, и счётчик пакета показывал «undefined». Теперь разбор общий с
    POST: пусто/мусор -> approve_at из /settings; 0 — честный ноль, дефолтом не подменяется."""
    from app.services.settings import update_settings
    update_settings(approve_at=0.9)
    _add(domain="mid.com", source="nominet", status="scored", score=0.85, wayback_checked=True,
         prior_flags={}, age_years=9.0, score_breakdown=CHECKED)
    _add(domain="top.com", source="nominet", status="scored", score=0.95, wayback_checked=True,
         prior_flags={}, age_years=9.0, score_breakdown=CHECKED)
    for q in ("?min_score=", "?min_score=abc"):
        r = client.get("/domains/bulk-preview" + q)
        assert r.status_code == 200 and r.json() == {"n": 1, "skipped": 0}, q
    assert client.get("/domains/bulk-preview?min_score=0").json() == {"n": 2, "skipped": 0}
    # POST — тот же разбор: мусор -> approve_at (взят только top.com)
    assert client.post("/domains/bulk-approve", data={"min_score": "abc"},
                       follow_redirects=False).status_code == 303
    with db.SessionLocal() as s:
        assert {d.domain: d.status for d in s.query(Domain).all()} == {"mid.com": "scored",
                                                                      "top.com": "approved"}


def test_inbox_row_wraps_and_dr_attribution_sits_above_the_table(client):
    """Финальное ревью (1024px): глобальный `th,td{white-space:nowrap}` плюс длинная строка score/DR/
    атрибуция/RD/возраст/язык растягивали таблицу инбокса шире контейнера — «✗ отклонить» и «зона не
    в белом списке» уезжали за край. Атрибуция «Domain Rating by Ahrefs» (лицензия) — одной подписью
    над таблицей, как в реестре; у значения DR — подсказка `title`; ячейка домена переносится."""
    _add(domain="wrap-a.com", source="nominet", status="scored", score=0.6, dr=33, market_lang="pl",
         score_breakdown=CHECKED)
    _add(domain="wrap-b.com", source="nominet", status="scored", score=0.5, dr=12, score_breakdown=CHECKED)
    html = client.get("/domains").text
    inbox = html[html.index("Ждёт твоего решения"):html.index("Готовы к покупке")]
    assert inbox.count(">Domain Rating by Ahrefs</a>") == 1                 # одна подпись, не в каждой строке
    assert inbox.index(">Domain Rating by Ahrefs</a>") < inbox.index("<table>")   # и она над таблицей
    assert ('<span title="Рейтинг домена по Ahrefs (DR) — Domain Rating by Ahrefs.">рейтинг Ahrefs <b>33</b></span>'
            in inbox)
    assert inbox.count('<td class="dom" style="white-space:normal">') == 2  # строки переносятся
