"""Transient Cloudflare embedding failures are retried; quota and auth errors are not."""

import io
import json
from urllib.error import HTTPError

import pytest

from index import embeddings


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _ok():
    return _Response(json.dumps({"result": {"data": [[0.1, 0.2]]}}).encode())


def _http(code):
    return HTTPError("https://api.cloudflare.com", code, "error", {}, io.BytesIO(b"{}"))


def _run(monkeypatch, outcomes):
    calls, sleeps = [], []

    def urlopen(req, timeout):
        calls.append(timeout)
        outcome = outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(embeddings.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(embeddings.time, "sleep", sleeps.append)
    monkeypatch.setattr(embeddings.settings, "cloudflare_account_id", "account", raising=False)
    monkeypatch.setattr(embeddings.settings, "cloudflare_api_token", "token", raising=False)
    return calls, sleeps


def test_a_timeout_and_a_server_error_are_retried(monkeypatch):
    calls, sleeps = _run(monkeypatch, [TimeoutError("slow"), _http(502), _ok()])
    assert embeddings._embed_cloudflare(["text"]) == [[0.1, 0.2]]
    assert len(calls) == 3 and sleeps == [5.0, 15.0]


def test_a_conflict_is_retried(monkeypatch):
    calls, _ = _run(monkeypatch, [_http(409), _ok()])
    assert embeddings._embed_cloudflare(["text"]) == [[0.1, 0.2]] and len(calls) == 2


def test_retries_are_bounded(monkeypatch):
    _run(monkeypatch, [TimeoutError("slow")] * 3)
    with pytest.raises(TimeoutError):
        embeddings._embed_cloudflare(["text"])


def test_quota_and_malformed_requests_are_not_retried(monkeypatch):
    for code in (429, 400, 404):
        calls, sleeps = _run(monkeypatch, [_http(code)])
        with pytest.raises(HTTPError):
            embeddings._embed_cloudflare(["text"])
        assert len(calls) == 1 and sleeps == []


def test_a_spurious_auth_error_is_retried_but_a_real_one_still_fails(monkeypatch):
    calls, _ = _run(monkeypatch, [_http(401), _ok()])
    assert embeddings._embed_cloudflare(["text"]) == [[0.1, 0.2]] and len(calls) == 2
    calls, _ = _run(monkeypatch, [_http(401)] * 3)
    with pytest.raises(HTTPError):
        embeddings._embed_cloudflare(["text"])
    assert len(calls) == 3
