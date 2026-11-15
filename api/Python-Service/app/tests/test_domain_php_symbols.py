"""Phase 2 tests — PHP language-level domain pass (hermetic, monkeypatched CLI).

The real ``php bin/project-inspect`` subprocess is never needed: the
``inspection.cli.inspect_symbols`` / ``inspect_references`` wrappers are
monkeypatched with exporter-shaped fixtures.
"""

from __future__ import annotations

from projects.domain import php_symbols as php_symbols_module
from projects.domain.php_symbols import DEFAULT_EXCLUDE, extract_php_symbol_domain
from projects.domain.types import DomainExtraction


def _def(
    kind: str,
    name: str,
    fqn: str,
    *,
    file: str = "src/File.php",
    line: int = 1,
    end_line: int = 10,
    owner: str | None = None,
    relations: list[dict] | None = None,
) -> dict:
    return {
        "kind": kind,
        "name": name,
        "fqn": fqn,
        "file": file,
        "line": line,
        "end_line": end_line,
        "owner": owner,
        "relations": relations,
    }


def _ref(
    kind: str,
    *,
    file: str = "src/File.php",
    line: int = 5,
    scope: str | None = None,
    to_fqn: str | None = None,
    to_member: str | None = None,
    unresolved: bool = False,
) -> dict:
    return {
        "kind": kind,
        "file": file,
        "line": line,
        "scope": scope,
        "to_fqn": to_fqn,
        "to_member": to_member,
        "unresolved": unresolved,
    }


def _extract(
    monkeypatch,
    defs: list[dict],
    refs: list[dict] | None = None,
    *,
    project_dir: str = "/proj",
) -> tuple[DomainExtraction, list]:
    calls: list[list] = []
    seen: dict = {}

    def fake_symbols(project_dir_arg, exclude=None):
        calls.append(("symbols", project_dir_arg, exclude))
        return defs

    def fake_references(project_dir_arg, exclude=None):
        calls.append(("references", project_dir_arg, exclude))
        return refs or []

    monkeypatch.setattr(
        "projects.domain.php_symbols.inspection.cli.inspect_symbols",
        fake_symbols,
    )
    monkeypatch.setattr(
        "projects.domain.php_symbols.inspection.cli.inspect_references",
        fake_references,
    )
    result = extract_php_symbol_domain("proj-x", project_dir)
    seen["calls"] = calls
    return result, calls


def _by_identity(extraction, identity):
    return next(c for c in extraction.concepts if c.identity_key == identity)


class TestPhpSymbolConcepts:
    def test_class_interface_trait_enum_and_method_concepts(self, monkeypatch):
        defs = [
            _def(
                "class", "PaymentService", "App\\Services\\PaymentService",
                file="src/PaymentService.php", line=10, end_line=80,
                relations=[{"kind": "implements", "target": "App\\Contracts\\PaymentGateway"}],
            ),
            _def(
                "interface", "PaymentGateway", "App\\Contracts\\PaymentGateway",
                file="src/Contracts.php", line=1, end_line=3, relations=[],
            ),
            _def(
                "trait", "Loggable", "App\\Support\\Loggable",
                file="src/Support.php", line=1, end_line=5,
            ),
            _def(
                "enum", "OrderStatus", "App\\Enums\\OrderStatus",
                file="src/Enums.php", line=2, end_line=8,
            ),
            _def(
                "method", "charge", "App\\Services\\PaymentService::charge",
                file="src/PaymentService.php", line=20, end_line=22,
                owner="App\\Services\\PaymentService",
            ),
        ]
        result, _ = _extract(monkeypatch, defs)

        service = _by_identity(result, "App\\Services\\PaymentService")
        assert service.concept_type == "class"
        assert service.canonical_name == "PaymentService"
        assert service.qualified_name == "App\\Services\\PaymentService"
        assert service.normalized_name == "payment service"
        assert service.roles == ["class"]
        assert service.metadata == {
            "namespace": "App\\Services",
            "kind": "class",
        }
        assert service.aliases[0].alias == "payment service"
        assert service.aliases[0].provenance == "normalizer"
        assert service.sources[0].file == "src/PaymentService.php"
        assert service.sources[0].start_line == 10
        assert service.sources[0].end_line == 80
        assert service.sources[0].method == "php-symbols"
        assert service.sources[0].confidence == 1.0

        assert _by_identity(result, "App\\Contracts\\PaymentGateway").concept_type == "interface"
        assert _by_identity(result, "App\\Support\\Loggable").concept_type == "trait"
        assert _by_identity(result, "App\\Enums\\OrderStatus").concept_type == "enum"

        method = _by_identity(
            result, "method:App\\Services\\PaymentService::charge"
        )
        assert method.concept_type == "method"
        assert method.canonical_name == "charge"
        assert method.qualified_name == "App\\Services\\PaymentService::charge"
        assert method.roles == ["method"]
        assert method.metadata == {"owner": "App\\Services\\PaymentService"}

    def test_ownerless_and_skipped_def_kinds_are_not_indexed(self, monkeypatch):
        defs = [
            _def("method", "orphan", "::orphan", owner=None),
            _def("class", "Real", "App\\Real", relations=[]),
            _def("property", "$amount", "App\\Real::$amount", owner="App\\Real"),
            _def("class_const", "STATUS", "App\\Real::STATUS", owner="App\\Real"),
            _def("function", "helper", "helper"),
            _def("const", "PI", "PI"),
            {"kind": "unknown_kind", "name": "zzz", "fqn": "App\\ZZZ"},
        ]
        result, _ = _extract(monkeypatch, defs)
        identities = [c.identity_key for c in result.concepts]
        assert "App\\Real" in identities
        assert len(identities) == 1

    def test_relation_kinds_only_when_target_concept_present(self, monkeypatch):
        defs = [
            _def("class", "Base", "App\\Base", relations=[]),
            _def("class", "PaymentGateway", "App\\PaymentGateway", relations=[]),
            _def("class", "Child", "App\\Child", relations=[
                {"kind": "extends", "target": "App\\Base"},
                {"kind": "implements", "target": "App\\PaymentGateway"},
                {"kind": "uses_trait", "target": "App\\MissingTrait"},
                {"kind": "extends", "target": "Ghost\\Base"},
                {"kind": "mystery", "target": "App\\Base"},
            ]),
        ]
        result, _ = _extract(monkeypatch, defs)
        rels = [(r.relation_type, r.source_identity, r.target_identity) for r in result.relationships]
        assert ("EXTENDS", "App\\Child", "App\\Base") in rels
        assert ("IMPLEMENTS", "App\\Child", "App\\PaymentGateway") in rels
        # Missing target and unknown relation kinds never invent edges.
        assert not any(r[2].startswith("Ghost") or r[2] == "App\\MissingTrait" for r in rels)
        assert not any(r[0] == "MYSTERY" for r in rels)

    def test_calls_relations_for_resolved_method_and_static_calls(self, monkeypatch):
        defs = [
            _def("class", "Checkout", "App\\Checkout", relations=[]),
            _def(
                "method", "run", "App\\Checkout::run", file="src/Checkout.php",
                line=2, end_line=9, owner="App\\Checkout",
            ),
            _def(
                "method", "helper", "App\\Checkout::helper", file="src/Checkout.php",
                line=11, end_line=12, owner="App\\Checkout",
            ),
            _def(
                "method", "charge", "App\\Gateway::charge", file="src/Gateway.php",
                line=4, end_line=5, owner="App\\Gateway",
            ),
        ]
        refs = [
            _ref(
                "method_call", file="src/Checkout.php", line=7,
                scope="App\\Checkout::run",
                to_fqn="App\\Gateway", to_member="charge",
            ),
            _ref(
                "static_call", file="src/Checkout.php", line=8,
                scope="App\\Checkout::run",
                to_fqn="App\\Checkout", to_member="helper",
            ),
        ]
        result, _ = _extract(monkeypatch, defs, refs)
        rels = [(r.relation_type, r.source_identity, r.target_identity) for r in result.relationships]
        assert ("CALLS", "method:App\\Checkout::run", "method:App\\Gateway::charge") in rels
        assert ("CALLS", "method:App\\Checkout::run", "method:App\\Checkout::helper") in rels
        call = result.relationships[0]
        assert call.confidence == 1.0
        assert call.sources[0].file == "src/Checkout.php"
        assert call.sources[0].start_line == 7
        assert call.sources[0].method == "php-symbols"

    def test_unresolved_and_missing_target_calls_never_become_relations(self, monkeypatch):
        defs = [
            _def("class", "App", "App\\Checkout", relations=[]),
            _def("method", "run", "App\\Checkout::run", owner="App\\Checkout"),
            _def("method", "charge", "App\\Gateway::charge", owner="App\\Gateway"),
        ]
        refs = [
            # dynamic receiver — never invented
            _ref(
                "method_call", scope="App\\Checkout::run",
                to_fqn=None, to_member="charge", unresolved=True,
            ),
            # receiver typed but target method definition absent
            _ref(
                "method_call", scope="App\\Checkout::run",
                to_fqn="App\\NoSuchClass", to_member="ghost",
            ),
            # source scope is not a method definition
            _ref(
                "method_call", scope="free_function",
                to_fqn="App\\Gateway", to_member="charge",
            ),
            # non-call reference kinds stay out of v1 relations
            _ref(
                "new", scope="App\\Checkout::run",
                to_fqn="App\\Gateway", to_member=None,
            ),
            _ref(
                "class_const", scope="App\\Checkout::run",
                to_fqn="App\\Gateway", to_member="class",
            ),
            _ref(
                "instanceof", scope="App\\Checkout::run",
                to_fqn="App\\Gateway", to_member=None,
            ),
        ]
        result, _ = _extract(monkeypatch, defs, refs)
        assert result.relationships == []

    def test_exclude_forwarded_and_default(self, monkeypatch):
        calls: list = []

        def fake_symbols(project_dir_arg, exclude=None):
            calls.append(("symbols", project_dir_arg, exclude))
            return []

        def fake_references(project_dir_arg, exclude=None):
            calls.append(("references", project_dir_arg, exclude))
            return []

        monkeypatch.setattr(
            "projects.domain.php_symbols.inspection.cli.inspect_symbols",
            fake_symbols,
        )
        monkeypatch.setattr(
            "projects.domain.php_symbols.inspection.cli.inspect_references",
            fake_references,
        )

        extract_php_symbol_domain("p", "/proj")
        assert calls == [
            ("symbols", "/proj", list(DEFAULT_EXCLUDE)),
            ("references", "/proj", list(DEFAULT_EXCLUDE)),
        ]

        calls.clear()
        extract_php_symbol_domain("p", "/proj", exclude=("vendor_tests",))
        assert calls == [
            ("symbols", "/proj", ["vendor_tests"]),
            ("references", "/proj", ["vendor_tests"]),
        ]

    def test_cli_failure_returns_degraded_extraction(self, monkeypatch):
        def boom(*_args, **_kwargs):
            raise php_symbols_module.inspection.cli.InspectError("export blew up")

        monkeypatch.setattr(
            "projects.domain.php_symbols.inspection.cli.inspect_symbols", boom
        )
        result = extract_php_symbol_domain("p", "/proj")
        assert result.concepts == []
        assert result.relationships == []
        assert result.degraded is True
        assert result.error is not None

    def test_deterministic_and_deduped(self, monkeypatch):
        defs = [
            _def("class", "Base", "App\\Base", relations=[]),
            _def("class", "Base", "App\\Base", relations=[]),
            _def("class", "Child", "App\\Child", relations=[
                {"kind": "extends", "target": "App\\Base"},
            ]),
            _def("class", "Child", "App\\Child", relations=[
                {"kind": "extends", "target": "App\\Base"},
            ]),
            _def("method", "run", "App\\Child::run", owner="App\\Child"),
            _def("method", "run", "App\\Child::run", owner="App\\Child"),
            _def("method", "helper", "App\\Child::helper", owner="App\\Child"),
        ]
        refs = [
            _ref(
                "method_call", scope="App\\Child::run",
                to_fqn="App\\Child", to_member="helper",
            ),
            _ref(
                "method_call", scope="App\\Child::run",
                to_fqn="App\\Child", to_member="helper",
            ),
        ]
        first, _ = _extract(monkeypatch, defs, refs)
        second, _ = _extract(monkeypatch, defs, refs)
        assert [c.identity_key for c in first.concepts] == [
            c.identity_key for c in second.concepts
        ]
        assert len(first.concepts) == 4  # Base, Child, Child::run, Child::helper
        assert len(first.relationships) == 2  # EXTENDS + CALLS, deduped


class TestPhpSymbolModule:
    def test_module_exports_default_exclude(self):
        assert DEFAULT_EXCLUDE == ("tests", "coverage", "stubs", "fixtures")
