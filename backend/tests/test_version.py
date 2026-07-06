"""Версия из git: парсер вывода (без запуска git)."""
from app.services.version import _parse


def test_parse_ok():
    v = _parse("a1b2c3d", "M1: воронка скоринга", "2026-07-06")
    assert v == {"hash": "a1b2c3d", "subject": "M1: воронка скоринга", "date": "2026-07-06"}


def test_parse_empty():
    v = _parse("", "", "")
    assert v["hash"] == "—"
