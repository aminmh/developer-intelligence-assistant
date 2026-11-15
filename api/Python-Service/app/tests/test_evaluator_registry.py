from evaluator.registry import EvaluatorAdapterRegistry


class FakeAdapter:
    name = "local"

    def collect(self, context=None):
        return ["raw"]

    def normalize(self, raw):
        return []


def test_registry_is_optional_and_best_effort():
    registry = EvaluatorAdapterRegistry()
    registry.register(FakeAdapter())
    assert registry.names() == ("local",)
    assert registry.collect("missing") == []
    assert registry.collect("local") == []
