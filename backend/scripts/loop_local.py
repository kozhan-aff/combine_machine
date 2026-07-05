"""Whole-loop DRY RUN on SQLite: real LLM content -> edit gate -> publish to ./dist.

Runs M4 (real LiteLLM on the box) + M5 render/deploy end-to-end WITHOUT infra:
provisioning is skipped (needs Cloudflare/VPS) and aaPanel's file write is redirected
to a local ./dist/<domain>/ folder so you get a real, openable site on disk.

The edit step here SIMULATES the human (calls the real mark_edited gate with the draft
body). In production the gate is a person hitting POST /pages/{id}/edit — never automatic.

    PYTHONPATH=. python scripts/loop_local.py
"""
import os
os.environ["DATABASE_URL"] = "sqlite:///loop_local.db"   # override BEFORE app.config loads

import pathlib
from sqlalchemy import select
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB


@compiles(JSONB, "sqlite")
def _jsonb_as_json(element, compiler, **kw):
    return "JSON"


import app.db as db
from app.db import Base
import app.models.domain  # noqa: F401
import app.models.site    # noqa: F401
import app.models.offer   # noqa: F401
import app.models.monitoring  # noqa: F401
from app.models.domain import Domain
from app.models.offer import Offer, SiteOffer
from app.models.site import Page
from app.services import provisioning, content, publish
from app.integrations.aapanel import AaPanelClient

Base.metadata.create_all(db.engine)

DIST = pathlib.Path("dist").resolve()


def _local_write(self, path, page_html):
    """Redirect the VPS file write to ./dist/<...> so the loop runs with no aaPanel."""
    local = DIST / path.lstrip("/").replace("www/wwwroot/", "")
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_text(page_html, encoding="utf-8")
    return {"status": True, "local_path": str(local)}


AaPanelClient.write_file = _local_write   # dry-run stub; production uses the real panel API

# --- seed: one approved domain + one offer (the machine's real input) ---------
with db.SessionLocal() as s:
    dom = s.execute(select(Domain).where(Domain.domain == "orby.ru")).scalar_one_or_none()
    if dom is None:
        dom = Domain(domain="orby.ru", source="backorder", status="approved", score=0.70)
        s.add(dom)
    off = s.execute(select(Offer)).scalars().first()
    if off is None:
        off = Offer(brand="NordVPN", affiliate_link="https://nordvpn.com/?aff=demo",
                    promo_code="SAVE65", country="RU", language="ru")
        s.add(off)
    s.commit()
    domain_id, offer_id = dom.id, off.id

# --- M3 (site row only; real provisioning needs CF/VPS) -----------------------
site_id = provisioning.create_site_for(domain_id)
with db.SessionLocal() as s:
    s.add(SiteOffer(site_id=site_id, offer_id=offer_id))
    s.commit()

# --- M4: real content generation via the box LLM ------------------------------
print("M4: generating draft pages via LiteLLM (box)... this calls the real model.\n")
vertical = ("NordVPN: 6300+ серверов в 111 странах; замер скорости 940→912 Мбит/с на "
            "1 Гбит канале (WireGuard/NordLynx); работает с Netflix US/UK, обход гео-блоков.")
created = content.generate_site(site_id, lang="ru", vertical_data=vertical)
print(f"  created {created} draft pages\n")

# --- edit gate: SIMULATE the human (production = POST /pages/{id}/edit) --------
with db.SessionLocal() as s:
    page_ids = s.execute(select(Page.id).where(Page.site_id == site_id)).scalars().all()
for pid in page_ids:
    content.mark_edited(pid)   # draft -> edited (real gate fn; human step simulated here)
print(f"edit gate: {len(page_ids)} pages marked 'edited' (simulated human)\n")

# --- M5: publish -> ./dist, then show the artifacts ---------------------------
res = publish.publish_site(site_id)
print(f"M5 publish -> {res['status']}: {res['pages']}\n")

print("generated site on disk:")
for f in sorted(DIST.rglob("*.html")):
    print(f"  {f}  ({f.stat().st_size} bytes)")
