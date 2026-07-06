"""Текущая версия кода из git в контейнере (репо смонтировано /repo). Дёшево, локально."""
import subprocess


def _parse(h: str, subject: str, date: str) -> dict:
    return {"hash": h.strip() or "—", "subject": subject.strip() or "—", "date": date.strip() or "—"}


def current_version() -> dict:
    """{hash, subject, date} последнего коммита /repo; {error} если git недоступен."""
    try:
        out = subprocess.run(
            ["git", "-C", "/repo", "-c", "safe.directory=/repo", "log", "-1", "--format=%h%n%s%n%cs"],
            capture_output=True, text=True, timeout=10)
        if out.returncode != 0:
            return {"error": (out.stderr or "git error").strip()[:150]}
        parts = (out.stdout.strip().split("\n") + ["", "", ""])[:3]
        return _parse(*parts)
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"[:150]}
