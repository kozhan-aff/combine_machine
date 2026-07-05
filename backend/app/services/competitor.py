"""M4 — структура от конкурента из выдачи (опц. обогащение скаффолда, PLAN §M4).

Зачем: чтобы черновик покрывал темы, которые реально ждёт пользователь (и топ Google),
а не только то, что придумал LLM. Берём топ-конкурента по «{бренд} обзор», тянем его
страницу через A-Parser (ходит через прокси — пробивает Cloudflare, в отличие от сырого
GET/Browserless; последний репой запрещён в рантайме) и вынимаем H2/H3 как «карту тем».

НЕ копируем контент — только структуру-подсказку для полноты охвата. Best-effort: любая
осечка (A-Parser недоступен, антибот, пустая страница) -> None, генерация идёт без неё.
Парсинг заголовков — stdlib html.parser, без новых зависимостей.
"""
from html.parser import HTMLParser

# агрегаторы/соцсети/форумы — там нет редакторской структуры обзора, пропускаем
_NOISE = ("reddit.", "youtube.", "youtu.be", "trustpilot.", "quora.", "habr.",
          "pikabu.", "facebook.", "twitter.", "x.com", "vk.com", "t.me",
          "wikipedia.", "otzovik.", "irecommend.")


class _Headings(HTMLParser):
    """Собирает текст H1/H2/H3 в порядке документа -> [(tag, text), ...]."""
    def __init__(self):
        super().__init__()
        self.items: list[tuple[str, str]] = []
        self._tag: str | None = None
        self._buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in ("h1", "h2", "h3"):
            self._tag, self._buf = tag, []

    def handle_data(self, data):
        if self._tag:
            self._buf.append(data)

    def handle_endtag(self, tag):
        if tag == self._tag:
            txt = " ".join("".join(self._buf).split())
            if txt:
                self.items.append((tag, txt))
            self._tag, self._buf = None, []


def extract_headings(html: str, cap: int = 12) -> list[str]:
    """H2/H3 из HTML: чистим, деду́пим, отсекаем мусор (нав/слишком длинные). Чистая функция."""
    p = _Headings()
    p.feed(html)
    out: list[str] = []
    seen: set[str] = set()
    for tag, txt in p.items:
        if tag == "h1":
            continue  # h1 = заголовок статьи, не структура
        words = txt.split()
        if not (2 <= len(words) <= 12):
            continue  # односложные пункты меню / слишком длинные абзацы-в-заголовке
        low = txt.lower()
        if low in seen:
            continue
        seen.add(low)
        out.append(txt)
        if len(out) >= cap:
            break
    return out


def _norm(s: str) -> str:
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


def outline_for(brand: str, lang: str = "ru", cap: int = 12) -> dict | None:
    """Топ-конкурент по бренду -> {'url', 'headings':[...]} или None (best-effort).

    Пропускает агрегаторы и собственный сайт бренда. Сеть/парсер завёрнуты — любая
    ошибка гасится в None, чтобы генерация не падала из-за необязательного шага.
    """
    from urllib.parse import urlparse
    from app.integrations.aparser import AParserClient

    query = f"{brand} обзор" if lang == "ru" else f"{brand} review"
    brand_key = _norm(brand)
    try:
        ap = AParserClient()
        for url in ap.serp_urls(query):
            # фильтруем по ХОСТУ, не по всему URL: бренд в пути (/reviews/nordvpn) —
            # это конкурент, а не сайт бренда; отсекаем только nordvpn.com в домене
            host = (urlparse(url).hostname or "").lower()
            if any(n.rstrip(".") in host for n in _NOISE):
                continue
            if brand_key and brand_key in _norm(host):
                continue  # сайт самого бренда — не конкурент
            html = ap.fetch_html(url)
            if not html:
                continue
            heads = extract_headings(html, cap=cap)
            if len(heads) >= 3:                 # значимая структура найдена
                return {"url": url, "headings": heads}
        return None
    except Exception:  # noqa: BLE001 — необязательный шаг, не роняем генерацию
        return None


if __name__ == "__main__":  # self-check парсера заголовков, без сети
    sample = """<html><head><title>t</title></head><body>
      <h1>NordVPN Review 2026</h1>
      <nav><h2>Menu</h2></nav>
      <h2>Скорость и реальные замеры</h2>
      <h3>Обход блокировок Netflix</h3>
      <h2>Скорость и реальные замеры</h2>  <!-- дубль -->
      <h2>Цена и гарантия возврата средств за 30 дней</h2>
      <h2>Слишком длинный заголовок который на самом деле является целым абзацем текста и не должен попасть в карту тем вообще никак</h2>
    </body></html>"""
    heads = extract_headings(sample)
    assert "Скорость и реальные замеры" in heads
    assert "Обход блокировок Netflix" in heads
    assert heads.count("Скорость и реальные замеры") == 1        # дедуп
    assert "Menu" not in heads                                    # односложный отсеян
    assert not any(len(h.split()) > 12 for h in heads)           # длинный отсеян
    assert "NordVPN Review 2026" not in heads                    # h1 не берём
    print("competitor extract_headings ok:", heads)
