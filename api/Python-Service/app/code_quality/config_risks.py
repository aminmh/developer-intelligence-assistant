"""Risky framework configuration: a framework-neutral check (plan task 11).

Reads only ``ConfigRiskRecord`` rows (capability ``config_risks``) from whichever framework
plugins the revision uses (decision: framework-knowledge-lives-in-plugins-over-language-facts).
Each plugin chooses its rules from its framework's own production guidance and reads only
committed configuration, never ``.env`` files. This check reports every record as a finding
in the record's category and severity, and summarizes them per framework and rule.
"""

from __future__ import annotations

from typing import Any

from agent.core.capability import Capability
from agent.core.records import ConfigRiskRecord

NAME = "config-risks"
VERSION = "config-risks-1"
MAX_FINDINGS = 100
_CATEGORIES = {"security", "reliability", "performance", "maintainability", "clean_code", "testing", "process"}
_SEVERITIES = {"error", "warning", "info"}


def run(collections: dict[Capability, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    records = [r for r in collections[Capability.CONFIG_RISKS].records if isinstance(r, ConfigRiskRecord)]
    findings, by_rule = [], {}
    for record in sorted(records, key=lambda r: (r.file, r.line, r.rule)):
        if record.category not in _CATEGORIES or record.severity not in _SEVERITIES:
            continue
        key = f"{record.framework}:{record.rule}"
        by_rule[key] = by_rule.get(key, 0) + 1
        findings.append({"file": record.file, "line": max(1, int(record.line)), "rule": record.rule,
                         "category": record.category, "severity": record.severity, "message": record.message[:600]})
    summary = {"total": len(findings), "by_rule": dict(sorted(by_rule.items())),
               "frameworks": sorted({r.framework for r in records})}
    if len(findings) > MAX_FINDINGS:
        summary["findings_truncated"] = len(findings) - MAX_FINDINGS
    return findings[:MAX_FINDINGS], summary


def check():
    from code_quality.framework_evidence import FrameworkCheck

    return FrameworkCheck(NAME, VERSION, (Capability.CONFIG_RISKS,), run)
