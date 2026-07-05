"""Wayback Machine history check. Transport + light classification. See docs/api/wayback.md.

Reconstruct what a domain hosted over time -> prior_flags (adult/pharma/casino/gambling/spam),
topic_switch, real age (first snapshot). Heavy — run only on pre-filtered candidates; cache.
"""
import time
from datetime import datetime, timezone
from app.integrations.base import BaseClient

# stop-words per category (EN + RU). Coarse but real; tune against data.
STOPWORDS = {
    "adult": ["porn", "xxx", "escort", "camgirl", "sexcam", "порно", "эротик"],
    "pharma": ["viagra", "cialis", "tadalafil", "pharmacy", "аптека", "таблетк"],
    "casino": ["casino", "roulette", "slots", "казино", "рулетк", "слот"],
    "gambling": ["betting", "poker", "bookmaker", "ставки", "букмекер", "покер"],
    "spam": ["buy cheap", "replica watches", "seo backlinks", "порнуха", "займ онлайн"],
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

        if ok == 0:
            # CDX есть, но ни один снапшот не скачался (archive.org задросселен/недоступен) —
            # историю НЕ проверили; нельзя выдавать чистый вердикт по нулю данных
            return {"prior_flags": {}, "first_seen": first_seen, "age_years": age_years,
                    "wayback_checked": False, "sampled": 0}

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
    print("wayback _classify_text ok")
