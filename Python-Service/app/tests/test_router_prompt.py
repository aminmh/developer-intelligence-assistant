"""Regression tests for the LLM adjudicator prompt builder."""

import rag.route_registry as reg
import rag.router_prompt as rp


def test_system_prompt_contains_routes_and_rules():
    prompt = rp.ROUTER_SYSTEM_PROMPT()
    for name in reg.ROUTE_NAMES:
        assert name in prompt
    assert "### code_explain" in prompt
    assert "Use when:" in prompt
    assert "Do not use when:" in prompt
    assert "classifier" in prompt
    assert "needs_clarification" in prompt
    assert "NOT sufficient to pick a route" in prompt
    assert "candidate_routes" in prompt
    assert "never rewrite" in prompt.casefold()


def test_boundary_blocks_one_per_route():
    blocks = rp.ROUTE_BOUNDARY_BLOCKS()
    assert len(blocks) == len(reg.ROUTE_NAMES)
    for block in blocks:
        assert block.startswith("### ")
        assert "— " in block
        assert "Use when:" in block
        assert "Do not use when:" in block


def test_few_shot_examples_shape():
    assert len(rp.FEW_SHOT_EXAMPLES) >= 10
    for user_text, context_line, expected_route, explanation in rp.FEW_SHOT_EXAMPLES:
        assert isinstance(user_text, str) and user_text
        assert isinstance(context_line, str)
        assert expected_route in reg.ROUTE_NAMES
        assert isinstance(explanation, str) and explanation


def test_few_shot_covers_key_boundaries():
    routes = {example[2] for example in rp.FEW_SHOT_EXAMPLES}
    assert {"debug", "code_explain", "code_refactor", "search", "knowledge", "chat"} <= routes


def test_precedence_rules_keep_app_route_questions_out_of_chat():
    # App-own HTTP-surface questions (route/endpoint/api for a feature) must be
    # knowledge — derived from the precedence rules and the assembled prompt.
    assert "which route" in rp.PRECEDENCE_RULES or "http surface" in rp.PRECEDENCE_RULES
    system_prompt = rp.ROUTER_SYSTEM_PROMPT()
    assert "which route" in system_prompt or "http surface" in system_prompt
    assert "chat" in rp.PRECEDENCE_RULES.casefold()


def test_few_shot_knowledge_examples_for_app_route_language():
    # The two new app-own-HTTP-surface few-shots must be present and must both
    # classify as knowledge (derived from the data, no brittle index
    # assumptions).
    http_terms = ("route", "endpoint", "api")
    knowledge_route_examples = [
        example
        for example in rp.FEW_SHOT_EXAMPLES
        if example[2] == "knowledge"
        and any(term in example[0].casefold() for term in http_terms)
    ]
    assert len(knowledge_route_examples) >= 2
    inputs = {example[0].casefold() for example in rp.FEW_SHOT_EXAMPLES}
    assert "which route is for appreciations?" in inputs
    assert "what endpoint does the miner upgrade call?" in inputs


def test_schema_describes_all_fields():
    schema = rp.ROUTER_JSON_SCHEMA
    properties = schema["properties"]
    for field in (
        "route",
        "confidence",
        "reason",
        "needs_clarification",
        "candidate_routes",
    ):
        assert field in properties
    assert properties["route"]["enum"] == reg.ROUTE_NAMES
    assert schema["additionalProperties"] is False


def test_build_adjudicator_prompt_embeds_query_and_candidates():
    query = "why is my job failing with a 500 error?"
    prompt = rp.build_adjudicator_prompt(query, ["debug", "code_explain"])

    assert query in prompt
    assert "debug" in prompt
    assert "code_explain" in prompt
    # The raw user text is preserved verbatim, never rewritten.
    assert prompt.count(query) >= 1


def test_build_adjudicator_prompt_optional_context_note():
    prompt = rp.build_adjudicator_prompt("where is save defined", ["search"])
    assert "Context:" not in prompt

    prompt_with_context = rp.build_adjudicator_prompt(
        "what about the other one", ["search"], context_note="previous route was search"
    )
    assert "previous route was search" in prompt_with_context


def test_build_adjudicator_prompt_no_candidates_still_lists_routes():
    prompt = rp.build_adjudicator_prompt("hello", None)
    assert "decide from all routes" in prompt
