import pytest
from app.services.cf_secret import resolve_secret_ref, SecretRefError


def test_env_ref(monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "tok-abc")
    assert resolve_secret_ref("env:CLOUDFLARE_API_TOKEN") == "tok-abc"


def test_env_ref_bad_name():
    with pytest.raises(SecretRefError):
        resolve_secret_ref("env:PATH; rm -rf /")


def test_env_missing():
    with pytest.raises(SecretRefError):
        resolve_secret_ref("env:DEFINITELY_UNSET_VAR_XYZ")


def test_file_ref_reads_and_strips_one_newline(tmp_path, monkeypatch):
    monkeypatch.setattr("app.services.cf_secret.settings.CLOUDFLARE_SECRETS_DIR", str(tmp_path))
    (tmp_path / "cf").write_text("secret-value\n")
    assert resolve_secret_ref("file:cf") == "secret-value"


def test_file_ref_rejects_traversal(tmp_path, monkeypatch):
    monkeypatch.setattr("app.services.cf_secret.settings.CLOUDFLARE_SECRETS_DIR", str(tmp_path))
    for bad in ("file:../etc/passwd", "file:/etc/passwd", "file:sub/nested"):
        with pytest.raises(SecretRefError):
            resolve_secret_ref(bad)


def test_file_ref_rejects_symlink_escape(tmp_path, monkeypatch):
    import os
    outside = tmp_path / "outside.txt"; outside.write_text("leak")
    secdir = tmp_path / "sec"; secdir.mkdir()
    os.symlink(outside, secdir / "link")
    monkeypatch.setattr("app.services.cf_secret.settings.CLOUDFLARE_SECRETS_DIR", str(secdir))
    with pytest.raises(SecretRefError):
        resolve_secret_ref("file:link")


def test_file_ref_rejects_oversized(tmp_path, monkeypatch):
    monkeypatch.setattr("app.services.cf_secret.settings.CLOUDFLARE_SECRETS_DIR", str(tmp_path))
    (tmp_path / "big").write_bytes(b"x" * (8 * 1024 + 1))
    with pytest.raises(SecretRefError):
        resolve_secret_ref("file:big")


def test_error_never_leaks_value(tmp_path, monkeypatch):
    monkeypatch.setattr("app.services.cf_secret.settings.CLOUDFLARE_SECRETS_DIR", str(tmp_path))
    (tmp_path / "big").write_bytes(b"SUPERSECRET" * 1000)
    try:
        resolve_secret_ref("file:big")
    except SecretRefError as e:
        assert "SUPERSECRET" not in str(e)
