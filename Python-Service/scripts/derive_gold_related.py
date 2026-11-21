"""Add one-call-hop alternate locations to a panel gold set (gap-closing plan, task 30).

A reviewer can name the same defect at the other end of a call: the endpoint that calls a
comparison instead of the comparison itself. So that the recall harness can credit that without
being fitted to any run's results, each gold item's alternates are derived mechanically from the
revision's PHP facts, before a run:

* callers of the method that contains the gold line (only when that method name is defined by
  at most two classes, so common names such as ``show`` do not pull in unrelated calls);
* definitions of the methods called on the gold line (±1 line), resolved through ``self``/``static``,
  static class calls and project-unique method names.

At most five of each per item. Items outside PHP source (YAML, templates, configuration) get none.

    python -I scripts/derive_gold_related.py --archive rev.zip --gold app/tests/fixtures/panel_gold/<project>.json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
MAX_PER_KIND = 5


def _methods(facts: Any) -> tuple[dict[str, list[tuple[str, str, dict]]], dict[tuple[str, str], tuple[str, int]]]:
    by_name: dict[str, list[tuple[str, str, dict]]] = defaultdict(list)   # name -> [(class, file, method)]
    by_class: dict[tuple[str, str], tuple[str, int]] = {}                 # (class, name) -> (file, line)
    for file in facts.files():
        for cls in file.get("classes") or []:
            for method in cls.get("methods") or []:
                by_name[method["name"].lower()].append((cls["fqn"], file["path"], method))
                by_class[(cls["fqn"].lower(), method["name"].lower())] = (file["path"], method.get("name_line") or method["line"])
    return by_name, by_class


def related(facts: Any, path: str, line: int) -> list[dict[str, Any]]:
    by_name, by_class = _methods(facts)
    file = facts.file(path)
    if not file:
        return []
    enclosing = next(((cls, m) for cls in file.get("classes") or [] for m in cls.get("methods") or []
                      if m["line"] <= line <= m["end_line"]), None)
    out: list[dict[str, Any]] = []
    if enclosing:
        cls, method = enclosing
        name = method["name"].lower()
        if len({owner for owner, _, _ in by_name[name]}) <= 2:
            callers = []
            for other in facts.files():
                for call in other.get("calls") or []:
                    names = [call.get("name"), *(part.get("name") for part in call.get("chain") or [])]
                    if any(str(n or "").lower() == name for n in names) and not (other["path"] == path and method["line"] <= call["line"] <= method["end_line"]):
                        callers.append({"file": other["path"], "line": call["line"], "via": f"calls {method['name']}()"})
            out += callers[:MAX_PER_KIND]
    callees = []
    for call in file.get("calls") or []:
        if abs(int(call.get("line") or 0) - line) > 1:
            continue
        for name in [call.get("name"), *(part.get("name") for part in call.get("chain") or [])]:
            name = str(name or "").lower()
            if not name:
                continue
            target = None
            owner = str(call.get("class") or "").lower()
            if call.get("kind") == "static" and (owner, name) in by_class:
                target = by_class[(owner, name)]
            elif call.get("receiver") in ("$this", "this", "self", "static") and enclosing and (enclosing[0]["fqn"].lower(), name) in by_class:
                target = by_class[(enclosing[0]["fqn"].lower(), name)]
            elif len(by_name.get(name, [])) == 1:
                _, target_file, method = by_name[name][0]
                target = (target_file, method.get("name_line") or method["line"])
            if target and target != (path, line):
                callees.append({"file": target[0], "line": target[1], "via": f"defines {name}()"})
    out += callees[:MAX_PER_KIND]
    unique, seen = [], set()
    for row in out:
        if (row["file"], row["line"]) not in seen:
            seen.add((row["file"], row["line"]))
            unique.append(row)
    return unique


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--gold", required=True, type=Path)
    args = parser.parse_args()
    from code_quality.framework_evidence import archive_context
    from code_quality.service import inventory_archive

    data = args.archive.read_bytes()
    _, _, files, _, _ = inventory_archive(data)
    gold = json.loads(args.gold.read_text())
    with archive_context(data, files) as context:
        facts = context.facts("php")
        if facts is None:
            raise SystemExit("PHP facts are unavailable; set PHP_CLI_ROOT to the repository's php tools")
        for item in gold["findings"]:
            item.pop("related", None)
            if item["scope"] == "line" and str(item.get("file") or "").endswith(".php"):
                rows = related(facts, item["file"], int(item["line"]))
                if rows:
                    item["related"] = rows
    gold["related_source"] = ("One call hop from the revision's PHP facts (scripts/derive_gold_related.py), derived "
                              "before scoring any run: callers of the enclosing method and definitions called on the line.")
    args.gold.write_text(json.dumps(gold, indent=1, ensure_ascii=False) + "\n")
    for item in gold["findings"]:
        if item.get("related"):
            print(item["id"], [f"{r['file']}:{r['line']} ({r['via']})" for r in item["related"]])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
