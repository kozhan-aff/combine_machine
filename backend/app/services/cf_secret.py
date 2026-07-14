"""Резолвер secret_ref для Cloudflare-токенов. Токен НИКОГДА не хранится в БД — только ссылка.
Формы: env:NAME (имя [A-Z0-9_]) | file:BASENAME (в allowlisted read-only каталоге). Значение секрета
не попадает ни в один текст ошибки/fingerprint/лог (аудит §2)."""
import os
import re
from app.config import settings

_MAX_BYTES = 8 * 1024
_ENV_NAME = re.compile(r"^[A-Z0-9_]+$")


class SecretRefError(Exception):
    """Проблема с secret_ref. Сообщение содержит только ref/имя, НИКОГДА не значение секрета."""


def resolve_secret_ref(ref: str) -> str:
    if not isinstance(ref, str) or ":" not in ref:
        raise SecretRefError(f"плохой secret_ref: {ref!r}")
    scheme, _, rest = ref.partition(":")
    if scheme == "env":
        if not _ENV_NAME.match(rest):
            raise SecretRefError(f"недопустимое имя env-переменной: {rest!r}")
        val = os.environ.get(rest)
        if not val:
            raise SecretRefError(f"env-переменная не задана: {rest}")
        return val.rstrip("\n") if val.endswith("\n") else val
    if scheme == "file":
        base = settings.CLOUDFLARE_SECRETS_DIR or "/run/secrets/cloudflare"
        # basename без разделителей и .. — только простое имя файла
        if not rest or "/" in rest or "\\" in rest or rest in (".", "..") or "\x00" in rest:
            raise SecretRefError(f"недопустимое имя secret-файла: {rest!r}")
        root = os.path.realpath(base)
        full = os.path.realpath(os.path.join(root, rest))
        # symlink-escape / traversal: реальный путь обязан лежать строго внутри root
        if full != os.path.join(root, rest) and not full.startswith(root + os.sep):
            raise SecretRefError(f"secret-файл вне разрешённого каталога: {rest!r}")
        if not full.startswith(root + os.sep):
            raise SecretRefError(f"secret-файл вне разрешённого каталога: {rest!r}")
        if not os.path.isfile(full):
            raise SecretRefError(f"secret-файл не найден: {rest!r}")
        if os.path.getsize(full) > _MAX_BYTES:
            raise SecretRefError(f"secret-файл слишком велик: {rest!r}")
        with open(full, "r", encoding="utf-8") as fh:
            data = fh.read(_MAX_BYTES + 1)
        if len(data.encode("utf-8")) > _MAX_BYTES:
            raise SecretRefError(f"secret-файл слишком велик: {rest!r}")
        return data[:-1] if data.endswith("\n") else data
    raise SecretRefError(f"неизвестная схема secret_ref: {scheme!r}")
