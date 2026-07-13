"""Wayback Machine history check. Transport + light classification. See docs/api/wayback.md.

Reconstruct what a domain hosted over time -> prior_flags (adult/pharma/casino/gambling/spam),
topic_switch, real age (first snapshot). Heavy — run only on pre-filtered candidates; cache.
"""
import time
from datetime import datetime, timedelta, timezone
from app.integrations.base import BaseClient

# «Опасное окно» — последние N дней ЖИЗНИ домена (не от «сегодня»: дроп мог перестать
# архивироваться год назад). Именно здесь чистый когда-то сайт становится казино перед сдачей.
_RECENT_DAYS = 730          # 24 месяца
_MID_SPAN_DAYS = 180        # окно «середины жизни» — по полгода в каждую сторону от медианы

# stop-words per category (EN + RU). Coarse but real; tune against data.
# Высокосигнальные маркеры на категорию (EN + RU, упор на RU — дропы .ru). Держим
# ДЛИННЫЕ/однозначные токены (фразы, бренды), а не короткие общие слова: список — это
# hard-reject гейт, ложняк отбраковывает чистый домен. Подстрочный счёт (low.count),
# порог _MIN_HITS на категорию.
STOPWORDS = {
    "adult": ["porn", "xxx", "escort", "camgirl", "sexcam", "webcam girl", "hentai",
              "adult dating", "sex video", "brazzers",
              "порно", "порнуха", "эротик", "интим услуг", "проститутк", "шлюх",
              "вебкам", "секс знакомств"],
    "pharma": ["viagra", "cialis", "tadalafil", "sildenafil", "pharmacy", "tramadol",
               "xanax", "no prescription", "canadian pharmacy",
               "аптека", "таблетк", "виагра", "сиалис", "дженерик", "без рецепта"],
    "casino": ["casino", "roulette", "slots", "jackpot", "blackjack", "baccarat",
               "free spins", "casino bonus", "azino", "azino777", "joycasino",
               "vulkan casino", "pin-up casino", "pinup casino",
               "казино", "рулетк", "слот", "игровые автоматы", "джекпот",
               "азартны", "игровой клуб", "азино777", "вулкан казино",
               "пинап казино", "джойказино"],
    "gambling": ["betting", "poker", "bookmaker", "sportsbook", "betting odds", "wager",
                 "1xbet", "melbet",
                 "ставки на спорт", "букмекер", "покер", "тотализатор", "париматч",
                 "фрибет"],
    "spam": ["buy cheap", "replica watches", "seo backlinks", "payday loan",
             "essay writing", "forex signals", "binary options", "crypto giveaway",
             "займ онлайн", "займы без", "кредит без", "накрутк",
             "прогон хрумер", "заработок в интернете"],
}
_MIN_HITS = 2  # stop-word hits in a snapshot to flag its category


def _classify_text(text: str) -> set[str]:
    """Categories whose stop-words appear >= _MIN_HITS times in the text."""
    low = text.lower()
    found = set()
    for cat, words in STOPWORDS.items():
        if sum(low.count(w) for w in words) >= _MIN_HITS:
            found.add(cat)
    return found


def _ts(timestamp: str) -> datetime:
    return datetime.strptime(timestamp, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)


def _day(dt: datetime) -> str:
    return dt.strftime("%Y%m%d")


def _pick(snaps: list[dict], sample: int) -> list[dict]:
    """Какие снимки СКАЧИВАТЬ. Не «равномерно по индексу»: равномерность отдаёт выборку тому
    окну, где записей больше (у плотно архивируемого домена — молодости), а платим мы за то,
    чем домен был В КОНЦЕ. Берём рождение + середину, остальное — из опасного окна, ОБЯЗАТЕЛЬНО
    включая последний capture; в хвосте предпочитаем РАЗНЫЕ URL (domain-матч приносит поддомены
    и внутренние страницы — корень мог остаться заглушкой, пока казино жило на /casino/)."""
    if len(snaps) <= sample:
        return list(snaps)
    edge = _day(_ts(snaps[-1]["timestamp"]) - timedelta(days=_RECENT_DAYS))
    tail = [s for s in snaps if s["timestamp"][:8] >= edge] or snaps[-1:]

    chosen = [snaps[0], snaps[len(snaps) // 2]]
    seen: set[str] = set()
    for s in reversed(tail):                      # от последнего capture к более ранним
        if len(chosen) >= sample:
            break
        if s["original"] not in seen:
            seen.add(s["original"])
            chosen.append(s)
    for s in reversed(tail):                      # разных URL не хватило — добираем по времени
        if len(chosen) >= sample:
            break
        if s not in chosen:
            chosen.append(s)
    uniq = {(s["timestamp"], s["original"]): s for s in chosen}
    return sorted(uniq.values(), key=lambda s: s["timestamp"])


class WaybackClient(BaseClient):
    def __init__(self):
        super().__init__("http://web.archive.org")

    def _cdx(self, domain: str, *, limit: int, frm: str | None = None, to: str | None = None,
             match_type: str | None = None) -> list[dict]:
        """Один CDX-запрос. HTML-200, одна запись в день (collapse). Семантика лимита —
        живая (проверено на web.archive.org): limit=N — ПЕРВЫЕ N, limit=-N — ПОСЛЕДНИЕ N."""
        params: dict = {
            "url": domain, "output": "json", "fl": "timestamp,original,statuscode",
            "filter": ["statuscode:200", "mimetype:text/html"],
            "collapse": "timestamp:8", "limit": str(limit),
        }
        if match_type:
            params["matchType"] = match_type
        if frm:
            params["from"] = frm
        if to:
            params["to"] = to
        rows = self.request("GET", f"{self.base_url}/cdx/search/cdx", params=params).json()
        return [dict(zip(rows[0], row)) for row in rows[1:]] if rows else []

    def get_snapshots(self, domain: str, per_window: int = 100) -> list[dict]:
        """Снимки ОКНАМИ по времени, а не «первые N». Ascending, без дублей.

        Почему не один запрос: CDX отдаёт записи по возрастанию времени и режет по лимиту —
        значит чем БОГАЧЕ архив (а богатый архив = ценный старый донор), тем УЖЕ окно и тем
        ближе оно к рождению домена. Живой замер: `lenta.ru` с limit=400 -> 199911…200512.
        Последние годы жизни — где домен и превращается в казино перед дропом — не
        запрашивались ВООБЩЕ, а мы штамповали «история чистая» и авто-одобряли.

        Четыре окна (бюджет — 4 запроса на домен, того же порядка, что был):
          1. рождение   — точный матч, первые N (он же даёт first_seen/возраст);
          2. конец жизни — точный матч, ПОСЛЕДНИЕ N: последний capture обязан быть в выборке;
          3. середина    — окно from/to вокруг медианы жизни;
          4. опасное окно — последние 24 месяца жизни, matchType=domain: поддомены и
             внутренние URL. Корень мог остаться заглушкой, пока казино жило на /casino/.

        ВАЖНО: хвостовой лимит (-N) годится ТОЛЬКО для точного матча. У matchType=domain
        живой CDX сортирует записи по urlkey, а не по времени, и «последние N» там — это
        алфавитно-последние URL, а не свежие капчуры. По домену ходим только окнами from/to.

        Сбой любого окна НЕ глушим: неполная история, выданная за проверенную, — ровно тот
        баг, который здесь чинится. Пусть исключение дойдёт до scoring (-> «оценён вслепую»).
        """
        head = self._cdx(domain, limit=per_window)
        if not head:
            return []
        # весь архив уместился в первое окно — хвост уже у нас, второй запрос был бы холостым
        tail = self._cdx(domain, limit=-per_window) if len(head) == per_window else head
        snaps = head + tail
        first, last = _ts(head[0]["timestamp"]), _ts(max(s["timestamp"] for s in (tail or head)))
        if last > first:
            mid = first + (last - first) / 2
            snaps += self._cdx(domain, limit=per_window,
                               frm=_day(mid - timedelta(days=_MID_SPAN_DAYS)),
                               to=_day(mid + timedelta(days=_MID_SPAN_DAYS)))
            snaps += self._cdx(domain, limit=per_window, match_type="domain",
                               frm=_day(last - timedelta(days=_RECENT_DAYS)), to=_day(last))
        uniq = {(s["timestamp"], s["original"]): s for s in snaps}
        return sorted(uniq.values(), key=lambda s: s["timestamp"])

    def _fetch_raw(self, timestamp: str, original: str) -> str:
        # id_ = original archived bytes (no Wayback banner/rewrites) -> best for text classify
        r = self.request("GET", f"{self.base_url}/web/{timestamp}id_/{original}")
        return r.text

    def classify_history(self, domain: str, sample: int = 5, polite: float = 1.0) -> dict:
        """Sample snapshots across the timeline, classify -> prior_flags + age + first_seen."""
        snaps = self.get_snapshots(domain)
        if not snaps:
            # домен не архивировался — историю подтвердить нечем, НЕ выдаём «проверено»
            return {"prior_flags": {}, "first_seen": None, "age_years": None,
                    "wayback_checked": False, "sampled": 0, "evidence": []}

        first_seen = _ts(snaps[0]["timestamp"])
        age_years = round((datetime.now(timezone.utc) - first_seen).days / 365.25, 2)

        cats_by_time: list[set[str]] = []
        evidence: list[dict] = []      # ЧТО именно смотрели — куратор обязан мочь перепроверить
        ok = 0  # реально скачанные и классифицированные снапшоты (не попытки)
        for s in _pick(snaps, sample):
            try:
                cats = _classify_text(self._fetch_raw(s["timestamp"], s["original"]))
                cats_by_time.append(cats)
                evidence.append({"url": s["original"], "timestamp": s["timestamp"],
                                 "cats": sorted(cats)})
                ok += 1
            except Exception:  # noqa: BLE001  # one bad snapshot must not sink the check
                cats_by_time.append(set())
            time.sleep(polite)

        checked = ok >= (sample // 2 + 1)      # «проверено» только при покрытии большинства
        if not checked:
            # мало данных (систематический троттлинг archive.org) — нельзя выдавать чистый
            # вердикт по паре снапшотов; sig-гард в scoring уведёт в manual
            return {"prior_flags": {}, "first_seen": first_seen, "age_years": age_years,
                    "wayback_checked": False, "sampled": ok, "evidence": evidence}

        all_cats = set().union(*cats_by_time) if cats_by_time else set()
        flags = {c: (c in all_cats) for c in STOPWORDS}
        # topic_switch: a bad category present in the later half but not in the earliest snapshot
        early = cats_by_time[0] if cats_by_time else set()
        later = set().union(*cats_by_time[len(cats_by_time) // 2:]) if cats_by_time else set()
        flags["topic_switch"] = bool((later - early) & {"adult", "pharma", "casino", "gambling"})
        return {"prior_flags": flags, "first_seen": first_seen, "age_years": age_years,
                "wayback_checked": True, "sampled": ok, "evidence": evidence}

    def ping(self) -> bool:
        r = self.request("GET", f"{self.base_url}/cdx/search/cdx",
                         params={"url": "example.com", "output": "json", "limit": "1"})
        return isinstance(r.json(), list)


if __name__ == "__main__":  # pure classifier self-check (no network)
    assert _classify_text("Best CASINO online, roulette and slots") == {"casino"}
    assert _classify_text("just one casino here") == set()        # 1 hit < _MIN_HITS
    assert _classify_text("clean vpn review, fast servers") == set()
    assert "adult" in _classify_text("porn xxx camgirl")          # multi-word category sums
    # RU-маркеры истории (дропы backorder.ru — RU-heavy)
    assert "casino" in _classify_text("Игровые автоматы и казино онлайн, джекпот")
    assert "gambling" in _classify_text("Ставки на спорт, букмекер 1xbet и фрибет")
    assert "pharma" in _classify_text("Виагра и сиалис без рецепта, аптека")
    assert "adult" in _classify_text("Интим услуги, проститутки, вебкам")
    assert _classify_text("Обзор лучших vpn для стриминга, быстрые серверы") == set()
    assert "casino" in _classify_text("Вулкан казино, азино777 бонусы")
    print("wayback _classify_text ok")
