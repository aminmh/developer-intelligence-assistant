"""PHP language adapter — reference implementation for the LSP framework.

Shows the full LanguageAdapter contract: fragment wrapping, diagnostic mapping
with external-reference downgrade, a non-LSP fallback (`bin/lint-php`), and
PHP-specific fence extraction (including bare fences that start with `<?php`).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from typing import Any, Dict, List, Optional, Tuple

from lsp.base import (
    LanguageAdapter,
    extract_fenced_blocks,
    is_truncated,
    validate_with_lsp,
)
from infra.settings import settings

# Messages that reference symbols outside the snippet (legitimate in
# snippet-local validation) — demoted from error to warning.
_NOT_FOUND_HINTS = (
    "not found",
    "not resolvable",
    "unknown class",
    "cannot be found",
    "does not exist",
    "not defined",
    "unresolvable",
)


_NAMESPACE_RE = re.compile(r"(?m)^\s*namespace\s+([A-Za-z_\\][A-Za-z0-9_\\]*)\s*[;{]")

# Settings for an index-time run (see IndexingAdapter in lsp/base.py). Phpactor
# loads the project's vendor/autoload.php when `composer.enable` is on, which
# runs project code; every external tool integration is off as well.
_INDEX_SETTINGS: Dict[str, Any] = {
    "composer.enable": False,
    "indexer.enabled_watchers": [],
    "language_server_phpstan.enabled": False,
    "language_server_psalm.enabled": False,
    "language_server_php_cs_fixer.enabled": False,
    "php_code_sniffer.enabled": False,
    "prophecy.enabled": False,
    "behat.enabled": False,
    "symfony.enabled": False,
    "language_server.diagnostics_on_open": False,
    "language_server.diagnostics_on_update": False,
    "language_server.diagnostics_on_save": False,
}


class Adapter:
    """PHP validator: Phpactor language server + PHPStan, lint-php fallback.

    Also an IndexingAdapter: phpactor feeds the symbol graph at index time.
    """

    name = "php"
    fence_tags = ["php"]
    extensions = [".php"]

    # ─── Index-time symbol driver ────────────────────────────────────────────

    def index_settings(self) -> Dict[str, Any]:
        return dict(_INDEX_SETTINGS)

    def prepare_argv(self, server_argv: List[str], settings: Dict[str, Any]) -> Optional[List[str]]:
        """Build phpactor's own index first, so `references` finds every use site.

        `server_argv` ends with the `language-server` sub-command; the same
        executable runs `index:build` with the index settings as `--config-extra`.
        """
        base = [part for part in server_argv if part != "language-server"]
        return [*base, "index:build", "--no-interaction", f"--config-extra={json.dumps(settings)}"]

    def namespace_of(self, text: str) -> str:
        match = _NAMESPACE_RE.search(text)
        return match.group(1).strip("\\") if match else ""

    def qualify(self, namespace: str, name: str) -> str:
        return f"{namespace}\\{name}" if namespace else name

    def declaration_kind(self, kind: str, line: str, name: str) -> Optional[str]:
        """phpactor reports traits as classes; the declaration keyword says which."""
        match = re.search(r"\b(trait|interface|enum|class)\s+" + re.escape(name) + r"\b", line)
        return match.group(1) if match else None

    def member_name(self, kind: str, name: str) -> str:
        if kind == "property" and not name.startswith("$"):
            return "$" + name
        return name

    def wrap(self, code: str) -> Tuple[str, int]:
        stripped = code.lstrip()
        head = stripped[:200].lower()

        if "<?php" in head or head.startswith("<?") or "<?=" in head:
            return code, 0

        if re.match(
            r"(class|interface|trait|enum|namespace|use\s|declare\s|abstract|final|readonly\s+class|function|const)\b",
            stripped,
        ):
            return "<?php\n" + code, 1

        if re.match(r"(public|protected|private|static)\b", stripped):
            return "<?php\nclass __Generated {\n" + code + "\n}\n", 2

        return (
            "<?php\nclass __Generated {\n  public function __gen(): mixed {\n"
            + code
            + "\n  }\n}\n",
            3,
        )

    def map_diagnostics(
        self, diagnostics: List[Dict[str, Any]], line_offset: int
    ) -> List[Dict[str, Any]]:
        errors: List[Dict[str, Any]] = []
        for d in diagnostics or []:
            message = (d.get("message") or "").strip()
            severity = int(d.get("severity", 1))
            start = (d.get("range") or {}).get("start") or {}
            line = max(1, int(start.get("line", 0)) + 1 - line_offset)
            column = max(1, int(start.get("character", 0)) + 1)

            low = message.lower()
            if severity == 1 and any(hint in low for hint in _NOT_FOUND_HINTS):
                severity = 2

            errors.append(
                {
                    "message": message,
                    "line": line,
                    "column": column,
                    "severity": severity,
                    "source": d.get("source"),
                    "code": d.get("code"),
                }
            )
        return errors

    def fallback(self, code: str) -> Dict[str, Any]:
        """Syntax gate: `bin/lint-php` (php-parser + `php -l`)."""
        binary = os.path.join(settings.php_cli_root, "bin", "lint-php")
        proc = subprocess.run(
            [os.environ.get("PHP_BIN", "php"), binary],
            input=code,
            capture_output=True,
            text=True,
            timeout=30,
        )
        try:
            parsed = json.loads(proc.stdout.strip() or "{}")
        except json.JSONDecodeError:
            parsed = {}
        if not parsed:
            parsed = {
                "valid": False,
                "errors": [
                    {"message": "PHP validator unavailable", "line": 1, "column": 1}
                ],
                "php_version": "",
            }
        return parsed

    def validate(
        self,
        code: str,
        filename: Optional[str] = None,
        client: Any = None,
    ) -> Dict[str, Any]:
        return validate_with_lsp(self, code, filename, client)

    def is_truncated(self, code: str) -> bool:
        return is_truncated(code)

    def extract_blocks(self, text: str) -> List[Dict[str, Any]]:
        """PHP fence extraction — also catches bare fences that contain `<?php`."""
        blocks = extract_fenced_blocks(text, self.fence_tags)
        for match in _PHP_BARE_FENCE_RE.finditer(text):
            lang = (match.group("lang") or "").strip().lower()
            code = match.group("code")
            if lang != "" or "<?php" not in code:
                continue
            blocks.append(
                {
                    "index": len(blocks),
                    "lang": "php",
                    "code": code,
                    "start": match.start(),
                    "end": match.end(),
                    "full_text": match.group(0),
                }
            )
        blocks.sort(key=lambda b: b["start"])
        for i, b in enumerate(blocks):
            b["index"] = i
        return blocks


_PHP_BARE_FENCE_RE = re.compile(
    r"(?ms)^(?P<fence>`{3,}|~{3,})(?P<lang>[a-zA-Z0-9_+-]*)[ \t]*\n(?P<code>.*?)^(?P=fence)[ \t]*$"
)
