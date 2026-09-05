"""Dependency scanner + doc retrieval pipeline tests."""

import json

import projects.dependencies as dependencies
import rag.doc_retrieval as doc_retrieval

# ─── dependency scanner ──────────────────────────────────────────────────────


def _write(tmp_path, name, content):
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return p


def test_scan_composer_lock_preferred_over_manifest(tmp_path):
    _write(
        tmp_path,
        "composer.json",
        json.dumps({"require": {"laravel/framework": "^11.0", "symfony/console": "^7.0"}}),
    )
    _write(
        tmp_path,
        "composer.lock",
        json.dumps(
            {
                "packages": [
                    {"name": "laravel/framework", "version": "11.38.2"},
                    {"name": "guzzlehttp/guzzle", "version": "7.9.2"},
                ]
            }
        ),
    )
    result = dependencies.scan_dependencies(str(tmp_path))
    assert result["ecosystem"] == "php"
    assert result["package_manager"] == "composer"
    assert "symfony/console" not in result["packages"]  # manifest-only → excluded
    laravel = result["packages"]["laravel/framework"]
    assert laravel["resolved_version"] == "11.38.2"
    assert laravel["requested_version"] == "^11.0"
    assert laravel["installed"] is True


def test_scan_npm_package_lock(tmp_path):
    _write(tmp_path, "package.json", json.dumps({"dependencies": {"react": "^19.0.0"}}))
    _write(
        tmp_path,
        "package-lock.json",
        json.dumps(
            {
                "packages": {
                    "": {"name": "demo"},
                    "node_modules/react": {"version": "19.1.0", "resolved": "https://registry.npmjs.org/react/-/react-19.1.0.tgz"},
                }
            }
        ),
    )
    result = dependencies.scan_dependencies(str(tmp_path))
    assert result["ecosystem"] == "js"
    assert result["package_manager"] == "npm"
    assert result["packages"]["react"]["resolved_version"] == "19.1.0"
    assert result["packages"]["react"]["requested_version"] == "^19.0.0"


def test_scan_python_poetry_lock(tmp_path):
    _write(
        tmp_path,
        "poetry.lock",
        '[[package]]\nname = "fastapi"\nversion = "0.115.0"\n',
    )
    result = dependencies.scan_dependencies(str(tmp_path))
    assert result["ecosystem"] == "python"
    assert result["package_manager"] == "poetry"
    assert result["packages"]["fastapi"]["resolved_version"] == "0.115.0"


def test_scan_empty_directory(tmp_path):
    result = dependencies.scan_dependencies(str(tmp_path))
    assert result["packages"] == {}


def test_scan_skips_vendor_and_node_modules(tmp_path):
    _write(tmp_path, "composer.lock", json.dumps({"packages": [{"name": "a/b", "version": "1.0.0"}]}))
    vend = tmp_path / "vendor"
    vend.mkdir()
    _write(vend, "composer.lock", json.dumps({"packages": []}))
    result = dependencies.scan_dependencies(str(tmp_path))
    assert "a/b" in result["packages"]


# ─── doc retrieval ───────────────────────────────────────────────────────────


def test_retrieve_documentation_disabled(monkeypatch):
    monkeypatch.setattr(doc_retrieval.settings, "doc_retrieval_enabled", False)
    result = doc_retrieval.retrieve_documentation({"a/b": {"resolved_version": "1.0.0"}}, "q")
    assert result["ok"] is True
    assert result["chunks"] == []


def test_retrieve_documentation_empty_packages(monkeypatch):
    monkeypatch.setattr(doc_retrieval.settings, "doc_retrieval_enabled", True)
    result = doc_retrieval.retrieve_documentation({}, "q")
    assert result["chunks"] == []


def test_retrieve_context7_happy_path(monkeypatch):
    monkeypatch.setattr(doc_retrieval.settings, "doc_retrieval_enabled", True)
    monkeypatch.setattr(doc_retrieval.settings, "doc_context7_enabled", True)
    monkeypatch.setattr(doc_retrieval.settings, "doc_context7_timeout", 5)

    def fake_get(url, timeout, api_key=""):
        if "/libs/search" in url:
            return json.dumps({"results": [{"id": "/laravel/framework", "versions": ["__branch__12.x", "__branch__11.x"]}]})
        if "/context" in url:
            return "### Blade\nUseful doc content.\n\nSource: https://github.com/laravel/framework\n\n--------------------------------\n\n### Routing\nMore doc.\n"
        return None

    monkeypatch.setattr(doc_retrieval, "_http_get", fake_get)

    result = doc_retrieval.retrieve_documentation(
        {"laravel/framework": {"resolved_version": "11.38.2"}}, "blade routing"
    )
    assert result["ok"] is True
    assert result["provider"] == "context7"
    assert len(result["chunks"]) == 2
    prov = result["provenance"][0]
    assert prov["provider"] == "context7"
    assert prov["library_id"] == "/laravel/framework"
    assert prov["exact_version_found"] is True  # __branch__11.x matched 11.x


def test_retrieve_version_never_newer_major(monkeypatch):
    monkeypatch.setattr(doc_retrieval.settings, "doc_retrieval_enabled", True)
    monkeypatch.setattr(doc_retrieval.settings, "doc_context7_enabled", True)

    # installed 10.x but only 11.x docs exist → no version match
    assert doc_retrieval._match_version(["__branch__11.x"], "10.4.1") is None
    # installed 9.x, docs 10.x and 8.x → pick 8.x (lower major) not 10.x
    assert doc_retrieval._match_version(["__branch__10.x", "__branch__8.x"], "9.0.0") == "__branch__8.x"
    # installed 11.38, docs 11.x → exact-ish match
    assert doc_retrieval._match_version(["__branch__12.x", "__branch__11.x"], "11.38.2") == "__branch__11.x"


def test_retrieve_gitmcp_fallback_when_context7_empty(monkeypatch):
    monkeypatch.setattr(doc_retrieval.settings, "doc_retrieval_enabled", True)
    monkeypatch.setattr(doc_retrieval.settings, "doc_context7_enabled", True)
    monkeypatch.setattr(doc_retrieval.settings, "doc_gitmcp_enabled", True)
    calls = []

    def fake_get(url, timeout, api_key=""):
        calls.append(url)
        if "/libs/search" in url:
            return json.dumps({"results": [{"id": "/laravel/framework", "versions": []}]})
        if "/context" in url:
            return ""
        # GitMCP llms.txt
        if "llms.txt" in url:
            return (
                "# Laravel\n\n"
                "> The Laravel framework documentation index.\n\n"
                "- [Installation](installation.md)\n"
                "- [Routing](routing.md)\n"
                "- [Blade Templates](blade.md)\n"
                "- [Eloquent ORM](eloquent.md)\n"
                "- [Authentication](authentication.md)\n"
                "- [Testing](testing.md)\n"
                "- [Queues](queues.md)\n"
            )
        return None

    monkeypatch.setattr(doc_retrieval, "_http_get", fake_get)

    result = doc_retrieval.retrieve_documentation(
        {"laravel/framework": {"resolved_version": "11.38.2"}}, "routing"
    )
    assert result["ok"] is True
    assert result["provider"] == "gitmcp"
    assert len(result["chunks"]) > 0
    assert any("llms.txt" in c for c in calls)


def test_retrieve_both_fail_non_strict_returns_warning(monkeypatch):
    monkeypatch.setattr(doc_retrieval.settings, "doc_retrieval_enabled", True)
    monkeypatch.setattr(doc_retrieval.settings, "doc_context7_enabled", True)
    monkeypatch.setattr(doc_retrieval.settings, "doc_gitmcp_enabled", True)
    monkeypatch.setattr(doc_retrieval.settings, "doc_strict_mode", False)

    monkeypatch.setattr(doc_retrieval, "_http_get", lambda url, timeout, api_key="": None)

    result = doc_retrieval.retrieve_documentation(
        {"laravel/framework": {"resolved_version": "11.38.2"}}, "routing"
    )
    assert result["ok"] is False
    assert "model knowledge" in (result["warning"] or "")


def test_retrieve_both_fail_strict_raises(monkeypatch):
    monkeypatch.setattr(doc_retrieval.settings, "doc_retrieval_enabled", True)
    monkeypatch.setattr(doc_retrieval.settings, "doc_context7_enabled", True)
    monkeypatch.setattr(doc_retrieval.settings, "doc_gitmcp_enabled", True)
    monkeypatch.setattr(doc_retrieval.settings, "doc_strict_mode", True)

    monkeypatch.setattr(doc_retrieval, "_http_get", lambda url, timeout, api_key="": None)

    try:
        doc_retrieval.retrieve_documentation(
            {"laravel/framework": {"resolved_version": "11.38.2"}}, "routing"
        )
        raised = False
    except doc_retrieval.DocRetrievalError:
        raised = True
    assert raised
