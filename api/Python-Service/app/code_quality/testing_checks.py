"""Testing evidence: test inventory and what the tests reach (gap-closing plan, task 16).

Framework-neutral; reads ``TestRecord`` rows (capability ``test_conventions``) and ``RouteRecord``
rows (``route_analysis``) that plugins provide. Tests are never executed (spec clause 26), and
every message says the evidence is static. Test configuration that the installed test runner
does not support arrives through the configuration-risk check in the same category.

Reports the test inventory (info), test classes without test methods (warning), and HTTP
handlers no test references by class or route path (warning, one per handler class).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from agent.core.capability import Capability
from agent.core.records import RouteRecord, TestRecord
from code_quality.evidence_checks import capped, files_of_kind, finding, plural

NAME = "testing-evidence"
VERSION = "testing-evidence-1"
CATEGORY = "testing"
STATIC = "Static evidence only: tests were not executed."


def _segments(path: str) -> list[str]:
    return [segment for segment in path.split("?", 1)[0].strip("/").split("/") if segment]


def reaches(route_path: str, reference: str) -> bool:
    """Whether a test's path reaches the route: the reference matches the route's last segments
    (route parameters match any value), so helpers that prepend a prefix such as /api still count."""
    route, ref = _segments(route_path), _segments(reference)
    if not ref or len(ref) > len(route) or (len(ref) == 1 and len(route) > 1):
        return False
    tail = route[len(route) - len(ref):]
    return all(part.startswith("{") or part == value for part, value in zip(tail, ref))


def run(collections: dict[Capability, Any], context: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    tests = [r for r in collections[Capability.TEST_CONVENTIONS].records if isinstance(r, TestRecord)]
    routes = [r for r in collections[Capability.ROUTE_ANALYSIS].records if isinstance(r, RouteRecord)]
    rows: list[dict[str, Any]] = []
    methods = sum(len(r.methods) for r in tests)
    pest = sum(1 for r in tests if r.framework == "pest")
    folders = Counter("/".join(r.file.split("/")[:2]) for r in tests)
    if tests:
        anchor = next((p for p in files_of_kind(context, "config") if "phpunit" in p or "test" in p.lower()), tests[0].file)
        rows.append(finding(anchor, 1, "testing-inventory", CATEGORY, "info",
                            f"{plural(len(tests), 'test class' if not pest else 'test class or Pest file')} with {plural(methods, 'test method')} "
                            f"({', '.join(f'{folder} {count}' for folder, count in folders.most_common(5))}). {STATIC}"))
    if tests:
        # What the tests are about, so the reviewer can judge coverage from names instead of only counts.
        listing = [f"{r.file} ({len(r.methods)}: {'; '.join(m[:50] for m in r.methods[:3])}{'…' if len(r.methods) > 3 else ''})"
                   for r in sorted(tests, key=lambda r: -len(r.methods))[:12] if r.methods]
        if listing:
            rows.append(finding(rows[0]["file"], 1, "testing-areas", CATEGORY, "info",
                                f"Largest test files by test count: {'; '.join(listing)}. {STATIC}"[:900]))
    for record in tests:
        if not record.methods:
            rows.append(finding(record.file, 1, "testing-class-without-tests", CATEGORY, "warning",
                                f"{record.test_class.rsplit(chr(92), 1)[-1]} has no test methods, so it runs nothing. {STATIC}"))
    referenced_classes = {ref for record in tests for ref in record.references if not ref.startswith("/")}
    referenced_paths = [ref for record in tests for ref in record.references if ref.startswith("/")]
    handlers: dict[str, list[RouteRecord]] = defaultdict(list)
    for route in routes:
        if route.handler:
            handlers[route.handler.split("::", 1)[0].lstrip("\\")].append(route)
    untested = []
    for handler, handled in sorted(handlers.items()):
        by_class = handler in referenced_classes
        by_path = any(reaches(route.path, path) for route in handled for path in referenced_paths)
        if not (by_class or by_path):
            untested.append(handler)
            first = min(handled, key=lambda r: (r.file, r.line))
            rows.append(finding(first.file, first.line, "testing-handler-without-tests", CATEGORY, "warning",
                                f"No test references {handler.rsplit(chr(92), 1)[-1]} or any of its {plural(len(handled), 'route')} "
                                f"(e.g. {' '.join(first.methods)} {first.path}). {STATIC}"))
    if tests and handlers:
        rows.append(finding(rows[0]["file"], 1, "testing-handler-reach", CATEGORY, "info",
                            f"Tests reference {len(handlers) - len(untested)} of {plural(len(handlers), 'HTTP handler class')} "
                            f"by class or route path. {STATIC}"))
    has_source = any(not p.startswith(("tests/", "test/")) for p in context.files if p.endswith((".php", ".py", ".js", ".ts", ".go", ".java", ".rb")))
    manifests = sorted(files_of_kind(context, "manifest"))
    if not tests and has_source and manifests and collections[Capability.TEST_CONVENTIONS].providers:
        rows.append(finding(manifests[0], 1, "testing-no-tests", CATEGORY, "error",
                            f"No automated test classes were found in this revision. {STATIC}"))
    summary = {"test_classes": len(tests), "test_methods": methods, "handler_classes": len(handlers),
               "handlers_without_tests": len(untested)}
    if not rows:
        providers = collections[Capability.TEST_CONVENTIONS].providers
        summary["reason"] = ("No tests were found." if providers else
                             "No detected language or framework plugin lists tests for this revision, so testing cannot be assessed.")
    return capped(rows, summary)


def check():
    from code_quality.framework_evidence import FrameworkCheck

    return FrameworkCheck(NAME, VERSION, (Capability.TEST_CONVENTIONS, Capability.ROUTE_ANALYSIS), run,
                          accepts_context=True, runs_without_providers=True, category=CATEGORY)
