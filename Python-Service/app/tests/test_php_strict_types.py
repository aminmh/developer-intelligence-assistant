"""declare(strict_types=1) coverage from PHP facts (gap-closing plan, task 10)."""

import io
import shutil
import zipfile
from pathlib import Path

import pytest

from code_quality import php_strict_types


def _php_cli_available() -> bool:
    from infra.settings import settings

    return shutil.which("php") is not None and Path(settings.php_cli_root, "bin", "project-inspect").is_file()


pytestmark = pytest.mark.skipif(not _php_cli_available(), reason="PHP CLI tools are not installed here")

STRICT = "<?php\n\ndeclare(strict_types=1);\n\nnamespace App;\n\nclass A {}\n"
LOOSE = "<?php\n\nnamespace App;\n\nclass B {}\n"


def _run(files: dict[str, str]):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for path, content in files.items():
            archive.writestr(path, content)
    return php_strict_types.run_on_archive(buffer.getvalue(), list(files))


def test_ratio_counts_production_php_and_lists_files_without_the_declaration():
    finding, summary = _run({
        "composer.json": "{}", "config/app.php": "<?php\nreturn [];\n", "src/A.php": STRICT, "src/B.php": LOOSE, "src/C.php": LOOSE.replace("B", "C"),
        "tests/BTest.php": LOOSE.replace("B", "BTest"), "vendor/x/Y.php": LOOSE.replace("B", "Y"),
    })
    assert {k: summary[k] for k in ("total", "declared", "ratio", "missing_files")} == {
        "total": 3, "declared": 1, "ratio": 0.3333, "missing_files": ["src/B.php", "src/C.php"]}
    assert finding["file"] == "src/B.php" and finding["line"] == 1 and finding["category"] == "maintainability"
    assert finding["severity"] == "warning" and finding["message"].startswith("declare(strict_types=1) in 1 of 3 production PHP code files (33%)")
    assert len(finding["message"]) <= 240


def test_mostly_strict_is_info_and_fully_strict_has_no_finding():
    files = {f"src/S{i}.php": STRICT.replace("class A", f"class S{i}") for i in range(3)}
    finding, _summary = _run({**files, "src/B.php": LOOSE})
    assert finding["severity"] == "info"
    finding, summary = _run(files)
    assert finding is None and summary["ratio"] == 1.0


def test_no_php_means_no_summary():
    assert _run({"main.py": "print(1)\n"}) == (None, None)
