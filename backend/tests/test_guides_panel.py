"""Экран «Правила письма» (/guides): пункт меню, список, загрузка пачкой, удаление; мусор не проходит.
Ниже — выжимки: роли файлов, состояния, запуск сжатия, просмотр и правка."""
import json
from urllib.parse import quote, unquote

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


# --- выжимки: роли, состояния, запуск сжатия, просмотр и правка (план Б, задача 8a) ---

BIG = "- правило стиля: пиши коротко и по делу\n" * 150          # длиннее SMALL_FILE — идёт в модель


def _llm(monkeypatch, *answers) -> list:
    calls, queue = [], list(answers)

    def complete(self, system, prompt, **kw):
        calls.append(prompt)
        return queue.pop(0)

    monkeypatch.setattr("app.integrations.llm.LlmClient.complete", complete)
    return calls


def _answer(role: str, digest: str, why: str = "причина роли") -> str:
    return json.dumps({"role": role, "why": why, "digest": digest}, ensure_ascii=False)


def _row(name: str) -> dict:
    return next(g for g in guides.status() if g["rel"] == name)


def _mixed_folder(gdir, monkeypatch) -> None:
    """Папка во всех состояниях: готовые выжимки трёх ролей, ошибка сборки, устаревшая, новая без выжимки."""
    (gdir / "10-стиль.md").write_text(BIG, encoding="utf-8")
    (gdir / "20-ошибки.md").write_text(BIG + "- каталог\n", encoding="utf-8")
    (gdir / "30-коротко.md").write_text("Не пиши слово «лучший».", encoding="utf-8")
    (gdir / "40-битый.md").write_text(BIG + "- битый\n", encoding="utf-8")
    (gdir / "50-менялся.md").write_text("старый текст", encoding="utf-8")
    _llm(monkeypatch, _answer("writer", "- пиши коротко", "правила стиля"),
         _answer("critic", "- нет воды\n- нет штампов", "каталог ошибок"), "модель ответила прозой")
    assert guides.build_digests() == {"built": 4, "skipped": 0, "failed": 1}
    (gdir / "50-менялся.md").write_text("новый текст", encoding="utf-8")
    (gdir / "60-новый.md").write_text("ещё не сжимали", encoding="utf-8")


def test_guides_page_shows_roles_digest_states_and_totals(client, gdir, monkeypatch):
    _mixed_folder(gdir, monkeypatch)
    guides.set_role("30-коротко.md", "critic")
    guides.save_digest("10-стиль.md", "- моя правка")
    html = client.get("/guides").text
    assert ">Сжать правила</button>" in html and 'action="/guides/digest"' in html
    # итог по ролям: писателю — «10-стиль», критику — «20-ошибки» и «30-коротко»
    w, c = guides.load_guides(role="writer"), guides.load_guides(role="critic")
    assert w["files"] == ["10-стиль.md"] and c["files"] == ["20-ошибки.md", "30-коротко.md"]
    assert f"писателю уйдёт {len(w['text'])} симв., критику — {len(c['text'])} (лимит 40 000 на каждого)" in html
    assert "3 файла без выжимки — в задание не попадают" in html
    assert "с замечаниями" in html and "40-битый.md — ответ модели" in html      # итог последнего сжатия
    for label in ("писателю", "критику", "обоим", "не использовать", "не назначено"):
        assert f">{label}</option>" in html
    assert '<option value="critic" selected>критику</option>' in html
    assert 'title="правила стиля"' in html                           # причина роли, назначенной машиной
    assert 'title="выбрано тобой — при сжатии не меняется"' in html
    assert ">12 симв.</a>" in html and ">правлена</span>" in html
    assert ">устарела</a>" in html and ">ошибка</a>" in html and ">нет</a>" in html
    assert 'title="ответ модели — не один JSON-объект"' in html
    assert f'href="/guides/digest/{quote("10-стиль.md")}"' in html
    assert "в задании" not in html and "не влез в лимит" not in html     # прежняя колонка убрана
    for word in ("pending", "stale", "digest_chars", "role_by"):         # служебных слов на экране нет
        assert word not in html.split("<main", 1)[-1].split("<script", 1)[0], word


def test_role_form_changes_the_role(client, gdir):
    (gdir / "a.md").write_text("правило", encoding="utf-8")
    guides.build_digests()
    r = client.post("/guides/role", data={"rel": "a.md", "role": "critic"}, follow_redirects=False)
    assert r.status_code == 303 and "msg=" in r.headers["location"]
    assert (_row("a.md")["role"], _row("a.md")["role_by"]) == ("critic", "operator")
    for bad in ({"rel": "a.md", "role": "boss"}, {"rel": "../a.md", "role": "writer"},
                {"rel": "нет.md", "role": "writer"}, {"role": "writer"}):
        r = client.post("/guides/role", data=bad, follow_redirects=False)
        assert r.status_code == 303 and "err=" in r.headers["location"], bad
    assert _row("a.md")["role"] == "critic"


def test_digest_button_spawns_the_job(client, gdir, monkeypatch):
    from app.services import jobs
    spawned, built = [], []
    monkeypatch.setattr(jobs, "spawn", lambda name, target: spawned.append((name, target)) or True)
    monkeypatch.setattr(guides, "build_digests", lambda force=False: built.append(force))
    r = client.post("/guides/digest", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/guides?msg=")
    client.post("/guides/digest", data={"force": "1"}, follow_redirects=False)
    assert [name for name, _ in spawned] == ["guides_digest", "guides_digest"]
    for _, target in spawned:
        target()
    assert built == [False, True]                                    # галочка «все файлы заново»


def test_digest_button_refuses_while_the_job_runs(client, gdir):
    from app.services import jobs
    (gdir / "a.md").write_text("правило", encoding="utf-8")
    with jobs.track("guides_digest"):
        r = client.post("/guides/digest", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/guides?err=")
        assert "Сжатие идёт" in client.get("/guides").text
    assert _row("a.md")["state"] == "pending"                        # второй прогон не стартовал


def test_digest_page_shows_and_saves_the_text(client, gdir, monkeypatch):
    (gdir / "10-стиль.md").write_text(BIG, encoding="utf-8")
    _llm(monkeypatch, _answer("writer", "- пиши коротко", "правила стиля"))
    guides.build_digests()
    url = "/guides/digest/" + quote("10-стиль.md")
    html = client.get(url).text
    assert "10-стиль.md" in html and ">- пиши коротко</textarea>" in html and ">Сохранить</button>" in html
    assert "<b style=\"color:var(--ink)\">писателю</b> (правила стиля)." in html
    assert f"исходник: {len(BIG)} симв., выжимка: 14" in html and 'href="/guides"' in html
    r = client.post(url, data={"text": "- пиши коротко\r\n- без штампов\r\n"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith(url + "?msg=")
    assert guides.read_digest("10-стиль.md") == "- пиши коротко\n- без штампов" and _row("10-стиль.md")["edited"]
    assert "Текст правлен тобой" in client.get(url).text
    r = client.post(url, data={"text": "д" * 9000}, follow_redirects=False)
    assert "7000" in unquote(r.headers["location"]) and len(guides.read_digest("10-стиль.md")) == 7000
    r = client.post(url, data={"text": ""}, follow_redirects=False)   # пусто — вернуть файл машине
    assert r.headers["location"].startswith("/guides?msg=") and _row("10-стиль.md")["state"] == "pending"


def test_digest_page_of_a_file_without_digest_explains_itself(client, gdir, monkeypatch):
    _mixed_folder(gdir, monkeypatch)
    assert "Выжимки ещё нет" in client.get("/guides/digest/" + quote("60-новый.md")).text
    assert "Файл изменился после сжатия" in client.get("/guides/digest/" + quote("50-менялся.md")).text
    assert "Сжать не получилось: ответ модели" in client.get("/guides/digest/" + quote("40-битый.md")).text


@pytest.mark.parametrize("name", ["нет.md", "index.json", ".env", "a.exe", "README.md", "..%2Fa.md", "%2e%2e"])
def test_digest_page_takes_only_existing_guides(client, gdir, name):
    (gdir / "a.md").write_text("правило", encoding="utf-8")
    (gdir / "README.md").write_text("пояснение", encoding="utf-8")
    guides.build_digests()
    for r in (client.get(f"/guides/digest/{name}", follow_redirects=False),
              client.post(f"/guides/digest/{name}", data={"text": "x"}, follow_redirects=False)):
        assert r.status_code == 404 or (r.status_code == 303 and r.headers["location"].startswith("/guides?err=")), name
    assert sorted(p.name for p in (gdir / ".digest").iterdir()) == ["a.md.md", "index.json"]
    assert guides.read_digest("a.md") == "правило"


def test_delete_drops_the_digest_and_the_index_entry(client, gdir):
    guides.save_guide("a.md", b"one"); guides.save_guide("b.md", b"two")
    guides.build_digests()
    r = client.post("/guides/delete", data={"rel": "a.md"}, follow_redirects=False)
    assert r.status_code == 303 and "msg=" in r.headers["location"]
    index = json.loads((gdir / ".digest" / "index.json").read_text(encoding="utf-8"))
    assert list(index) == ["b.md"] and not (gdir / ".digest" / "a.md.md").exists()
    assert (gdir / ".digest" / "b.md.md").exists()


def test_digest_job_is_known_to_the_panel(client):
    from app.api import panel
    assert "guides_digest" in panel._JOBS
    assert client.post("/run/guides_digest/cancel", follow_redirects=False).status_code != 404
    assert "guides_digest:'Выжимка правил'" in client.get("/guides").text      # подпись задачи в полосе вверху
    assert client.get("/api/jobs/live").json()["last"]["guides_digest"] is None


def test_site_vertical_is_an_editable_key():
    from app.services import api_keys
    group = next(fields for gid, _, _, fields in api_keys.GROUPS if gid == "m45")
    field = next(f for f in group if f.key == "SITE_VERTICAL")
    assert field.label == "Ниша сайтов" and field.kind == "text" and not field.secret
    assert "правил письма" in field.hint and "Сжать правила" in field.hint
    assert type(settings).model_fields["SITE_VERTICAL"].default.startswith("VPN-сервисы")
    assert api_keys.validate(field, "Онлайн-кинотеатры: обзоры и подборки") == "Онлайн-кинотеатры: обзоры и подборки"
