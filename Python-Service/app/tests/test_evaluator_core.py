from __future__ import annotations

from evaluator import (
    EvaluatorReport,
    SourceLinker,
    SourceTrace,
    analyze_explain,
    parse_mysql_slow_log,
    parse_postgres_slow_log,
)


def test_report_round_trip_and_stable_fingerprint():
    report = EvaluatorReport(
        query_fingerprint="abc",
        metrics={"rows_examined": 12},
        source_trace=SourceTrace(file="src/db.py", line=4),
        profiler="fixture",
    )
    assert EvaluatorReport.from_dict(report.to_dict()).to_dict() == report.to_dict()
    assert report.to_dict()["source_trace"]["file"] == "src/db.py"


def test_mysql_slow_log_parser_normalizes_metrics_and_slow_issue():
    text = """# Time: 2026-09-12T10:00:00.000000Z
# User@Host: app[app] @ localhost []
# Query_time: 2.500  Lock_time: 0.001 Rows_sent: 2  Rows_examined: 20000
SET timestamp=1789207200;
SELECT * FROM orders WHERE customer_id = 7;
"""
    reports = parse_mysql_slow_log(text)
    assert len(reports) == 1
    report = reports[0]
    assert report.metrics["execution_time_ms"] == 2500.0
    assert report.metrics["rows_examined"] == 20000
    assert "excessive_execution_time" in report.issues


def test_postgres_parser_supports_duration_statement_format():
    reports = parse_postgres_slow_log(
        "2026-09-12 10:00:00 UTC [1] LOG:  duration: 1200.5 ms  statement: SELECT * FROM users;"
    )
    assert len(reports) == 1
    assert reports[0].metrics["execution_time_ms"] == 1200.5
    assert reports[0].query == "SELECT * FROM users;"


def test_explain_json_detects_plan_issues():
    report = analyze_explain(
        {
            "Plan": {
                "Node Type": "Seq Scan",
                "Relation Name": "orders",
                "Actual Rows": 20000,
                "Actual Total Time": 1800.0,
            }
        },
        "SELECT * FROM orders",
    )
    assert "full_table_scan" in report.issues
    assert "high_rows_examined" in report.issues
    assert "excessive_execution_time" in report.issues


def test_source_linker_is_framework_neutral_and_bounded(tmp_path):
    source = tmp_path / "service.py"
    source.write_text(
        "namespace = 'ignored'\n"
        "class OrderService:\n"
        "    def find(self):\n"
        "        return db.query('orders')\n"
        "\n",
        encoding="utf-8",
    )
    trace = SourceLinker(tmp_path, context_lines=1).link(file="service.py", line=4)
    assert trace.available is True
    assert trace.class_name == "OrderService"
    assert trace.method == "find"
    assert trace.language == "python"
    assert len(trace.context) <= 3
    escaped = SourceLinker(tmp_path).link(file="../service.py", line=1)
    assert escaped.available is False


def test_source_linker_reports_missing_source_honestly(tmp_path):
    trace = SourceLinker(tmp_path).link(source_hint={"file": "missing.py", "line": 3})
    assert trace.available is False
    assert trace.error

