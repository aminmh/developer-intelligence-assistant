"""Anonymous classes and their members stay out of the snapshot inspection graph (no stable identity)."""

from inspection.service import InspectionService


def _node(record):
    service = InspectionService.__new__(InspectionService)
    return service._def_node(record, "sym_c")


def test_members_of_anonymous_classes_have_no_graph_node():
    # Two test files each declare `new class { private bool $hasFired; public function preFlush() {} }`.
    for record in ({"kind": "class", "fqn": "", "name": "", "file": "tests/A.php", "line": 66},
                   {"kind": "property", "fqn": "::$hasFired", "name": "$hasFired", "file": "tests/A.php", "line": 67},
                   {"kind": "class_const", "fqn": "::LIMIT", "name": "LIMIT", "file": "tests/B.php", "line": 99},
                   {"kind": "method", "fqn": "::preFlush", "name": "preFlush", "file": "tests/A.php", "line": 75}):
        assert _node(record) is None, record


def test_members_of_named_classes_keep_their_node():
    node = _node({"kind": "property", "fqn": "App\\Entity\\Client::$email", "name": "$email",
                  "file": "src/Entity/Client.php", "line": 12})
    assert node is not None and node["id"]


class _Graph:
    def __init__(self):
        self.nodes, self.declarations, self.references = [], [], []

    def upsert_nodes(self, rows):
        self.nodes += rows
        return len(rows)

    def upsert_declaration_relations(self, rows):
        self.declarations += rows
        return len(rows)

    def upsert_references(self, rows):
        self.references += rows
        return len(rows)

    def __getattr__(self, name):  # remaining writes (runtime facts, cleanup) are no-ops here
        return lambda *args, **kwargs: 0


def test_snapshot_indexing_skips_edges_of_anonymous_classes(tmp_path):
    from telemetry.snapshot import Snapshot

    base = {"kind": "class", "fqn": "App\\Repository\\ClientRepository", "name": "ClientRepository",
            "file": "src/Repository/ClientRepository.php", "line": 10}
    # `new class extends ClientRepository { private bool $hasFired; }` in a test: no node, so no edge.
    anonymous = {"kind": "class", "fqn": "", "name": "", "file": "tests/ClientTest.php", "line": 40,
                 "relations": [{"kind": "extends", "target": "App\\Repository\\ClientRepository"}]}
    member = {"kind": "property", "fqn": "::$hasFired", "name": "$hasFired", "file": "tests/ClientTest.php", "line": 41}
    graph = _Graph()
    service = InspectionService.__new__(InspectionService)
    service._graph = graph
    result = service.index(str(tmp_path), "proj", exported={"definitions": [base, anonymous, member], "references": []},
                           snapshot=Snapshot("pizza", "a" * 40, "c" * 64))
    assert len(graph.nodes) == 1 and graph.declarations == [] and result["nodes"] == 1
