from __future__ import annotations

from code_quality.flow_tracing import build_call_traces
from telemetry.snapshot import Snapshot


PROJECT = "pizza"
COMMIT = "a" * 40
REVISION = "rev_" + "b" * 32
ARCHIVE_HASH = "c" * 64
COLLECTION = Snapshot(PROJECT, COMMIT, ARCHIVE_HASH).collection
BUSINESS_FILES = ["src/Controller.php", "src/Service.php"]


def _snapshot(**overrides):
    return {
        "project_id": PROJECT,
        "revision_id": REVISION,
        "commit_sha": COMMIT,
        "source_archive_sha256": ARCHIVE_HASH,
        "collection_name": COLLECTION,
        "state": "ready",
        **overrides,
    }


def _chunk(symbol, file, line, code, calls=(), **overrides):
    return {
        "project_id": PROJECT,
        "revision_id": REVISION,
        "commit_sha": COMMIT,
        "source_archive_sha256": ARCHIVE_HASH,
        "dirty": False,
        "collection": COLLECTION,
        "node_type": "method",
        "fq_symbol_name": symbol,
        "file_path": file,
        "start_line": line,
        "end_line": line + 5,
        "called_symbols": list(calls),
        "unresolved_reference_count": 0,
        "code": code,
        **overrides,
    }


def test_call_trace_is_deterministic_revision_scoped_and_cited():
    chunks = [
        _chunk("App\\Controller::create", "src/Controller.php", 10,
               "$value = $_POST['value']; return $this->save($value);", ["App\\Service::save"]),
        _chunk("App\\Service::save", "src/Service.php", 20,
               "$db->query($value);"),
        _chunk("App\\OldController::create", "src/Controller.php", 40,
               "$_POST['value']; exec($value);", (), revision_id="rev_" + "d" * 32),
        _chunk("App\\Generated::run", "vendor/lib.php", 50,
               "$_POST['value']; exec($value);"),
    ]

    summary, evidence = build_call_traces(PROJECT, REVISION, _snapshot(), chunks, BUSINESS_FILES)
    repeated_summary, repeated_evidence = build_call_traces(PROJECT, REVISION, _snapshot(), chunks, BUSINESS_FILES)

    assert summary["status"] == "complete"
    assert summary["node_count"] == 2
    assert summary["edge_count"] == 1
    assert summary["trace_count"] == 1
    assert evidence[0]["trace"]["nodes"] == [
        {"symbol": "App\\Controller::create", "file": "src/Controller.php", "start_line": 10, "end_line": 15},
        {"symbol": "App\\Service::save", "file": "src/Service.php", "start_line": 20, "end_line": 25},
    ]
    assert evidence[0]["trace"]["edges"][0]["line"] == 10
    assert evidence[0]["evidence_id"] == repeated_evidence[0]["evidence_id"]
    assert summary == repeated_summary
    assert "variable-level data flow is not proven" in summary["reason"]


def test_unknown_edges_and_graph_limits_mark_trace_partial():
    chunks = [
        _chunk("App\\Controller::create", "src/Controller.php", 10,
               "$_POST['value']; $this->dispatch();", ["App\\Service::save", "Vendor\\Dynamic::call"],
               unresolved_reference_count=1),
        _chunk("App\\Service::save", "src/Service.php", 20, "$db->query($value);"),
    ]

    summary, evidence = build_call_traces(PROJECT, REVISION, _snapshot(), chunks, BUSINESS_FILES)

    assert summary["status"] == "partial"
    assert summary["unresolved_edge_count"] >= 2
    assert summary["unresolved_edges"]
    assert all(edge["file"] in BUSINESS_FILES and edge["line"] > 0 for edge in summary["unresolved_edges"])
    assert len(evidence) == 1
    assert "unresolved" in summary["reason"]


def test_wrong_snapshot_and_missing_business_files_are_unavailable():
    summary, evidence = build_call_traces(
        PROJECT, REVISION, _snapshot(state="indexing"), [], BUSINESS_FILES,
    )
    assert summary["status"] == "unavailable"
    assert evidence == []


def test_non_php_revision_reports_flow_tracing_unavailable():
    summary, evidence = build_call_traces(
        PROJECT, REVISION, _snapshot(), [], ["app/main.py", "web/src/App.tsx"],
    )

    assert summary["status"] == "unavailable"
    assert "supports PHP only" in summary["reason"]
    assert evidence == []

    summary, evidence = build_call_traces(PROJECT, REVISION, _snapshot(), [], [])
    assert summary["status"] == "unavailable"
    assert evidence == []


def test_assessment_sends_trace_excerpts_but_persists_only_citations(monkeypatch, tmp_path):
    monkeypatch.setenv("SETTINGS_DB_PATH", str(tmp_path / "settings.db"))
    import code_quality.rules as rules
    import code_quality.service as service
    import code_quality.semantic_context as semantic_context
    import code_quality.flow_tracing as flow_tracing
    import code_quality.php_design as php_design

    from io import BytesIO
    import zipfile

    source = "<?php class Controller {}"
    archive_buffer = BytesIO()
    with zipfile.ZipFile(archive_buffer, "w") as archive:
        archive.writestr("src/Controller.php", source)
    archive_data = archive_buffer.getvalue()
    monkeypatch.setattr(service, "inventory_archive", lambda _archive, _exclusions=None: (
        {"php": 1}, [], ["src/Controller.php"],
        {"detected_files": 1, "supported_files": 1, "scanned_files": 1,
         "skipped_files": 0, "excluded_files": 0, "scanned_bytes": len(source),
         "unsupported_extensions": {}}, [],
    ))
    monkeypatch.setattr(rules, "CustomRuleStore", lambda: type("Rules", (), {"enabled": lambda self, _user: []})())
    monkeypatch.setattr(service, "_run_static_rules", lambda *_args: ([], [], None))
    monkeypatch.setattr(semantic_context, "retrieve_revision_context", lambda *_args: (
        {"status": "unavailable", "revision_id": REVISION, "retriever_version": "revision-context-1",
         "query_count": 0, "evidence_count": 0, "reason": "not indexed"}, []))
    private_snippet = "$_POST['value']; $db->query($value);"
    trace_summary = {"status": "complete", "revision_id": REVISION,
                     "tracer_version": "php-call-reachability-2", "flow_types": ["php.request_input_to_sensitive_sink"],
                     "node_count": 2, "edge_count": 1, "trace_count": 1,
                     "unresolved_edge_count": 0, "limits": {}, "limits_reached": [],
                     "reason": "Call reachability only.",
                     "php_design_signals": {"status": "complete", "analyzer_version": "php-solid-design-signals-1",
                                            "chunk_count": 0, "signal_count": 0,
                                            "principles_with_static_signals": []}}
    trace_evidence = [{"evidence_id": "ev_flow", "kind": "semantic_trace", "category": "security",
                       "rule": "php-call-reachability-2", "file": "src/Controller.php", "line": 10,
                       "message": "PHP request-input to sensitive-operation call path.",
                       "context_snippet": private_snippet,
                       "trace": {"nodes": [{"symbol": "App\\Controller::create", "file": "src/Controller.php",
                                              "start_line": 10, "end_line": 15}], "edges": []}}]
    local_design_summary = {"status": "complete", "analyzer_version": "php-solid-design-signals-1",
                            "source": "local-php-ast-fallback", "chunk_count": 1,
                            "signal_count": 1, "principles_with_static_signals": ["DIP"]}
    local_design_evidence = [{"evidence_id": "ev_design", "kind": "finding", "category": "clean_code",
                              "rule": "php-solid-dip-direct-infrastructure", "file": "src/Controller.php",
                              "line": 10, "message": "Domain code directly constructs an infrastructure type.",
                              "severity": "INFO", "tool": "php-solid-design-signals",
                              "tool_version": "php-solid-design-signals-1", "design_principle": "DIP",
                              "context_snippet": "new \\PDO($dsn);"}]
    local_review_context = [{"evidence_id": "ev_php_context", "kind": "semantic_context",
                             "category": "clean_code", "rule": "php-clean-code-context-1",
                             "file": "src/Controller.php", "line": 1,
                             "message": "Revision-matched PHP excerpt supplied as AI review context; this excerpt alone is not a finding.",
                             "context_snippet": "<?php class Controller { public function save() { return true; } }"}]
    monkeypatch.setattr(php_design, "build_php_assessment_evidence_from_archive",
                        lambda *_args: (local_design_summary, local_design_evidence, local_review_context))
    monkeypatch.setattr(flow_tracing, "retrieve_revision_call_traces", lambda *_args: (trace_summary, trace_evidence))
    model_input = {}

    def score(evidence, *_args):
        model_input["evidence"] = evidence
        categories = {
            category: {"assessed": True, "score": 60, "reason": f"Reviewed {category} source context.",
                       "evidence_ids": [next(row["evidence_id"] for row in evidence
                                             if row.get("category") == category and row.get("kind") != "coverage")]}
            for category in service.CATEGORIES
        }
        return categories, "model", "local", {"total_tokens": 1}, [], []

    monkeypatch.setattr(service, "_score", score)
    report = service.assess_revision(PROJECT, REVISION, _snapshot(), archive_data, "alice",
                                     service.ReportStore(tmp_path / "reports.sqlite"))

    assert any(row.get("context_snippet") == private_snippet for row in model_input["evidence"])
    assert report["coverage"]["semantic_flow_tracing"] == trace_summary
    assert report["coverage"]["php_design_signals"] == local_design_summary
    assert all(report["categories"][category]["assessed"] for category in service.CATEGORIES)
    assert report["overall_score"] == 60
    assert report["shadow_scoring"]["version"] == "deterministic-category-score-1"
    assert report["coverage"]["category_review_context"]["status"] == "available"
    local_context_categories = {row["category"] for row in model_input["evidence"]
                                if row.get("rule") == "local-category-context-1"}
    assert local_context_categories == {"maintainability", "reliability", "performance"}
    assert any(row["rule"] == "php-solid-dip-direct-infrastructure" for row in report["findings"])
    assert any(row.get("context_snippet") == "new \\PDO($dsn);" for row in model_input["evidence"])
    assert any(row.get("evidence_id") == "ev_php_context" for row in model_input["evidence"])
    design_analyzer = next(row for row in report["coverage"]["analyzers"]
                           if row["name"] == "php-solid-design-signals")
    assert design_analyzer["source"] == "local-php-ast-fallback"
    clean_code_analyzer = next(row for row in report["coverage"]["analyzers"]
                               if row["name"] == "ai-semantic-clean-code-review")
    assert clean_code_analyzer["status"] == "partial"
    assert clean_code_analyzer["source_evidence_count"] == 1
    saved_trace = next(row for row in report["evidence"] if row["kind"] == "semantic_trace")
    assert "context_snippet" not in saved_trace
    assert saved_trace["trace"]["nodes"][0]["file"] == "src/Controller.php"
    saved_design = next(row for row in report["evidence"] if row.get("tool") == "php-solid-design-signals")
    assert "context_snippet" not in saved_design
    saved_context = next(row for row in report["evidence"] if row.get("evidence_id") == "ev_php_context")
    assert "context_snippet" not in saved_context


# Laravel code from the pizza project that version 1 reported: Eloquent's ->get() was taken as
# request input and Model::query() / ->select() as raw SQL.
PIZZA_FALSE_POSITIVES = [
    """$cartItems = CartItem::query()->with('cart')->where('pizza_id', $pizza->id)
        ->whereHas('cart', fn ($query) => $query->whereNull('user_id'))->get();""",
    """$size = PizzaSize::find($data['size_id']);
    $options = PizzaOption::query()->with(['prices' => fn ($query) => $query->where('pizza_size_id', $data['size_id'])])
        ->whereIn('id', $optionIds)->get()->keyBy('id');""",
    """return QueryBuilder::for(PizzaOptionPrice::class)->allowedSorts(...$sorts)
        ->where('pizza_option_id', $pizzaOptionId)->with('size')->get();
    $query->orderBy(PizzaSize::query()->select('sort_order')->whereColumn('a', 'b'), $descending ? 'desc' : 'asc');""",
    """$perPage = (int) request()->query('per_page', 20); return $this->query($pizzaOptionId)->paginate($perPage);""",
    """public function system(): string { return $this->exec('status'); }""",
]


def test_eloquent_queries_are_neither_request_input_nor_sinks():
    for code in PIZZA_FALSE_POSITIVES:
        chunks = [_chunk("App\\Controller::create", "src/Controller.php", 10, code)]
        summary, evidence = build_call_traces(PROJECT, REVISION, _snapshot(), chunks, BUSINESS_FILES)
        assert evidence == [], code
        assert summary["trace_count"] == 0


def test_request_input_reaching_raw_sql_or_commands_is_still_traced():
    risky = [
        ("$sort = $request->input('sort');", "$query->orderByRaw($sort);"),
        ("$term = request('q');", "return DB::select(\"select * from pizzas where name like '%$term%'\");"),
        ("$id = $request->query->get('id');", "$this->connection->executeQuery('select ' . $id);"),
        ("$name = $_GET['name'];", "exec('convert ' . $name);"),
        ("$data = $request->getContent();", "return unserialize($data);"),
        ("$value = request()->input('v');", "$pdo->query(\"delete from t where id = $value\");"),
    ]
    for source, sink in risky:
        chunks = [
            _chunk("App\\Controller::create", "src/Controller.php", 10, source + " $this->save($x);",
                   ["App\\Service::save"]),
            _chunk("App\\Service::save", "src/Service.php", 20, sink),
        ]
        _, evidence = build_call_traces(PROJECT, REVISION, _snapshot(), chunks, BUSINESS_FILES)
        assert len(evidence) == 1, (source, sink)
        assert [node["symbol"] for node in evidence[0]["trace"]["nodes"]] == ["App\\Controller::create", "App\\Service::save"]
