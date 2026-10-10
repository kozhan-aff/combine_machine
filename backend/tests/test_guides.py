"""content_guides/: склейка правил письма по языку/типу страницы, лимит, безопасная загрузка (спека §5)."""
import pytest
from app.config import settings
from app.services import guides


@pytest.fixture
def gdir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "CONTENT_GUIDES_DIR", str(tmp_path))
    return tmp_path


def test_load_merges_root_then_lang_then_kind_in_alpha_order(gdir):
    (gdir / "20-b.md").write_text("корень B")
    (gdir / "10-a.md").write_text("корень A")
    (gdir / "ru").mkdir(); (gdir / "ru" / "тон.md").write_text("по-русски")
    (gdir / "review").mkdir(); (gdir / "review" / "обзор.txt").write_text("структура обзора")
    (gdir / "en").mkdir(); (gdir / "en" / "x.md").write_text("english only")
    (gdir / "ru" / "скрытый.pdf").write_bytes(b"%PDF")              # чужое расширение не читаем
    r = guides.load_guides("ru", "review")
    assert r["files"] == ["10-a.md", "20-b.md", "ru/тон.md", "review/обзор.txt"] and not r["truncated"]
    assert r["text"].index("корень A") < r["text"].index("корень B") < r["text"].index("по-русски") < r["text"].index("структура обзора")
    assert "english only" not in r["text"] and "--- ru/тон.md ---" in r["text"]


def test_load_without_folder_is_empty_not_error(gdir, monkeypatch):
    monkeypatch.setattr(settings, "CONTENT_GUIDES_DIR", str(gdir / "нет-такой"))
    assert guides.load_guides("ru", "review") == {"text": "", "files": [], "truncated": False}


def test_limit_drops_files_from_the_end_and_flags(gdir):
    (gdir / "a.md").write_text("A" * 30)
    (gdir / "b.md").write_text("B" * 30)
    r = guides.load_guides(None, None, limit=50)
    assert r["files"] == ["a.md"] and r["truncated"] is True and "B" not in r["text"]


def test_save_and_delete_are_confined_to_whitelist(gdir):
    rel = guides.save_guide("ru", "10-тон.md", "текст".encode())
    assert rel == "ru/10-тон.md" and (gdir / "ru" / "10-тон.md").read_text() == "текст"
    assert guides.list_guides()[0]["rel"] == "ru/10-тон.md"
    for sub, name in (("..", "x.md"), ("ru", "../x.md"), ("ru", "..\\x.md"), ("ru", ".env"),
                      ("ru", "a b.md"), ("pl", "x.md"), ("ru", "x.exe"), ("ru", "")):
        with pytest.raises(ValueError):
            guides.save_guide(sub, name, b"x")
    with pytest.raises(ValueError):
        guides.save_guide("ru", "big.md", b"x" * (guides.MAX_FILE + 1))
    assert not (gdir.parent / "x.md").exists()
    guides.delete_guide("ru/10-тон.md")
    assert guides.list_guides() == []
    with pytest.raises(ValueError):
        guides.delete_guide("../README.md")


def test_readme_is_not_a_guide(gdir):
    (gdir / "README.md").write_text("как раскладывать")
    (gdir / "10-тон.md").write_text("правило")
    r = guides.load_guides(None, None)
    assert r["files"] == ["10-тон.md"] and "как раскладывать" not in r["text"]
    assert [g["rel"] for g in guides.list_guides()] == ["10-тон.md"]


@pytest.mark.parametrize("name", ["README.md", "readme.txt", "ReadMe.md"])
def test_readme_upload_is_rejected_not_silently_dropped(gdir, name):
    """Загрузка README.* тихо пропала бы из промпта (_files_in его пропускает) — отказ словами."""
    with pytest.raises(ValueError, match="README"):
        guides.save_guide("ru", name, b"x")
    assert not (gdir / "ru" / name).exists()
