"""Общие гейты роутов. Живут отдельно от panel.py, чтобы JSON-двойник /api и HTML-панель проходили
ОДНИ И ТЕ ЖЕ ворота (S4-11/S7-15: гейт висел только на HTML-роуте, а /api/sites/{id}/provision
мутировал Cloudflare без него)."""
from fastapi import HTTPException

from app.config import settings


def require_cf_write() -> None:
    """Hard gate: любой CF-write требует НАСТРОЕННЫЙ panel auth. Same-origin недостаточен
    (аудит §11/§15) — панель живёт на LAN, а same-origin ничего не доказывает про то, кто
    физически может достучаться до порта. Транспортная Basic-проверка (если включена) стоит
    отдельно; здесь проверяется, что auth ВООБЩЕ сконфигурирован — иначе плоская LAN-экспозиция
    открывает Cloudflare-мутации кому угодно, кто знает IP."""
    if not (settings.PANEL_USER and settings.PANEL_PASS):
        raise HTTPException(status_code=403,
                            detail="Cloudflare-операции требуют настроенных PANEL_USER/PANEL_PASS")
