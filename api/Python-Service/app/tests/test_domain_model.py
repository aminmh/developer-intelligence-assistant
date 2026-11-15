from agent.domain_model import build_domain_model, enrich_domain_model, profile_for_saved_analysis
from agent.plugins.frameworks.laravel.models import parse_model


def _profile():
    return {
        "framework": "laravel",
        "schema": [
            {"table": "users", "columns": [{"name": "id", "type": "bigint", "primary": True}]},
            {"table": "posts", "columns": [
                {"name": "id", "type": "bigint", "primary": True},
                {"name": "user_id", "type": "bigint", "nullable": True,
                 "foreign": {"table": "users", "column": "id"}},
            ]},
        ],
        "models": [{
            "name": "Post",
            "table": "posts",
            "casts": [["status", "PostStatus"]],
            "scopes": ["Published"],
            "relations": [{"method": "user", "type": "belongsTo", "target": "App\\Models\\User"}],
        }],
    }


def test_domain_model_is_deterministic_and_has_graph_contract():
    first = build_domain_model(_profile(), "analysis-1", include_domain_concepts=False, generated_at="now")
    second = build_domain_model(_profile(), "analysis-1", include_domain_concepts=False, generated_at="now")
    assert first["plantuml"] == second["plantuml"]
    assert [node["id"] for node in first["antv_x6"]["nodes"]] == ["node-post"]
    assert first["plantuml"].startswith("@startuml") and first["plantuml"].endswith("@enduml")
    assert first["antv_x6"]["edges"] == []
    post = next(entity for entity in first["entities"] if entity["name"] == "Post")
    user_id = next(prop for prop in post["properties"] if prop["name"] == "user_id")
    assert user_id["is_foreign"] is True and user_id["is_nullable"] is True
    assert "Published" in post["scopes_or_behaviors"]
    assert any(warning["code"] == "schema-without-entity" for warning in first["warnings"])


def test_unsupported_framework_is_explicit():
    result = build_domain_model({"framework": "django", "schema": []}, "analysis-2", include_domain_concepts=False, generated_at="now")
    assert any(warning["code"] == "unsupported-framework" for warning in result["warnings"])


def test_enrichment_preserves_authoritative_properties(monkeypatch):
    base = build_domain_model(_profile(), "analysis-3", include_domain_concepts=False, generated_at="now")
    monkeypatch.setattr("agent.domain_model.complete", lambda *args, **kwargs: '{"entities":[{"name":"Post","description":"Published content","behaviors":["publish"]}]}')
    enriched = enrich_domain_model(base, _profile())
    post = next(entity for entity in enriched["entities"] if entity["name"] == "Post")
    assert post["description"] == "Published content"
    assert "publish" in post["scopes_or_behaviors"]
    assert any(prop["name"] == "user_id" and prop["is_foreign"] for prop in post["properties"])
    assert enriched["status"] == "enriched"


def test_malformed_enrichment_raises_for_endpoint_fallback(monkeypatch):
    base = build_domain_model(_profile(), "analysis-4", include_domain_concepts=False, generated_at="now")
    monkeypatch.setattr("agent.domain_model.complete", lambda *args, **kwargs: "not json")
    try:
        enrich_domain_model(base, _profile())
    except ValueError as exc:
        assert "malformed JSON" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("malformed enrichment should fail validation")


def test_missing_profile_uses_same_filename_profile_sibling():
    class Store:
        def list_by_filename(self, filename, limit=25):
            return [
                {"id": "code-index", "filename": filename, "profile": None},
                {"id": "structure", "filename": filename, "profile": _profile()},
            ]

    profile, source_id = profile_for_saved_analysis(
        {"id": "code-index", "filename": "armaiti-api-develop.zip", "profile": None},
        Store(),
    )
    assert profile["framework"] == "laravel"
    assert source_id == "structure"


def test_parent_materialization_does_not_duplicate_an_existing_model():
    result = build_domain_model(
        {
            "framework": "laravel",
            "models": [
                {"name": "BaseModel", "table": "base_models"},
                {"name": "Invoice", "extends": ["BaseModel"]},
            ],
        },
        "analysis-parent-dedup",
        include_domain_concepts=False,
        generated_at="now",
    )

    names = [entity["name"] for entity in result["entities"]]
    assert names.count("BaseModel") == 1
    assert {node["id"] for node in result["antv_x6"]["nodes"]} == {"node-basemodel", "node-invoice"}


def test_laravel_parser_and_uml_preserve_class_contract():
    source = '''
    namespace App\\Models;
    class Invoice extends BaseInvoice implements App\\Contracts\\Payable, Billable {
        protected string $status;
        private int $attempts;
        public function pay(int $amount): bool { }
    }
    '''
    parsed = parse_model(source, "Invoice.php")
    assert parsed["extends"] == ["BaseInvoice"]
    assert parsed["implements"] == ["App\\Contracts\\Payable", "Billable"]
    assert parsed["properties"][0]["visibility"] == "protected"
    assert any(method["name"] == "pay" and method["visibility"] == "public" for method in parsed["methods"])

    model = build_domain_model({"framework": "laravel", "models": [parsed]}, "uml", include_domain_concepts=False, generated_at="now")
    uml = model["plantuml"]
    assert 'interface "Payable"' in uml
    assert "Invoice --|> BaseInvoice" in uml
    assert "Invoice ..|> Payable" in uml
    assert "-int: attempts" in uml
    assert "+pay(int $amount): bool" in uml
    assert {edge["type"] for edge in model["relationships"]} >= {"inheritance", "realization"}
