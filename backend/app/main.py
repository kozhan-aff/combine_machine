"""FastAPI entrypoint. Include routers from app.api as they are implemented."""
from fastapi import FastAPI

app = FastAPI(title="VPN Affiliate Portfolio")


@app.get("/health")
def health():
    return {"status": "ok"}


from app.api import domains, panel, pipeline  # noqa: E402
app.include_router(panel.router)       # GET / -> shortlist panel
app.include_router(domains.router)     # GET /domains -> JSON API
app.include_router(pipeline.router)    # POST loop actions: provision/generate/edit/publish
