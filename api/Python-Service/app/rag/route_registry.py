"""Single source of truth for semantic-router route definitions.

Route entries carry embedding example utterances plus positive/negative
boundary criteria used by the LLM adjudicator prompt. The embedding set only
matters for the fast embedding path; the boundary text only matters for the
slow LLM path — keeping both in one place prevents the two classifiers from
silently disagreeing about what each route means.

Every route name must exist as a key in ``config/orchestrator.yml`` under the
top-level ``routes:`` block so a classified route can always be executed by the
job orchestrator. ``assert_route_enum`` is the parity guard for that contract.
"""

from __future__ import annotations

from collections.abc import Iterable

# Route entries are deliberately plain dicts (no dataclass) so the module stays
# a dependency-free data file. Each entry:
#   name          -- must match an orchestrator.yml top-level route key
#   weight        -- "heavy" routes trigger LLM adjudication, "light" do not
#   use_when      -- single-sentence positive boundary (shown to the LLM)
#   do_not_use_when -- single-sentence negative boundary (shown to the LLM)
#   utterances    -- natural developer phrasings for embedding examples
#   precedence_note -- one line about when this route wins or loses
ROUTE_ENTRIES: list[dict] = [
    {
        "name": "code_explain",
        "weight": "heavy",
        "use_when": (
            "The user wants to understand, summarize, or walk through what "
            "existing code does; no change is requested and no failure is "
            "reported."
        ),
        "do_not_use_when": (
            "The user asks to change code (code_refactor), to locate a symbol "
            "(search), to diagnose a failure (debug), or about project-wide "
            "architecture/config (knowledge)."
        ),
        "utterances": [
            "explain this code",
            "summarize what this function does",
            "describe the logic of this file",
            "what does this method do?",
            "explain this php class",
            "walk me through this function",
            "break down this code step by step",
            "tell me what this module does",
            "what does this method return",
            "explain the flow of this request handler",
            "what is happening in this controller",
            "why does this query builder build this sql",
            "walk me through what happens when this endpoint is hit",
            "what is the purpose of this class",
            "describe what this middleware does",
            "summarize the behavior of this route handler",
            "what does this endpoint do",
            "explain how this route handler behaves",
            "walk me through this route's logic",
            "what does this controller method do",
        ],
        "precedence_note": (
            "Loses to debug on explicit errors, to search on where-is-it "
            "wording, and to code_refactor on imperative change verbs."
        ),
    },
    {
        "name": "code_refactor",
        "weight": "heavy",
        "use_when": (
            "The user wants to change code structure or quality: rewrite, "
            "clean up, split, extract, rename, or reduce duplication, with no "
            "concrete failure reported."
        ),
        "do_not_use_when": (
            "Only understanding/description is wanted (code_explain) or the "
            "request is about a runtime error, exception, or crash (debug)."
        ),
        "utterances": [
            "refactor this code",
            "improve the structure of this class",
            "rewrite this function to be cleaner",
            "optimize this code and reduce duplication",
            "suggest a safer refactor for this file",
            "make this code more maintainable",
            "split this method into smaller parts",
            "improve naming and structure here",
            "clean up this legacy code",
            "rewrite this to be cleaner",
            "split this long method",
            "extract this into a helper",
            "reduce duplication in this class",
            "pull this repeated logic into one place",
            "rename these variables so they make sense",
            "restructure this file so it reads top-down",
            "make this function smaller",
            "convert this callback nesting into async await",
            "separate this god class into focused pieces",
        ],
        "precedence_note": (
            "Dominates code_explain when a change verb is present; loses to "
            "debug when a concrete error accompanies the request."
        ),
    },
    {
        "name": "debug",
        "weight": "heavy",
        "use_when": (
            "The user reports an error, exception, stack trace, crash, runtime "
            "failure, HTTP 5xx, or failing behavior and wants it diagnosed or "
            "fixed."
        ),
        "do_not_use_when": (
            "No failure is reported and the user only wants to understand code "
            "(code_explain) or change its structure (code_refactor)."
        ),
        "utterances": [
            "i have an exception",
            "this code crashes at runtime",
            "find the bug in this function",
            "debug this error",
            "why is this failing?",
            "fix this runtime issue",
            "why am i getting this stack trace?",
            "track down this bug",
            "this throws an error",
            "this throws an exception",
            "why is this crashing",
            "this returns a 500",
            "null pointer here",
            "fix this runtime error",
            "this stack trace",
            "where does this error come from",
            "what went wrong in this log",
        ],
        "precedence_note": (
            "Highest precedence among heavy routes: a concrete error wins even "
            "under explain/refactor wording."
        ),
    },
    {
        "name": "search",
        "weight": "light",
        "use_when": (
            "The user asks where a symbol, class, method, file, or import is "
            "defined, called, used, referenced, or reachable in the codebase."
        ),
        "do_not_use_when": (
            "The user wants the meaning or behavior explained (code_explain), "
            "code changed (code_refactor), or a failure fixed (debug)."
        ),
        "utterances": [
            "find where this function is defined",
            "search for callers of this method",
            "locate imports of this class",
            "show me files related to this symbol",
            "search the codebase for this term",
            "where is this variable used?",
            "find references to this method",
            "show all usages of this class",
            "locate this symbol in the repo",
            "where is this called",
            "who uses this class",
            "find references to",
            "locate the definition of",
            "which files import this",
            "show me every place that touches this function",
            "is this method reachable from anywhere",
        ],
        "precedence_note": (
            "Where/who/which-files wording wins over code_explain; loses to "
            "debug on explicit errors."
        ),
    },
    {
        "name": "knowledge",
        "weight": "light",
        "use_when": (
            "The user asks about project architecture, configuration, schema, "
            "providers, wiring, or how parts of the system fit together — "
            "including the app's own HTTP surface (which route, endpoint, api, "
            "or controller serves a feature, what routes exist for a feature, "
            "how a flow is routed) — without pointing at one code snippet. "
            "Routing questions about the app's own HTTP surface are "
            "knowledge — NOT chat; chat only applies to pure small talk with "
            "no project intent."
        ),
        "do_not_use_when": (
            "The question targets one specific function or snippet "
            "(code_explain), or asks where a code symbol is used (search), or "
            "is pure small talk or non-project general conversation."
        ),
        "utterances": [
            "explain the project architecture",
            "how is this system configured?",
            "where are the environment settings?",
            "show me the deployment details",
            "what is the schema for this module?",
            "explain the repository structure",
            "how does this service work?",
            "where are the config files?",
            "what is the architecture of this app?",
            "show me the system design",
            "how is auth wired",
            "what providers are registered",
            "explain the routing design",
            "where is the mail service configured",
            "what does the schema look like",
            "how are jobs dispatched",
            "which api does the payment flow use",
            "describe the deployment topology",
            "how do services discover each other",
            "what database does this project use",
            "which route is for appreciations",
            "what route serves this feature",
            "which api endpoint is used for this feature",
            "what routes exist for this feature",
            "which controller responds to this feature",
            "is there a route for this feature",
            "which url maps to this feature",
            "which api does the checkout flow use",
            "what endpoint does the checkout flow call",
            "how is the checkout flow routed in this app",
            "list the routes for the checkout flow",
            "where is checkout exposed as an api",
            "what middleware protects the checkout route",
            "which version of the api serves checkout",
        ],
        "precedence_note": (
            "Whole-project architecture/config questions beat chat; loses to "
            "code_explain when a single snippet is the subject."
        ),
    },
    {
        "name": "chat",
        "weight": "light",
        "use_when": (
            "The message is a greeting, thanks, small talk, or chit-chat with "
            "no code or project intent."
        ),
        "do_not_use_when": (
            "The message contains any real technical request — a greeting word "
            "inside an otherwise technical request must not select chat."
        ),
        "utterances": [
            "hello",
            "thanks",
            "goodbye",
            "how are you?",
            "general conversation",
            "hi",
            "hey",
            "thank you",
            "bye",
            "how are you",
            "what can you do",
            "good morning",
            "good afternoon",
            "see you later",
            "cheers",
            "what's up",
            "no problem",
        ],
        "precedence_note": (
            "Lowest priority; only selected when no technical route clearly "
            "applies."
        ),
    },
]

# Ordered as listed above — this order also drives RouteLayer ranking order.
ROUTE_NAMES: list[str] = [entry["name"] for entry in ROUTE_ENTRIES]

ROUTE_HEAVY: set[str] = {
    entry["name"] for entry in ROUTE_ENTRIES if entry["weight"] == "heavy"
}
ROUTE_LIGHT: set[str] = {
    entry["name"] for entry in ROUTE_ENTRIES if entry["weight"] == "light"
}

# One-line route descriptions (display + LLM boundary headers).
ROUTE_DESCRIPTIONS: dict[str, str] = {
    "code_explain": "Explain or summarize what code does",
    "code_refactor": "Refactor, clean up, or improve code structure",
    "debug": "Debug errors, crashes, or bugs",
    "search": "Find/search symbols, references, or usages",
    "knowledge": "Explain project architecture or configuration",
    "chat": "General conversation or small talk",
}

def assert_route_enum(
    route_keys: Iterable[str],
    *,
    allowed_extra: frozenset[str] = frozenset(),
) -> None:
    """Fail loudly when the classifier route enum drifts from the orchestrator.

    ``route_keys`` is the full set of top-level ``routes:`` keys from
    ``config/orchestrator.yml``. ``allowed_extra`` names orchestrator-only
    routes that are launched by dedicated endpoints (never through the chat
    classifier) and are therefore exempt from the enum — e.g.
    dedicated analysis routes. Raises an AssertionError listing
    missing and extra names so a route added to one side but not the other is
    caught immediately.
    """
    known = set(route_keys) - set(allowed_extra)
    if set(ROUTE_NAMES) == known:
        return
    missing = sorted(set(ROUTE_NAMES) - known)
    extra = sorted(known - set(ROUTE_NAMES))
    problems = []
    if missing:
        problems.append(f"missing from orchestrator config: {missing}")
    if extra:
        problems.append(f"not registered as classifier routes: {extra}")
    raise AssertionError(
        "Route enum mismatch with orchestrator config; " + "; ".join(problems)
    )
