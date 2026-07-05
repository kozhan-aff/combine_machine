"""FastAPI entrypoint. Include routers from app.api as they are implemented."""
from fastapi import FastAPI

app = FastAPI(title="VPN Affiliate Portfolio")


@app.get("/health")
def health():
    return {"status": "ok"}


from app.api import domains, panel, pipeline  # noqa: E402
app.include_router(panel.router)                     # HTML-панель: / /domains /offers /sites/{id} ...
app.include_router(domains.router, prefix="/api")    # JSON: GET /api/domains/
app.include_router(pipeline.router, prefix="/api")   # JSON: /api/offers /api/sites/... (loop actions)
