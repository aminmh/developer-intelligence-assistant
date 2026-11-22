"""In-house CWE rules for classes the taint analysis does not cover (plan task 9f).

Written from the CWE definitions (CWE-78 shell commands from non-constant values, CWE-347
unverified JWT payloads), never from one project's findings. A rule that Semgrep rejects
would drop the whole built-in pass, so these run the real pinned Semgrep.
"""

import io
import shutil
import zipfile

import pytest

from code_quality.analyzers import run_semgrep

pytestmark = pytest.mark.skipif(shutil.which("semgrep") is None, reason="Semgrep is not installed here")

PHP = """<?php
class Cleaner
{
    public function remove(Tenant $tenant): void
    {
        exec("rm -rf {$this->uploadDir}/{$tenant->getDomain()}");
        shell_exec('du -sh ' . $this->path);
        exec('rm -rf ' . escapeshellarg($this->path));
        exec('git rev-parse HEAD');
        $this->pdo->exec('DELETE FROM jobs');
        $process = new Process(['rm', '-rf', $this->path]);
        $cmd = sprintf('magick %s %s', escapeshellarg($this->path), escapeshellarg($this->target));
        exec($cmd, $output, $exitCode);
    }

    public function claims(string $jwt): array
    {
        $parts = explode('.', $jwt);
        $header = base64_decode($parts[0]);
        return json_decode(base64_decode($parts[1]), true);
    }

    public function inline(string $jwt): array
    {
        return json_decode(base64_decode(explode('.', $jwt)[1]), true);
    }

    public function host(string $host): string
    {
        return base64_decode(explode('.', $host)[0]);
    }
}
"""

PYTHON = """import jwt

def claims(token):
    return jwt.decode(token, options={"verify_signature": False})

def verified(token, key):
    return jwt.decode(token, key, algorithms=["HS256"])
"""

JAVASCRIPT = """const jwt = require('jsonwebtoken');

function claims(token) {
  return jwt.decode(token);
}

function verified(token, key) {
  return jwt.verify(token, key);
}
"""


def _findings(files: dict[str, str]) -> list[tuple[str, str, int]]:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for path, content in files.items():
            archive.writestr(path, content)
    result = run_semgrep(buffer.getvalue(), list(files), [])
    return sorted((row["rule"], row["file"], row["line"]) for row in result["findings"]
                  if row["rule"].startswith(("builtin-php-shell", "builtin-jwt")))


def test_shell_commands_from_non_constant_values_and_unverified_jwt_payloads():
    assert _findings({"src/Cleaner.php": PHP, "app/tokens.py": PYTHON, "web/tokens.js": JAVASCRIPT}) == [
        ("builtin-jwt-decode-without-verify", "web/tokens.js", 4),
        ("builtin-jwt-payload-decoded-without-verification", "src/Cleaner.php", 18),
        ("builtin-jwt-payload-decoded-without-verification", "src/Cleaner.php", 25),
        ("builtin-jwt-verification-disabled", "app/tokens.py", 4),
        ("builtin-php-shell-command-from-variable", "src/Cleaner.php", 6),
        ("builtin-php-shell-command-from-variable", "src/Cleaner.php", 7),
    ]
