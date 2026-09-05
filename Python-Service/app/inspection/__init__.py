"""PHP-Architect inspection package.

Tier-2/Tier-4 code-intelligence services backed by the native PHP exporter
(bin/project-inspect over nikic/php-parser) and a project symbol graph in
Neo4j (label ``PhpSymbol``, collection ``sym_<stem>`` — separate from the
conceptual-search CodeSymbol/``chunk_code_*`` index by design).

Modules:
    cli      — subprocess bridge to the PHP exporter (JSON contracts)
    graph    — Neo4j symbol-graph writer + typed relation edges
    queries  — read-side symbol queries (resolve/references/implementations/
               call hierarchy)
    service  — orchestration: index a project once, then answer Tier-4 ops
"""

from inspection.service import InspectionService, inspection_service

__all__ = ["InspectionService", "inspection_service"]
