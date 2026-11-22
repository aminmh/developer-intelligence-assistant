"""Project-declared intentionally public routes (route-authorization check)."""

import asyncio

import pytest

from agent.core.capability import Capability
from agent.core.records import GuardRecord, RouteRecord
from code_quality import route_authorization
from code_quality.public_routes import PublicRouteStore, matching_pattern, normalize_public_route_patterns


def test_patterns_are_normalized_and_validated():
    assert normalize_public_route_patterns(["post  /api/v1/auth/*", "/health", "ANY /cart*", "", "POST /api/v1/auth/*"]) == [
        "* /cart*", "* /health", "POST /api/v1/auth/*"]
    for bad in (["FETCH /x"], ["POST api/x"], ["POST /a b"], ["POST /x\x07"], "POST /x", ["/x"] * 101):
        with pytest.raises(ValueError):
            normalize_public_route_patterns(bad)


def test_matching_by_method_and_path_glob():
    patterns = ["* /api/v1/cart*", "POST /api/v1/auth/*"]
    assert matching_pattern("DELETE", "/api/v1/cart/items/{cartItemId}", patterns) == "* /api/v1/cart*"
    assert matching_pattern("POST", "/api/v1/auth/request-otp", patterns) == "POST /api/v1/auth/*"
    assert matching_pattern("GET", "/api/v1/auth/me", patterns) is None
    assert matching_pattern("ANY", "/api/v1/cart", patterns) == "* /api/v1/cart*"


class _Collection:
    def __init__(self, records):
        self.records = records


def _collections(routes, guards=()):
    return {Capability.ROUTE_ANALYSIS: _Collection(list(routes)),
            Capability.ROUTE_AUTHORIZATION: _Collection(list(guards))}


def _route(method, path, handler):
    return RouteRecord(framework="laravel", methods=(method,), path=path, handler=handler, file="routes/api.php", line=3)


def test_declared_public_routes_are_public_with_the_pattern_and_never_downgrade_protection():
    routes = [_route("POST", "/api/v1/auth/request-otp", "App\\Auth::otp"),
              _route("POST", "/api/v1/auth/logout", "App\\Auth::logout"),
              _route("DELETE", "/api/v1/cart", "App\\Cart::clear"),
              _route("POST", "/api/v1/orders", "App\\Orders::store")]
    guards = [GuardRecord(framework="laravel", handler=None, route="POST /api/v1/auth/logout", kind="middleware",
                          requirement="auth", public=False, file="routes/api.php", line=3)]
    settings = {"public_routes": ["POST /api/v1/auth/*", "* /api/v1/cart*", "GET /never"]}
    findings, summary = route_authorization.run(_collections(routes, guards), settings)
    rows = {(r["method"], r["path"]): r for r in route_authorization.evaluate(_collections(routes, guards), settings["public_routes"])}
    otp = rows[("POST", "/api/v1/auth/request-otp")]
    assert otp["status"] == "public" and "POST /api/v1/auth/*" in otp["public_reason"]
    assert rows[("POST", "/api/v1/auth/logout")]["status"] == "protected"
    assert rows[("DELETE", "/api/v1/cart")]["status"] == "public"
    assert [f["message"].split(" (")[0] for f in findings] == ["POST /api/v1/orders"]
    assert summary["declared_public"] == 2 and summary["unmatched_public_route_patterns"] == ["GET /never"]
    assert route_authorization.run(_collections(routes, guards))[1]["unprotected"] == 3   # nothing declared


def test_store_and_http_endpoints(tmp_path, monkeypatch):
    from fastapi import HTTPException
    from starlette.requests import Request

    import code_quality.http as http
    import code_quality.public_routes as public_routes

    store = PublicRouteStore(tmp_path / "quality.db")
    monkeypatch.setattr(public_routes, "PublicRouteStore", lambda: store)
    monkeypatch.setenv("RAG_CALLBACK_SECRET", "x" * 40)

    def request(method: str, payload: bytes = b"", secret: bytes = b"x" * 40) -> Request:
        sent = False

        async def receive():
            nonlocal sent
            if sent:
                return {"type": "http.request", "body": b"", "more_body": False}
            sent = True
            return {"type": "http.request", "body": payload, "more_body": False}

        return Request({"type": "http", "method": method, "path": "/projects/pizza/public-routes",
                        "headers": [(b"x-internal-secret", secret), (b"x-user-id", b"alice")]}, receive)

    assert http.get_project_public_routes("pizza", request("GET")) == {"patterns": []}
    saved = asyncio.run(http.put_project_public_routes("pizza", request("PUT", b'{"patterns":["post /api/v1/auth/*"]}')))
    assert saved == {"patterns": ["POST /api/v1/auth/*"]} and store.patterns("pizza") == ["POST /api/v1/auth/*"]
    with pytest.raises(HTTPException) as invalid:
        asyncio.run(http.put_project_public_routes("pizza", request("PUT", b'{"patterns":["POST api"]}')))
    assert invalid.value.status_code == 422 and store.patterns("pizza") == ["POST /api/v1/auth/*"]
    with pytest.raises(HTTPException) as unauthorized:
        http.get_project_public_routes("pizza", request("GET", secret=b"wrong"))
    assert unauthorized.value.status_code == 401
