"""Phase 5 tests — RAG integration of the domain-concept recognizer.

Covers the UI/store setting key, best-effort auto-index on save, the /answer
domain-context block, and the /domain/recognize + /domain/reconcile endpoints.
No daemon/Qdrant/Neo4j/embedding contact — the concept store, extractor,
indexer and recognizer are faked at their module seams.
"""

from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

import analysis.analysis_service as svc
import infra.settings_store as settings_store_module
import main as main_module
import projects.domain.extractor as domain_extractor_mod
import projects.domain.indexer as domain_indexer_mod
import projects.domain.recognizer as domain_recognizer_mod
import projects.domain.store as domain_store_mod
from infra.settings import RuntimeSettings, Settings
from projects.domain.types import DomainExtraction

# --- fakes ------------------------------------------------------------------


class FakeConceptStore:
    """In-memory stand-in for ConceptStore (Phase-5 surface only)."""

    def __init__(self, events=None):
        self.events = events if events is not None else []
        self.project_concepts: dict[str, list] = {}
        self.project_relationships: dict[str, list] = {}

    def upsert_extraction(self, project_id, extraction):
        self.events.append(("upsert", project_id))
        concepts = list(getattr(extraction, "concepts", None) or [])
        relationships = list(getattr(extraction, "relationships", None) or [])
        # Mirrors ConceptStore.upsert_extraction: additive per identity_key.
        by_identity = {
            getattr(c, "identity_key", ""): c
            for c in self.project_concepts.get(project_id, [])
        }
        for concept in concepts:
            by_identity[getattr(concept, "identity_key", "")] = concept
        self.project_concepts[project_id] = list(by_identity.values())
        self.project_relationships[project_id] = relationships
        return {
            "concepts": len(concepts),
            "relationships": len(relationships),
            "error": None,
        }

    def remove_concepts_not_in(self, project_id, keep_identity_keys):
        self.events.append(("remove_stale", project_id))
        keep = {str(key) for key in (keep_identity_keys or [])}
        existing = self.project_concepts.get(project_id, [])
        kept = [
            c
            for c in existing
            if str(getattr(c, "identity_key", "")) in keep
        ]
        removed = len(existing) - len(kept)
        self.project_concepts[project_id] = kept
        kept_ids = {str(getattr(c, "identity_key", "")) for c in kept}
        self.project_relationships[project_id] = [
            rel
            for rel in self.project_relationships.get(project_id, [])
            if str(getattr(rel, "source_identity", "")) in kept_ids
            and str(getattr(rel, "target_identity", "")) in kept_ids
        ]
        return removed

    def list_concepts(self, project_id):
        return [
            {"id": c.identity_key, "identity_key": c.identity_key}
            for c in self.project_concepts.get(project_id, [])
        ]

    def clear_index_states(self, project_id):
        self.events.append(("clear", project_id))
        return 0

    def counts(self, project_id=None):
        if project_id is None:
            concepts = sum(len(v) for v in self.project_concepts.values())
        else:
            concepts = len(self.project_concepts.get(project_id, []))
        return {
            "concepts": concepts,
            "aliases": 0,
            "relationships": len(
                self.project_relationships.get(project_id, [])
                if project_id is not None
                else sum(len(v) for v in self.project_relationships.values())
            ),
            "pending_index": 0,
        }


class BoomStore(FakeConceptStore):
    """Store whose upsert always blows up (total-behaviour tests)."""

    def upsert_extraction(self, project_id, extraction):
        raise RuntimeError("store boom")


def _extraction(project_id: str = "Archive", n_concepts: int = 2, n_rels: int = 1):
    concepts = [
        SimpleNamespace(identity_key=f"concept{i}") for i in range(n_concepts)
    ]
    relationships = [
        SimpleNamespace(
            relation_type="depends",
            source_identity="concept0",
            target_identity="concept1",
        )
        for _ in range(n_rels)
    ]
    return DomainExtraction(
        project_id=project_id,
        concepts=concepts,
        relationships=relationships,
    )


def _patch_domain_backend(
    monkeypatch,
    tmp_path,
    extraction,
    *,
    store=None,
    events=None,
    prepare_workspace: bool = False,
    removed=None,
):
    """Wire the service's domain seams to in-memory fakes."""
    events = events if events is not None else []
    retained = tmp_path / "retained"
    retained.mkdir(exist_ok=True)
    if prepare_workspace:
        monkeypatch.setattr(svc, "_retained_workspace", lambda filename: None)
        monkeypatch.setattr(
            svc, "_prepare_workspace_http", lambda filename: str(retained)
        )
    else:
        monkeypatch.setattr(
            svc, "_retained_workspace", lambda filename: str(retained)
        )
        monkeypatch.setattr(
            svc, "_prepare_workspace_http", lambda filename: (_ for _ in ()).throw(
                AssertionError("HTTP prepare must not run for a retained workspace")
            )
        )
    store = store or FakeConceptStore(events=events)
    monkeypatch.setattr(domain_store_mod, "concept_store", store)

    def _fake_extract(project_id, workspace_path, **kwargs):
        events.append(("extract", project_id))
        return extraction

    monkeypatch.setattr(domain_extractor_mod, "extract_for_workspace", _fake_extract)
    monkeypatch.setattr(
        domain_indexer_mod,
        "ensure_collection",
        lambda collection="domain_concepts": {"ok": True, "count": 0},
    )
    monkeypatch.setattr(
        domain_indexer_mod,
        "purge_project_vectors",
        lambda project_id, collection="domain_concepts": (
            events.append(("purge", project_id))
            or {"ok": True, "deleted": 1}
        ),
    )
    monkeypatch.setattr(
        domain_indexer_mod,
        "index_project_concepts",
        lambda project_id, rows, collection="domain_concepts": (
            events.append(("index", project_id))
            or {"ok": True, "indexed": len(rows)}
        ),
    )
    if removed is not None:
        monkeypatch.setattr(
            "infra.php_tools.safe_rmtree",
            lambda path: removed.append(str(path)),
        )
    return store


def _recognition_result(recognized=None, relationships=None, rejected=None):
    return SimpleNamespace(
        recognized=list(recognized or []),
        ambiguous=[],
        rejected=list(rejected or []),
        relationships=list(relationships or []),
        degraded=False,
        error=None,
        query_note=None,
    )


def _make_persist(kind):
    """Persist fake bound to one kind (avoids loop-scope closures)."""

    def _fake_persist(job_id, label="", **kwargs):
        return {"job_id": job_id, "kind": kind}

    return _fake_persist


def _make_index(calls):
    """Auto-index fake recording into a shared list (avoids loop closures)."""

    def _fake_index(filename):
        calls.append(filename)
        return {"ok": True, "project_id": "Archive"}

    return _fake_index


def _client():
    return TestClient(main_module.app)


# --- setting wiring ---------------------------------------------------------


class TestDomainSetting:
    def test_domain_concepts_enabled_plumbed_three_ways(self):
        assert (
            settings_store_module.RAG_CONFIG_DEFAULTS.get("domainConceptsEnabled")
            is True
        )
        assert (
            RuntimeSettings._DYNAMIC.get("domain_concepts_enabled")
            == "domainConceptsEnabled"
        )
        # Env default (no DOMAIN_CONCEPTS_ENABLED override at import time).
        assert Settings().domain_concepts_enabled is True


# --- persist_for_bundle auto-index hook --------------------------------------


class TestPersistAutoIndex:
    def test_persist_for_bundle_calls_index_when_enabled(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ANALYSES_DIR", str(tmp_path))
        svc.write_bundle(
            "jobA",
            {
                "filename": "Archive.zip",
                "manifest": {"route": "structure_analyze"},
            },
        )
        calls = []
        monkeypatch.setattr(
            svc,
            "persist_structure",
            lambda job_id, label="": {"job_id": job_id, "kind": "structure"},
        )
        monkeypatch.setattr(
            svc,
            "index_domain_for_workspace",
            lambda filename: calls.append(filename) or {"ok": True, "project_id": "Archive"},
        )
        monkeypatch.setattr(svc.settings, "domain_concepts_enabled", True)
        row = svc.persist_for_bundle("jobA")
        assert row is not None
        assert calls == ["Archive.zip"]
        assert row["domain_index"] == {"ok": True, "project_id": "Archive"}

    def test_persist_for_bundle_skips_index_when_disabled(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ANALYSES_DIR", str(tmp_path))
        svc.write_bundle(
            "jobB",
            {
                "filename": "Archive.zip",
                "manifest": {"route": "structure_analyze"},
            },
        )
        calls = []
        monkeypatch.setattr(
            svc,
            "persist_structure",
            lambda job_id, label="": {"job_id": job_id, "kind": "structure"},
        )

        def _should_not_run(filename):
            calls.append(filename)
            raise AssertionError("auto-index must be skipped when disabled")

        monkeypatch.setattr(svc, "index_domain_for_workspace", _should_not_run)
        monkeypatch.setattr(svc.settings, "domain_concepts_enabled", False)
        row = svc.persist_for_bundle("jobB")
        assert row is not None
        assert calls == []
        assert "domain_index" not in row

    def test_persist_for_bundle_indexes_each_kind(self, tmp_path, monkeypatch):
        for route, kind, fn_name in (
            ("structure_analyze", "structure", "persist_structure"),
            ("index_code", "code_index", "persist_code_index"),
            ("", "analysis", "persist_analysis"),
        ):
            monkeypatch.setenv("ANALYSES_DIR", str(tmp_path))
            job = f"job-{route or 'analysis'}"
            svc.write_bundle(
                job,
                {
                    "filename": "Archive.zip",
                    "manifest": {"route": route},
                },
            )
            calls = []
            monkeypatch.setattr(svc, fn_name, _make_persist(kind))
            monkeypatch.setattr(svc, "index_domain_for_workspace", _make_index(calls))
            monkeypatch.setattr(svc.settings, "domain_concepts_enabled", True)
            row = svc.persist_for_bundle(job)
            assert row is not None
            assert calls == ["Archive.zip"], f"{route} must auto-index"
            assert row["domain_index"]["ok"] is True


# --- index_domain_for_workspace (B) ------------------------------------------


class TestIndexDomainForWorkspace:
    def test_auto_index_upserts_and_indexes(self, tmp_path, monkeypatch):
        events = []
        _patch_domain_backend(
            monkeypatch, tmp_path, _extraction(n_concepts=2, n_rels=1), events=events
        )
        out = svc.index_domain_for_workspace("Archive.zip")
        assert out["ok"] is True
        assert out["project_id"] == "Archive"
        assert out["concepts"] == 2
        assert out["relationships"] == 1
        assert out["indexed"] == 2
        kinds = [kind for kind, _ in events]
        assert kinds.count("upsert") == 1
        assert kinds.count("index") == 1

    def test_auto_index_never_raises_on_store_failure(self, tmp_path, monkeypatch):
        _patch_domain_backend(
            monkeypatch,
            tmp_path,
            _extraction(),
            store=BoomStore(events=[]),
            events=[],
        )
        out = svc.index_domain_for_workspace("Archive.zip")  # must not raise
        assert out["ok"] is False
        assert "store boom" in out["error"]

    def test_auto_index_cleans_http_prepared_workspace(self, tmp_path, monkeypatch):
        removed = []
        _patch_domain_backend(
            monkeypatch,
            tmp_path,
            _extraction(n_concepts=1),
            events=[],
            prepare_workspace=True,
            removed=removed,
        )
        out = svc.index_domain_for_workspace("Archive.zip")
        assert out["ok"] is True
        assert removed, "HTTP-prepared workspace must be removed"

    def test_auto_index_missing_workspace_is_honest(self, tmp_path, monkeypatch):
        monkeypatch.setattr(svc, "_retained_workspace", lambda filename: None)
        monkeypatch.setattr(svc, "_prepare_workspace_http", lambda filename: None)
        out = svc.index_domain_for_workspace("Archive.zip")
        assert out["ok"] is False
        assert "workspace unavailable" in out["error"]


# --- reconcile (E) ------------------------------------------------------------


class TestReconcileDomainIndex:
    def test_reconcile_orders_clear_before_purge(self, tmp_path, monkeypatch):
        events = []
        _patch_domain_backend(
            monkeypatch,
            tmp_path,
            _extraction(n_concepts=2, n_rels=1),
            events=events,
        )
        out = svc.reconcile_domain_index("Archive.zip")
        assert out["success"] is True
        assert out["indexed"] == 2
        assert out["concepts"]["concepts"] == 2
        kinds = [kind for kind, _ in events]
        assert kinds.index("clear") < kinds.index("purge"), (
            "index_state must be cleared before the Qdrant purge "
            "(skip-trap avoidance)"
        )
        assert kinds.index("purge") < kinds.index("index")

    def test_reconcile_never_raises_on_store_failure(self, tmp_path, monkeypatch):
        _patch_domain_backend(
            monkeypatch,
            tmp_path,
            _extraction(),
            store=BoomStore(events=[]),
            events=[],
        )
        out = svc.reconcile_domain_index("Archive.zip")  # must not raise
        assert out["success"] is False
        assert "store boom" in out["error"]

    def test_reconcile_cleans_http_prepared_workspace(self, tmp_path, monkeypatch):
        removed = []
        _patch_domain_backend(
            monkeypatch,
            tmp_path,
            _extraction(n_concepts=1),
            events=[],
            prepare_workspace=True,
            removed=removed,
        )
        out = svc.reconcile_domain_index("Archive.zip")
        assert out["success"] is True
        assert removed, "HTTP-prepared workspace must be removed"

    def test_reconcile_removes_stale_concepts_absent_from_fresh_extraction(
        self, tmp_path, monkeypatch
    ):
        events = []
        store = FakeConceptStore(events=events)
        store.project_concepts["Archive"] = [
            SimpleNamespace(identity_key="staleConcept"),
            SimpleNamespace(identity_key="concept0"),
        ]
        _patch_domain_backend(
            monkeypatch,
            tmp_path,
            _extraction(n_concepts=1, n_rels=0),
            store=store,
            events=events,
        )
        out = svc.reconcile_domain_index("Archive.zip")
        assert out["success"] is True
        assert out["removed"] == 1
        assert out["concepts"]["concepts"] == 1
        remaining = {
            getattr(c, "identity_key", None)
            for c in store.project_concepts.get("Archive", [])
        }
        assert remaining == {"concept0"}
        kinds = [kind for kind, _ in events]
        assert "remove_stale" in kinds
        assert kinds.index("remove_stale") < kinds.index("index")


# --- structure_analyze workspace retention (C) --------------------------------


class TestRetentionWiring:
    def test_structure_analyze_route_retains_workspace(self):
        from pipelines.orchestrator import orchestrator as orch

        route = orch.get_route("structure_analyze")
        assert route and route.get("retain_workspace") is True
        # index_code retained for persist re-parse (unchanged); plain chat/search
        # routes must NOT accidentally retain.
        assert orch.get_route("index_code").get("retain_workspace") is True
        assert not orch.get_route("chat").get("retain_workspace")
        assert not orch.get_route("search").get("retain_workspace")


# --- store bulk API (additive) -----------------------------------------------


class TestStoreUpsertExtraction:
    def test_upsert_extraction_persists_concepts_and_relationships(
        self, tmp_path
    ):
        from projects.domain.normalizer import ConceptNormalizer
        from projects.domain.store import ConceptStore
        from projects.domain.types import (
            DomainAlias,
            DomainConcept,
            DomainRelationship,
        )

        normalize = ConceptNormalizer().normalize

        def _concept(identity: str, canonical: str) -> DomainConcept:
            return DomainConcept(
                identity_key=identity,
                canonical_name=canonical,
                qualified_name=f"App\\{canonical}",
                concept_type="class",
                roles=["class", "service"],
                normalized_name=normalize(canonical),
                description=None,
                aliases=[DomainAlias(alias=canonical.lower())],
                sources=[],
                metadata={},
            )

        store = ConceptStore(db_path=str(tmp_path / "domain.db"))
        extraction = DomainExtraction(
            project_id="Archive",
            concepts=[
                _concept("UserService", "UserService"),
                _concept("Invoice", "Invoice"),
            ],
            relationships=[
                DomainRelationship(
                    relation_type="depends",
                    source_identity="UserService",
                    target_identity="Invoice",
                    confidence=1.0,
                    sources=[],
                    metadata={},
                )
            ],
        )
        saved = store.upsert_extraction("Archive", extraction)
        assert saved["concepts"] == 2
        assert saved["relationships"] == 1
        assert saved["error"] is None
        counts = store.counts("Archive")
        assert counts["concepts"] == 2
        assert counts["relationships"] == 1
        # Idempotent re-save never duplicates canonical rows.
        again = store.upsert_extraction("Archive", extraction)
        assert again["concepts"] == 2
        assert store.counts("Archive")["concepts"] == 2


# --- /answer domain-context block (D) ----------------------------------------


class TestMaybeDomainContext:
    def test_returns_none_when_disabled(self, monkeypatch):
        monkeypatch.setattr(main_module.settings, "domain_concepts_enabled", False)
        monkeypatch.setattr(main_module, "_domain_store_has_concepts", lambda pid: True)
        assert (
            main_module._maybe_domain_context(
                [], {"query": "user service", "filename": "Archive.zip"}
            )
            is None
        )

    def test_returns_none_without_project_anchor(self, monkeypatch):
        monkeypatch.setattr(main_module.settings, "domain_concepts_enabled", True)
        assert main_module._maybe_domain_context([], {"query": "user service"}) is None

    def test_returns_none_when_only_rejected(self, monkeypatch):
        monkeypatch.setattr(main_module.settings, "domain_concepts_enabled", True)
        monkeypatch.setattr(main_module, "_domain_store_has_concepts", lambda pid: True)
        monkeypatch.setattr(
            domain_recognizer_mod,
            "recognize",
            lambda query, project_id: _recognition_result(
                recognized=[],
                rejected=[{"identity_key": "c1", "canonical_name": "Thing", "final_score": 0.1}],
            ),
        )
        assert (
            main_module._maybe_domain_context(
                [], {"query": "nothing here", "filename": "Archive.zip"}
            )
            is None
        )

    def test_renders_recognized_and_relationships(self, monkeypatch):
        monkeypatch.setattr(main_module.settings, "domain_concepts_enabled", True)
        monkeypatch.setattr(main_module, "_domain_store_has_concepts", lambda pid: True)
        recognized = [
            {
                "identity_key": "UserService",
                "canonical_name": "UserService",
                "concept_type": "class",
                "qualified_name": "App\\Services\\UserService",
                "normalized_name": "userservice",
            }
        ]
        relationships = [
            {
                "relation_type": "depends",
                "source": {"identity_key": "a", "canonical_name": "UserService"},
                "target": {"identity_key": "b", "canonical_name": "Invoice"},
            }
        ]
        monkeypatch.setattr(
            domain_recognizer_mod,
            "recognize",
            lambda query, project_id: _recognition_result(
                recognized=recognized, relationships=relationships
            ),
        )
        block = main_module._maybe_domain_context(
            [], {"query": "user service depends on invoice", "filename": "Archive.zip"}
        )
        assert block is not None
        assert "# Recognized domain concepts" in block
        assert "- **UserService** (class) — App\\Services\\UserService" in block
        assert "# Verified related facts" in block
        assert "- UserService depends Invoice" in block

    def test_recognizer_receives_derived_project_id(self, monkeypatch):
        monkeypatch.setattr(main_module.settings, "domain_concepts_enabled", True)
        monkeypatch.setattr(main_module, "_domain_store_has_concepts", lambda pid: True)
        calls = []

        def _fake_recognize(query, project_id):
            calls.append((query, project_id))
            return _recognition_result(
                recognized=[{"identity_key": "c", "canonical_name": "C"}]
            )

        monkeypatch.setattr(domain_recognizer_mod, "recognize", _fake_recognize)
        block = main_module._maybe_domain_context(
            [], {"query": "checkout flow", "filename": "Archive.zip"}
        )
        assert block is not None
        assert calls == [("checkout flow", "Archive")]

    def test_collection_anchor_derives_project_id(self, monkeypatch):
        monkeypatch.setattr(main_module.settings, "domain_concepts_enabled", True)
        monkeypatch.setattr(main_module, "_domain_store_has_concepts", lambda pid: True)
        calls = []
        monkeypatch.setattr(
            domain_recognizer_mod,
            "recognize",
            lambda query, project_id: calls.append(project_id)
            or _recognition_result(
                recognized=[{"identity_key": "c", "canonical_name": "C"}]
            ),
        )
        main_module._maybe_domain_context(
            [], {"query": "x", "collection": "proj_knowledge_Archive"}
        )
        assert calls == ["Archive"]


# --- endpoints (E) ------------------------------------------------------------


class TestDomainEndpoints:
    def test_get_domain_recognize_success_shape(self, monkeypatch):
        recognized = [{"identity_key": "c1", "canonical_name": "UserService"}]
        monkeypatch.setattr(
            domain_recognizer_mod,
            "recognize",
            lambda query, project_id, **kwargs: _recognition_result(
                recognized=recognized,
                rejected=[{"identity_key": "c2", "canonical_name": "Nope", "final_score": 0.2}],
            ),
        )
        resp = _client().get(
            "/domain/recognize", params={"query": "user", "project_id": "Archive"}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        result = data["result"]
        assert result["recognized"] == recognized
        assert result["rejected"]
        assert result["relationships"] == []
        assert "error" in result and "query_note" in result
        assert result["degraded"] is False

    def test_get_domain_recognize_empty_project_id_400(self):
        resp = _client().get(
            "/domain/recognize", params={"query": "user", "project_id": ""}
        )
        assert resp.status_code == 400

    def test_get_domain_recognize_guard_returns_200_false(self, monkeypatch):
        def _boom(query, project_id, **kwargs):
            raise RuntimeError("recognize exploded")

        monkeypatch.setattr(domain_recognizer_mod, "recognize", _boom)
        resp = _client().get(
            "/domain/recognize", params={"query": "user", "project_id": "Archive"}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert "exploded" in data["error"]

    def test_post_domain_reconcile_success_endpoint(self, tmp_path, monkeypatch):
        _patch_domain_backend(
            monkeypatch,
            tmp_path,
            _extraction(n_concepts=2, n_rels=1),
            events=[],
        )
        resp = _client().post("/domain/reconcile", json={"filename": "Archive.zip"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["concepts"]["concepts"] == 2
        assert data["indexed"] == 2

    def test_post_domain_reconcile_missing_filename_400(self):
        resp = _client().post("/domain/reconcile", json={})
        assert resp.status_code == 400

    def test_post_domain_reconcile_never_raises_on_store_failure(
        self, tmp_path, monkeypatch
    ):
        _patch_domain_backend(
            monkeypatch,
            tmp_path,
            _extraction(),
            store=BoomStore(events=[]),
            events=[],
        )
        resp = _client().post(
            "/domain/reconcile", json={"filename": "Archive.zip"}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert "store boom" in data["error"]
