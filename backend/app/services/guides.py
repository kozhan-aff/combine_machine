"""Правила письма оператора — папка content_guides/ (спека 2026-10-10 §5).

Корень действует на всё, подпапка языка (ru/en/…) — на язык вывода, подпапка типа (review/
comparison/howto) — на тип страницы. Файлы по алфавиту; итог — один текст для системного промпта
писателя и критика. Загрузка из панели пишет СЮДА ЖЕ, путь строится только из белого списка подпапок
и санированного имени — никаких `..`, абсолютных путей, чужих расширений.
"""
import re
from datetime import datetime, timezone
from pathlib import Path

from app.config import settings

LANGS = ("ru", "en", "de", "es", "fr", "nl", "pt", "it")
KINDS = ("review", "comparison", "howto")
SUBDIRS = ("",) + LANGS + KINDS
ALLOWED_EXT = (".md", ".txt")
MAX_FILE = 200 * 1024
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


def _files_in(sub: str) -> list[Path]:
    d = guides_dir() / sub if sub else guides_dir()
    if not d.is_dir():
        return []
    return sorted(p for p in d.iterdir() if p.is_file() and p.suffix.lower() in ALLOWED_EXT and not p.name.startswith("."))


def _rel(p: Path) -> str:
    return p.relative_to(guides_dir()).as_posix()


def list_guides() -> list[dict]:
    out = []
    for sub in SUBDIRS:
        for p in _files_in(sub):
            st = p.stat()
            out.append({"rel": _rel(p), "size": st.st_size,
                        "mtime": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(timespec="minutes")})
    return out


def load_guides(lang: str | None, kind: str | None, limit: int = 40_000) -> dict:
    """{"text", "files", "truncated"}: корень + язык + тип, по алфавиту, с разделителями `--- rel ---`.
    Превышение лимита режет файлы С КОНЦА целиком (пол-файла правил хуже, чем его отсутствие)."""
    subs = [""] + ([lang] if lang in LANGS else []) + ([kind] if kind in KINDS else [])
    parts, files, total, truncated = [], [], 0, False
    for sub in subs:
        for p in _files_in(sub):
            body = p.read_text(encoding="utf-8", errors="replace").strip()
            chunk = f"--- {_rel(p)} ---\n{body}\n"
            if total + len(chunk) > limit:
                truncated = True
                break
            parts.append(chunk); files.append(_rel(p)); total += len(chunk)
        if truncated:
            break
    return {"text": "\n".join(parts), "files": files, "truncated": truncated}


def _check_name(filename: str) -> str:
    name = (filename or "").strip()
    if not _NAME_RE.match(name) or ".." in name or "/" in name or "\\" in name \
            or name.startswith(".") or not name.lower().endswith(ALLOWED_EXT):
        raise ValueError("имя файла: только буквы/цифры/._-, расширение .md или .txt")
    return name


def save_guide(subdir: str, filename: str, data: bytes) -> str:
    sub = (subdir or "").strip()
    if sub not in SUBDIRS:
        raise ValueError(f"подпапка «{sub}» не из списка: корень, {', '.join(LANGS)}, {', '.join(KINDS)}")
    name = _check_name(filename)
    if len(data) > MAX_FILE:
        raise ValueError(f"файл больше {MAX_FILE // 1024} КБ")
    d = guides_dir() / sub if sub else guides_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_bytes(data)
    return _rel(d / name)


def delete_guide(rel: str) -> None:
    sub, _, name = (rel or "").replace("\\", "/").rpartition("/")
    if sub not in SUBDIRS:
        raise ValueError("путь вне папки правил")
    name = _check_name(name)
    p = (guides_dir() / sub if sub else guides_dir()) / name
    if not p.is_file():
        raise ValueError(f"файла {rel} нет")
    p.unlink()
