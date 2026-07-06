"""Live M1 smoke: real discovery + scoring into a local SQLite (no Docker/PG).

Hits the real free stack (backorder drop feed, Wayback, RKN, SearXNG) and persists
to ./m1_live.db so you can eyeball the funnel before Postgres is up. Small sample —
Wayback is rate-limited, so be polite.

    PYTHONPATH=. python scripts/m1_live.py [sample]   # domains to score, default 3
"""
import os
os.environ["DATABASE_URL"] = "sqlite:///m1_live.db"   # override BEFORE app.config loads

import sys
from sqlalchemy import select
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB


@compiles(JSONB, "sqlite")
def _jsonb_as_json(element, compiler, **kw):  # DDL only; dicts still round-trip via json
    return "JSON"


import app.db as db
from app.db import Base
import app.models.domain  # noqa: F401  (register tables)
import app.models.site    # noqa: F401
import app.models.offer   # noqa: F401
import app.models.monitoring  # noqa: F401
from app.models.domain import Domain
from app.services import discovery, scoring

Base.metadata.create_all(db.engine)

sample = int(sys.argv[1]) if len(sys.argv) > 1 else 3

n = discovery.run_discovery()
print(f"discovery: +{n} new candidates from the live backorder feed\n")

with db.SessionLocal() as s:
    ids = s.execute(
        select(Domain.id).where(Domain.status == "discovered")
        .order_by(Domain.referring_domains.desc()).limit(sample)   # score the top donors
    ).scalars().all()

print(f"scoring {len(ids)} top-donor domains LIVE (Wayback ~5s each)...\n")
print(f"{'domain':34} {'rd':>4} {'age':>5}  rkn  echo  flags / score status")
print("-" * 92)
for did in ids:
    r = scoring.score_domain(did)
    with db.SessionLocal() as s:
        d = s.get(Domain, did)
    bad = [k for k, v in (d.prior_flags or {}).items() if v]
    err = ("  ERR:" + ",".join(r["errors"])) if r.get("errors") else ""
    age = f"{float(d.age_years):.1f}" if d.age_years is not None else "  ?"
    print(f"{d.domain:34} {d.referring_domains or 0:>5} {age:>5}  "
          f"{str(d.rkn_listed):>5} {str(d.indexed_echo):>5}  {bad} "
          f"-> {r['score']:.4f} {r['status']}{err}")
