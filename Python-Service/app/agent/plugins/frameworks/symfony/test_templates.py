"""Symfony ``characterization_tests``: WebTestCase tests that pin recorded HTTP responses.

Files go under ``tests/`` (the default ``phpunit.xml.dist`` suite). The tests start from the same
data the baseline was recorded with and do not reset the database.
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
 * They pin the responses recorded from the base version: run them against the base version
 * first (they must pass), then against the changed version. They start from the same data
 * the recording used and do not reset the database.
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
    run_command="php bin/phpunit tests/Characterization",
)


def provide_test_templates(context: RevisionContext) -> list[TestTemplateRecord]:
    return [WEB_TEST_CASE]
