from __future__ import annotations

import index.indexing_service as svc


def test_release_memory_trims_the_heap_once_libc_loads(monkeypatch):
    calls = []

    class Libc:
        def malloc_trim(self, pad):
            calls.append(pad)

    monkeypatch.setattr(svc, "_libc", Libc())
    svc.release_memory()
    assert calls == [0]


def test_release_memory_skips_trim_without_glibc(monkeypatch):
    def missing(_name):
        raise OSError("musl")

    monkeypatch.setattr(svc, "_libc", None)
    monkeypatch.setattr("ctypes.CDLL", missing)
    svc.release_memory()
    assert svc._libc is False
    svc.release_memory()  # later calls only collect garbage


def test_parse_job_releases_memory_even_when_it_fails(monkeypatch, tmp_path):
    released = []
    monkeypatch.setattr(svc, "release_memory", lambda: released.append(True))
    monkeypatch.setattr(svc.settings, "download_dir", str(tmp_path), raising=False)
    service = svc.IndexingService.__new__(svc.IndexingService)
    service.write_status = lambda *args, **kwargs: None

    def broken(*_args, **_kwargs):
        raise RuntimeError("bad archive")

    monkeypatch.setattr(svc, "extract_zip_safely", broken)
    service.parse_and_index("job", "p.zip", b"", "collection")
    assert released == [True]
