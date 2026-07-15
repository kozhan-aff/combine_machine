"""M5: проверка индексации не выдумывает (аудит F15).

Стадия крутится АВТОПИЛОТОМ, поэтому её неточности — не разовая ошибка, а вымысел, который
машина регулярно и молча пишет в IndexHistory. Судья врал в обе стороны сразу:
  (1) сравнивался только ХОСТ — главная в выдаче помечала `/setup` проиндексированной;
  (2) пустая выдача = `not_indexed`, хотя у SearXNG она значит и «движки словили CAPTCHA»
      (код сам это документировал) — «не знаю» выдавалось за «нет».

Фикстуры — форма РЕАЛЬНОГО ответа SearXNG (сверено с боксом 192.168.1.77:8080, 2026-07-14):
`/search?format=json` отдаёт словарь с ключами `results` и `unresponsive_engines` (там же живьём:
`[['brave','too many requests'], ['startpage','CAPTCHA']]`) — оба в ОДНОМ ответе, отдельного
проб-запроса не нужно. Патчим и `search_full` (его зовёт M5), и `search` (его зовёт M1
indexed_echo, и он же был единственной дверью до фикса) — так тест судит ПОВЕДЕНИЕ, а не форму
мока: на старом коде он краснеет вердиктом, а не сетевой ошибкой.
"""
from sqlalchemy import select

import app.db as db
from app.models.domain import Domain
from app.models.site import Site, Page
from app.models.monitoring import IndexHistory


def _add(obj):
    with db.SessionLocal() as s:
        s.add(obj)
        s.commit()
        s.refresh(obj)
        return obj.id


def _site_with_page(domain="mydomain.ru", url_path="/setup"):
    did = _add(Domain(domain=domain, source="backorder", status="purchased"))
    sid = _add(Site(domain_id=did, status="published", doc_root=f"/www/wwwroot/{domain}"))
    pid = _add(Page(site_id=sid, url_path=url_path, title="t", status="published", body="<p>x</p>"))
    return sid, pid


def _serp(monkeypatch, results, dead=()):
    """Мок SearXNG: ответ /search как он есть у живого бокса — results + unresponsive_engines."""
    payload = {"query": "q", "number_of_results": 0, "results": list(results),
               "answers": [], "corrections": [], "infoboxes": [], "suggestions": [],
               "unresponsive_engines": [list(e) for e in dead]}
    monkeypatch.setattr("app.integrations.searxng.SearxngClient.search_full",
                        lambda self, q, **kw: payload, raising=False)
    monkeypatch.setattr("app.integrations.searxng.SearxngClient.search",
                        lambda self, q, **kw: payload["results"])


def _status(pid):
    with db.SessionLocal() as s:
        return s.get(Page, pid).index_status


def _history(pid):
    with db.SessionLocal() as s:
        return [h.index_status for h in s.execute(
            select(IndexHistory).where(IndexHistory.page_id == pid)
            .order_by(IndexHistory.id)).scalars().all()]


# ── (1) главная в выдаче НЕ доказывает, что в индексе /setup ───────────────────
def test_homepage_hit_does_not_prove_a_subpage_is_indexed(monkeypatch):
    """Ядро F15: домен в индексе ≠ ЭТА страница в индексе."""
    from app.services.publish import check_index

    sid, pid = _site_with_page(url_path="/setup")
    _serp(monkeypatch, [{"url": "https://mydomain.ru/", "title": "Главная"},
                        {"url": "https://mydomain.ru/reviews", "title": "Обзоры"}])

    assert check_index(sid)["pages"]["/setup"] == "not_indexed"
    assert _status(pid) == "not_indexed"
    # выдача НЕ пуста => поисковик ответил и нашей страницы не показал — это знание, не «не знаю»
    assert _history(pid) == ["not_indexed"]


def test_deeper_path_does_not_count_as_this_page(monkeypatch):
    """/setup/windows — другая страница, а не «та же с хвостиком»."""
    from app.services.publish import check_index

    sid, _ = _site_with_page(url_path="/setup")
    _serp(monkeypatch, [{"url": "https://mydomain.ru/setup/windows"}])
    assert check_index(sid)["pages"]["/setup"] == "not_indexed"


# ── (2) но и слишком строгим быть нельзя: та же страница в чужой форме ─────────
def test_same_page_in_any_serp_form_counts_as_indexed(monkeypatch):
    """Строгость — та же ложь, вид сбоку: страница В индексе, а машина скажет «нет», и оператор
    пойдёт чинить работающее. Одна страница = один путь, независимо от формы URL."""
    from app.services.publish import check_index

    forms = [
        "https://mydomain.ru/setup",              # каноническая
        "https://mydomain.ru/setup/",             # слэш на конце
        "http://mydomain.ru/setup",               # http вместо https
        "https://www.mydomain.ru/setup",          # www — законный алиас
        "https://mydomain.ru/setup?utm_source=x",  # трекинг-хвост
        "https://mydomain.ru/setup#top",          # фрагмент
        "https://mydomain.ru/Setup",              # регистр (слуги у нас всегда lowercase)
        "https://mydomain.ru/setup/index.html",   # файл вместо каталога
    ]
    for i, url in enumerate(forms):
        # свой сайт на каждую форму (domains.domain уникален), но домен в URL — mydomain.ru:
        # сравнение хоста и так проверено выше, здесь судим ПУТЬ
        sid, _ = _site_with_page(domain=f"mydomain{i}.ru", url_path="/setup")
        _serp(monkeypatch, [{"url": url.replace("mydomain.ru", f"mydomain{i}.ru")}])
        assert check_index(sid)["pages"]["/setup"] == "indexed", url


def test_root_page_matches_only_the_root(monkeypatch):
    from app.services.publish import check_index

    sid, _ = _site_with_page(url_path="/")
    _serp(monkeypatch, [{"url": "https://mydomain.ru/reviews"}])
    assert check_index(sid)["pages"]["/"] == "not_indexed"

    sid2, _ = _site_with_page(domain="other.ru", url_path="/")
    _serp(monkeypatch, [{"url": "https://other.ru/"}])
    assert check_index(sid2)["pages"]["/"] == "indexed"


def test_hostile_lookalike_host_still_rejected(monkeypatch):
    """Гард host_matches никуда не делся: mydomain.ru.evil.com — не наш сайт."""
    from app.services.publish import check_index

    sid, _ = _site_with_page(url_path="/setup")
    _serp(monkeypatch, [{"url": "https://notmydomain.ru.evil.com/setup"}])
    assert check_index(sid)["pages"]["/setup"] == "not_indexed"


# ── (3) мёртвые движки: «не знаю» перестаёт притворяться «нет» ─────────────────
def test_dead_engines_yield_unknown_not_not_indexed(monkeypatch):
    """Пустая выдача + движки в CAPTCHA = спросить НЕ УДАЛОСЬ. Раньше машина записывала это
    как «страницы нет в индексе» — и так, автопилотом, регулярно."""
    from app.services.publish import check_index

    sid, pid = _site_with_page(url_path="/setup")
    _serp(monkeypatch, [], dead=[["duckduckgo", "CAPTCHA"], ["yandex", "CAPTCHA"]])

    assert check_index(sid)["pages"]["/setup"] == "unknown"
    assert _status(pid) == "unknown"
    assert _history(pid) == ["unknown"]
    with db.SessionLocal() as s:            # попытка была — время проверки проставлено
        assert s.get(Page, pid).index_checked_at is not None


def test_empty_serp_with_healthy_engines_is_a_real_no(monkeypatch):
    """Все движки ответили, выдача пуста — вот это законное «нет в индексе»."""
    from app.services.publish import check_index

    sid, _ = _site_with_page(url_path="/setup")
    _serp(monkeypatch, [], dead=[])
    assert check_index(sid)["pages"]["/setup"] == "not_indexed"


def test_partial_engine_failure_with_results_still_judges(monkeypatch):
    """На живом боксе часть движков лежит ПОСТОЯННО (brave/startpage). Правило «умер любой →
    не знаю» означало бы, что машина не скажет `not_indexed` НИКОГДА. Непустая выдача доказывает,
    что запрос обслужен."""
    from app.services.publish import check_index

    sid, _ = _site_with_page(url_path="/setup")
    _serp(monkeypatch, [{"url": "https://mydomain.ru/"}],
          dead=[["brave", "too many requests"], ["startpage", "CAPTCHA"]])
    assert check_index(sid)["pages"]["/setup"] == "not_indexed"


def test_unknown_page_is_asked_again_next_check(monkeypatch):
    """Мёртвый SearXNG — беда поисковика, а не приговор сайту: страница не застревает в
    `unknown`, следующая проверка её переспрашивает."""
    from app.services.publish import check_index

    sid, pid = _site_with_page(url_path="/setup")
    _serp(monkeypatch, [], dead=[["yandex", "CAPTCHA"]])
    assert check_index(sid)["pages"]["/setup"] == "unknown"

    _serp(monkeypatch, [{"url": "https://mydomain.ru/setup/"}])   # движки ожили
    assert check_index(sid)["pages"]["/setup"] == "indexed"
    assert _status(pid) == "indexed"
    assert _history(pid) == ["unknown", "indexed"]


# ── (4) автопилот считает «не выяснено» вслух ─────────────────────────────────
def test_sweep_counts_unknown_pages_separately(monkeypatch):
    """`index_unknown` в счётчиках свипа: «сделано N» при мёртвых движках выдавало бы незнание
    за проделанную работу (прецедент — queue_dirty/ssl_failed)."""
    from app.services.orchestrator import _stage_check_index

    _site_with_page(domain="blind.ru", url_path="/setup")
    _serp(monkeypatch, [], dead=[["startpage", "CAPTCHA"]])

    done, errs, extra = _stage_check_index(10)
    assert (done, errs) == (1, [])
    assert extra == {"index_unknown": 1}


# ── (5) экран сайта: «не знаю» не выглядит ни «в индексе», ни «нет в индексе» ──
def test_site_card_shows_unknown_as_its_own_state(monkeypatch, client):
    from app.services.publish import check_index

    sid, _ = _site_with_page(domain="blind2.ru", url_path="/setup")
    html = client.get(f"/sites/{sid}").text
    assert "не проверялось" in html            # ещё не спрашивали

    _serp(monkeypatch, [], dead=[["yandex", "CAPTCHA"]])
    check_index(sid)
    html = client.get(f"/sites/{sid}").text
    assert "led-warn" in html and "не знаю" in html
    # ни «в индексе», ни «не в индексе» — оба вердикта в таблице печатаются сразу после </span>
    assert "</span>в индексе" not in html and "</span>не в индексе" not in html
    assert "не проверялось" not in html      # проверка БЫЛА, просто ничего не выяснила


# ── (6) .рф: хосты сравниваются в ОДНОЙ форме, иначе вечное `not_indexed` ──────
def test_idn_site_is_seen_as_indexed_across_punycode_and_cyrillic(monkeypatch):
    """В БД домен лежит punycode (`discovery.canonical_domain`), а .рф-сайт выдача показывает
    кириллицей. Сырое сравнение хостов-строк дало бы такому сайту ВЕЧНОЕ `not_indexed` — ту же
    ложь «не нашли = нет в индексе», ради которой писалась вся задача, только с другой стороны
    (M2 по этой же причине сверяет заказы через norm_domain)."""
    from app.services.publish import check_index

    sid, pid = _site_with_page(domain="xn--80aswg.xn--p1ai", url_path="/setup")   # сайт.рф
    _serp(monkeypatch, [{"url": "https://сайт.рф/setup", "title": "Настройка"}])

    assert check_index(sid)["pages"]["/setup"] == "indexed"
    assert _status(pid) == "indexed"


def test_idn_site_not_indexed_stays_not_indexed(monkeypatch):
    """Нормализация не превращается в «всё подходит»: чужая кириллическая выдача и чужой путь
    на .рф-сайте — по-прежнему `not_indexed`."""
    from app.services.publish import check_index

    sid, _ = _site_with_page(domain="xn--80aswg.xn--p1ai", url_path="/setup")     # сайт.рф
    _serp(monkeypatch, [{"url": "https://другой.рф/setup"},          # чужой сайт
                        {"url": "https://сайт.рф/reviews"}])         # наш сайт, чужая страница
    assert check_index(sid)["pages"]["/setup"] == "not_indexed"


def test_garbage_host_in_serp_does_not_sink_the_check(monkeypatch):
    """IDNA падает на кривом хосте (пустая метка, метка >63 символов). URL в выдаче — ЧУЖОЙ
    текст: одна мусорная строка не имеет права уронить проверку и оставить сайт без вердикта."""
    from app.services.publish import check_index

    sid, _ = _site_with_page(domain="mydomain.ru", url_path="/setup")
    _serp(monkeypatch, [{"url": "https://ex..ru/setup"},             # пустая метка
                        {"url": "https://" + "a" * 70 + ".ru/setup"},  # метка длиннее 63
                        {"url": "не url вовсе"},
                        {"url": None},
                        {"url": "https://mydomain.ru/setup"}])       # а вот и наша страница
    assert check_index(sid)["pages"]["/setup"] == "indexed"


# ── (7) сырой `unknown` не доходит до оператора ────────────────────────────────
def test_check_index_flash_says_unknown_out_loud(monkeypatch, client):
    """Флеш ручной кнопки — по-русски и со смыслом: «unknown» оператор прочтёт как «нет»,
    а это ровно то состояние, чей смысл обязан быть произнесён вслух."""
    from urllib.parse import unquote

    sid, _ = _site_with_page(domain="flash.ru", url_path="/setup")
    _serp(monkeypatch, [], dead=[["yandex", "CAPTCHA"]])

    r = client.post(f"/sites/{sid}/check-index", follow_redirects=False)
    assert r.status_code == 303
    flash = unquote(r.headers["location"])
    assert "не знаю (движки молчат)" in flash
    assert "unknown" not in flash


# ── (8) percent-encoding: %3F/%23 — буква сегмента, а не хвост URL ─────────────
def test_encoded_question_mark_is_not_treated_as_a_query():
    """Порядок операций в _norm_path: `?`/`#` режутся ДО unquote. Наоборот — `%3F` в имени
    сегмента становился разделителем и путь обрубался по букве, хвостом не бывшей."""
    from app.services.publish import _norm_path

    assert _norm_path("/what%3Fnow") == "/what?now"      # раньше -> "/what"
    assert _norm_path("/a%23b") == "/a#b"                # раньше -> "/a"
    assert _norm_path("/setup?utm=1#top") == "/setup"    # настоящий хвост режется как и раньше
