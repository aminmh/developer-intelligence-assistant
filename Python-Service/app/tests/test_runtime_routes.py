"""Tests for project-profile runtime route labeling (D8)."""

from __future__ import annotations

from projects.project_profile import merge_runtime_routes

_PROFILE = {
    "framework": "laravel",
    "version": "12",
    "routes": [
        {"method": "GET", "uri": "/login", "action": "A@l", "name": "login",
         "middleware": ["web", "guest"], "source": "static"}
    ],
    "routes_source": "static",
}


def _sandbox(routes_stdout: str | None = None, **probe_overrides) -> dict:
    probes = []
    if routes_stdout is not None:
        probes.append(
            {"name": "routes", "status": "done", "exit_code": 0, "stdout": routes_stdout}
        )
    return {"status": "done", "install": {"status": "done", "exit_code": 0},
            "probes": probes, **probe_overrides}


def _routes_payload() -> str:
    import json

    return json.dumps([
        {"method": "GET|HEAD", "uri": "/", "action": "Closure", "name": None,
         "middleware": ["web"]},
        {"method": "POST", "uri": "/register", "action": "App\\Http\\Auth@store",
         "name": "register", "middleware": "web,guest"},
        {"method": "PUT", "uri": "/storage/{path}", "action": "Package@put",
         "name": "storage.local.upload", "middleware": []},
    ])


class TestMergeRuntimeRoutes:
    def test_no_profile_or_no_sandbox_passthrough(self):
        assert merge_runtime_routes(None, _sandbox("[]")) is None
        assert merge_runtime_routes(_PROFILE, None) is _PROFILE

    def test_replaces_static_with_runtime_when_probe_done(self):
        merged = merge_runtime_routes(_PROFILE, _sandbox(_routes_payload()))
        assert merged["routes_source"] == "runtime"
        assert len(merged["routes"]) == 3
        assert all(r["source"] == "runtime" for r in merged["routes"])
        # static /login row is gone — the runtime Router table is authoritative
        assert not any(r["uri"] == "/login" for r in merged["routes"])

    def test_normalizes_uri_method_and_middleware(self):
        merged = merge_runtime_routes(_PROFILE, _sandbox(_routes_payload()))
        first = merged["routes"][0]
        assert first["uri"] == "/"
        second = merged["routes"][1]
        assert second["method"] == "POST"
        assert second["middleware"] == ["web", "guest"]  # csv string split
        assert second["uri"].startswith("/")

    def test_failed_probe_or_bad_json_leaves_profile(self):
        assert merge_runtime_routes(_PROFILE, _sandbox("[]")) is _PROFILE
        assert merge_runtime_routes(
            _PROFILE,
            {"probes": [{"name": "routes", "status": "failed", "exit_code": 7,
                         "stdout": ""}]},
        ) is _PROFILE
        assert merge_runtime_routes(_PROFILE, _sandbox("not json {")) is _PROFILE

    def test_empty_payload_no_marker(self):
        assert merge_runtime_routes(_PROFILE, _sandbox("[]")) is _PROFILE
