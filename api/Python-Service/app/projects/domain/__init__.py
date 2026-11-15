"""Domain-concept recognition layer.

Value types (``projects.domain.types``), the deterministic identifier
normalizer (``projects.domain.normalizer``) and the SQLite concept store
(``projects.domain.store``). Public classes re-exported here for callers that
want one import site; the module-level ``concept_store`` singleton is lazily
constructed and opens no DB until first use.
"""

from projects.domain.normalizer import ConceptNormalizer
from projects.domain.store import ConceptStore, concept_store
from projects.domain.types import (
    DomainAlias,
    DomainConcept,
    DomainExtraction,
    DomainRelationship,
    DomainSource,
    ProjectScope,
)

__all__ = [
    "ConceptNormalizer",
    "ConceptStore",
    "DomainAlias",
    "DomainConcept",
    "DomainExtraction",
    "DomainRelationship",
    "DomainSource",
    "ProjectScope",
    "concept_store",
]
