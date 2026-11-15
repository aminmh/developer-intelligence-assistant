"""Phase 2 tests — domain extraction composition + merge (hermetic)."""

from __future__ import annotations

from pathlib import Path

from projects.domain import extractor as extractor_module
from projects.domain.extractor import (
    detect_php,
    extract_for_workspace,
    merge_extractions,
)
from projects.domain.normalizer import ConceptNormalizer
from projects.domain.types import (
    DomainAlias,
    DomainConcept,
    DomainExtraction,
    DomainRelationship,
    DomainSource,
)

NORMALIZE = ConceptNormalizer().normalize


def _alias(alias: str, provenance: str = "normalizer") -> DomainAlias:
    return DomainAlias(alias=alias, confidence=0.5, provenance=provenance)


def _source(
    file: str,
    method: str = "parser",
    start_line: int | None = 1,
    confidence: float = 1.0,
) -> DomainSource:
    return DomainSource(
        concept_identity="x",
        file=file,
        source_hash=f"h:{file}:{method}:{start_line}",
        start_line=start_line,
        end_line=None,
        method=method,
        confidence=confidence,
        metadata=None,
    )


def _concept(
    identity: str,
    concept_type: str = "class",
    roles: list[str] | None = None,
    aliases: list[DomainAlias] | None = None,
    sources: list[DomainSource] | None = None,
    description: str | None = None,
    metadata: dict | None = None,
) -> DomainConcept:
    leaf = identity.rsplit("\\", 1)[-1]
    return DomainConcept(
        identity_key=identity,
        canonical_name=leaf,
        qualified_name=identity,
        concept_type=concept_type,
        roles=list(roles or [concept_type]),
        normalized_name=NORMALIZE(leaf),
        description=description,
        aliases=list(aliases or [_alias(NORMALIZE(leaf))]),
        sources=list(sources or [_source("a.php")]),
        metadata=metadata or {},
    )


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _rel(
    source: str,
    target: str,
    relation_type: str = "CALLS",
    confidence: float = 1.0,
) -> DomainRelationship:
    return DomainRelationship(
        relation_type=relation_type,
        source_identity=source,
        target_identity=target,
        confidence=confidence,
        sources=[],
        metadata={},
    )


class TestMergeExtractions:
    def test_merge_unions_roles_aliases_sources_and_dedupes(self):
        merged = merge_extractions(
            [
                DomainExtraction(
                    project_id="p",
                    concepts=[
                        _concept(
                            "App\\Payment",
                            concept_type="class",
                            roles=["class"],
                            aliases=[_alias("payment")],
                            sources=[_source("app/Payment.php", method="php-symbols")],
                        )
                    ],
                    relationships=[_rel("a", "b")],
                ),
                DomainExtraction(
                    project_id="p",
                    concepts=[
                        _concept(
                            "App\\Payment",
                            concept_type="model",
                            roles=["model", "table"],
                            aliases=[
                                _alias("payment"),
                                _alias("payments", provenance="laravel-plugin"),
                            ],
                            sources=[
                                _source(
                                    "app/Payment.php",
                                    method="laravel-model-path-convention",
                                    confidence=0.6,
                                ),
                                _source("app/Payment.php", method="php-symbols"),
                            ],
                        )
                    ],
                    relationships=[_rel("a", "b")],
                ),
            ]
        )
        assert len(merged.concepts) == 1
        concept = merged.concepts[0]
        assert concept.concept_type == "class"  # first occurrence wins
        assert concept.roles == ["class", "model", "table"]
        assert [a.alias for a in concept.aliases] == ["payment", "payments"]
        assert len(concept.sources) == 2
        assert len(merged.relationships) == 1

    def test_merge_keeps_first_description_and_error_aggregates(self):
        merged = merge_extractions(
            [
                DomainExtraction(
                    project_id="p",
                    concepts=[_concept("App\\X", description=None)],
                    relationships=[],
                    error="first error",
                    degraded=True,
                ),
                DomainExtraction(
                    project_id="p",
                    concepts=[
                        _concept("App\\X", description="explained", metadata={"kind": "class"})
                    ],
                    relationships=[],
                    error="second error",
                    degraded=True,
                ),
            ]
        )
        assert merged.concepts[0].description == "explained"
        assert merged.error == "first error; second error"
        assert merged.degraded is True

    def test_merge_empty_returns_clean_empty(self):
        merged = merge_extractions([])
        assert merged.concepts == []
        assert merged.relationships == []
        assert merged.error is not None


class TestDetectPhp:
    def test_composer_marker_detects(self, tmp_path):
        (tmp_path / "composer.json").write_text('{"require": {}}')
        assert detect_php(tmp_path) is True

    def test_artisan_marker_detects(self, tmp_path):
        (tmp_path / "artisan").write_text("<?php\n")
        assert detect_php(tmp_path) is True

    def test_any_php_file_detects(self, tmp_path):
        (tmp_path / "index.php").write_text("<?php\n")
        assert detect_php(tmp_path) is True

    def test_non_php_tree_and_missing_dir_are_false(self, tmp_path):
        (tmp_path / "main.py").write_text("print('x')\n")
        assert detect_php(tmp_path) is False
        assert detect_php(tmp_path / "missing") is False


class TestExtractForWorkspace:
    def test_invalid_dir_returns_error_extraction(self, tmp_path):
        result = extract_for_workspace("proj-x", tmp_path / "missing")
        assert result.error is not None
        assert result.degraded is True
        assert result.concepts == []

    def test_unsupported_language_returns_clean_unsupported(self, tmp_path):
        (tmp_path / "main.py").write_text("print('x')\n")
        result = extract_for_workspace("proj-x", str(tmp_path))
        assert result.error is not None
        assert "unsupported language" in result.error
        assert result.concepts == []
        assert result.relationships == []

    def test_explicit_non_php_language_is_unsupported(self, tmp_path):
        result = extract_for_workspace(
            "proj-x", str(tmp_path), language="python"
        )
        assert "unsupported language 'python'" in (result.error or "")

    def test_php_extraction_without_framework_plugin(self, tmp_path, monkeypatch):
        php_only = DomainExtraction(
            project_id="proj-x",
            concepts=[_concept("App\\Payment", concept_type="class", roles=["class"])],
            relationships=[],
        )
        monkeypatch.setattr(
            extractor_module, "extract_php_symbol_domain", lambda *a, **k: php_only
        )
        class NoFrameworkPlugin:
            pass

        result = extract_for_workspace(
            "proj-x", str(tmp_path), framework_plugin=NoFrameworkPlugin(), language="php"
        )
        assert [c.identity_key for c in result.concepts] == ["App\\Payment"]
        assert result.error is None
        assert result.project_id == "proj-x"

    def test_framework_plugin_with_method_merges(self, tmp_path, monkeypatch):
        php_only = DomainExtraction(
            project_id="proj-x",
            concepts=[_concept("App\\Payment", concept_type="class", roles=["class"])],
            relationships=[],
        )
        plugin_result = DomainExtraction(
            project_id="whatever-root",
            concepts=[
                _concept(
                    "App\\Payment",
                    concept_type="model",
                    roles=["model"],
                    aliases=[_alias("payments", provenance="laravel-plugin")],
                ),
                _concept("route:get:/orders", concept_type="route", roles=["route"]),
            ],
            relationships=[_rel("App\\Payment", "route:get:/orders", "EXPOSES")],
        )

        class FakeFrameworkPlugin:
            def __init__(self, result):
                self.result = result
                self.called_with = None

            def extract_domain_concepts(self, workspace):
                self.called_with = workspace
                return self.result

        plugin = FakeFrameworkPlugin(plugin_result)
        monkeypatch.setattr(
            extractor_module, "extract_php_symbol_domain", lambda *a, **k: php_only
        )
        result = extract_for_workspace(
            "proj-x", str(tmp_path), framework_plugin=plugin, language="php"
        )
        assert plugin.called_with is not None
        assert plugin.called_with.root == Path(str(tmp_path)).resolve()
        identities = [c.identity_key for c in result.concepts]
        assert identities == ["App\\Payment", "route:get:/orders"]
        payment = next(c for c in result.concepts if c.identity_key == "App\\Payment")
        assert payment.roles == ["class", "model"]
        assert any(a.alias == "payments" for a in payment.aliases)
        assert len(result.relationships) == 1
        assert result.project_id == "proj-x"

    def test_framework_plugin_raising_is_caught_and_php_kept(self, tmp_path, monkeypatch):
        php_only = DomainExtraction(
            project_id="proj-x",
            concepts=[_concept("App\\Payment", concept_type="class", roles=["class"])],
            relationships=[],
        )
        monkeypatch.setattr(
            extractor_module, "extract_php_symbol_domain", lambda *a, **k: php_only
        )

        class BoomPlugin:
            def extract_domain_concepts(self, workspace):
                raise RuntimeError("nope")

        result = extract_for_workspace(
            "proj-x", str(tmp_path), framework_plugin=BoomPlugin(), language="php"
        )
        assert [c.identity_key for c in result.concepts] == ["App\\Payment"]
        assert result.degraded is True
        assert "framework domain extraction failed" in (result.error or "")

    def test_language_auto_detection_uses_primary(self, tmp_path, monkeypatch):
        (tmp_path / "composer.json").write_text('{"require": {}}')
        (tmp_path / "a.php").write_text("<?php\n")
        captured = {}

        def fake_extract_php(*args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            return DomainExtraction(project_id="proj-x", concepts=[], relationships=[])

        monkeypatch.setattr(extractor_module, "extract_php_symbol_domain", fake_extract_php)
        result = extract_for_workspace("proj-x", str(tmp_path))
        assert result.error is None
        assert captured["args"][0] == "proj-x"
        assert Path(captured["args"][1]).is_dir()

    def test_auto_discovers_laravel_plugin_when_none_passed(
        self, tmp_path, monkeypatch
    ):
        """framework_plugin=None still merges route/table concepts for laravel trees.

        The real laravel plugin runs over a tiny composer-marked tree (no
        vendor, runtime_routes=False) while the php symbol pass is canned.
        """
        _write(tmp_path, "composer.json", '{"require": {"laravel/framework": "^11.0"}}\n')
        _write(
            tmp_path,
            "routes/web.php",
            "<?php\n"
            "use Illuminate\\Support\\Facades\\Route;\n"
            "use App\\Http\\Controllers\\OrderController;\n"
            "Route::get('/orders', [OrderController::class, 'index'])->name('orders.index');\n",
        )
        _write(
            tmp_path,
            "app/Http/Controllers/OrderController.php",
            "<?php\n"
            "namespace App\\Http\\Controllers;\n"
            "class OrderController { public function index() {} }\n",
        )
        _write(
            tmp_path,
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
        monkeypatch.setattr(
            extractor_module,
            "extract_php_symbol_domain",
            lambda *a, **k: DomainExtraction(
                project_id="proj-x", concepts=[], relationships=[]
            ),
        )
        result = extract_for_workspace("proj-x", str(tmp_path))
        identities = [c.identity_key for c in result.concepts]
        assert any(i.startswith("route:") for i in identities), identities
        assert any(i.startswith("table:") for i in identities), identities
        assert result.error is None
        assert result.project_id == "proj-x"

    def test_auto_discovery_noop_on_bare_php_tree(self, tmp_path, monkeypatch):
        """No composer/laravel marker -> language pass alone, no plugin merge."""
        _write(tmp_path, "index.php", "<?php\necho 1;\n")
        monkeypatch.setattr(
            extractor_module,
            "extract_php_symbol_domain",
            lambda *a, **k: DomainExtraction(
                project_id="proj-x",
                concepts=[_concept("App\\Payment", concept_type="class", roles=["class"])],
                relationships=[],
            ),
        )
        result = extract_for_workspace("proj-x", str(tmp_path))
        assert [c.identity_key for c in result.concepts] == ["App\\Payment"]
        assert result.error is None
