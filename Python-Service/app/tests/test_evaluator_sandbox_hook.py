from evaluator.sandbox_hook import EphemeralSqlTagger


class FakeExecutor:
    def __init__(self):
        self.injected = []
        self.removed = []

    def inject_evaluator_hook(self, project_id, source):
        self.injected.append((project_id, source))
        return {"status": "done"}

    def remove_evaluator_hook(self, project_id):
        self.removed.append(project_id)


def test_hook_injection_and_cleanup_are_explicit():
    executor = FakeExecutor()
    hook = EphemeralSqlTagger()
    assert hook.inject(executor, "project-1").status == "injected"
    assert executor.injected and ".evaluator" not in executor.injected[0][1]
    assert hook.cleanup(executor).status == "cleaned"
    assert executor.removed == ["project-1"]


def test_missing_executor_capability_is_honest():
    result = EphemeralSqlTagger().inject(object(), "project-1")
    assert result.status == "unavailable"
    assert result.reason
