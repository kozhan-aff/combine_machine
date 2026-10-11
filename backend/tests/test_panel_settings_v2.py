"""Экран /settings v2: новые поля видны и сохраняются; пустая textarea очищает, отсутствующее поле
не трогает; плохой JSON EMD не затирает сохранённое и не теряет ввод; остаток units — из кэша /diag."""
from datetime import datetime, timedelta, timezone
from markupsafe import escape as html_escape      # тот же экранер, что у Jinja (`"` -> &#34;)

import app.db as db
from app.models.domain import Domain
from app.services import diag_cache
from app.services import scoring_config as cfg
from app.services.settings import get_settings, update_settings

BASE = {"min_referring_domains": 1, "min_age_years": 3.0, "approve_at": 0.7, "manual_review_at": 0.4}


def test_settings_page_shows_v2_fields_and_dr_attribution(client):
    html = client.get("/settings").text
    for name in ("min_dr", "tld_allowlist", "brand_tokens", "emd_sets", "max_links_per_run",
                 "max_deep_per_run", "units_floor", "spam_anchor_max", "dropcatch", "nominet", "w_topical_fit"):
        assert f'name="{name}"' in html, name
    assert "Domain Rating by Ahrefs" in html and "max_ahrefs_per_run" not in html
    assert "Оценка, с которой домен сильный" in html and "авто-одобрение" not in html     # Р2


def test_settings_save_v2_fields(client):
    r = client.post("/settings/save", data={**BASE, "v2_lists": "1", "min_dr": 8, "tld_allowlist": "com\nco.uk",
                                            "brand_tokens": "nordvpn", "max_links_per_run": 300,
                                            "max_deep_per_run": 5, "units_floor": 250000, "spam_anchor_max": 0.3,
                                            "emd_sets": '[{"keywords":["vpn gratis"],"tlds":["mx"]}]',
                                            "nominet": "on"}, follow_redirects=False)
    assert r.status_code == 303 and "err=" not in r.headers["location"]
    s = get_settings()
    assert s["min_dr"] == 8.0 and s["tld_allowlist"] == ["com", "co.uk"] and s["max_deep_per_run"] == 5
    assert s["units_floor"] == 250000 and s["max_links_per_run"] == 300 and s["spam_anchor_max"] == 0.3
    assert s["emd_sets"][0]["keywords"] == ["vpn gratis"] and s["sources_enabled"]["nominet"] is True


def test_settings_save_without_v2_fields_keeps_them(client):
    """Форма без полей v2 (старый шаблон, curl) не затирает сохранённое — и `max_whois_per_run`
    тоже: раньше дефолт формы 200 молча перезаписывал настройку (находка 6.1)."""
    update_settings(min_dr=12, tld_allowlist=["com"], max_whois_per_run=77, units_floor=150000)
    client.post("/settings/save", data=BASE, follow_redirects=False)
    s = get_settings()
    assert s["min_dr"] == 12.0 and s["tld_allowlist"] == ["com"]
    assert s["max_whois_per_run"] == 77 and s["units_floor"] == 150000


def test_empty_textarea_clears_emd_sets(client):
    """6.1: пустая textarea приходит в FastAPI как «поля нет»; форма v2 несёт маркер `v2_lists`, и
    пустое поле — это «очистить», а не «не трогать». Пустые зоны = стартовый список: пустой белый
    список остановил бы всю машину (settings.get_settings)."""
    update_settings(emd_sets=[{"keywords": ["vpn"], "tlds": ["com"]}], tld_allowlist=["com"])
    client.post("/settings/save", data={**BASE, "v2_lists": "1", "emd_sets": "", "tld_allowlist": "",
                                        "brand_tokens": "nordvpn"}, follow_redirects=False)
    s = get_settings()
    assert s["emd_sets"] == [] and s["tld_allowlist"] == cfg.TLD_ALLOWLIST


def test_bad_emd_json_keeps_old_sets_and_operator_input(client):
    """6.1: плохой JSON — ничего не сохранено, а ввод оператора не потерян: форма возвращается с его
    текстом и ошибкой (редирект унёс бы JSON в никуда)."""
    update_settings(emd_sets=[{"keywords": ["vpn"], "tlds": ["com"]}])
    r = client.post("/settings/save", data={**BASE, "v2_lists": "1",
                                            "emd_sets": '[{"keywords": ["mejor vpn"], oops'},
                    follow_redirects=False)
    assert r.status_code == 400
    assert "Не сохранено" in r.text and "mejor vpn" in r.text and "oops" in r.text
    assert get_settings()["emd_sets"][0]["keywords"] == ["vpn"]


def test_emd_sets_shown_without_unicode_escapes(client):
    """6.1: наборы показываются читаемо — «vpn grátis», а не «vpn gr\\u00e1tis» (так экранировал |tojson)."""
    update_settings(emd_sets=[{"market": "es-MX", "lang": "es", "keywords": ["vpn grátis"], "tlds": ["mx"]}])
    html = client.get("/settings").text
    assert "vpn grátis" in html and "\\u00e1" not in html


def test_units_left_comes_from_diag_cache_without_calling_ahrefs(client, monkeypatch):
    """3.4: /settings не ходит в Ahrefs на рендере (таймаут 60 с × 3 повесил бы страницу) — остаток
    из кэша диагностики, который фон обновляет раз в 5 минут."""
    from app.config import settings
    from app.integrations.ahrefs import AhrefsClient

    def _no_network(self):
        raise AssertionError("/settings не вправе ходить в Ahrefs на рендере")
    monkeypatch.setattr(settings, "AHREFS_API_KEY", "k")
    monkeypatch.setattr(AhrefsClient, "units_left", _no_network)
    monkeypatch.setattr(diag_cache, "_checks", None)
    assert "осталось единиц в месяце: <b>—</b>" in client.get("/settings").text
    monkeypatch.setattr(diag_cache, "_checks", [{"key": "ahrefs", "label": "Ahrefs API", "status": "ok",
                                                 "value": 1234567}])
    assert "осталось единиц в месяце: <b>1 234 567</b>" in client.get("/settings").text


def test_ahrefs_diag_records_units_left(monkeypatch):
    """Остаток units кладёт в кэш сам пинг Ahrefs в /diag (запрос бесплатный)."""
    from app.config import settings
    from app.integrations.ahrefs import AhrefsClient
    from app.services import diagnostics
    monkeypatch.setattr(settings, "AHREFS_API_KEY", "k")
    monkeypatch.setattr(AhrefsClient, "units_left", lambda self: 1500000)
    out = diagnostics.run_diagnostics(specs=[s for s in diagnostics._spec() if s[0] == "ahrefs"])
    assert out[0]["status"] == "ok" and out[0]["value"] == 1500000


def test_age_preview_counts_the_older_date(client):
    """Счётчик «проходит возраст» зеркалит W5 (Р5): возраст — старшая из даты RDAP/whois и первого
    снимка; перехваченный домен с молодой регистрацией, но 12 годами архива — проходит."""
    now = datetime.now(timezone.utc)
    with db.SessionLocal() as s:
        s.add_all([Domain(domain="recaught.com", whois_created=now - timedelta(days=365), age_years=12.0),
                   Domain(domain="old.com", whois_created=now - timedelta(days=3650)),
                   Domain(domain="young.com", whois_created=now - timedelta(days=365), age_years=1.0),
                   Domain(domain="unknown.com")])
        s.commit()
    assert client.get("/settings/preview?min_rd=0&min_age=3&approve=0.7&manual=0.4").json()["age"] == 2


def test_preview_counts_skip_legacy_ru_archive(client):
    """R2-16: архив РФ-пула v1 (`legacy_ru`, миграция 0025) машина больше не судит — превью
    «сколько пройдёт» его не считает: тысячи старых .ru раздували бы каждый счётчик."""
    old = datetime.now(timezone.utc) - timedelta(days=3650)
    with db.SessionLocal() as s:
        s.add_all([Domain(domain="old.ru", status="rejected", reject_reason="legacy_ru",
                          referring_domains=500, whois_created=old, score=0.9),
                   Domain(domain="live.com", referring_domains=500, whois_created=old, score=0.9)])
        s.commit()
    got = client.get("/settings/preview?min_rd=1&min_age=3&approve=0.7&manual=0.4").json()
    assert got == {"total": 1, "rd": 1, "age": 1, "approve": 1, "manual": 0}


def test_invalid_lists_flash_error_keep_saved_and_input(client):
    """Контроллер: ValueError из update_settings -> дружелюбная ошибка, а не голый 500; ничего не
    сохранено (прежние значения целы), ввод оператора возвращается в форму. Три случая: не JSON,
    скаляр вместо списка, ключ с точкой (`best.vpn` — не регистрируемое имя)."""
    update_settings(emd_sets=[{"keywords": ["vpn"], "tlds": ["com"]}], tld_allowlist=["com"], min_dr=12)
    for bad in ('[{"keywords": ["x"], oops', "5", '[{"keywords":["best.vpn"],"tlds":["com"]}]'):
        r = client.post("/settings/save", data={**BASE, "v2_lists": "1", "min_dr": 40, "emd_sets": bad,
                                                "tld_allowlist": "mx"}, follow_redirects=False)
        assert r.status_code == 400, bad
        assert 'class="flash err"' in r.text and "Не сохранено" in r.text, bad
        s = get_settings()
        assert s["emd_sets"][0]["keywords"] == ["vpn"] and s["min_dr"] == 12.0, bad
        assert s["tld_allowlist"] == ["com"], bad


def test_brand_tokens_hint_one_brand_one_token(client):
    """Токены режутся по пробелам: «private internet access» стал бы тремя токенами и массово
    ложно отклонял бы домены — подсказка под полем говорит писать бренд слитно."""
    html = client.get("/settings").text
    # фраза есть ТОЛЬКО в подсказке (слово privateinternetaccess рендерится и в textarea из дефолтов)
    assert "один бренд — одно слово" in html and "стал бы тремя словами" in html


def test_error_rerender_keeps_all_operator_input(client):
    """I1: на ошибке форма возвращается с ОТПРАВЛЕННЫМИ значениями (пороги, зоны, бренды, EMD, веса,
    тумблеры), а не сохранёнными: иначе оператор чинит JSON, жмёт «Сохранить» — и остальные правки
    молча теряются. В БД при этом ничего не пишется."""
    update_settings(min_dr=12, tld_allowlist=["com"], brand_tokens=["nordvpn"])
    for bad in ('[{"keywords": ["x"], oops', "5", '[{"keywords":["best.vpn"],"tlds":["com"]}]'):
        r = client.post("/settings/save", data={**BASE, "v2_lists": "1", "min_dr": 40, "tld_allowlist": "mx",
                                                "brand_tokens": "foo", "emd_sets": bad, "units_floor": 750000,
                                                "w_age": 0.5, "nominet": "on"}, follow_redirects=False)
        assert r.status_code == 400 and 'class="flash err"' in r.text, bad
        assert 'name="min_dr" min="0" max="60" step="1" value="40"' in r.text, bad
        assert 'name="units_floor" min="0" max="2000000" step="50000" value="750000"' in r.text, bad
        assert 'name="w_age" min="0" max="1" step="0.01"\n             value="0.5"' in r.text, bad
        assert ">mx</textarea>" in r.text and ">foo</textarea>" in r.text, bad
        assert 'name="nominet" checked' in r.text and 'name="mx" checked' not in r.text, bad
        assert str(html_escape(bad)) in r.text, bad
        s = get_settings()
        assert s["min_dr"] == 12.0 and s["tld_allowlist"] == ["com"] and s["brand_tokens"] == ["nordvpn"], bad


def test_ahrefs_units_zero_or_negative_is_fail(monkeypatch):
    """M4: 0 и отрицательный остаток (перерасход) — «fail»: без units платные волны стоят."""
    from app.config import settings
    from app.integrations.ahrefs import AhrefsClient
    from app.services import diagnostics
    monkeypatch.setattr(settings, "AHREFS_API_KEY", "k")
    for left, status in ((-5, "fail"), (0, "fail"), (1, "ok")):
        monkeypatch.setattr(AhrefsClient, "units_left", lambda self, v=left: v)
        out = diagnostics.run_diagnostics(specs=[s for s in diagnostics._spec() if s[0] == "ahrefs"])
        assert out[0]["status"] == status, left
