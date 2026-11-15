"""Pure value types for the domain-concept recognition layer.

These dataclasses are the trusted, typed boundary of the layer: parsers and
downstream consumers exchange ``DomainConcept``/``DomainRelationship`` objects,
never raw dicts, once data has crossed into internal code.

Multi-classification rule: a single source element can play several conceptual
roles at once (a class that is also an event). ``concept_type`` holds the
PRIMARY classification; the ``roles`` list may carry the extra ones. The
concept is stored ONCE — there is no per-role row explosion.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class DomainSource:
    """One concrete occurrence of a concept in a source file."""

    concept_identity: str
    file: str
    source_hash: str
    start_line: int | None = None
    end_line: int | None = None
    method: str = "parser"
    confidence: float = 1.0
    metadata: dict | None = None


@dataclass
class DomainAlias:
    """Alternate spelling under which a concept may be looked up."""

    alias: str
    alias_type: str = "generated"
    confidence: float = 0.5
    provenance: str = "normalizer"


@dataclass
class DomainConcept:
    """Canonical concept row: identity key + primary type + role tags."""

    identity_key: str
    canonical_name: str
    qualified_name: str | None
    concept_type: str
    roles: list[str]
    normalized_name: str
    description: str | None
    aliases: list[DomainAlias]
    sources: list[DomainSource]
    metadata: dict


@dataclass
class DomainRelationship:
    """Directed typed edge between two concept identity keys."""

    relation_type: str
    source_identity: str
    target_identity: str
    confidence: float
    sources: list[DomainSource]
    metadata: dict


@dataclass
class DomainExtraction:
    """Total container for one extraction pass over a project.

    Extraction never raises: failures are reported through ``error`` and the
    ``degraded`` flag while still returning whatever partial result exists.
    """

    project_id: str
    concepts: list[DomainConcept]
    relationships: list[DomainRelationship]
    error: str | None = None
    degraded: bool = False


@dataclass(frozen=True)
class ProjectScope:
    """Carrier for one project's concept namespace.

    ``project_id`` is already sanitized by callers (typically the uploaded
    filename stem); the store treats it as an opaque string key — never a path,
    never parsed or rewritten here.
    """

    project_id: str
