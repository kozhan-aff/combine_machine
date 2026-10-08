"""G4: Wayback — словари es/de/fr/nl/pt/it/sl/pl/cs (S1-05), улика не теряется при недочитанной
выборке (S2-04), маленький архив проверяем целиком (S2-05), пусто != сбой (S2-14), предохранитель
и cooldown по 429 (S2-03), общий кэш CDX, https и лёгкий пинг (F8-04/S2-15)."""
import httpx
import pytest

from app.integrations import wayback
from app.integrations.wayback import WaybackClient, WaybackUnavailable, _classify_text

FURNITURE = "<h1>Мебель на заказ</h1><p>Диваны, кресла и шкафы-купе. Доставка по Москве.</p>"
CASINO_HTML = "<p>Игровые автоматы и казино онлайн: джекпот, вулкан казино, бонусы каждый день</p>"


# ---- S1-05: словари ----

@pytest.mark.parametrize("text,cat", [
    ("Apuestas deportivas en línea y casa de apuestas con pronósticos", "gambling"),
    ("Tragamonedas, ruleta y casino en línea con giros gratis", "casino"),
    ("Farmacia en línea sin receta, comprar viagra barato", "pharma"),
    ("Videos de sexo gratis, mujeres desnudas y putas", "adult"),
    ("Online gokken en wedden: gokkasten, speelautomaten en gratis spins", "casino"),
    ("Apotheek zonder recept, potentiemiddel kopen", "pharma"),
    ("Sportwetten bei jedem Wettanbieter: beste Wettquoten", "gambling"),
    ("Online Spielbank mit Spielautomaten und Freispiele", "casino"),
    ("Pharmacie en ligne sans ordonnance, médicaments sans", "pharma"),
    ("Machines à sous et casino en ligne, tours gratuits", "casino"),
    ("Apostas esportivas e casa de apostas online, jogos de azar", "gambling"),
    ("Cassino online com caça-níqueis e rodadas grátis", "casino"),
    ("Scommesse sportive online, quote scommesse e giochi d'azzardo", "gambling"),
    ("Farmacia online senza ricetta, farmaci generici", "pharma"),
    ("Igralnica z igralni avtomati in brezplačni vrtljaji", "casino"),
    ("Športne stave in stavnica z najboljšimi kvotami", "gambling"),
    ("Kasyno online, jednoręki bandyta i darmowe spiny", "casino"),
    ("Zakłady sportowe u każdego bukmacher, kursy bukmacherskie", "gambling"),
    ("Online kasino a hrací automaty, výherní automaty", "casino"),
    ("Sportovní sázky a kurzy sázek u sázkové kanceláře", "gambling"),
    ("Rychlá půjčka bez dokladů, nebankovní půjčky", "spam"),
    ("Szybka pożyczka i chwilówki, pożyczki bez bik", "spam"),
    ("Préstamos rápidos y préstamos sin aval, créditos rápidos", "spam"),
    ("Kredit ohne Schufa, schnelle Kredite und Sofortkredit", "spam"),
])
def test_foreign_dictionaries_catch_dirt(text, cat):
    assert cat in _classify_text(text)


def test_foreign_clean_texts_stay_clean():
    """Словари — hard-reject гейт: обычный текст на тех же языках не должен срабатывать."""
    for t in ("Nuestro taller familiar fabrica muebles de madera a medida desde 1985",
              "Onze bakkerij verkoopt vers brood, taarten en koffie in het centrum",
              "Unser Familienbetrieb bietet Heizungsbau und Sanitärinstallation in Köln",
              "Kancelář nabízí účetnictví, daňové poradenství a mzdy pro malé firmy",
              # подстроки коротких стоп-слов внутри обычных слов (ревью G4: putas/film x/cassino)
              "Las disputas vecinales se resuelven en el juzgado; las disputas laborales también",
              "Ons film X-Men avond: film X-Men is vanavond te zien in de bioscoop",
              "La città di Cassino ospita un museo; Cassino fu ricostruita dopo la guerra"):
        assert _classify_text(t) == set()


# ---- S2-04 / S2-05: классификация при малой выборке ----

def _client(pages_by_ts: dict, ok_ts: set | None = None):
    """Архив из pages_by_ts {timestamp: html}; снимки вне ok_ts не скачиваются (троттлинг)."""
    c = WaybackClient()
    snaps = [{"timestamp": ts, "original": f"http://x.com/{i}"} for i, ts in enumerate(sorted(pages_by_ts))]
    c.get_snapshots = lambda domain, **kw: list(snaps)

    def fetch(ts, original):
        if ok_ts is not None and ts not in ok_ts:
            raise httpx.ReadTimeout("throttled")
        return pages_by_ts[ts]
    c._fetch_raw = fetch
    return c


def test_dirty_snapshot_survives_partial_read():
    """5 снимков, прочитаны 2, на одном казино: раньше prior_flags={} (улика выброшена)."""
    pages = {f"20{10 + i}0101000000": FURNITURE for i in range(5)}
    pages["20140101000000"] = CASINO_HTML
    c = _client(pages, ok_ts={"20100101000000", "20140101000000"})
    h = c.classify_history("x.com", polite=0)
    assert h["wayback_checked"] is False and h["sampled"] == 2
    assert h["prior_flags"].get("casino") is True


def test_partial_read_without_dirt_stays_unknown_not_clean():
    pages = {f"20{10 + i}0101000000": FURNITURE for i in range(5)}
    h = _client(pages, ok_ts={"20100101000000"}).classify_history("x.com", polite=0)
    assert h["wayback_checked"] is False and not any(h["prior_flags"].values())


@pytest.mark.parametrize("n", [1, 2])
def test_tiny_archive_read_fully_is_checked(n):
    pages = {f"20{10 + i}0101000000": FURNITURE for i in range(n)}
    h = _client(pages).classify_history("x.com", polite=0)
    assert h["wayback_checked"] is True and h["sampled"] == n


def test_tiny_archive_half_read_is_not_checked():
    pages = {f"20{10 + i}0101000000": FURNITURE for i in range(2)}
    h = _client(pages, ok_ts={"20100101000000"}).classify_history("x.com", polite=0)
    assert h["wayback_checked"] is False


# ---- S2-14: пусто != сбой ----

def test_probe_empty_is_confirmed_by_second_query(monkeypatch):
    c = WaybackClient()
    seen = []
    monkeypatch.setattr(c, "_cdx", lambda d, **kw: seen.append(kw) or [])
    assert c.probe("x.com")["archive_empty"] is True
    assert len(seen) == 2 and seen[1]["limit"] == -1       # второй запрос — ДРУГОЙ


def test_probe_disagreement_is_not_empty(monkeypatch):
    c = WaybackClient()
    calls = iter([[], [{"timestamp": "20150101000000", "original": "http://x.com/"}]])
    monkeypatch.setattr(c, "_cdx", lambda d, **kw: next(calls))
    p = c.probe("x.com")
    assert p["archive_empty"] is False and p["first_seen"] is None


def test_probe_gives_first_seen_age(monkeypatch):
    c = WaybackClient()
    monkeypatch.setattr(c, "_cdx", lambda d, **kw: [{"timestamp": "20050101000000", "original": "u"}])
    p = c.probe("x.com")
    assert p["archive_empty"] is False and p["age_years"] > 15 and p["snapshots"] == 1


# ---- S2-03: транспорт ----

class _Clock:
    def __init__(self):
        self.t = 1000.0
        self.slept = []

    def now(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


@pytest.fixture
def clk(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(wayback, "_clock", c.now)
    monkeypatch.setattr(wayback, "_sleep", c.sleep)
    return c


def _status(code, headers=None):
    req = httpx.Request("GET", "https://web.archive.org/x")
    return httpx.HTTPStatusError("e", request=req, response=httpx.Response(code, headers=headers, request=req))


def _seq(c, outcomes):
    """_request_once отдаёт исходы по очереди (исключение — бросается)."""
    it, calls = iter(outcomes), []

    def once(method, url, **kw):
        calls.append(url)
        o = next(it)
        if isinstance(o, Exception):
            raise o
        return o
    c._request_once = once
    return calls


def test_request_retries_once_not_thrice(clk):
    c = WaybackClient()
    calls = _seq(c, [_status(503), _status(503), _status(503)])
    with pytest.raises(httpx.HTTPStatusError):
        c.request("GET", "https://web.archive.org/x")
    assert len(calls) == 2


def test_retry_after_pauses_all_threads(clk):
    c = WaybackClient()
    _seq(c, [_status(429, {"Retry-After": "7"}), httpx.Response(200, json=[])])
    c.request("GET", "https://web.archive.org/x")
    assert sum(clk.slept) >= 7            # пауза по Retry-After, а не 1-2 с экспоненты


def test_long_retry_after_refuses_instead_of_sleeping(clk):
    c = WaybackClient()
    _seq(c, [_status(429, {"Retry-After": "100"}), httpx.Response(200, json=[])])
    with pytest.raises(WaybackUnavailable):
        c.request("GET", "https://web.archive.org/x")
    assert sum(clk.slept) == 0            # слот волны не засыпает на 100 с


def test_breaker_opens_after_consecutive_failures(clk):
    c = WaybackClient()
    calls = _seq(c, [httpx.ConnectTimeout("t")] * 40)
    for _ in range(wayback._FAIL_LIMIT):
        with pytest.raises(httpx.ConnectTimeout):
            c.request("GET", "https://web.archive.org/x")
    n = len(calls)
    with pytest.raises(WaybackUnavailable):
        c.request("GET", "https://web.archive.org/x")
    assert len(calls) == n                # лежащий архив больше не бомбим


def test_success_resets_breaker(clk):
    c = WaybackClient()
    ok = httpx.Response(200, json=[])
    _seq(c, [httpx.ConnectTimeout("t")] * 2 + [ok])     # запрос = 2 попытки
    with pytest.raises(httpx.ConnectTimeout):
        c.request("GET", "https://web.archive.org/x")
    assert c._fails == 1
    c.request("GET", "https://web.archive.org/x")
    assert c._fails == 0


def test_404_snapshot_is_not_a_channel_failure(clk):
    c = WaybackClient()
    _seq(c, [_status(404)] * 10)
    for _ in range(8):
        with pytest.raises(httpx.HTTPStatusError):
            c.request("GET", "https://web.archive.org/x")
    assert c._fails == 0


def test_token_bucket_spaces_requests(clk):
    c = WaybackClient()
    _seq(c, [httpx.Response(200, json=[])] * 3)
    for _ in range(3):
        c.request("GET", "https://web.archive.org/x")
    assert sum(clk.slept) >= 2 * wayback._MIN_INTERVAL - 1e-9


def test_cdx_cache_is_shared_and_empty_is_not_cached(clk):
    c = WaybackClient()
    rows = [["timestamp", "original", "statuscode"], ["20100101000000", "http://x.com/", "200"]]
    calls = _seq(c, [httpx.Response(200, json=[]), httpx.Response(200, json=rows)])
    assert c._cdx("x.com", limit=100) == []
    assert len(c._cdx("x.com", limit=100)) == 1       # пустое не закэшировано: второй запрос ушёл
    assert len(c._cdx("x.com", limit=100)) == 1       # непустое — из кэша
    assert len(calls) == 2


def test_fetch_failure_waybackunavailable_propagates(monkeypatch):
    """Архив лёг посреди домена — это не «один плохой снимок»: классификатор не глотает."""
    c = WaybackClient()
    c.get_snapshots = lambda d, **kw: [{"timestamp": "20100101000000", "original": "http://x.com/"}]

    def boom(ts, o):
        raise WaybackUnavailable("down")
    c._fetch_raw = boom
    with pytest.raises(WaybackUnavailable):
        c.classify_history("x.com")


def test_https_and_split_timeouts():
    c = WaybackClient()
    assert c.base_url.startswith("https://")
    t = c._client.timeout
    assert t.connect == 5.0 and t.read == 45.0


def test_ping_is_light_and_does_not_follow_redirect(monkeypatch):
    c = WaybackClient()
    seen = {}

    def get(url, **kw):
        seen.update(url=url, **kw)
        return httpx.Response(302)
    monkeypatch.setattr(c._client, "get", get)
    assert c.ping() is True
    assert "/cdx/" not in seen["url"] and seen["follow_redirects"] is False
    assert seen["timeout"].read <= 10
    monkeypatch.setattr(c._client, "get", lambda url, **kw: httpx.Response(503))
    assert c.ping() is False
