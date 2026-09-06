"""LLM adjudicator prompt for the semantic router.

The embedding path of ``rag.router`` is fast and cheap but cannot see intent
beyond surface similarity. When a heavy route is in the top candidates, or the
top match is ambiguous, ``rag.router`` falls back to an LLM classifier. This
module owns that prompt: route boundaries (from ``rag.route_registry``), the
output schema, precedence and keyword-trap rules, few-shot calibration, and the
final ``build_adjudicator_prompt`` used by the HTTP call.
"""

from __future__ import annotations

import json

from rag.route_registry import (
    ROUTE_DESCRIPTIONS,
    ROUTE_ENTRIES,
    ROUTE_NAMES,
)

# JSON schema the classifier must emit. Rendered into the system prompt and
# also used as the single documented output contract for consumers.
ROUTER_JSON_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "route": {
            "type": "string",
            "enum": list(ROUTE_NAMES),
            "description": "The single best-matching route.",
        },
        "confidence": {
            "type": "number",
            "minimum": 0.0,
            "maximum": 1.0,
            "description": "Classifier confidence in the chosen route.",
        },
        "reason": {
            "type": "string",
            "maxLength": 300,
            "description": "Short justification; may note a secondary intent.",
        },
        "needs_clarification": {
            "type": "boolean",
            "description": "True only when the request is genuinely ambiguous.",
        },
        "candidate_routes": {
            "type": "array",
            "items": {"type": "string", "enum": list(ROUTE_NAMES)},
            "maxItems": 3,
            "description": "Plausible alternatives from the embedding pre-filter.",
        },
    },
    "required": [
        "route",
        "confidence",
        "reason",
        "needs_clarification",
        "candidate_routes",
    ],
    "additionalProperties": False,
}


def ROUTE_BOUNDARY_BLOCKS() -> list[str]:
    """Return one markdown boundary block per route for the system prompt."""
    blocks = []
    for entry in ROUTE_ENTRIES:
        name = entry["name"]
        blocks.append(
            f"### {name} — {ROUTE_DESCRIPTIONS[name]}\n"
            f"Use when: {entry['use_when']}\n"
            f"Do not use when: {entry['do_not_use_when']}"
        )
    return blocks


PRECEDENCE_RULES: str = (
    "Route precedence (a later rule wins over an earlier one when both apply):\n"
    '- A concrete error, exception, stack trace, or HTTP 5xx means debug, even '
    'when the wording also sounds like "explain".\n'
    "- Wording that asks where something is defined, called, used, or imported "
    "means search, not code_explain.\n"
    "- Imperative change intent (rewrite, improve, clean, split, extract, "
    "refactor, reduce duplication, rename) means code_refactor; mere "
    "understanding or description means code_explain.\n"
    "- Architecture, configuration, schema, or how-it-works questions that do "
    "not point at one code snippet mean knowledge.\n"
    "- Questions about the app's OWN http surface — which route, endpoint, api, "
    "or controller handles a feature, what routes exist for X, how is X routed "
    "— are knowledge (project structure). Do NOT fall to chat just because the "
    "feature noun is unfamiliar; chat is only for small talk / non-project "
    "conversation.\n"
    "- Pure greeting or small talk with no project intent means chat."
)

KEYWORD_TRAP_WARNING: str = (
    "Words like search, find, help, fix, why, explain, document are NOT "
    "sufficient to pick a route — decide from the full intent. A 'why is this "
    "broken' IS debug; 'search where this is used' IS search."
)


def ROUTER_SYSTEM_PROMPT() -> str:
    """Assemble the full classifier system prompt (no user text yet)."""
    sections: list[str] = [
        (
            "You are a routing classifier, not an answerer. You never explain "
            "code, debug, refactor, search, or chat — you only decide which "
            "single route a user request should be handled by."
        ),
        "",
        "Routes and their boundaries:",
        *ROUTE_BOUNDARY_BLOCKS(),
        "",
        PRECEDENCE_RULES,
        "",
        KEYWORD_TRAP_WARNING,
        "",
        (
            "Ambiguity: if the request is genuinely ambiguous, still pick ONE "
            "best route and set needs_clarification to true. Never invent an "
            "ambiguous route."
        ),
        "",
        (
            "Multi-intent: pick the single dominant route. If the intents are "
            "co-equal, pick the safer read-only one. Never merge routes; "
            "mention the secondary intent in the reason field."
        ),
        "",
        (
            "Never rewrite or paraphrase the user's text — the raw text is "
            "preserved verbatim downstream and handed to the chosen route."
        ),
        "",
        "Confidence must reflect your own certainty about the classification.",
        "",
        (
            "Output strictly one JSON object matching this schema and nothing "
            "else (no markdown fences, no commentary):"
        ),
        json.dumps(ROUTER_JSON_SCHEMA, sort_keys=True),
        "",
        "Only choose a route from this list: " + ", ".join(ROUTE_NAMES) + ".",
    ]
    return "\n".join(sections)


# (user_text, context_line, expected_route, explanation). context_line is empty
# for single-turn prompts; the model has no conversation history today.
FEW_SHOT_EXAMPLES: tuple = (
    (
        "my api endpoint keeps returning a 500",
        "",
        "debug",
        (
            "An HTTP 5xx is a concrete failure — debug wins regardless of any "
            "'why' or 'explain' wording."
        ),
    ),
    (
        "search for the bug in the payment job",
        "",
        "debug",
        (
            "Keyword trap: 'search' only frames the hunt; the real intent is "
            "finding a bug, so debug wins, not search."
        ),
    ),
    (
        "where is save defined",
        "",
        "search",
        (
            "Asks where a symbol is defined — a location question for search, "
            "not code_explain."
        ),
    ),
    (
        "what does this function do",
        "",
        "code_explain",
        (
            "Understanding/description intent with no change request → "
            "code_explain."
        ),
    ),
    (
        "make this function cleaner",
        "",
        "code_refactor",
        (
            "Imperative change intent ('cleaner') → code_refactor even though "
            "it is short and vague."
        ),
    ),
    (
        "which api does the payment flow use",
        "",
        "knowledge",
        (
            "Project-level architecture question without a single snippet "
            "pointed at → knowledge."
        ),
    ),
    (
        "which route is for appreciations?",
        "",
        "knowledge",
        (
            "Asks about the project's own HTTP routes for a feature → "
            "knowledge, not chat."
        ),
    ),
    (
        "what endpoint does the miner upgrade call?",
        "",
        "knowledge",
        (
            "Endpoint question about the app's own API surface → knowledge, "
            "not chat."
        ),
    ),
    (
        "what about the other one",
        "",
        "chat",
        (
            "Follow-up with no prior context provided; no route is defensible, "
            "so return chat and set needs_clarification to true."
        ),
    ),
    (
        "explain this function and then improve it",
        "",
        "code_refactor",
        (
            "Two intents; the dominant action is the change ('improve'), so "
            "code_refactor wins and the reason notes the secondary explain "
            "intent."
        ),
    ),
    (
        "hello",
        "",
        "chat",
        "Pure greeting with no technical intent → chat.",
    ),
    (
        "where is the mail config",
        "",
        "knowledge",
        (
            "Asks where project configuration lives → knowledge, not search "
            "(it is config, not a code symbol)."
        ),
    ),
    (
        "what tables store payments",
        "",
        "knowledge",
        "Schema-level question about the whole data model → knowledge.",
    ),
    (
        "help me document my routes",
        "",
        "knowledge",
        (
            "Adversarial keywords: 'help' and 'document' alone are not enough; "
            "documenting the project's route design is knowledge."
        ),
    ),
    (
        "explain why this exception is thrown",
        "",
        "debug",
        (
            "Explain wording but an explicit exception is present → debug by "
            "precedence."
        ),
    ),
)


def build_adjudicator_prompt(
    query: str,
    candidate_routes: list[str] | None,
    context_note: str = "",
) -> str:
    """Assemble the full adjudicator prompt for one user query.

    The raw ``query`` is embedded verbatim — this router never rewrites user
    text. ``candidate_routes`` are the top embedding matches (≤3) and are shown
    as a starting hint only; the model may still choose any valid route.
    """
    if candidate_routes:
        candidate_line = ", ".join(candidate_routes)
    else:
        candidate_line = "(none — decide from all routes)"

    lines: list[str] = [
        ROUTER_SYSTEM_PROMPT(),
        "",
        "Few-shot calibration examples:",
    ]
    for user_text, context_line, expected_route, explanation in FEW_SHOT_EXAMPLES:
        lines.append(f"User: {user_text}")
        if context_line:
            lines.append(f"Context: {context_line}")
        lines.append(f"Correct route: {expected_route} — {explanation}")

    lines.extend(
        [
            "",
            "Now classify the request below.",
            (
                "Candidate routes from the embedding pre-filter (consider them "
                "first, but the final route may be any valid route): "
                + candidate_line
            ),
            f"User: {query}",
        ]
    )
    if context_note:
        lines.append(f"Context: {context_note}")
    lines.append("JSON:")
    return "\n".join(lines)
