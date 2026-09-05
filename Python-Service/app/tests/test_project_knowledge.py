"""Tests for project_knowledge.build_grounding (canonical profile → prompt
grounding) and code_validation (LSP gating + dispatch)."""

from __future__ import annotations

from rag.code_validation import validate_answer
from projects.project_knowledge import build_grounding


def _rich_profile() -> dict:
    return {
        "framework": "laravel",
        "version": "12",
        "language": "php",
        "summary": "demo app",
        "routes": [
            {"method": "POST", "uri": "/checkout", "action": "PaymentController@checkout", "name": "checkout", "middleware": [], "file": "web.php"}
        ],
        # canonical schema slice (plugin already merged + stripped boilerplate)
        "schema": [
            {
                "table": "payments",
                "columns": [
                    {"name": "id", "type": "bigint", "nullable": False, "default": None, "unique": False, "indexed": False, "foreign": None},
                    {"name": "amount", "type": "decimal", "nullable": False, "default": None, "unique": False, "indexed": False, "foreign": None},
                ],
            }
        ],
        "models": [{"name": "Payment", "file": "Payment.php", "table": "payments", "relations": []}],
        "controllers": [{"name": "PaymentController", "file": "app/Http/Controllers/PaymentController.php", "methods": ["checkout"]}],
        "config": {"mail": {"default": "env:MAIL_MAILER"}},
        "wiring": {"providers": [], "provider_count": 0, "bindings": [{"type": "singleton", "abstract": "payments.gateway", "concrete": "StripeGateway", "file": "app/Providers/AppServiceProvider.php"}], "middleware": {"aliases": {}, "groups": {}, "global": []}},
        "behaviors": {"jobs": [], "events": [], "listeners": [], "notifications": [], "schedule": [], "dispatch_sites": {}, "event_sites": {}},
        "frontend": {"views": [{"name": "checkout", "file": "checkout.blade.php"}], "components": [], "route_view_links": []},
        "errors": {"custom_exceptions": [], "throw_hotspots": [], "catch_hotspots": [], "handler_configured": True},
        "packages": {"payment_gateways": [{"gateway": "Stripe", "package": "stripe/stripe-php", "required": "^13.0", "version": "13.9.0"}], "payment_absent": ["Mollie"], "notable_dependencies": []},
    }


class TestGroundingSlices:
    def test_payment_question_selects_routes_and_packages(self):
        out = build_grounding(_rich_profile(), "which API does the payment?")
        assert "POST /checkout" in out
        assert "Payment SDK(s): Stripe" in out
        assert "mail.default" not in out  # no env-var noise for a routes question

    def test_config_question_selects_config(self):
        out = build_grounding(_rich_profile(), "where is mail configured?")
        assert "mail.default" in out
        assert "Payment SDK" not in out  # irrelevant section excluded

    def test_schema_question_selects_schema(self):
        out = build_grounding(_rich_profile(), "how are payments stored in the db?")
        assert "payments(" in out
        assert "amount:decimal" in out

    def test_frontend_question_selects_views(self):
        out = build_grounding(_rich_profile(), "where is the login page rendered?")
        assert "Views" in out

    def test_no_hints_defaults_to_core(self):
        out = build_grounding(_rich_profile(), None)
        assert "Routes (1 total)" in out
        assert "Payment SDK" not in out
        assert "Controllers: PaymentController" in out

    def test_falsy_profile_returns_none(self):
        assert build_grounding(None, "anything") is None
        assert build_grounding({}, "anything") is None  # empty profile → no grounding

    def test_only_canonical_keys_read(self):
        # unknown/extra keys must never crash or leak into output
        profile = _rich_profile()
        profile["surprise"] = {"huge": "blob"}
        profile["custom_laravel_migrations"] = [{"created": {"x": {}}}]
        out = build_grounding(profile, "which API does the payment?")
        assert "surprise" not in out
        assert "custom_laravel_migrations" not in out

    def test_schema_boilerplate_not_required(self):
        # canonical schema arrives pre-filtered; renderer must not assume
        # any framework column conventions
        profile = _rich_profile()
        profile["schema"][0]["columns"].append(
            {"name": "created_at", "type": "timestamp", "nullable": False,
             "default": None, "unique": False, "indexed": False, "foreign": None}
        )
        out = build_grounding(profile, "stored?")
        assert "created_at" in out  # whatever the plugin sends is rendered


class TestGroundingCap:
    def test_long_output_capped(self):
        profile = _rich_profile()
        profile["routes"] = [
            {"method": "GET", "uri": f"/r{i}", "action": f"C{i}@m", "name": None,
             "middleware": [], "file": "web.php"}
            for i in range(500)
        ]
        out = build_grounding(profile, None)
        assert len(out) <= 3500


class TestCodeValidation:
    def test_prose_routes_skip_validation(self, monkeypatch):
        import builtins
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "lsp":
                raise AssertionError("lsp imported for chat route")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        _answer, validation = validate_answer("hello world", "q", "chat")
        assert validation == []

    def test_lsp_disabled_skips(self, monkeypatch):
        monkeypatch.setattr("rag.code_validation.settings", type("S", (), {"lsp_validation_enabled": False})())
        _answer, validation = validate_answer("```php\n<?php echo 1;\n```", "q", "code_explain")
        assert validation == []

    def test_lsp_failure_degrades_gracefully(self, monkeypatch):
        import rag.code_validation as cv

        class Boom(Exception):
            pass

        def fake_settings_attr(name, default=None):
            return False if name == "lsp_validation_enabled" else default

        class FakeSettings:
            lsp_validation_enabled = True
            lsp_max_retries = 2

        monkeypatch.setattr(cv, "settings", FakeSettings())
        # force the lazy import path to raise
        import builtins
        real_import = builtins.__import__

        def fake_import(name, *a, **k):
            if name == "lsp":
                raise Boom("lsp missing")
            return real_import(name, *a, **k)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        _answer, validation = validate_answer("```python\nx=1\n```", "q", "debug")
        assert validation == []
