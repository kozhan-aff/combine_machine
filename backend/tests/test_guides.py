"""content_guides/: одна плоская папка правил письма — склейка, лимит, безопасная загрузка (спека §5)."""
import pytest
from app.config import settings
from app.services import guides


@pytest.fixture
def gdir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "CONTENT_GUIDES_DIR", str(tmp_path))
    return tmp_path


def test_load_takes_every_file_in_alpha_order_for_any_lang_and_kind(gdir):
    (gdir / "20-b.md").write_text("правило B")
    (gdir / "10-a.txt").write_text("правило A")
    (gdir / "скрытый.pdf").write_bytes(b"%PDF")                    # чужое расширение не читаем
    (gdir / "ru").mkdir(); (gdir / "ru" / "старое.md").write_text("из подпапки")   # подпапок больше нет
    r = guides.load_guides("en", "howto")
    assert r == guides.load_guides() == guides.load_guides("ru", "review")       # язык и тип не влияют
    assert r["files"] == ["10-a.txt", "20-b.md"] and not r["truncated"]
    assert r["text"].index("правило A") < r["text"].index("правило B") and "--- 20-b.md ---" in r["text"]
    assert "из подпапки" not in r["text"]


def test_load_without_folder_is_empty_not_error(gdir, monkeypatch):
    monkeypatch.setattr(settings, "CONTENT_GUIDES_DIR", str(gdir / "нет-такой"))
    assert guides.load_guides("ru", "review") == {"text": "", "files": [], "truncated": False, "pending": [],
                                                  "cut": [], "missing": True}


def test_limit_drops_files_from_the_end_and_flags(gdir):
    (gdir / "a.md").write_text("A" * 30)
    (gdir / "b.md").write_text("B" * 30)
    r = guides.load_guides(None, None, limit=50)
    assert r["files"] == ["a.md"] and r["truncated"] is True and "B" not in r["text"]


def test_list_marks_files_cut_by_the_limit(gdir, monkeypatch):
    (gdir / "a.md").write_text("A" * 30)
    (gdir / "b.md").write_text("B" * 30)
    monkeypatch.setattr(guides, "LIMIT", 50)
    assert [(g["rel"], g["used"]) for g in guides.list_guides()] == [("a.md", True), ("b.md", False)]


def test_save_and_delete_stay_inside_the_folder(gdir):
    assert guides.save_guide("10-тон.md", "текст".encode()) == "10-тон.md"
    assert (gdir / "10-тон.md").read_text() == "текст"
    assert guides.save_guide("моя папка/20-структура.md", b"x") == "20-структура.md"   # выбор папки в браузере
    assert [g["rel"] for g in guides.list_guides()] == ["10-тон.md", "20-структура.md"]
    for name in ("..", ".env", "a b.md", "x.exe", "", "sub/.env", "../.env"):
        with pytest.raises(ValueError):
            guides.save_guide(name, b"x")
    # путь в имени не выводит из папки: берётся только имя файла
    assert guides.save_guide("../x.md", b"x") == "x.md" and guides.save_guide("..\\y.md", b"y") == "y.md"
    guides.delete_guide("x.md"); guides.delete_guide("y.md")
    with pytest.raises(ValueError):
        guides.save_guide("big.md", b"x" * (guides.MAX_FILE + 1))
    assert not (gdir.parent / "x.md").exists() and not (gdir / "моя папка").exists()
    guides.delete_guide("10-тон.md")
    assert [g["rel"] for g in guides.list_guides()] == ["20-структура.md"]
    for bad in ("../README.md", "sub/20-структура.md", "нет.md"):
        with pytest.raises(ValueError):
            guides.delete_guide(bad)


def test_readme_is_not_a_guide(gdir):
    (gdir / "README.md").write_text("как раскладывать")
    (gdir / "10-тон.md").write_text("правило")
    r = guides.load_guides(None, None)
    assert r["files"] == ["10-тон.md"] and "как раскладывать" not in r["text"]
    assert [g["rel"] for g in guides.list_guides()] == ["10-тон.md"]


@pytest.mark.parametrize("name", ["README.md", "readme.txt", "ReadMe.md"])
def test_readme_upload_is_rejected_not_silently_dropped(gdir, name):
    """Загрузка README.* тихо пропала бы из промпта (_files его пропускает) — отказ словами."""
    with pytest.raises(ValueError, match="README"):
        guides.save_guide(name, b"x")
    assert not (gdir / name).exists()
