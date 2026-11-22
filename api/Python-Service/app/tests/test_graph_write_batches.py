"""Large projects' graphs are written in bounded Neo4j transactions."""

from index import llamaindex_layer as layer


class Store:
    def __init__(self):
        self.calls = []

    def structured_query(self, cypher, param_map=None):
        self.calls.append(len(param_map["data"]))
        return [{"count": len(param_map["data"])}]


def test_upserts_are_split_into_batches_and_counts_summed(monkeypatch):
    monkeypatch.setattr(layer, "_GRAPH_WRITE_BATCH_SIZE", 4)
    store = Store()
    assert layer._batched_write(store, "UNWIND $data AS row RETURN count(*) AS count", [{"i": n} for n in range(10)]) == 10
    assert store.calls == [4, 4, 2]
    assert layer._batched_write(store, "x", []) == 0 and store.calls == [4, 4, 2]
