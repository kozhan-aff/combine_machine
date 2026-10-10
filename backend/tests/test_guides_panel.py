"""Экран «Правила письма» (/guides): пункт меню, список, загрузка пачкой, удаление; мусор не проходит."""
import pytest
from app.config import settings
from app.services import guides


@pytest.fixture
def gdir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "CONTENT_GUIDES_DIR", str(tmp_path))
    return tmp_path


def test_guides_page_is_in_the_menu_and_lists_files(client, gdir):
    (gdir / "10-тон.md").write_text("x")
    html = client.get("/guides").text
    assert "10-тон.md" in html and 'action="/guides/upload"' in html and "multiple" in html
    assert 'href="/guides"' in client.get("/").text                 # виден из меню на любом экране
    assert 'href="/guides"' in client.get("/settings").text


def test_empty_folder_says_so(client, gdir):
    assert "Сейчас правил нет" in client.get("/guides").text


def test_upload_takes_several_files_at_once(client, gdir):
    r = client.post("/guides/upload", follow_redirects=False, files=[
        ("file", ("10-тон.md", "## Тон\n".encode(), "text/markdown")),
        ("file", ("правила/20-структура.txt", "структура".encode(), "text/plain"))])
    assert r.status_code == 303 and "msg=" in r.headers["location"]
    assert (gdir / "10-тон.md").read_text() == "## Тон\n" and (gdir / "20-структура.txt").read_text() == "структура"


def test_upload_reports_rejected_files_and_keeps_good_ones(client, gdir):
    r = client.post("/guides/upload", follow_redirects=False, files=[
        ("file", ("ok.md", b"ok", "text/markdown")), ("file", ("картинка.png", b"x", "image/png"))])
    assert "err=" in r.headers["location"]
    assert [g["rel"] for g in guides.list_guides()] == ["ok.md"]


@pytest.mark.parametrize("name", ["../x.md", ".env", "x.exe"])
def test_upload_rejects_bad_names(client, gdir, name):
    r = client.post("/guides/upload", files={"file": (name, b"x", "text/plain")}, follow_redirects=False)
    assert r.status_code == 303
    assert not (gdir.parent / "x.md").exists() and not (gdir / ".env").exists() and not (gdir / "x.exe").exists()


def test_upload_without_file_is_an_error(client, gdir):
    r = client.post("/guides/upload", data={"x": "1"}, follow_redirects=False)
    assert "err=" in r.headers["location"]


def test_delete_removes_only_listed_file(client, gdir):
    guides.save_guide("a.md", b"a")
    r = client.post("/guides/delete", data={"rel": "a.md"}, follow_redirects=False)
    assert r.status_code == 303 and guides.list_guides() == []
    r = client.post("/guides/delete", data={"rel": "../README.md"}, follow_redirects=False)
    assert "err=" in r.headers["location"]
