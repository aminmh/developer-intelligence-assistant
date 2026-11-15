"""D10 regression tests for the Laravel config parser.

Shapes observed in real projects (armaiti): quoted keys (the old scanner
swallowed them as string literals → config came back empty), single-line
env() calls with a default (comma inside parens must not truncate the
value), ``::class``-keyed arrays, empty array leaves, nested siblings.
"""

import pytest

from agent.plugins.frameworks.laravel.config import parse_config_file

_MAIL = """return [
    'default' => env('MAIL_MAILER', 'smtp'),
    'from' => [
        'address' => env('MAIL_FROM_ADDRESS', 'hello@example.com'),
        'name' => env('MAIL_FROM_NAME', 'Example'),
    ],
    'stream' => ['ssl' => null],
    'markdown' => ['paths' => ['resources/views/mail']],
];"""

_INSIGHTS = """return [
    'preset' => 'laravel',
    'exclude' => [],
    'add' => [
        Classes::class => [ForbiddenFinalClasses::class,],
    ],
    'config' => [
        ForbiddenPrivateMethods::class => ['title' => 'value'],
    ],
];"""

_APP = """return [
    'organization_value_size' => 15,
    'pagination' => [
        'limit' => 10,
        'perPage' => 5,
    ],
    'debug' => false,
    'enabled' => true,
    'timeout' => 3.5,
    'legacy' => null,
];"""


class TestQuotedKeys:
    def test_quoted_keys_parse(self):
        out = parse_config_file(_MAIL)
        assert out["default"] == "env:MAIL_MAILER"

    def test_nested_assoc_preserved(self):
        out = parse_config_file(_MAIL)
        assert out["from"] == {
            "address": "env:MAIL_FROM_ADDRESS",
            "name": "env:MAIL_FROM_NAME",
        }

    def test_siblings_after_inline_array(self):
        # Old slot handling dropped every sibling after a nested array.
        out = parse_config_file(_MAIL)
        assert set(out) == {"default", "from", "stream", "markdown"}

    def test_scalar_array_marker(self):
        out = parse_config_file(_MAIL)
        assert out["markdown"] == {"paths": "array"}  # list leaf → marker

    def test_no_mangling_from_nested_leaf_lists(self):
        out = parse_config_file(_MAIL)
        assert out["stream"] == {"ssl": None}


class TestClassConstKeys:
    def test_class_key_arrays_marker(self):
        out = parse_config_file(_INSIGHTS)
        assert out["preset"] == "laravel"
        assert out["exclude"] == "array"
        assert out["add"] == {"Classes::class": "array"}

    def test_second_class_key_after_first_list(self):
        out = parse_config_file(_INSIGHTS)
        assert set(out) == {"preset", "exclude", "add", "config"}
        assert out["config"] == {"ForbiddenPrivateMethods::class": "array"}


class TestValueTyping:
    def test_scalar_types(self):
        out = parse_config_file(_APP)
        assert out["organization_value_size"] == 15
        assert out["debug"] is False
        assert out["enabled"] is True
        assert out["timeout"] == 3.5
        assert out["legacy"] is None
        assert out["pagination"] == {"limit": 10, "perPage": 5}

    def test_empty_file(self):
        assert parse_config_file("<?php return [];") == {}

    def test_no_return_no_parse(self):
        assert parse_config_file("<?php $x = 1;") == {}


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
