"""ORIGIN_CA_AUTO: пустая строка в .env (как велит .env.example «пусто/0») не должна ронять запуск."""
import pytest

from app.config import Settings


@pytest.mark.parametrize("raw,want", [("", False), ("  ", False), ("0", False), ("1", True), ("true", True)])
def test_origin_ca_auto_blank_env_means_off(monkeypatch, raw, want):
    monkeypatch.setenv("ORIGIN_CA_AUTO", raw)
    assert Settings(_env_file=None).ORIGIN_CA_AUTO is want


def test_env_example_origin_ca_line_parses():
    """Сама строка из .env.example (cp .env.example .env) обязана быть валидной."""
    import pathlib
    line = next(l for l in pathlib.Path(__file__).resolve().parents[2].joinpath(".env.example")
                .read_text().splitlines() if l.startswith("ORIGIN_CA_AUTO="))
    assert Settings(_env_file=None, ORIGIN_CA_AUTO=line.split("=", 1)[1].split("#")[0].strip()).ORIGIN_CA_AUTO is False
