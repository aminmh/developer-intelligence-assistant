from evaluator.native import run_explain


def test_explain_runner_reports_missing_capability():
    result = run_explain(object(), "p", "select 1")
    assert result["status"] == "unavailable"


def test_explain_runner_bounds_query():
    result = run_explain(object(), "p", "x" * 20001)
    assert result["status"] == "unavailable"
