"""Typed subprocess adapter for the pure-PHP CLI tools."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from infra.settings import settings


@dataclass(frozen=True)
class PhpCommandResult:
    """Captured result from one PHP CLI invocation."""

    command: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    @property
    def diagnostics(self) -> str:
        return self.stderr.strip() or self.stdout.strip()


class PhpCliError(RuntimeError):
    """A PHP CLI process could not produce the requested contract."""

    def __init__(self, message: str, result: PhpCommandResult | None = None) -> None:
        super().__init__(message)
        self.result = result


class PhpCli:
    """Run and decode the PHP command-line tools shipped with the service."""

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root or settings.php_cli_root or "/app/php")

    def binary(self, name: str) -> str:
        return str(self.root / "bin" / name)

    def run(self, name: str, args: Sequence[str], *, timeout: float) -> PhpCommandResult:
        command = (self.binary(name), *args)
        try:
            process = subprocess.run(
                list(command), capture_output=True, text=True, timeout=timeout
            )
        except subprocess.TimeoutExpired as exc:
            raise PhpCliError(f"{name} timed out after {timeout:g}s") from exc
        except OSError as exc:
            raise PhpCliError(f"{name} could not start: {exc}") from exc
        return PhpCommandResult(
            command=command,
            returncode=process.returncode,
            stdout=process.stdout or "",
            stderr=process.stderr or "",
        )

    def code_parse(self, project_path: str) -> list[dict[str, Any]]:
        result = self.run(
            "code-parse", ["--path", project_path, "--output", "json"], timeout=600
        )
        if result.returncode not in (0, 2):
            raise PhpCliError(
                result.diagnostics or f"code-parse exited {result.returncode}", result
            )
        parsed = _decode_json_array(result.stdout)
        if parsed is None:
            raise PhpCliError("code-parse produced unparseable output", result)
        return parsed

    def export_structure(self, project_path: str, depth: int = 4) -> str:
        result = self.run(
            "project-export-structure",
            [project_path, f"--depth={max(1, depth)}"],
            timeout=120,
        )
        if result.returncode != 0:
            raise PhpCliError(
                result.diagnostics or f"project-export-structure exited {result.returncode}",
                result,
            )
        return result.stdout.strip()

    def inspect_index(
        self, project_path: str, exclude: Sequence[str] | None = None
    ) -> dict[str, list[dict[str, Any]]]:
        """Export project definitions and use-sites for the symbol graph."""
        args = [project_path]
        if exclude:
            args.append(",".join(item for item in exclude if item))
        result = self.run("project-inspect", ["index", *args], timeout=900)
        if result.returncode != 0:
            raise PhpCliError(
                result.diagnostics or f"project-inspect index exited {result.returncode}",
                result,
            )
        payload = _decode_json_object(result.stdout)
        if not isinstance(payload, dict):
            raise PhpCliError("project-inspect index produced unparseable output", result)
        definitions = payload.get("definitions")
        references = payload.get("references")
        from inspection.graph import stamp_origin

        return stamp_origin({
            "definitions": definitions if isinstance(definitions, list) else [],
            "references": references if isinstance(references, list) else [],
        }, "php-parser")

    def inspect_facts(
        self, project_path: str, exclude: Sequence[str] | None = None
    ) -> dict[str, Any]:
        """Export language facts for framework plugins (schema ``php-facts-1``)."""
        args = [project_path]
        if exclude:
            args.append(",".join(item for item in exclude if item))
        result = self.run("project-inspect", ["facts", *args], timeout=900)
        if result.returncode != 0:
            raise PhpCliError(
                result.diagnostics or f"project-inspect facts exited {result.returncode}",
                result,
            )
        payload = _decode_json_object(result.stdout)
        if not isinstance(payload, dict) or payload.get("schema") != "php-facts-1" \
                or not isinstance(payload.get("files"), list):
            raise PhpCliError("project-inspect facts produced unparseable output", result)
        return payload


def _decode_json_array(stdout: str) -> list[dict[str, Any]] | None:
    """Decode the CLI's JSON array, tolerating a leading diagnostic line."""
    text = stdout.strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
        match = re.search(r"\[\s*\n?\s*\{", text)
        if match:
            depth = 0
            start = match.start()
            for index in range(start, len(text)):
                if text[index] == "[":
                    depth += 1
                elif text[index] == "]":
                    depth -= 1
                    if depth == 0:
                        try:
                            parsed = json.loads(text[start : index + 1])
                        except json.JSONDecodeError:
                            parsed = None
                        break
    if not isinstance(parsed, list):
        return None
    return parsed


def _decode_json_object(stdout: str) -> dict[str, Any] | None:
    """Decode an object payload, tolerating a leading diagnostic line."""
    text = stdout.strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            parsed = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    return parsed if isinstance(parsed, dict) else None
