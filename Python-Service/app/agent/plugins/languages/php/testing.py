"""PHP test conventions: PHPUnit test classes and test configuration (gap-closing plan, task 16).

Capability ``test_conventions`` gives one ``TestRecord`` per PHPUnit test class from PHP facts:
its test methods and what they reference (classes created, called or named with ``::class``,
and literal route paths passed to calls). Capability ``config_risks`` reports PHPUnit
configuration that the installed PHPUnit (from ``composer.lock``) does not support, in the
``testing`` category. Tests are never run.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ElementTree
from typing import Any

from agent.core.facts import literal_str
from agent.core.records import ConfigRiskRecord, TestRecord
from agent.core.revision import RevisionContext

LANGUAGE = "php"
CONFIG_FILES = ("phpunit.xml", "phpunit.xml.dist", "phpunit.dist.xml")
_TEST_DIRS = ("tests/", "test/")
_ROUTE = re.compile(r"^/[A-Za-z0-9_{}\-./:]*$")

# Configuration PHPUnit 10 introduced: ignored or rejected by PHPUnit 9 (PHPUnit 10 release notes and schema).
_SINCE_10_ELEMENTS = ("source",)
_SINCE_10_ATTRIBUTES = ("failOnDeprecation", "failOnNotice", "failOnPhpunitDeprecation", "cacheDirectory",
                        "displayDetailsOnTestsThatTriggerDeprecations", "displayDetailsOnTestsThatTriggerErrors",
                        "displayDetailsOnTestsThatTriggerNotices", "displayDetailsOnTestsThatTriggerWarnings",
                        "displayDetailsOnIncompleteTests", "displayDetailsOnSkippedTests", "requireCoverageMetadata")
# Configuration PHPUnit 10 removed: rejected by PHPUnit 10 and later.
_UNTIL_9_ELEMENTS = ("listeners", "filter")
_UNTIL_9_ATTRIBUTES = ("convertDeprecationsToExceptions", "convertErrorsToExceptions", "convertNoticesToExceptions",
                       "convertWarningsToExceptions", "verbose", "printerClass", "beStrictAboutTodoAnnotatedTests",
                       "beStrictAboutResourceUsageDuringSmallTests", "forceCoversAnnotation", "processUncoveredFiles",
                       "cacheResultFile", "noInteraction")


# ── test_conventions ──────────────────────────────────────────────────────────

def _is_test_class(cls: dict[str, Any], path: str) -> bool:
    if cls.get("kind") not in (None, "class") or cls.get("abstract"):
        return False
    in_tests = path.startswith(_TEST_DIRS) or "/tests/" in f"/{path}"
    return in_tests and (str(cls.get("name", "")).endswith("Test") or str(cls.get("extends") or "").endswith("TestCase"))


def _is_test_method(method: dict[str, Any]) -> bool:
    if method.get("visibility", "public") != "public" or method.get("static"):
        return False
    attributes = [str(a.get("name", "")).rsplit("\\", 1)[-1] for a in method.get("attributes") or [] if isinstance(a, dict)]
    annotations = method.get("annotations") or {}
    return (str(method.get("name", "")).startswith("test") or "Test" in attributes
            or (isinstance(annotations, dict) and "test" in annotations))


def _path_like(value: str | None) -> bool:
    return bool(value) and len(value) <= 200 and bool(_ROUTE.match(value)) and value.count("/") >= 1 and len(value) > 1


def _scopes(facts: Any, fqn: str) -> list[str]:
    """The class, its ancestors defined in the project, and the traits any of them use."""
    classes = [fqn, *(a for a in facts.ancestors(fqn) if facts.class_(a) is not None)]
    traits: list[str] = []
    queue = [t for c in classes for t in (facts.class_(c) or {}).get("traits") or []]
    while queue:
        trait = queue.pop(0).lstrip("\\")
        if trait not in traits:
            traits.append(trait)
            queue.extend((facts.class_(trait) or {}).get("traits") or [])
    return [*classes, *traits]


def _references(facts: Any, calls_by_class: dict[str, list[dict[str, Any]]], fqn: str) -> tuple[str, ...]:
    """Classes and route-like paths a test class reaches: in its calls (constants and concatenation
    resolved through facts) and in path-like string constants of the class, its ancestors and traits."""
    found: list[str] = []
    for scope in _scopes(facts, fqn):
        for const in (facts.class_(scope) or {}).get("constants") or []:
            value = literal_str(const.get("value"), facts)
            if _path_like(value):
                found.append(value)
        for call in calls_by_class.get(scope, []):
            if call.get("kind") in ("new", "static") and isinstance(call.get("class"), str):
                found.append(call["class"].lstrip("\\"))
            for part in [call, *(call.get("chain") or [])]:
                for arg in part.get("args") or []:
                    value = arg.get("value") if isinstance(arg, dict) else None
                    if isinstance(value, dict) and isinstance(value.get("class"), str) and len(value) == 1:
                        found.append(value["class"].lstrip("\\"))
                    elif _path_like(text := literal_str(value, facts)):
                        found.append(text)
    return tuple(dict.fromkeys(found))[:300]


def provide_tests(context: RevisionContext, facts: Any) -> list[TestRecord]:
    calls_by_class: dict[str, list[dict[str, Any]]] = {}
    for file in facts.files():
        for call in file.get("calls") or []:
            scope = str(call.get("scope") or "")
            if "::" in scope:
                calls_by_class.setdefault(scope.split("::", 1)[0], []).append(call)
    records = []
    for file in facts.files():
        path = str(file.get("path") or "")
        for cls in file.get("classes") or []:
            if not _is_test_class(cls, path):
                continue
            fqn = cls.get("fqn") or cls.get("name", "")
            methods = tuple(m["name"] for m in cls.get("methods") or [] if _is_test_method(m))
            records.append(TestRecord(framework=LANGUAGE, file=path, test_class=fqn, methods=methods,
                                      references=_references(facts, calls_by_class, fqn)))
    return records


# ── config_risks: PHPUnit configuration against the installed PHPUnit ───────

def installed_version(context: RevisionContext, package: str) -> str | None:
    """A package's locked version from ``composer.lock`` (``9.6.13``), or None."""
    text = context.read_text("composer.lock", 8_000_000)
    try:
        lock = json.loads(text) if text else None
    except ValueError:
        return None
    for row in [*(lock or {}).get("packages", []), *(lock or {}).get("packages-dev", [])]:
        if isinstance(row, dict) and row.get("name") == package:
            return str(row.get("version") or "").lstrip("v") or None
    return None


def _major(version: str | None) -> int | None:
    match = re.match(r"(\d+)", version or "")
    return int(match.group(1)) if match else None


def _line(text: str, needle: str) -> int:
    for index, row in enumerate(text.splitlines(), 1):
        if needle in row:
            return index
    return 1


def provide_test_config_risks(context: RevisionContext) -> list[ConfigRiskRecord]:
    path = next((name for name in CONFIG_FILES if context.exists(name)), None)
    version = installed_version(context, "phpunit/phpunit")
    major = _major(version)
    if path is None or major is None:
        return []
    text = context.read_text(path, 512_000) or ""
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError:
        return [ConfigRiskRecord(framework=LANGUAGE, rule="phpunit-config-unreadable", category="testing",
                                 severity="warning", message=f"{path} is not well-formed XML, so PHPUnit cannot load it.",
                                 file=path, line=1)]
    elements = {element.tag for element in root.iter()}
    attributes = {name for element in root.iter() for name in element.attrib}
    if major < 10:
        newer = [f"<{tag}>" for tag in _SINCE_10_ELEMENTS if tag in elements] + [a for a in _SINCE_10_ATTRIBUTES if a in attributes]
        if root.find("extensions/bootstrap") is not None:
            newer.append("<extensions><bootstrap>")
        unsupported, needs = newer, "PHPUnit 10 or later"
    else:
        unsupported = [f"<{tag}>" for tag in _UNTIL_9_ELEMENTS if tag in elements] + [a for a in _UNTIL_9_ATTRIBUTES if a in attributes]
        needs = "PHPUnit 9 or earlier"
    risks = []
    for item in unsupported[:20]:
        needle = item.split(">")[0].lstrip("<") if item.startswith("<") else item
        risks.append(ConfigRiskRecord(
            framework=LANGUAGE, rule="phpunit-config-version-mismatch", category="testing", severity="error",
            message=(f"{path} uses {item}, which needs {needs}, but composer.lock installs PHPUnit {version}; "
                     "PHPUnit rejects or ignores it, so the suite does not run as configured."),
            file=path, line=_line(text, f"<{needle}" if item.startswith("<") else needle),
            details={"installed": version, "setting": item}))
    schema = next((value for name, value in root.attrib.items() if name.endswith("noNamespaceSchemaLocation")), "")
    pinned = re.search(r"schema\.phpunit\.de/(\d+)\.", schema)
    if pinned and int(pinned.group(1)) != major:
        risks.append(ConfigRiskRecord(
            framework=LANGUAGE, rule="phpunit-schema-version-mismatch", category="testing", severity="warning",
            message=f"{path} validates against the PHPUnit {pinned.group(1)} schema, but composer.lock installs PHPUnit {version}.",
            file=path, line=_line(text, "noNamespaceSchemaLocation"), details={"installed": version}))
    return risks
