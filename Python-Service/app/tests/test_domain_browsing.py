"""Focused contracts for read-only domain browsing and expanded context."""

from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

import analysis.analysis_store as analysis_store_module
import main as main_module
import projects.domain.recognizer as recognizer_module
import projects.domain.store as store_module
import rag.domain_dictionary as dictionary_module


class FakeAnalysisStore:
    def get(self, analysis_id):
        if analysis_id != "analysis-1":
            return None
        return {"id": analysis_id, "filename": "Archive.zip"}


class FakeDomainStore:
    def __init__(self):
        self.rows = [
            {
                "id": "c1",
                "project_id": "Archive",
                "identity_key": "class:UserService",
                "canonical_name": "UserService",
                "qualified_name": "App\\Services\\UserService",
                "concept_type": "class",
                "roles": ["service"],
                "normalized_name": "userservice",
                "description": "Coordinates users.",
                "metadata": {"roles": ["service"]},
            },
            {
                "id": "c2",
                "project_id": "Archive",
                "identity_key": "table:users",
                "canonical_name": "users",
                "qualified_name": "users",
                "concept_type": "table",
                "roles": [],
                "normalized_name": "users",
                "description": None,
                "metadata": {},
            },
        ]

    def count_concepts(self, project_id, *, search=None, concept_type=None):
        return len(self.list_concepts(project_id, search=search, concept_type=concept_type))

    def list_concepts(self, project_id, limit=1000, *, search=None, concept_type=None, offset=0):
        rows = [row for row in self.rows if row["project_id"] == project_id]
        if search:
            needle = search.casefold()
            rows = [row for row in rows if needle in row["canonical_name"].casefold()]
        if concept_type:
            rows = [row for row in rows if row["concept_type"] == concept_type]
        return rows[offset : offset + limit]

    def aliases_for_concept(self, concept_id, limit=100):
        return [{"alias": "users", "alias_type": "generated", "confidence": 0.5}]

    def find_sources_for_concept(self, project_id, identity_key):
        return [{"source_file": "app/Services/UserService.php", "start_line": 4, "end_line": 20, "extraction_method": "parser", "confidence": 1.0}]

    def relationships_for(self, project_id, concept_id=None, limit=200):
        return [{"id": "r1"}] if concept_id == "c1" else []

    def find_by_id(self, project_id, concept_id):
        return next((row for row in self.rows if row["project_id"] == project_id and row["id"] == concept_id), None)

    def find_by_identity(self, project_id, identity_key):
        return next((row for row in self.rows if row["project_id"] == project_id and row["identity_key"] == identity_key), None)

    def related_nodes(self, project_id, concept_id, **kwargs):
        return {
            "nodes": [next(row for row in self.rows if row["id"] == concept_id)],
            "edges": [],
            "truncated": True,
        }


def _client():
    return TestClient(main_module.app)


def test_domain_concepts_missing_analysis_is_404(monkeypatch):
    monkeypatch.setattr(analysis_store_module, "analysis_store", FakeAnalysisStore())
    response = _client().get("/analysis/missing/domain-concepts")
    assert response.status_code == 404


def test_domain_concepts_contract_filters_and_redacts(monkeypatch):
    monkeypatch.setattr(analysis_store_module, "analysis_store", FakeAnalysisStore())
    fake_store = FakeDomainStore()
    monkeypatch.setattr(store_module, "concept_store", fake_store)
    response = _client().get(
        "/analysis/analysis-1/domain-concepts",
        params={"search": "user", "type": "class", "offset": 0, "limit": 1},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["source"]["project_id"] == "Archive"
    assert body["total"] == 1
    assert body["items"][0]["sources"][0]["file"].endswith("UserService.php")
    assert "source_hash" not in body["items"][0]["metadata"]


def test_domain_context_is_bounded_and_reports_truncation(monkeypatch):
    monkeypatch.setattr(analysis_store_module, "analysis_store", FakeAnalysisStore())
    fake_store = FakeDomainStore()
    monkeypatch.setattr(store_module, "concept_store", fake_store)
    monkeypatch.setattr(
        recognizer_module,
        "recognize",
        lambda query, project_id, **kwargs: SimpleNamespace(
            recognized=[fake_store.rows[0]],
            ambiguous=[],
            rejected=[],
            relationships=[],
            degraded=False,
            error=None,
            query_note=None,
        ),
    )
    response = _client().get(
        "/analysis/analysis-1/domain-context", params={"query": "user service"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["recognized"][0]["canonical_name"] == "UserService"
    assert body["truncated"] is True
    assert body["evidence"][0]["file"].endswith("UserService.php")


def test_answer_expanded_mode_is_opt_in(monkeypatch):
    fake_store = FakeDomainStore()
    monkeypatch.setattr(store_module, "concept_store", fake_store)
    monkeypatch.setattr(main_module.settings, "domain_concepts_enabled", True)
    monkeypatch.setattr(
        recognizer_module,
        "recognize",
        lambda query, project_id, **kwargs: SimpleNamespace(
            recognized=[fake_store.rows[0]],
            ambiguous=[],
            rejected=[],
            relationships=[],
            degraded=False,
            error=None,
            query_note=None,
        ),
    )
    default = main_module._maybe_domain_context(
        [], {"query": "user service", "filename": "Archive.zip"}
    )
    assert default is not None
    assert "# Recognized domain concepts" in default
    assert "# Expanded domain context" not in default
    expanded = main_module._maybe_domain_context(
        [],
        {
            "query": "user service",
            "filename": "Archive.zip",
            "domain_context_mode": "expanded",
        },
    )
    assert expanded is not None
    assert "# Expanded domain context" in expanded


def test_domain_dictionary_parses_and_filters(monkeypatch, tmp_path):
    dictionary = tmp_path / "domain_dictionary.txt"
    dictionary.write_text("checkout\t3\nuser\tbad\nmalformed\n", encoding="utf-8")
    monkeypatch.setattr(dictionary_module, "DICTIONARY_PATH", dictionary)
    response = _client().get("/domain/dictionary", params={"search": "checkout"})
    assert response.status_code == 200
    body = response.json()
    assert body["items"] == [{"term": "checkout", "count": 3}]
    assert body["total"] == 1
    assert body["warnings"]
