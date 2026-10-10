"""Карточка «Правила письма» на /settings: список, загрузка, удаление; мусорные пути не проходят."""
import pytest
from app.config import settings
from app.services import guides


@pytest.fixture
def gdir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "CONTENT_GUIDES_DIR", str(tmp_path))
    return tmp_path


def test_settings_lists_guides_and_upload_form(client, gdir):
    (gdir / "10-тон.md").write_text("x")
    html = client.get("/settings").text
    assert "Правила письма" in html and "10-тон.md" in html and 'action="/settings/guides/upload"' in html


def test_upload_writes_file_and_redirects(client, gdir):
    r = client.post("/settings/guides/upload", data={"subdir": "ru"},
                    files={"file": ("20-структура.md", "## Структура\n".encode(), "text/markdown")},
                    follow_redirects=False)
    assert r.status_code == 303 and "msg=" in r.headers["location"]
    assert (gdir / "ru" / "20-структура.md").read_text() == "## Структура\n"


@pytest.mark.parametrize("subdir,name", [("..", "x.md"), ("ru", "../x.md"), ("ru", ".env"), ("ru", "x.exe")])
def test_upload_rejects_bad_paths(client, gdir, subdir, name):
    r = client.post("/settings/guides/upload", data={"subdir": subdir},
                    files={"file": (name, b"x", "text/plain")}, follow_redirects=False)
    assert r.status_code == 303 and "err=" in r.headers["location"]
    assert guides.list_guides() == [] and not (gdir.parent / "x.md").exists()


def test_delete_removes_only_listed_file(client, gdir):
    guides.save_guide("", "a.md", b"a")
    r = client.post("/settings/guides/delete", data={"rel": "a.md"}, follow_redirects=False)
    assert r.status_code == 303 and guides.list_guides() == []
    r = client.post("/settings/guides/delete", data={"rel": "../README.md"}, follow_redirects=False)
    assert "err=" in r.headers["location"]
