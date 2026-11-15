#!/usr/bin/env python3
"""Write the Runtime telemetry settings into a PHP project's .env.

Usage: write-php-otel-env.py ENV_PATH SERVICE_NAME PROJECT_ID REVISION_ID OTLP_ENDPOINT [--hooks-file PATH] [--check]

Existing values for these keys are replaced in place; other lines are kept. --hooks-file sets
INDEXER_OTEL_HOOKS_FILE only when the .env does not name one yet. Unrelated
OTEL_RESOURCE_ATTRIBUTES entries are preserved. Duplicate keys stop the run so the user can
resolve which one applies. With --check, only validate (no write).
"""
from __future__ import annotations

from pathlib import Path
import re
import sys

RESOURCE_KEY = "OTEL_RESOURCE_ATTRIBUTES"
HOOKS_KEY = "INDEXER_OTEL_HOOKS_FILE"


def settings(service_name: str, project_id: str, revision_id: str, endpoint: str) -> tuple[dict, dict]:
    values = {
        "INDEXER_OTEL_ENABLED": "true",
        "INDEXER_PROJECT_ID": project_id,
        "INDEXER_REVISION_ID": revision_id,
        "OTEL_PHP_AUTOLOAD_ENABLED": "true",
        "OTEL_SERVICE_NAME": service_name,
        "OTEL_TRACES_EXPORTER": "otlp",
        "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
        "OTEL_EXPORTER_OTLP_ENDPOINT": endpoint,
        "OTEL_TRACES_SAMPLER": "parentbased_traceidratio",
        "OTEL_TRACES_SAMPLER_ARG": "1.0",
        "OTEL_METRICS_EXPORTER": "none",
        "OTEL_LOGS_EXPORTER": "none",
    }
    resources = {
        "service.name": service_name,
        "indexer.project_id": project_id,
        "indexer.revision_id": revision_id,
        "indexer.code_dirty": "false",
        "indexer.schema.version": "indexer-telemetry-v2",
    }
    return values, resources


def key_of(line: str) -> str | None:
    match = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
    return match.group(1) if match else None


def main(argv: list[str]) -> int:
    check_only = "--check" in argv
    args = [arg for arg in argv[1:] if arg != "--check"]
    hooks_file = None
    if "--hooks-file" in args:
        index = args.index("--hooks-file")
        if index + 1 >= len(args) or not args[index + 1].strip():
            print("--hooks-file needs a path.", file=sys.stderr)
            return 2
        hooks_file = args[index + 1].strip()
        del args[index:index + 2]
    if len(args) != 5:
        print(__doc__, file=sys.stderr)
        return 2
    path = Path(args[0])
    values, resources = settings(*args[1:])
    if any("," in value or "=" in value for value in resources.values()):
        print("service.name and IDs cannot contain commas or equals signs.", file=sys.stderr)
        return 2
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    counts: dict[str, int] = {}
    for line in lines:
        key = key_of(line)
        if key in values or key == RESOURCE_KEY:
            counts[key] = counts.get(key, 0) + 1
    duplicates = sorted(key for key, count in counts.items() if count > 1)
    if duplicates:
        print("Duplicate .env keys must be resolved first: " + ", ".join(duplicates), file=sys.stderr)
        return 2
    if check_only:
        return 0
    if hooks_file and not any(key_of(line) == HOOKS_KEY for line in lines):
        values[HOOKS_KEY] = hooks_file

    attributes = []
    for line in lines:
        match = re.match(rf"^\s*{RESOURCE_KEY}\s*=\s*(.*?)\s*$", line)
        if match:
            for item in match.group(1).strip().strip("\"'").split(","):
                key, separator, value = item.partition("=")
                if separator and key not in resources:
                    attributes.append(f"{key}={value}")
    attributes.extend(f"{key}={value}" for key, value in resources.items())
    values[RESOURCE_KEY] = ",".join(attributes)

    updated, written = [], set()
    for line in lines:
        key = key_of(line)
        if key in values:
            if key not in written:
                updated.append(f"{key}={values[key]}")
                written.add(key)
        else:
            updated.append(line)
    updated.extend(f"{key}={value}" for key, value in values.items() if key not in written)
    path.write_text("\n".join(updated).rstrip() + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
