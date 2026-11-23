"""Symfony ``characterization_tests``: WebTestCase tests that pin recorded HTTP responses.

Files go under ``tests/`` (the default ``phpunit.xml.dist`` suite). The tests read the database the
baseline was recorded on and never reset it (see the Laravel templates for why a fresh load is not enough).
"""

from __future__ import annotations

from agent.core.records import TestTemplateRecord
from agent.core.revision import RevisionContext

WEB_TEST_CASE = TestTemplateRecord(
    framework="symfony", runner="phpunit", language="php",
    file="tests/Characterization/{name}Test.php",
    file_template="""<?php

namespace App\\Tests\\Characterization;

use Symfony\\Bundle\\FrameworkBundle\\Test\\WebTestCase;

/**
 * Characterization tests from a Developer Assistant regression check.
 * They pin the responses the base version gave on the database the baseline was recorded on.
 * Run them against a copy of that database: first on the base version (they must pass), then on
 * the changed version. They do not reset or load fixtures.
 */
class {name}Test extends WebTestCase
{
{tests}
}
""",
    route_test="""    /** {note} */
    public function test_{test_name}(): void
    {
        $client = static::createClient();
        $body = {body};
        $client->request({method}, {path}, [], [], ['CONTENT_TYPE' => 'application/json', 'HTTP_ACCEPT' => 'application/json'],
            $body === null ? null : json_encode($body));
        $response = $client->getResponse();

        self::assertSame({status}, $response->getStatusCode());
        $data = json_decode((string) $response->getContent(), true);
{assertions}
    }
""",
    json_assertion="        self::assertSame({value}, $data{bracket_path});",
    text_assertion="        self::assertStringContainsString({text}, (string) $response->getContent());",
    json_value="json_decode({json}, true)", no_body="null", uses_dot_path=False,
    run_command="DATABASE_URL=<recorded database> php bin/phpunit tests/Characterization",
    setup_note=("These tests read the database the baseline was recorded on and never reset it. Point the test "
                "environment's DATABASE_URL at a copy of it (.env.test.local or the command line). Write probes change "
                "that copy, so restore it before each run."),
)


def provide_test_templates(context: RevisionContext) -> list[TestTemplateRecord]:
    return [WEB_TEST_CASE]
