"""Списки дропов: живые форматы 2026-10-01 (фикстуры), без сети."""
import io
import pathlib
import zipfile
from datetime import datetime, timezone

import httpx
import pytest

from app.integrations import dropcatch, nominet, registry_mx

FX = pathlib.Path(__file__).parent / "fixtures" / "v2"


def _zip(csv_text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Dropping_Domains_2026-10-03.csv", csv_text)
    return buf.getvalue()


def test_dropcatch_parse_lowercases_and_skips_bad_dates():
    rows = dropcatch.parse_dropping_zip(_zip((FX / "dropcatch_head.csv").read_text()))
    names = [r["domain"] for r in rows]
    assert names == ["werkleittechnik.com", "pharmaindustrie.com", "huntingthugs.com", "goodname.cc"]
    assert rows[0] == {"domain": "werkleittechnik.com", "source": "dropcatch", "lane": "bid",
                       "acquire_deadline": datetime(2026, 10, 3, tzinfo=timezone.utc)}


def test_dropcatch_list_dropping_asks_file_url_then_downloads(monkeypatch):
    c, calls = dropcatch.DropCatchClient(), []
    zipped = _zip((FX / "dropcatch_head.csv").read_text())

    def request(method, url, **kw):
        calls.append((url, kw.get("params")))
        req = httpx.Request(method, url)
        if "GetFileUrl" in url:
            return httpx.Response(200, request=req, json={
                "result": {"fileUrl": "https://dropcatch-downloads.s3.amazonaws.com/production/x.csv.zip?sig=1",
                           "fileName": "x.csv.zip"}, "statusCode": "OK", "success": True})
        return httpx.Response(200, request=req, content=zipped)
    monkeypatch.setattr(c, "request", request)
    assert len(c.list_dropping()) == 4
    assert calls[0][1] == {"FileType": "csv", "RequestType": "Dropping", "BackorderDay": "DaysOut2"}
    assert calls[1][0].startswith("https://dropcatch-downloads.s3.amazonaws.com/")


def test_dropcatch_no_file_url_is_an_error(monkeypatch):
    c = dropcatch.DropCatchClient()
    monkeypatch.setattr(c, "request", lambda m, u, **kw: httpx.Response(
        200, request=httpx.Request(m, u), json={"result": None, "success": False, "statusCode": "Error"}))
    try:
        c.list_dropping()
        raise AssertionError("ожидали RuntimeError")
    except RuntimeError as e:
        assert "DropCatch" in str(e)


def test_nominet_window_only_future_three_days():
    now = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)
    rows = nominet.parse_droplist((FX / "nominet_head.csv").read_text(), now, lookahead_days=3)
    assert [r["domain"] for r in rows] == ["5thjuly.uk", "mandemgpt.co.uk", "laterdrop.co.uk"]
    assert rows[0]["lane"] == "bid" and rows[0]["source"] == "nominet"
    assert rows[0]["acquire_deadline"] == datetime(2026, 10, 1, 3, 1, 55, tzinfo=timezone.utc)


def test_registry_mx_skips_preamble_and_unavailable():
    rows = registry_mx.parse_deleted((FX / "registry_mx_head.csv").read_text())
    assert [r["domain"] for r in rows] == ["dibanhi.com.mx", "ric77.com.mx", "lennoxind.mx"]
    assert rows[0] == {"domain": "dibanhi.com.mx", "source": "mx", "lane": "free", "acquire_deadline": None}


# --- смена формата источника = громкая ошибка, а не тихий пустой день ---

def test_dropcatch_renamed_column_is_format_error():
    with pytest.raises(ValueError, match="DropCatch: сменился формат, нет колонки Drop Date"):
        dropcatch.parse_dropping_zip(_zip("Domain,TLD,Type,DropDate\nfoo.com,com,PendingDelete,2026-10-03\n"))


def test_dropcatch_archive_without_csv_is_error():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("readme.txt", "x")
    with pytest.raises(ValueError, match="DropCatch: в архиве нет CSV"):
        dropcatch.parse_dropping_zip(buf.getvalue())


def test_dropcatch_valid_header_without_rows_is_empty():
    assert dropcatch.parse_dropping_zip(_zip("Domain,TLD,Type,Drop Date\n")) == []


def test_nominet_renamed_column_is_format_error():
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="Nominet: сменился формат, нет колонки drop_time"):
        nominet.parse_droplist("roid,domain,droptime\nD_1-UK,a.uk,2026-10-01T03:00:00Z\n", now, 3)


def test_registry_mx_no_header_is_format_error():
    with pytest.raises(ValueError, match="registry.mx: сменился формат"):
        registry_mx.parse_deleted("01/10/26 03:00:16 GMT-6\nDomain,Available\nfoo.mx,true\n")


def test_registry_mx_missing_column_is_format_error():
    with pytest.raises(ValueError, match="registry.mx: сменился формат, нет колонки Disponible"):
        registry_mx.parse_deleted("Dominio,Estado\nfoo.mx,true\n")


def test_registry_mx_valid_header_without_rows_is_empty():
    assert registry_mx.parse_deleted("01/10/26 03:00:16 GMT-6\nDominio,Disponible\n") == []
