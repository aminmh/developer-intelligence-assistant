"""Regression tests for the single-source-of-truth route registry.

Checks that the classifier route enum stays in parity with the executable
orchestrator config (``config/orchestrator.yml``), that heavy/light weights are
as designed, and that every route carries usable boundary text and at least 15
embedding utterances.
"""

from pathlib import Path

import pytest
import yaml

import rag.route_registry as reg

_CONFIG_PATH = (
    Path(__file__).resolve().parents[1] / "config" / "orchestrator.yml"
)


def test_route_names_parity_with_orchestrator_config():
    # Real YAML read — a parse error (or a missing pyyaml) must fail loudly,
    # never skip silently. Config path is relative to this file: tests live
    # under app/tests, orchestrator.yml at app/config.
    config = yaml.safe_load(_CONFIG_PATH.read_text(encoding="utf-8"))
    orchestrator_keys = list(config["routes"].keys())

    # sandbox_analyze, structure_analyze and index_code are launched
    # programmatically by dedicated analysis endpoints (POST /project/analyze,
    # /structure/analyze, /code/index), not through the chat classifier, so
    # they intentionally have no ROUTE_NAMES entry.
    reg.assert_route_enum(
        orchestrator_keys,
        allowed_extra={
            "sandbox_analyze",
            "structure_analyze",
            "index_code",
        },
    )


def test_route_names_ordered_and_unique():
    assert reg.ROUTE_NAMES == [
        "code_explain",
        "code_refactor",
        "debug",
        "search",
        "knowledge",
        "chat",
    ]
    assert len(set(reg.ROUTE_NAMES)) == len(reg.ROUTE_NAMES)


def test_heavy_and_light_sets():
    assert reg.ROUTE_HEAVY == {"code_explain", "code_refactor", "debug"}
    assert reg.ROUTE_LIGHT == {"search", "knowledge", "chat"}
    assert reg.ROUTE_HEAVY | reg.ROUTE_LIGHT == set(reg.ROUTE_NAMES)


def test_assert_route_enum_passes_on_match():
    reg.assert_route_enum(list(reg.ROUTE_NAMES))
    # An orchestrator-only route passes when listed as allowed_extra.
    reg.assert_route_enum(
        list(reg.ROUTE_NAMES) + ["sandbox_analyze"],
        allowed_extra=frozenset({"sandbox_analyze"}),
    )


def test_assert_route_enum_raises_on_mismatch():
    with pytest.raises(AssertionError) as excinfo:
        reg.assert_route_enum(list(reg.ROUTE_NAMES) + ["sandbox_analyze"])
    message = str(excinfo.value)
    assert "sandbox_analyze" in message
    assert "Route enum mismatch" in message

    with pytest.raises(AssertionError):
        reg.assert_route_enum(list(reg.ROUTE_NAMES)[:-1])

    with pytest.raises(AssertionError):
        reg.assert_route_enum(
            list(reg.ROUTE_NAMES) + ["sandbox_analyze"],
            allowed_extra=frozenset({"other_route"}),
        )


def test_every_route_has_boundaries_and_utterances():
    for entry in reg.ROUTE_ENTRIES:
        assert entry["name"] in reg.ROUTE_NAMES
        assert entry["weight"] in {"heavy", "light"}
        assert entry["use_when"].strip()
        assert entry["do_not_use_when"].strip()
        assert entry["precedence_note"].strip()
        assert entry["name"] in reg.ROUTE_DESCRIPTIONS
        assert len(entry["utterances"]) >= 15
        normalized = {utterance.casefold() for utterance in entry["utterances"]}
        assert len(normalized) == len(entry["utterances"]), (
            f"{entry['name']} has duplicate utterances after casefolding"
        )


def test_knowledge_utterances_cover_app_route_language():
    knowledge = next(
        entry for entry in reg.ROUTE_ENTRIES if entry["name"] == "knowledge"
    )
    assert len(knowledge["utterances"]) >= 20
    # Routing questions about the app's own HTTP surface must embed as
    # knowledge, not chat — a literal feature-route ask is the canary.
    assert any(
        "route is for" in utterance or "which route" in utterance
        for utterance in knowledge["utterances"]
    )
    assert "which route is for appreciations" in knowledge["utterances"]
    # The positive boundary must keep app-route questions as knowledge —
    # never hand them to chat.
    assert "NOT chat" in knowledge["use_when"]
    assert "HTTP surface" in knowledge["use_when"]
