"""Wayback Machine history check. Transport + light classification. See docs/api/wayback.md.

Reconstruct what a domain hosted over time -> prior_flags (adult/pharma/casino/gambling/spam),
topic_switch, real age (first snapshot). Heavy — run only on pre-filtered candidates; cache.
"""
import time
from datetime import datetime, timezone
from app.integrations.base import BaseClient

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


class WaybackClient(BaseClient):
    def __init__(self):
        super().__init__("http://web.archive.org")

    def get_snapshots(self, domain: str, limit: int = 400) -> list[dict]:
        """CDX list of HTML 200 captures, one per day, ascending (earliest first)."""
        r = self.request("GET", f"{self.base_url}/cdx/search/cdx", params={
            "url": domain, "output": "json", "fl": "timestamp,original,statuscode",
            "filter": ["statuscode:200"], "collapse": "timestamp:8", "limit": str(limit),
        })
        rows = r.json()
        return [dict(zip(rows[0], row)) for row in rows[1:]] if rows else []

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
                    "wayback_checked": False, "sampled": 0}

        first_ts = snaps[0]["timestamp"]
        first_seen = datetime.strptime(first_ts, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        age_years = round((datetime.now(timezone.utc) - first_seen).days / 365.25, 2)

        # evenly sample across the timeline
        idxs = sorted({int(i * (len(snaps) - 1) / max(sample - 1, 1)) for i in range(sample)})
        cats_by_time: list[set[str]] = []
        ok = 0  # реально скачанные и классифицированные снапшоты (не попытки)
        for i in idxs:
            try:
                cats_by_time.append(_classify_text(self._fetch_raw(snaps[i]["timestamp"], snaps[i]["original"])))
                ok += 1
            except Exception:  # noqa: BLE001  # one bad snapshot must not sink the check
                cats_by_time.append(set())
            time.sleep(polite)

        checked = ok >= (sample // 2 + 1)      # «проверено» только при покрытии большинства
        if not checked:
            # мало данных (систематический троттлинг archive.org) — нельзя выдавать чистый
            # вердикт по паре снапшотов; sig-гард в scoring уведёт в manual
            return {"prior_flags": {}, "first_seen": first_seen, "age_years": age_years,
                    "wayback_checked": False, "sampled": ok}

        all_cats = set().union(*cats_by_time) if cats_by_time else set()
        flags = {c: (c in all_cats) for c in STOPWORDS}
        # topic_switch: a bad category present in the later half but not in the earliest snapshot
        early = cats_by_time[0] if cats_by_time else set()
        later = set().union(*cats_by_time[len(cats_by_time) // 2:]) if cats_by_time else set()
        flags["topic_switch"] = bool((later - early) & {"adult", "pharma", "casino", "gambling"})
        return {"prior_flags": flags, "first_seen": first_seen, "age_years": age_years,
                "wayback_checked": True, "sampled": ok}

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
