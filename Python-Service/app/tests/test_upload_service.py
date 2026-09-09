from __future__ import annotations

import base64
import pytest

from index.upload_service import UploadService


class _Storage:
    def __init__(self):
        self.rows = []

    def put_rag_file(self, session, filename, data):
        self.rows.append((session, filename, data))


def test_direct_upload_decodes_and_stores_content():
    storage = _Storage()
    result = UploadService().store_direct(storage, "project.zip", base64.b64encode(b"zip").decode())
    assert result["filename"] == "project.zip"
    assert result["size"] == 3
    assert storage.rows[0][1:] == ("project.zip", b"zip")


def test_invalid_direct_upload_is_rejected():
    with pytest.raises(ValueError, match="Invalid base64"):
        UploadService().decode_content("not-base64")


def test_workspace_extraction_is_cleaned_on_failure(monkeypatch, tmp_path):
    from index import upload_service as module

    monkeypatch.setattr(module.settings, "workspace_root", str(tmp_path))
    monkeypatch.setattr(module, "extract_zip_safely", lambda data, path: (_ for _ in ()).throw(ValueError("bad zip")))
    with pytest.raises(ValueError, match="bad zip"):
        UploadService().extract_workspace("w1", b"bad")
    assert not (tmp_path / "w1").exists()


def test_tus_session_contains_signed_upload_metadata(monkeypatch):
    from index import upload_service as module

    monkeypatch.setattr(module.settings, "upload_token_secret", "secret")
    monkeypatch.setattr(module.settings, "tus_public_url", "http://tusd/files/")
    result = UploadService().create_tus_session("project.zip", 42)
    assert result["tus_endpoint"] == "http://tusd/files/"
    assert result["upload_token"]
