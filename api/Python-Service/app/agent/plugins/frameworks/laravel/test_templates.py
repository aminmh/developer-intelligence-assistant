"""Laravel ``characterization_tests``: tests that pin recorded HTTP responses.

PHPUnit or Pest, whichever the project requires. The tests read the same database the baseline
was recorded on, so they must not reset it: files go under ``tests/Characterization``, outside the
``uses(..., RefreshDatabase::class)->in('Feature')`` that Laravel projects commonly declare in
``tests/Pest.php``, and use the project's plain ``Tests\\TestCase``. Seeders that generate ULIDs,
UUIDs or timestamps produce different data on every run, which is why a fresh seed is not enough.
Found in a live run on pizza-builder (2026-11-23).
"""

from __future__ import annotations

import json

from agent.core.records import TestTemplateRecord
from agent.core.revision import RevisionContext

_HEADER = """ * Characterization tests from a Developer Assistant regression check.
 * They pin the responses the base version gave on the database the baseline was recorded on.
 * Run them against a copy of that database: first on the base version (they must pass), then on
 * the changed version. They do not reset or seed the database."""

_SETUP = ("These tests read the database the baseline was recorded on and never reset it. Point the test run at a copy "
          "of it, for example `DB_CONNECTION=sqlite DB_DATABASE=/path/to/recorded.sqlite`, or the MySQL database the "
          "app used, with the variables the project's phpunit.xml sets without `force=\"true\"`. Write probes change that "
          "copy, so restore it before each run.")

PHPUNIT = TestTemplateRecord(
    framework="laravel", runner="phpunit", language="php",
    file="tests/Characterization/{name}Test.php",
    file_template=f"""<?php

namespace Tests\\Characterization;

use Tests\\TestCase;

/**
{_HEADER}
 */
class {{name}}Test extends TestCase
{{
{{tests}}
}}
""",
    route_test="""    /** {note} */
    public function test_{test_name}(): void
    {
        $response = $this->json({method}, {path}, {body});

        $response->assertStatus({status});
{assertions}
    }
""",
    json_assertion="        $response->assertJsonPath({dot_path}, {value});",
    text_assertion="        $response->assertSee({text}, false);",
    json_value="json_decode({json}, true)", no_body="[]",
    run_command="DB_DATABASE=<recorded database> php artisan test tests/Characterization",
    setup_note=_SETUP,
)

PEST = TestTemplateRecord(
    framework="laravel", runner="pest", language="php",
    file="tests/Characterization/{name}Test.php",
    file_template=f"""<?php

/*
{_HEADER}
 */

uses(Tests\\TestCase::class);

{{tests}}
""",
    route_test="""// {note}
test({title}, function () {
    $response = $this->json({method}, {path}, {body});

    $response->assertStatus({status});
{assertions}
});
""",
    json_assertion="    $response->assertJsonPath({dot_path}, {value});",
    text_assertion="    $response->assertSee({text}, false);",
    json_value="json_decode({json}, true)", no_body="[]",
    run_command="DB_DATABASE=<recorded database> ./vendor/bin/pest tests/Characterization",
    setup_note=_SETUP,
)


def provide_test_templates(context: RevisionContext) -> list[TestTemplateRecord]:
    try:
        composer = json.loads(context.read_text("composer.json") or "{}")
    except ValueError:
        composer = {}
    requires = {**(composer.get("require") or {}), **(composer.get("require-dev") or {})}
    return [PEST if "pestphp/pest" in requires else PHPUNIT]
