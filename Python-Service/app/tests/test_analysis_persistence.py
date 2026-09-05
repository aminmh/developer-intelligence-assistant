"""Phase 1 tests — persistent analysis store + knowledge-context chunk builder."""

from __future__ import annotations

import json

from analysis.analysis_store import AnalysisStore
from projects.project_context import (
    build_grounding_md,
    build_knowledge_chunks,
)

SAMPLE = {
    "framework": "laravel",
    "version": "12",
    "language": "php",
    "summary": "demo",
    "routes": [
        {"method": "POST", "uri": "/checkout", "action": "PaymentController@checkout",
         "name": "checkout", "middleware": ["auth"], "file": "web.php", "source": "static"}
    ],
    "schema": [
        {"table": "payments", "columns": [
            {"name": "id", "type": "bigint", "nullable": False, "default": None,
             "unique": False, "indexed": False, "foreign": None},
            {"name": "user_id", "type": "bigint", "nullable": False, "default": None,
             "unique": False, "indexed": True,
             "foreign": {"table": "users", "column": "id"}},
        ]}
    ],
    "models": [{"name": "Payment", "file": "Payment.php", "table": "payments",
                "relations": [{"method": "user", "type": "belongsTo", "target": "App\\Models\\User"}]}],
    "controllers": [{"name": "PaymentController", "file": "app/Http/Controllers/PaymentController.php",
                     "methods": ["checkout"]}],
    "config": {"mail": {"default": "env:MAIL_MAILER"}},
    "wiring": {"providers": [{"name": "AppServiceProvider", "file": "app/Providers/AppServiceProvider.php",
                              "register_has_code": False, "boot_has_code": False}],
               "provider_count": 1, "registration_site": "bootstrap/providers.php",
               "bindings": [{"type": "singleton", "abstract": "payments.gateway",
                             "concrete": "StripeGateway", "file": "app/Providers/AppServiceProvider.php"}],
               "middleware": {"aliases": {"auth": "Authenticate"}, "groups": {}, "global": []}},
    "behaviors": {"jobs": [], "events": [], "listeners": [], "notifications": [],
                  "schedule": [{"task": "report:send", "frequency": "daily"}],
                  "dispatch_sites": {}, "event_sites": {}},
    "frontend": {"views": [{"name": "checkout", "file": "checkout.blade.php"}],
                 "components": [], "route_view_links": [],
                 "controller_view_links": []},
    "errors": {"custom_exceptions": [], "throw_hotspots": [], "catch_hotspots": []},
    "packages": {"payment_gateways": [], "payment_absent": ["Stripe", "Mollie"],
                 "notable_dependencies": [{"package": "laravel/framework", "required": "^12.0"}]},
}

RUNTIME = {
    "probes": [
        {"name": "routes", "status": "done", "exit_code": 0, "image": "composer:2",
         "command": ["php", "artisan", "route:list", "--json"],
         "stdout": '[{"method":"POST","uri":"/checkout"}]', "stderr": ""}
    ]
}


class TestKnowledgeChunks:
    def test_deterministic_ids(self):
        a = build_knowledge_chunks(SAMPLE, "Archive")
        b = build_knowledge_chunks(SAMPLE, "Archive")
        assert a == b
        assert [c["class_fqn"] for c in a] == [c["class_fqn"] for c in b]

    def test_route_chunk_content(self):
        chunks = build_knowledge_chunks(SAMPLE, "Archive")
        route = next(c for c in chunks if c["type"] == "route")
        assert route["name"].startswith("route:")
        assert "POST /checkout" in route["code"]
        assert "middleware: auth" in route["code"]
        assert route["file"] == "web.php"

    def test_table_chunk_with_fk(self):
        chunks = build_knowledge_chunks(SAMPLE, "Archive")
        table = next(c for c in chunks if c["type"] == "table")
        assert "payments" in table["code"]
        assert "user_id: bigint -> users.id" in table["code"]

    def test_no_payment_sdk_chunk(self):
        chunks = build_knowledge_chunks(SAMPLE, "Archive")
        pkg = next(c for c in chunks if c["type"] == "package")
        assert "NO PAYMENT SDK INSTALLED" in pkg["code"]
        assert "Stripe" in pkg["code"]  # searched list

    def test_kind_prefixes_distinct(self):
        chunks = build_knowledge_chunks(SAMPLE, "Archive")
        fqns = {c["class_fqn"] for c in chunks}
        # each canonical section kind yields its own kn:{kind} namespace
        assert any(f.startswith("kn:route:Archive") for f in fqns)
        assert any(f.startswith("kn:table:Archive") for f in fqns)
        assert any(f.startswith("kn:provider:Archive") for f in fqns)
        assert any(f.startswith("kn:binding:Archive") for f in fqns)

    def test_runtime_probe_chunk(self):
        chunks = build_knowledge_chunks(SAMPLE, "Archive", runtime=RUNTIME)
        probe = next(c for c in chunks if c["type"] == "runtime_probe")
        assert "RUNTIME PROBE routes" in probe["code"]
        assert "route:list" in probe["code"]

    def test_no_secret_config_values(self):
        # config values reaching chunks are env:KEY-safe (plugin guarantee);
        # assert the rendered chunk never embeds a raw secret-shaped value
        leaky = dict(SAMPLE)
        leaky["config"] = {"services": {"stripe": {"secret": "sk_live_LEAK"}}}
        chunks = build_knowledge_chunks(leaky, "Archive")
        blob = " ".join(c["code"] for c in chunks)
        assert "sk_live_LEAK" not in blob

    def test_grounding_md_includes_runtime(self):
        md = build_grounding_md(SAMPLE, runtime=RUNTIME, filename="Archive.zip")
        assert "Archive.zip" in md
        assert "Runtime probes (1)" in md
        assert "route:list" in md


class TestAnalysisStore:
    def _store(self, tmp_path) -> AnalysisStore:
        return AnalysisStore(
            db_path=str(tmp_path / "settings.db"),
            analysis_dir=str(tmp_path / "analyses"),
        )

    def test_save_and_get(self, tmp_path):
        store = self._store(tmp_path)
        store.save("s1", "job-1", "Archive.zip", label="first",
                   profile=SAMPLE, grounding_md="# g", runtime=RUNTIME,
                   counts={"routes": 1}, collection_name="proj_knowledge_Archive")
        row = store.get("s1")
        assert row["job_id"] == "job-1"
        assert row["label"] == "first"
        assert row["profile"]["framework"] == "laravel"
        assert row["runtime"]["probes"][0]["name"] == "routes"
        assert row["counts"]["routes"] == 1
        assert row["collection_name"] == "proj_knowledge_Archive"

    def test_artifacts_written(self, tmp_path):
        store = self._store(tmp_path)
        store.save("s1", "job-1", "a.zip", profile=SAMPLE, grounding_md="# g")
        d = store.artifacts_dir("s1")
        assert (d / "profile.json").is_file()
        assert json.loads((d / "profile.json").read_text())["framework"] == "laravel"
        assert (d / "grounding.md").read_text() == "# g"
        assert not (d / "runtime.json").exists()  # only when provided

    def test_upsert_keeps_created_at(self, tmp_path):
        store = self._store(tmp_path)
        store.save("s1", "job-1", "a.zip", label="one")
        first = store.get("s1")["created_at"]
        store.save("s1", "job-1", "a.zip", label="two")
        row = store.get("s1")
        assert row["label"] == "two"
        assert row["created_at"] == first  # preserved on overwrite

    def test_by_job_and_list_order(self, tmp_path):
        store = self._store(tmp_path)
        store.save("older", "job-a", "a.zip", label="a")
        store.save("newer", "job-b", "b.zip", label="b")
        assert store.get_by_job("job-b")["id"] == "newer"
        ids = [r["id"] for r in store.list()]
        assert ids == ["newer", "older"]  # newest first

    def test_delete_removes_row_and_artifacts(self, tmp_path):
        store = self._store(tmp_path)
        store.save("s1", "job-1", "a.zip", profile=SAMPLE)
        assert store.delete("s1") is True
        assert store.get("s1") is None
        assert not store.artifacts_dir("s1").exists()
        assert store.delete("s1") is False  # already gone

    def test_isolated_tables_shared_db(self, tmp_path):
        # analysis table lives alongside the settings KV table without clash
        import sqlite3

        db = str(tmp_path / "settings.db")
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
        conn.commit()
        conn.close()

        store = AnalysisStore(db_path=db, analysis_dir=str(tmp_path / "analyses"))
        store.save("s1", "job-1", "a.zip")
        conn = sqlite3.connect(db)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
        assert "analysis_sessions" in tables
        assert "settings" in tables  # untouched KV table coexists
