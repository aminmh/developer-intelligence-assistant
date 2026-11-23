"""Laravel ``characterization_tests``: feature tests that pin recorded HTTP responses.

PHPUnit or Pest, whichever the project requires. Files go under ``tests/Feature`` so the default
``phpunit.xml`` suites and Pest's ``uses(TestCase::class)->in('Feature')`` pick them up. The tests do
not reset the database: they must start from the same seeded data the baseline was recorded with.
"""

from __future__ import annotations

import json

from agent.core.records import TestTemplateRecord
from agent.core.revision import RevisionContext

_HEADER = """ * Characterization tests from a Developer Assistant regression check.
 * They pin the responses recorded from the base version: run them against the base version
 * first (they must pass), then against the changed version. They start from the same seeded
 * data the recording used and do not reset the database."""

PHPUNIT = TestTemplateRecord(
    framework="laravel", runner="phpunit", language="php",
    file="tests/Feature/Characterization/{name}Test.php",
    file_template=f"""<?php

namespace Tests\\Feature\\Characterization;

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
    run_command="php artisan test tests/Feature/Characterization",
)

PEST = TestTemplateRecord(
    framework="laravel", runner="pest", language="php",
    file="tests/Feature/Characterization/{name}Test.php",
    file_template=f"""<?php

/*
{_HEADER}
 */

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
    run_command="./vendor/bin/pest tests/Feature/Characterization",
)


def provide_test_templates(context: RevisionContext) -> list[TestTemplateRecord]:
    try:
        composer = json.loads(context.read_text("composer.json") or "{}")
    except ValueError:
        composer = {}
    requires = {**(composer.get("require") or {}), **(composer.get("require-dev") or {})}
    return [PEST if "pestphp/pest" in requires else PHPUNIT]
