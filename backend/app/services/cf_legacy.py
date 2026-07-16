"""Импорт существующей пары CLOUDFLARE_API_TOKEN/CLOUDFLARE_ACCOUNT_ID как legacy-Connection.
secret_ref = 'env:CLOUDFLARE_API_TOKEN' (сам токен в БД НЕ пишется). Идемпотентно: повторный
вызов не плодит строк. .env fallback НЕ удаляется — старый провижн (P1) им ещё пользуется.

P0: только заводит строку CloudflareConnection. Ни одного сетевого запроса к Cloudflare здесь
нет — верификация токена (status unverified -> ok/error) приезжает отдельной задачей плана."""
import hashlib

from app.config import settings
from app.models.cloudflare import CloudflareConnection

LEGACY_SECRET_REF = "env:CLOUDFLARE_API_TOKEN"


def import_legacy_connection(db) -> int | None:
    token = settings.CLOUDFLARE_API_TOKEN
    if not token:
        return None
    existing = (db.query(CloudflareConnection)
                  .filter_by(secret_ref=LEGACY_SECRET_REF).first())
    if existing:
        return existing.id
    fp = hashlib.sha256(token.encode()).hexdigest()
    conn = CloudflareConnection(
        label="legacy .env",
        secret_ref=LEGACY_SECRET_REF,
        # account_id НЕ доказывает владельца токена: живой .env-токен проверен user-owned
        # (2026-07-11, /user/tokens/verify, 20 зон). account-owned токены заводятся отдельным
        # путём с явным token_kind, не этим legacy-импортом (аудит 2026-07-15, F1.1).
        token_kind="user",
        owner_cf_account_id=settings.CLOUDFLARE_ACCOUNT_ID or None,
        token_fingerprint=fp,
        token_hint="..." + token[-4:] if len(token) >= 4 else None,
        status="unverified",
    )
    db.add(conn)
    db.commit()
    return conn.id
