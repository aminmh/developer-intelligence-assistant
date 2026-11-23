"""Laravel ``run_hints``: what a developer needs to start a Laravel revision locally.

Read from configuration files as data; nothing here runs a command
(@.archcore/regression/developer-runs-targets-app-only-sends-http.adr.md).
"""

from __future__ import annotations

from agent.core.records import RunHintRecord
from agent.core.revision import RevisionContext

_ENV_EXAMPLES = (".env.example", ".env.dist")
# Variables a Laravel app cannot boot or serve requests without.
_REQUIRED_ENV = ("APP_KEY", "APP_URL", "DB_CONNECTION", "DB_HOST", "DB_DATABASE", "DB_USERNAME")


def _hint(kind: str, name: str, detail: str, file: str = "", line: int | None = None) -> RunHintRecord:
    return RunHintRecord(framework="laravel", kind=kind, name=name, detail=detail, file=file, line=line)


def provide_run_hints(context: RevisionContext) -> list[RunHintRecord]:
    hints: list[RunHintRecord] = []
    if context.exists("composer.json"):
        hints.append(_hint("command", "composer install", "Install PHP dependencies.", "composer.json"))
    example = next((name for name in _ENV_EXAMPLES if context.exists(name)), None)
    if example:
        hints.append(_hint("command", "cp " + example + " .env", "Create the environment file.", example))
        text = context.read_text(example) or ""
        for number, raw in enumerate(text.splitlines(), start=1):
            key = raw.split("=", 1)[0].strip()
            if key in _REQUIRED_ENV:
                hints.append(_hint("env", key, "Set this variable in .env.", example, number))
    hints.append(_hint("command", "php artisan key:generate", "Generate APP_KEY when it is empty.", example or ""))
    if any(path.startswith("database/migrations/") for path in context.files):
        seed = " --seed" if any(path.startswith("database/seeders/") for path in context.files) else ""
        hints.append(_hint("command", "php artisan migrate" + seed,
                           "Create the schema" + (" and seed data" if seed else "") +
                           ". Use the same seed for base and candidate so responses compare.", "database/migrations"))
    hints.append(_hint("port", "8000", "`php artisan serve` listens on port 8000 unless --port is given.", "artisan"))
    return hints
