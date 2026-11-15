"""Phase 2 tests — Laravel framework-level domain concepts (static, hermetic).

The real plugin ``analyze()`` runs over tiny on-disk fixture trees (no vendor,
no runtime). Where a merge with the PHP symbol pass is exercised, that pass is
monkeypatched — the PHP CLI never runs.
"""

from __future__ import annotations

from pathlib import Path

from agent.core.workspace import Workspace
from agent.plugins.frameworks.laravel.plugin import LaravelFrameworkPlugin
from projects.domain import extractor as extractor_module
from projects.domain.normalizer import ConceptNormalizer
from projects.domain.types import DomainConcept, DomainExtraction, DomainSource

NORMALIZE = ConceptNormalizer().normalize

_COMPOSER = '{"require": {"laravel/framework": "^11.0"}}\n'
_PLUGIN = LaravelFrameworkPlugin()


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _base_fixture(root: Path, *, two_events: bool = False) -> None:
    _write(root, "composer.json", _COMPOSER)
    routes = (
        "<?php\n"
        "use Illuminate\\Support\\Facades\\Route;\n"
        "use App\\Http\\Controllers\\OrderController;\n"
        "Route::get('/orders', [OrderController::class, 'index'])->name('orders.index')->middleware('auth');\n"
        "Route::post('/api/orders', [OrderController::class, 'store'])->name('orders.store');\n"
    )
    _write(root, "routes/web.php", routes)
    _write(
        root,
        "app/Models/Payment.php",
        "<?php\n"
        "namespace App\\Models;\n"
        "use Illuminate\\Database\\Eloquent\\Model;\n"
        "class Payment extends Model { protected $table = 'payments'; }\n",
    )
    _write(
        root,
        "app/Http/Controllers/OrderController.php",
        "<?php\n"
        "namespace App\\Http\\Controllers;\n"
        "class OrderController { public function index() {} public function store() {} }\n",
    )
    event_source = "<?php\nnamespace App\\Events;\nclass OrderCreated {}\n"
    if two_events:
        # Two files declaring the SAME class leaf -> ambiguous leaf resolution.
        _write(root, "app/Events/A.php", event_source)
        _write(root, "app/Events/B.php", event_source)
    else:
        _write(root, "app/Events/OrderCreated.php", event_source)
    _write(
        root,
        "app/Listeners/SendOrderNotification.php",
        "<?php\n"
        "namespace App\\Listeners;\n"
        "use App\\Events\\OrderCreated;\n"
        "class SendOrderNotification { public function handle(OrderCreated $event) {} }\n",
    )
    _write(
        root,
        "database/migrations/2024_01_01_000000_create_payments_table.php",
        "<?php\n"
        "use Illuminate\\Database\\Migrations\\Migration;\n"
        "use Illuminate\\Database\\Schema\\Blueprint;\n"
        "use Illuminate\\Support\\Facades\\Schema;\n"
        "return new class extends Migration {\n"
        "  public function up(): void {\n"
        "    Schema::create('payments', function (Blueprint $table) {\n"
        "      $table->id();\n"
        "      $table->string('currency');\n"
        "      $table->timestamps();\n"
        "    });\n"
        "  }\n"
        "  public function down(): void { Schema::dropIfExists('payments'); }\n"
        "};\n",
    )


def _concept_of(result: DomainExtraction, identity: str) -> DomainConcept:
    return next(c for c in result.concepts if c.identity_key == identity)


def _php_symbol_extraction() -> DomainExtraction:
    """Canned PHP symbol pass for ``App\\*`` classes the plugin enriches."""
    concepts = []
    for fqn, kind in (
        ("App\\Models\\Payment", "class"),
        ("App\\Events\\OrderCreated", "class"),
        ("App\\Listeners\\SendOrderNotification", "class"),
    ):
        leaf = fqn.rsplit("\\", 1)[-1]
        concepts.append(
            DomainConcept(
                identity_key=fqn,
                canonical_name=leaf,
                qualified_name=fqn,
                concept_type=kind,
                roles=[kind],
                normalized_name=NORMALIZE(leaf),
                description=None,
                aliases=[],
                sources=[
                    DomainSource(
                        concept_identity=fqn,
                        file=f"app/{leaf}.php",
                        source_hash=f"php:{fqn}",
                        start_line=1,
                        end_line=3,
                        method="php-symbols",
                        confidence=1.0,
                        metadata=None,
                    )
                ],
                metadata={"namespace": fqn.rsplit("\\", 1)[0], "kind": kind},
            )
        )
    return DomainExtraction(
        project_id="proj-x", concepts=concepts, relationships=[]
    )


class TestRouteConcepts:
    def test_route_concept_identity_and_metadata(self, tmp_path):
        _base_fixture(tmp_path)
        result = _PLUGIN.extract_domain_concepts(Workspace(str(tmp_path)))

        route = _concept_of(result, "route:get:/orders")
        assert route.concept_type == "route"
        assert route.roles == ["route"]
        assert route.canonical_name == "GET /orders"
        assert route.normalized_name == "get orders"
        assert route.aliases[0].alias == "get orders"
        assert any(a.alias == "orders.index" and a.alias_type == "route" for a in route.aliases)
        assert route.metadata["method"] == "get"
        assert route.metadata["uri"] == "/orders"
        assert route.metadata["name"] == "orders.index"
        assert route.metadata["middleware"] == ["auth"]
        assert route.metadata["action"].endswith("OrderController@index")
        assert route.metadata["file"] == "routes/web.php"
        assert route.metadata["source"] == "static"
        assert route.sources[0].file == "routes/web.php"
        assert route.sources[0].method == "laravel-routes"
        assert route.sources[0].confidence == 1.0

        assert any(c.identity_key == "route:post:/api/orders" for c in result.concepts)

    def test_no_controller_concept_route_carries_action(self, tmp_path):
        _base_fixture(tmp_path)
        result = _PLUGIN.extract_domain_concepts(Workspace(str(tmp_path)))
        assert not any("Http\\Controllers" in c.identity_key for c in result.concepts)
        assert any(
            c.concept_type == "route"
            and "OrderController@store" in (c.metadata.get("action") or "")
            for c in result.concepts
        )


class TestTableAndModelConcepts:
    def test_table_concept_from_migration(self, tmp_path):
        _base_fixture(tmp_path)
        result = _PLUGIN.extract_domain_concepts(Workspace(str(tmp_path)))
        table = _concept_of(result, "table:payments")
        assert table.concept_type == "table"
        assert table.roles == ["table"]
        assert table.canonical_name == "payments"
        assert table.metadata["table"] == "payments"
        assert table.metadata["column_count"] == 2  # id + currency
        assert any(a.alias == "payments" for a in table.aliases)

    def test_model_enrichment_merges_onto_php_class_concept(self, tmp_path, monkeypatch):
        _base_fixture(tmp_path)
        monkeypatch.setattr(
            extractor_module,
            "extract_php_symbol_domain",
            lambda *a, **k: _php_symbol_extraction(),
        )
        result = extractor_module.extract_for_workspace(
            "proj-x",
            str(tmp_path),
            framework_plugin=_PLUGIN,
            language="php",
        )
        payment = _concept_of(result, "App\\Models\\Payment")
        assert payment.concept_type == "class"  # php pass is primary
        assert "class" in payment.roles and "model" in payment.roles
        assert any(a.alias == "payments" and a.alias_type == "table" for a in payment.aliases)
        methods = {s.method for s in payment.sources}
        assert "php-symbols" in methods
        assert "laravel-model-path-convention" in methods
        assert result.project_id == "proj-x"


class TestBehaviorEnrichments:
    def test_event_and_listener_concepts_use_path_convention(self, tmp_path):
        _base_fixture(tmp_path)
        result = _PLUGIN.extract_domain_concepts(Workspace(str(tmp_path)))
        event = _concept_of(result, "App\\Events\\OrderCreated")
        assert event.concept_type == "event"
        assert event.roles == ["event"]
        assert event.sources[0].method == "laravel-path-convention"
        assert event.sources[0].confidence == 0.6
        assert event.sources[0].file == "app/Events/OrderCreated.php"

        listener = _concept_of(result, "App\\Listeners\\SendOrderNotification")
        assert listener.concept_type == "listener"
        assert listener.metadata["handles"] == "OrderCreated"

    def test_handled_by_relation_when_unique_event_leaf(self, tmp_path):
        _base_fixture(tmp_path)
        result = _PLUGIN.extract_domain_concepts(Workspace(str(tmp_path)))
        rels = [
            (r.relation_type, r.source_identity, r.target_identity)
            for r in result.relationships
        ]
        assert (
            "HANDLED_BY",
            "App\\Events\\OrderCreated",
            "App\\Listeners\\SendOrderNotification",
        ) in rels

    def test_basename_collision_yields_no_relation(self, tmp_path):
        _base_fixture(tmp_path, two_events=True)
        result = _PLUGIN.extract_domain_concepts(Workspace(str(tmp_path)))
        # Both files enrich as standalone events keyed by their file paths.
        assert _concept_of(result, "App\\Events\\A")
        assert _concept_of(result, "App\\Events\\B")
        # Leaf "OrderCreated" is ambiguous -> no invented HANDLED_BY edge.
        assert all(r.relation_type != "HANDLED_BY" for r in result.relationships)

    def test_integration_merge_preserves_handled_by(self, tmp_path, monkeypatch):
        _base_fixture(tmp_path)
        monkeypatch.setattr(
            extractor_module,
            "extract_php_symbol_domain",
            lambda *a, **k: _php_symbol_extraction(),
        )
        result = extractor_module.extract_for_workspace(
            "proj-x", str(tmp_path), framework_plugin=_PLUGIN, language="php"
        )
        event = _concept_of(result, "App\\Events\\OrderCreated")
        assert "class" in event.roles and "event" in event.roles
        rels = [
            (r.relation_type, r.source_identity, r.target_identity)
            for r in result.relationships
        ]
        assert (
            "HANDLED_BY",
            "App\\Events\\OrderCreated",
            "App\\Listeners\\SendOrderNotification",
        ) in rels


class TestRobustness:
    def test_empty_non_laravel_dir_never_raises(self, tmp_path):
        (tmp_path / "readme.txt").write_text("hi\n")
        result = _PLUGIN.extract_domain_concepts(Workspace(str(tmp_path)))
        assert result.concepts == []
        assert result.relationships == []
        assert result.error is None
        assert result.degraded is False
