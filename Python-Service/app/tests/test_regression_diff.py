"""Response differ: @.archcore/regression/regression-check.spec.md clauses 13, 15-16; plan task T11."""

from __future__ import annotations

import json

from regression.diff import compare, compile_rules, flatten, noise_between


def _json(body, status=200, **headers):
    return {"status": status, "headers": {"content-type": "application/json; charset=utf-8", **headers},
            "body": json.dumps(body).encode()}


def test_noise_structural_diff_and_verdicts():
    base = _json({"id": 7, "total": 12, "items": [{"sku": "a", "updated_at": "2026-11-20T10:00:00Z"}],
                  "request_id": "r-1", "_token": "abc"})
    candidate = _json({"id": 7, "total": 13, "items": [{"sku": "a", "updated_at": "2026-11-23T09:00:00+01:00"}],
                       "request_id": "r-2", "_token": "xyz", "currency": "EUR"})
    second_baseline = _json({**json.loads(base["body"]), "request_id": "r-9"})
    noise = noise_between(base, second_baseline)
    assert noise == ["body:$.request_id"]
    result = compare(base, candidate, noise=noise)
    # Timestamps compare by shape, tokens are dropped, noise is ignored; real changes remain, by path.
    assert [(d["path"], d["base"], d["candidate"]) for d in result["differences"]] == [
        ("$.currency", "<missing>", "EUR"), ("$.total", 12, 13)]
    assert result["equal"] is False and result["dropped"] == {"noise": 1, "ignored": 0}


def test_noise_structural_diff_ignore_rules_and_list_noise():
    base = _json({"items": [{"seen": 1, "n": 1}, {"seen": 1, "n": 2}], "meta": {"took": 3}})
    candidate = _json({"items": [{"seen": 5, "n": 1}, {"seen": 9, "n": 2}], "meta": {"took": 9}})
    noise = noise_between(base, _json({"items": [{"seen": 2, "n": 1}, {"seen": 1, "n": 2}], "meta": {"took": 3}}))
    assert noise == ["body:$.items[*].seen"]  # one noisy item covers its siblings
    result = compare(base, candidate, noise=noise, ignore=["$..took"])
    assert result["equal"] is True and result["dropped"] == {"noise": 2, "ignored": 1}
    rules = compile_rules(["$.items[*].x", "$..took", "bogus"])
    assert len(rules) == 2 and rules[0].match("$.items[3].x.y") and not rules[0].match("$.items[3].xy")


def test_noise_structural_diff_status_headers_html_and_hash():
    html = lambda text, token: {"status": 200, "headers": {"content-type": "text/html"},
                                "body": f"<html><head><meta name=\"csrf-token\" content=\"{token}\"><script>x={token}</script>"
                                        f"</head><body><input type=hidden name=_token value={token}><p>{text}</p></body></html>".encode()}
    assert compare(html("Hello  world", "a"), html("Hello world", "b"))["equal"] is True
    changed = compare(html("Total: 12", "a"), html("Total: 13", "a"))
    assert changed["differences"][0]["where"] == "body" and "Total: 12" in changed["differences"][0]["base"]
    status = compare(_json({}, 200), _json({}, 500, location="/x"))
    assert [(d["where"], d["path"]) for d in status["differences"]] == [("status", ""), ("header", "location")]
    big = lambda digest: {"status": 200, "headers": {}, "body": None, "body_sha256": digest, "truncated": True}
    assert compare(big("aa"), big("aa"))["equal"] and not compare(big("aa"), big("bb"))["equal"]
    errors = compare({"error": "connection refused"}, _json({}))
    assert errors["differences"][0]["where"] == "error"


def test_noise_structural_diff_flatten_keeps_empty_containers():
    assert flatten({"a": [], "b": {}, "c": [1]}) == {"$.a": [], "$.b": {}, "$.c[0]": 1}


def test_noise_structural_diff_keys_with_dots_stay_unambiguous():
    """Found writing characterization tests: {"a.b": 1} and {"a": {"b": 1}} must not share a path."""
    assert flatten({"a.b": 1, "a": {"b": 2}, "has space": 3}) == {'$["a.b"]': 1, "$.a.b": 2, '$["has space"]': 3}
    assert not compare(_json({"a.b": 1}), _json({"a": {"b": 1}}))["equal"]
