"""M1a — Domain discovery. See BUILD_SPEC.md §7.

Pull candidates from the backorder public feed (no auth) and upsert into `domains`
with status='discovered'. Feed `links` (donor count) rides straight into referring_domains
as a free RD signal. Transport lives in integrations; this is the business logic.
"""
import re

_DOMAIN_RE = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)+$")


def normalize_row(row: dict) -> dict | None:
    """One feed row -> {domain, source, referring_domains} or None if junk."""
    domain = (row.get("domainname") or "").strip().lower().rstrip(".")
    if not domain or len(domain) > 253 or not _DOMAIN_RE.match(domain):
        return None
    try:
        links = int(row.get("links") or 0)
    except (TypeError, ValueError):
        links = 0
    return {"domain": domain, "source": "backorder", "referring_domains": links}


def run_discovery(min_links: int = 1) -> int:
    """Fetch the drop feed, upsert new candidates, return count of newly inserted domains."""
    from sqlalchemy import select
    from app.db import SessionLocal
    from app.models.domain import Domain
    from app.integrations.backorder import BackorderClient

    rows = BackorderClient().list_dropping(min_links=min_links)
    candidates = {c["domain"]: c for c in (normalize_row(r) for r in rows) if c}
    if not candidates:
        return 0

    with SessionLocal() as db:
        existing = set(db.execute(
            select(Domain.domain).where(Domain.domain.in_(candidates))
        ).scalars().all())
        new = [Domain(**c) for name, c in candidates.items() if name not in existing]
        db.add_all(new)
        db.commit()
        return len(new)


if __name__ == "__main__":  # pure normalize self-check (no network)
    assert normalize_row({"domainname": "Example.COM.", "links": "12"}) == {
        "domain": "example.com", "source": "backorder", "referring_domains": 12}
    assert normalize_row({"domainname": "under_score.ru", "links": 1}) is None  # junk char
    assert normalize_row({"domainname": "", "links": 5}) is None
    assert normalize_row({"domainname": "sub.dropzone.ru"})["referring_domains"] == 0
    print("discovery normalize_row ok")
