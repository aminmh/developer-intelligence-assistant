"""PHP language-level domain pass — typed symbols + verified call edges.

This pass turns the existing PHP symbol exporter output into domain concepts
DETERMINISTICALLY. It is the language-level half of the two-pass design: the
exporter already resolves names, so class/interface/trait/enum/method
definitions and their ``extends``/``implements``/``uses_trait`` relations are
verified facts, and only receiver-typed ``method_call``/``static_call`` rows
whose BOTH endpoints resolve to exported method definitions become CALLS
edges. Never-guess rule (spec §5): unresolved/dynamic call sites, missing
targets and unknown reference kinds are dropped silently — a degraded row is
never turned into an invented edge.

v1 limitations (documented, deliberate):
  * ``property``/``class_const``/``function``/``const`` definitions are NOT
    indexed as concepts — only class-likes and methods are.
  * Only ``extends``/``implements``/``uses_trait`` definition relations and
    resolved method-to-method CALLS are emitted; ``new``/``instanceof``/
    ``catch``/``class_const``/``function_call`` are future relation types.

The public entry is total: failures surface through ``DomainExtraction.error``
and the ``degraded`` flag, never as exceptions.
"""

from __future__ import annotations

import hashlib
import os
from typing import Any

import inspection.cli
from projects.domain.normalizer import ConceptNormalizer
from projects.domain.types import (
    DomainAlias,
    DomainConcept,
    DomainExtraction,
    DomainRelationship,
    DomainSource,
)

DEFAULT_EXCLUDE: tuple[str, ...] = ("tests", "coverage", "stubs", "fixtures")

# Definition kinds that become class-like concepts.
_CLASS_LIKE_KINDS = frozenset({"class", "interface", "trait", "enum"})

# Exporter relation kinds -> domain relation types (class-level edges).
_RELATION_TYPES = {
    "extends": "EXTENDS",
    "implements": "IMPLEMENTS",
    "uses_trait": "USES",
}

# Reference kinds that MAY become CALLS edges (both endpoints verified).
_CALL_REFERENCE_KINDS = frozenset({"method_call", "static_call"})

_NORMALIZER = ConceptNormalizer()
_NORMALIZE = _NORMALIZER.normalize

_METHOD_PREFIX = "method:"


def _evidence_hash(*parts: Any) -> str:
    """Deterministic location hash for a source/evidence record.

    Content hashing belongs to the indexing layer; the extractor has no file
    text, so the hash is a stable digest of the evidence location itself.
    """
    raw = "|".join(str(part) for part in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _rel_file(file: Any, project_dir: str) -> str:
    """Exporter file path -> POSIX path relative to ``project_dir``.

    Absolute exporter paths (the CLI passes ``realpath``) are relativized;
    already-relative fixture paths are kept verbatim.
    """
    if not isinstance(file, str) or not file:
        return ""
    path = file.replace("\\", "/")
    if not os.path.isabs(file):
        return path
    try:
        rel = os.path.relpath(file, project_dir)
    except ValueError:
        return path
    return rel.replace(os.sep, "/")


def _space_alias(text: str) -> DomainAlias:
    """Single generated alias: the space-joined display form of ``text``."""
    return DomainAlias(
        alias=" ".join(_NORMALIZER.display_tokens(text)),
        alias_type="generated",
        confidence=0.5,
        provenance="normalizer",
    )


def _method_identity(owner: str, name: str) -> str:
    return f"{_METHOD_PREFIX}{owner}::{name}"


def _error_extraction(project_id: str, message: str) -> DomainExtraction:
    return DomainExtraction(
        project_id=project_id,
        concepts=[],
        relationships=[],
        error=message,
        degraded=True,
    )


def extract_php_symbol_domain(
    project_id: str,
    project_dir: str,
    exclude: tuple[str, ...] | None = None,
) -> DomainExtraction:
    """Map a project's PHP symbol + reference exports into domain concepts.

    ``exclude`` defaults to ``DEFAULT_EXCLUDE`` when omitted (passing an empty
    tuple opts out of every extra exclusion). The exporter is invoked exactly
    twice (symbols then references) with the same exclusion list. Never raises.
    """
    exclusion = DEFAULT_EXCLUDE if exclude is None else tuple(exclude)
    exclude_list = [segment for segment in exclusion if segment]
    try:
        defs = inspection.cli.inspect_symbols(project_dir, exclude=exclude_list)
        refs = inspection.cli.inspect_references(project_dir, exclude=exclude_list)
    except inspection.cli.InspectError as exc:
        return _error_extraction(project_id, f"php symbol export failed: {exc}")
    except Exception as exc:  # noqa: BLE001 — extraction never raises
        return _error_extraction(project_id, f"php symbol export failed: {exc}")

    try:
        return _build_extraction(project_id, project_dir, defs, refs)
    except Exception as exc:  # noqa: BLE001 — extraction never raises
        return _error_extraction(project_id, f"php symbol mapping failed: {exc}")


def _build_extraction(
    project_id: str,
    project_dir: str,
    defs: list[dict[str, Any]],
    refs: list[dict[str, Any]],
) -> DomainExtraction:
    """Pure builder over exporter rows; deterministic, deduped, never raises."""
    concepts: list[DomainConcept] = []
    concepts_by_identity: dict[str, DomainConcept] = {}
    method_identities: set[str] = set()

    for definition in defs if isinstance(defs, list) else []:
        if not isinstance(definition, dict):
            continue
        concept = _definition_concept(definition, project_dir)
        if concept is None:
            continue
        if concept.identity_key in concepts_by_identity:
            continue
        concepts.append(concept)
        concepts_by_identity[concept.identity_key] = concept
        if concept.concept_type == "method":
            method_identities.add(concept.identity_key)

    relationships: dict[tuple[str, str, str], DomainRelationship] = {}

    # Definition-level edges: EXTENDS / IMPLEMENTS / USES between class-likes.
    for definition in defs if isinstance(defs, list) else []:
        if not isinstance(definition, dict):
            continue
        kind = definition.get("kind")
        if kind not in _CLASS_LIKE_KINDS:
            continue
        source = str(definition.get("fqn") or "").strip()
        if source not in concepts_by_identity:
            continue
        for relation in definition.get("relations") or []:
            if not isinstance(relation, dict):
                continue
            relation_type = _RELATION_TYPES.get(relation.get("kind"))
            if relation_type is None:
                continue
            target = str(relation.get("target") or "").strip()
            if not target or target not in concepts_by_identity:
                continue
            key = (relation_type, source, target)
            if key in relationships:
                continue
            relationships[key] = DomainRelationship(
                relation_type=relation_type,
                source_identity=source,
                target_identity=target,
                confidence=1.0,
                sources=[
                    DomainSource(
                        concept_identity=source,
                        file=_rel_file(definition.get("file"), project_dir),
                        source_hash=_evidence_hash(
                            "php-symbols", definition.get("file"),
                            definition.get("line"), definition.get("end_line"),
                        ),
                        start_line=definition.get("line"),
                        end_line=definition.get("end_line"),
                        method="php-symbols",
                        confidence=1.0,
                        metadata={"relation": relation.get("kind")},
                    )
                ],
                metadata={},
            )

    # Reference edges: CALLS between methods whose definitions exist in-pass.
    for reference in refs if isinstance(refs, list) else []:
        if not isinstance(reference, dict):
            continue
        if reference.get("kind") not in _CALL_REFERENCE_KINDS:
            continue
        if reference.get("unresolved"):
            continue
        to_fqn = reference.get("to_fqn")
        to_member = reference.get("to_member")
        if not isinstance(to_fqn, str) or not to_fqn:
            continue
        if not isinstance(to_member, str) or not to_member:
            continue
        target_identity = _method_identity(to_fqn, to_member)
        if target_identity not in method_identities:
            continue
        scope = reference.get("scope")
        if not isinstance(scope, str) or "::" not in scope:
            continue
        owner, name = scope.rsplit("::", 1)
        if not owner or not name:
            continue
        source_identity = _method_identity(owner, name)
        if source_identity not in method_identities:
            continue
        key = ("CALLS", source_identity, target_identity)
        if key in relationships:
            continue
        relationships[key] = DomainRelationship(
            relation_type="CALLS",
            source_identity=source_identity,
            target_identity=target_identity,
            confidence=1.0,
            sources=[
                DomainSource(
                    concept_identity=source_identity,
                    file=_rel_file(reference.get("file"), project_dir),
                    source_hash=_evidence_hash(
                        "php-symbols", reference.get("file"),
                        reference.get("line"),
                    ),
                    start_line=reference.get("line"),
                    end_line=None,
                    method="php-symbols",
                    confidence=1.0,
                    metadata={
                        "reference": reference.get("kind"),
                        "line": reference.get("line"),
                    },
                )
            ],
            metadata={},
        )

    return DomainExtraction(
        project_id=project_id,
        concepts=concepts,
        relationships=list(relationships.values()),
        error=None,
        degraded=False,
    )


def _definition_concept(
    definition: dict[str, Any], project_dir: str
) -> DomainConcept | None:
    """One exporter definition row -> concept (None when not indexed)."""
    kind = definition.get("kind")
    if kind in _CLASS_LIKE_KINDS:
        return _class_like_concept(definition, project_dir)
    if kind == "method":
        return _method_concept(definition, project_dir)
    # property / class_const / function / const and unknown kinds: v1 skip.
    return None


def _class_like_concept(
    definition: dict[str, Any], project_dir: str
) -> DomainConcept | None:
    """Class/interface/trait/enum concept keyed by its FQN."""
    leaf = str(definition.get("name") or "").strip()
    fqn = str(definition.get("fqn") or "").strip() or leaf
    if not fqn:
        return None
    if not leaf:
        leaf = fqn.rsplit("\\", 1)[-1]
    namespace = fqn.rsplit("\\", 1)[0] if "\\" in fqn else None
    kind = str(definition.get("kind") or "class")
    return DomainConcept(
        identity_key=fqn,
        canonical_name=leaf,
        qualified_name=fqn,
        concept_type=kind,
        roles=[kind],
        normalized_name=_NORMALIZE(leaf),
        description=None,
        aliases=[_space_alias(leaf)],
        sources=[
            DomainSource(
                concept_identity=fqn,
                file=_rel_file(definition.get("file"), project_dir),
                source_hash=_evidence_hash(
                    "php-symbols", definition.get("file"),
                    definition.get("line"), definition.get("end_line"),
                ),
                start_line=definition.get("line"),
                end_line=definition.get("end_line"),
                method="php-symbols",
                confidence=1.0,
                metadata=None,
            )
        ],
        metadata={"namespace": namespace, "kind": kind},
    )


def _method_concept(
    definition: dict[str, Any], project_dir: str
) -> DomainConcept | None:
    """Method concept keyed ``method:<owner>::<name>`` (owner-less skipped)."""
    owner = definition.get("owner")
    name = definition.get("name")
    if not isinstance(owner, str) or not owner.strip():
        return None
    if not isinstance(name, str) or not name.strip():
        return None
    owner = owner.strip()
    name = name.strip()
    identity = _method_identity(owner, name)
    return DomainConcept(
        identity_key=identity,
        canonical_name=name,
        qualified_name=f"{owner}::{name}",
        concept_type="method",
        roles=["method"],
        normalized_name=_NORMALIZE(name),
        description=None,
        aliases=[_space_alias(name)],
        sources=[
            DomainSource(
                concept_identity=identity,
                file=_rel_file(definition.get("file"), project_dir),
                source_hash=_evidence_hash(
                    "php-symbols", definition.get("file"),
                    definition.get("line"), definition.get("end_line"),
                ),
                start_line=definition.get("line"),
                end_line=definition.get("end_line"),
                method="php-symbols",
                confidence=1.0,
                metadata=None,
            )
        ],
        metadata={"owner": owner},
    )
