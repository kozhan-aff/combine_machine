"""FastAPI entrypoint. Include routers from app.api as they are implemented."""
import base64
import secrets
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from starlette.responses import Response

from app.config import settings

app = FastAPI(title="VPN Affiliate Portfolio")

_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


@app.middleware("http")
async def csrf_guard(request: Request, call_next):
    """Same-origin guard на state-changing запросы (защита от CSRF).

    Панель на LAN за опц. Basic-auth; браузер оператора авто-прикладывает закэшированные
    креды к ЛЮБОМУ запросу на этот origin — поэтому враждебная интернет-страница могла бы
    авто-сабмитом дёрнуть POST /admin/pull или подделать «человеческое» подтверждение
    денежного гейта (/queue/{id}/confirm|execute); CORS «simple requests» это не блокирует.
    Правило: если браузер прислал Origin (или Referer) — его хост обязан совпасть с Host
    запроса, иначе 403. Без обоих заголовков (curl/скрипты/API-клиенты/healthcheck) — пропускаем.
    """
    if request.method in _UNSAFE_METHODS:
        source = request.headers.get("origin") or request.headers.get("referer")
        if source and urlsplit(source).netloc != request.headers.get("host", ""):
            return Response("Cross-origin запрос отклонён", status_code=403)
    return await call_next(request)


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    """Basic-auth на всю панель/API, если заданы PANEL_USER+PANEL_PASS.

    Панель выставлена на LAN без иной защиты (docker-compose), а /admin/pull дёргает
    git-pull+reload — поэтому без кредов её мог бы дёрнуть любой в сети. Пусто = выкл
    (локалхост-разработка). /health открыт всегда — для мониторинга/healthcheck.
    """
    if settings.PANEL_USER and settings.PANEL_PASS and request.url.path != "/health":
        hdr = request.headers.get("authorization", "")
        ok = False
        if hdr.startswith("Basic "):
            try:
                user, _, pw = base64.b64decode(hdr[6:]).decode("utf-8").partition(":")
                # обе проверки постоянного времени (защита от тайминг-атаки на логин/пароль)
                ok = (secrets.compare_digest(user, settings.PANEL_USER)
                      & secrets.compare_digest(pw, settings.PANEL_PASS))
            except Exception:  # noqa: BLE001 — битый заголовок = не авторизован
                ok = False
        if not ok:
            return Response("Требуется авторизация", status_code=401,
                            headers={"WWW-Authenticate": 'Basic realm="Combine"'})
    return await call_next(request)


@app.get("/health")
def health():
    return {"status": "ok"}


from app.api import domains, panel, pipeline  # noqa: E402
app.include_router(panel.router)                     # HTML-панель: / /domains /offers /sites/{id} ...
app.include_router(domains.router, prefix="/api")    # JSON: GET /api/domains/
app.include_router(pipeline.router, prefix="/api")   # JSON: /api/offers /api/sites/... (loop actions)
