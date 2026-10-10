"""Правила письма оператора — папка content_guides/ (спека 2026-10-10 §5).

Одна плоская папка: каждый файл действует на все сайты, языки и типы страниц (подпапки по языку и
типу убраны 2026-10-10 — правила оператора на практике общие). Файлы по алфавиту; итог — один текст
для системного промпта писателя и критика. Загрузка из панели пишет СЮДА ЖЕ, имя санируется —
никаких `..`, путей, чужих расширений.
"""
import re
from datetime import datetime, timezone
from pathlib import Path

from app.config import settings

ALLOWED_EXT = (".md", ".txt")
MAX_FILE = 200 * 1024
LIMIT = 40_000                       # символов на весь блок правил в промпте
_NAME_RE = re.compile(r"^[A-Za-z0-9А-Яа-яЁё._-]{1,80}$")


def guides_dir() -> Path:
    """Явный CONTENT_GUIDES_DIR, иначе /repo/content_guides (бокс, compose монтирует репо в /repo),
    иначе <корень репо>/content_guides от этого файла (локальный запуск). Может не существовать."""
    if settings.CONTENT_GUIDES_DIR:
        return Path(settings.CONTENT_GUIDES_DIR)
    repo = Path("/repo/content_guides")
    if repo.is_dir():
        return repo
    return Path(__file__).resolve().parents[3] / "content_guides"


def _files() -> list[Path]:
    d = guides_dir()
    if not d.is_dir():
        return []
    # README — пояснение к папке, не правило: в промпт не попадает
    return sorted(p for p in d.iterdir() if p.is_file() and p.suffix.lower() in ALLOWED_EXT
                  and not p.name.startswith(".") and not p.name.lower().startswith("readme"))


def load_guides(lang: str | None = None, kind: str | None = None, limit: int | None = None) -> dict:
    """{"text", "files", "truncated"}: все файлы папки по алфавиту с разделителями `--- имя ---`.
    `lang`/`kind` не используются (правила общие) — оставлены в сигнатуре для вызывающих.
    Превышение лимита режет файлы С КОНЦА целиком (пол-файла правил хуже, чем его отсутствие)."""
    limit = LIMIT if limit is None else limit
    parts, files, total, truncated = [], [], 0, False
    for p in _files():
        body = p.read_text(encoding="utf-8", errors="replace").strip()
        chunk = f"--- {p.name} ---\n{body}\n"
        if total + len(chunk) > limit:
            truncated = True
            break
        parts.append(chunk); files.append(p.name); total += len(chunk)
    return {"text": "\n".join(parts), "files": files, "truncated": truncated}


def list_guides() -> list[dict]:
    """Файлы для панели: имя, размер, дата и `used` — попал ли файл в промпт (не срезан лимитом)."""
    used = set(load_guides()["files"])
    out = []
    for p in _files():
        st = p.stat()
        out.append({"rel": p.name, "size": st.st_size, "used": p.name in used,
                    "mtime": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(timespec="minutes")})
    return out


def _check_name(filename: str) -> str:
    # браузер при выборе папки шлёт «папка/файл.md» — берём только имя файла
    name = (filename or "").strip().replace("\\", "/").rpartition("/")[2]
    if not _NAME_RE.match(name) or ".." in name or name.startswith(".") \
            or not name.lower().endswith(ALLOWED_EXT):
        raise ValueError(f"«{name or filename}»: имя — только буквы/цифры/._-, расширение .md или .txt")
    if name.lower().startswith("readme."):
        # README в промпт не попадает (_files) — загрузка под этим именем тихо пропала бы
        raise ValueError("README — пояснение, не правило: назови файл иначе")
    return name


def save_guide(filename: str, data: bytes) -> str:
    name = _check_name(filename)
    if len(data) > MAX_FILE:
        raise ValueError(f"«{name}»: файл больше {MAX_FILE // 1024} КБ")
    d = guides_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_bytes(data)
    return name


def delete_guide(name: str) -> None:
    if "/" in (name or "") or "\\" in (name or ""):
        raise ValueError("путь вне папки правил")
    name = _check_name(name)
    p = guides_dir() / name
    if not p.is_file():
        raise ValueError(f"файла {name} нет")
    p.unlink()
