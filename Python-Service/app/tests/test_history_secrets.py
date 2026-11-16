"""Secrets committed to Git history (gap-closing plan, task 12)."""

import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from code_quality import history_secrets
from code_quality.history_secrets import HISTORY_REF, pack_history, unpack_history

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="Git is not installed here")

# Built at run time so secret scanners never flag this file.
FAKE_TOKEN = "ghp_" + "1A2b3C4d5E6f7G8h9I0j" + "K1lM2nO3pQ4rS5tU6v7W"
GIT_ENV = {"PATH": os.environ.get("PATH", ""), "HOME": "/tmp", "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_AUTHOR_NAME": "Dev", "GIT_AUTHOR_EMAIL": "dev@example.test",
           "GIT_COMMITTER_NAME": "Dev", "GIT_COMMITTER_EMAIL": "dev@example.test"}


def _git(cwd, *args) -> str:
    return subprocess.run(["git", *args], cwd=cwd, env=GIT_ENV, check=True, capture_output=True).stdout.decode().strip()


def _repository(root: Path, commits: int = 4) -> tuple[Path, str]:
    """A repository whose second commit adds a token and whose third removes it."""
    repo = root / "origin"
    repo.mkdir()
    _git(repo, "init", "--quiet", "--initial-branch=main")
    for number in range(commits):
        if number == 1:
            (repo / "config.php").write_text(f"<?php\n$token = '{FAKE_TOKEN}';\n")
        elif number == 2:
            (repo / "config.php").unlink()
        (repo / "a.php").write_text(f"<?php\n$x = {number};\n")
        _git(repo, "add", "--all")
        _git(repo, "commit", "--quiet", "-m", f"commit {number}")
    return repo, _git(repo, "rev-parse", "HEAD")


def _artifact(root: Path, origin: Path, commit: str, depth: int) -> bytes:
    """What the Git sync stores: a bundle of ``depth`` commits and its shallow boundary."""
    work = root / "work"
    work.mkdir()
    _git(work, "init", "--quiet")
    _git(work, "fetch", "--quiet", f"--depth={depth}", f"file://{origin}", commit)
    _git(work, "update-ref", HISTORY_REF, commit)
    bundle = subprocess.run(["git", "bundle", "create", "--quiet", "-", HISTORY_REF], cwd=work, env=GIT_ENV,
                            check=True, capture_output=True).stdout
    shallow = work / ".git" / "shallow"
    return pack_history(bundle, shallow.read_text().split() if shallow.is_file() else [])


FAKE_SCANNER = r'''#!/usr/bin/env python3
import json, os, subprocess, sys
args = sys.argv[1:]
repo = args[1]
log = subprocess.run(["git", "-C", repo, "log", "--format=%H %cs", "-p", "-U0", "--all"], capture_output=True, text=True, check=True).stdout
rows = []
commit = date = None
for line in log.splitlines():
    if len(line.split(" ")[0]) == 40 and " " in line and not line.startswith(("+", "-")):
        commit, date = line.split(" ")[0], line.split(" ")[1]
    if line.startswith("+") and "ghp_" in line:
        secret = line.split("'")[1]
        rows.append({"RuleID": "github-pat", "Description": "GitHub Personal Access Token", "File": "config.php",
                     "StartLine": 2, "Commit": commit, "Date": date + "T10:00:00Z", "Secret": secret,
                     "Match": line, "Author": "Dev", "Email": "dev@example.test", "Message": "commit",
                     "Fingerprint": commit + ":config.php:github-pat:2", "Attributes": {"secret": secret}})
rows.append({"RuleID": "github-pat", "File": "../escape.php", "StartLine": 1, "Commit": commit})
open(args[args.index("--report-path") + 1], "w").write(json.dumps(rows))
'''


@pytest.fixture
def fake_scanner(tmp_path, monkeypatch):
    path = tmp_path / "bin" / "betterleaks"
    path.parent.mkdir()
    path.write_text(FAKE_SCANNER.replace("#!/usr/bin/env python3", f"#!{sys.executable}"))
    path.chmod(0o755)
    monkeypatch.setenv("BETTERLEAKS_BIN", str(path))
    return path


def test_artifact_round_trip_and_header_checks(tmp_path):
    origin, head = _repository(tmp_path)
    data = _artifact(tmp_path, origin, head, depth=2)
    bundle, shallow = unpack_history(data, head)
    assert bundle.startswith(b"# v2 git bundle\n") and len(shallow) == 1
    other = "b" * 40
    with pytest.raises(ValueError, match="exactly the revision"):
        unpack_history(data, other)
    with pytest.raises(ValueError, match="unexpected entries"):
        import io, zipfile
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as archive:
            archive.writestr("history.bundle", bundle)
            archive.writestr("shallow", "")
            archive.writestr("config", "[core]\n")
        unpack_history(out.getvalue(), head)
    with pytest.raises(ValueError, match="prerequisites|exactly the revision"):
        unpack_history(pack_history(bundle.replace(b"\n" + head.encode(), b"\n-" + other.encode() + b" x\n" + head.encode(), 1), []), head)
    with pytest.raises(ValueError, match="shallow boundary"):
        unpack_history(pack_history(bundle, ["../../etc/passwd"]), head)


def test_history_key_sits_next_to_the_archive():
    archive_key = f"telemetry-sources/{'a' * 64}/{'b' * 64}.zip"
    key = history_secrets.history_key(archive_key, "c" * 40)
    assert key == f"telemetry-sources/{'a' * 64}/history-{'c' * 40}.zip" and history_secrets.is_history_key(key)
    for bad in ("uploads/x.zip", f"telemetry-sources/{'a' * 64}/../x.zip"):
        with pytest.raises(ValueError):
            history_secrets.history_key(bad, "c" * 40)


def test_scan_keeps_rule_file_line_commit_and_date_only(tmp_path, fake_scanner, monkeypatch):
    monkeypatch.setenv("BETTERLEAKS_CONFIG_TOML", "[extend]\nuseDefault = false\n")
    origin, head = _repository(tmp_path)
    result = history_secrets.run_history_scan(_artifact(tmp_path, origin, head, depth=3), head)
    assert result["status"] == "complete", result
    assert result["commits_scanned"] == 3 and result["shallow"] is True
    [finding] = result["findings"]
    added = _git(origin, "rev-list", "--skip=2", "-1", "HEAD")
    assert finding == {"rule": "github-pat", "description": "GitHub Personal Access Token", "file": "config.php",
                       "line": 2, "commit": added, "date": finding["date"], "commits": 1}
    assert len(finding["date"]) == 10
    serialized = json.dumps(result)
    assert FAKE_TOKEN not in serialized and "dev@example.test" not in serialized and "Dev" not in serialized
    rows = history_secrets.findings(result)
    assert rows[0]["rule"] == "history-secret-github-pat" and rows[0]["severity"] == "error"
    assert added[:12] in rows[0]["message"] and len(rows[0]["message"]) <= 240 and FAKE_TOKEN not in rows[0]["message"]


def test_scanner_gets_a_bare_repository_redaction_and_no_project_config(tmp_path, fake_scanner, monkeypatch):
    monkeypatch.setenv("BETTERLEAKS_CONFIG", "/etc/hostile.toml")
    seen_file = {}
    real_run = subprocess.run

    def spy(command, *args, **kwargs):
        if command and command[0] == str(fake_scanner):
            seen_file.update(args=command[1:], env=kwargs["env"], cwd_files=os.listdir(kwargs["cwd"]))
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(history_secrets.subprocess, "run", spy)
    origin, head = _repository(tmp_path)
    assert history_secrets.run_history_scan(_artifact(tmp_path, origin, head, depth=10), head)["status"] == "complete"
    args, env = seen_file["args"], seen_file["env"]
    assert args[0] == "git" and args[1].endswith("repo.git")
    assert "--redact=100" in args and "--ignore-gitleaks-allow" in args
    assert not any("valid" in arg or arg == "--config" for arg in args)
    assert not any(name.startswith(("BETTERLEAKS", "GITLEAKS")) for name in env)
    assert env["GIT_CONFIG_NOSYSTEM"] == "1" and seen_file["cwd_files"] == []


def test_complete_history_is_not_shallow(tmp_path, fake_scanner):
    origin, head = _repository(tmp_path)
    result = history_secrets.run_history_scan(_artifact(tmp_path, origin, head, depth=50), head)
    assert result["status"] == "complete" and result["commits_scanned"] == 4 and result["shallow"] is False


def test_failures_are_results_not_exceptions(tmp_path, fake_scanner):
    origin, head = _repository(tmp_path)
    assert history_secrets.run_history_scan(b"not a zip", head)["status"] == "failed"
    fake_scanner.write_text(f"#!{sys.executable}\nimport sys; print('leaked ' + 'ghp_x'); sys.exit(3)\n")
    result = history_secrets.run_history_scan(_artifact(tmp_path, origin, head, depth=2), head)
    assert result["status"] == "failed" and result["reason"] == "Betterleaks exited with status 3."


def test_unavailable_without_the_scanner(monkeypatch):
    monkeypatch.setenv("BETTERLEAKS_BIN", "/nonexistent/betterleaks")
    assert history_secrets.run_history_scan(b"", "a" * 40)["status"] == "unavailable"


def test_worker_results_are_renormalized():
    value = {"status": "complete", "version": "1.9.0", "commits_scanned": 7, "shallow": True,
             "findings": [{"rule": "aws-access-token", "description": "AWS", "file": "a.env", "line": 3,
                           "commit": "d" * 40, "date": "2026-01-02", "secret": FAKE_TOKEN},
                          {"rule": "x", "file": "/etc/passwd", "line": 1, "commit": "d" * 40}]}
    result = history_secrets.validate_result(value)
    assert result["findings"] == [{"rule": "aws-access-token", "description": "AWS", "file": "a.env", "line": 3,
                                   "commit": "d" * 40, "date": "2026-01-02", "commits": 1}]
    assert result["commits_scanned"] == 7 and FAKE_TOKEN not in json.dumps(result)
    with pytest.raises(ValueError):
        history_secrets.validate_result({"status": "weird"})


def test_load_history_reads_the_artifact_beside_the_archive():
    archive_key = f"telemetry-sources/{'a' * 64}/{'b' * 64}.zip"

    class Registry:
        def archive_key(self, _project, _commit):
            return archive_key

    class Storage:
        objects = {history_secrets.history_key(archive_key, "c" * 40): b"history"}

        def get_snapshot_archive(self, key):
            return self.objects[key]

    snapshot = {"commit_sha": "c" * 40}
    assert history_secrets.load_history("p", snapshot, Registry(), Storage()) == b"history"
    assert history_secrets.load_history("p", {"commit_sha": "e" * 40}, Registry(), Storage()) is None
    assert history_secrets.load_history("p", {"commit_sha": None}, Registry(), Storage()) is None
    assert history_secrets.load_history("p", {"commit_sha": "c" * 40, "dirty": True}, Registry(), Storage()) is None


def test_git_sync_fetches_and_stores_history(tmp_path, monkeypatch):
    import telemetry.git_source as git_source

    origin, head = _repository(tmp_path, commits=6)
    work = tmp_path / "sync"
    work.mkdir()
    _git(work, "init", "--quiet")
    _git(work, "remote", "add", "origin", f"file://{origin}")
    _git(work, "fetch", "--quiet", "--depth=1", "origin", "refs/heads/main")
    monkeypatch.setattr(git_source, "_git", lambda args, cwd, env, **kw: subprocess.run(
        ["git", *args], cwd=cwd, env={**env, **GIT_ENV}, check=True, capture_output=True).stdout)
    monkeypatch.setattr(history_secrets, "HISTORY_DEPTH", 4)
    artifact = git_source._fetch_history(work, dict(GIT_ENV), head)
    bundle, shallow = unpack_history(artifact, head)
    assert len(shallow) == 1 and bundle

    stored = {}

    class Storage:
        def put_snapshot_history(self, key, data):
            stored[key] = data

    class Sources:
        storage = Storage()

        def upload_git_snapshot(self, _project, _commit, _archive):
            return {"revision_id": "rev_" + "0" * 32, "state": "indexing"}

    class Registry:
        def get_git_source(self, _user, _project):
            return {"repository": "owner/repo", "branch": "main"}

        def update_git_source_status(self, *a, **k):
            pass

        def archive_key(self, _project, _commit):
            return f"telemetry-sources/{'a' * 64}/{'b' * 64}.zip"

    service = git_source.GitSourceService(Registry(), Sources(), lambda *_: (head, b"zip", artifact))
    assert service.synchronize("u", "p")["history_available"] is True
    assert list(stored) == [history_secrets.history_key(Registry().archive_key("p", head), head)]
    old_fetcher = git_source.GitSourceService(Registry(), Sources(), lambda *_: (head, b"zip"))
    assert old_fetcher.synchronize("u", "p")["history_available"] is False


def test_worker_validates_and_runs_history_jobs(tmp_path, fake_scanner, monkeypatch):
    app_path = str(Path(__file__).parents[3] / "analyzer-worker")
    if not Path(app_path, "worker.py").is_file():
        pytest.skip("analyzer-worker is not mounted here")
    monkeypatch.syspath_prepend(app_path)
    import worker
    from fastapi import HTTPException

    origin, head = _repository(tmp_path)
    data = _artifact(tmp_path, origin, head, depth=10)
    payload = {"schema_version": 1, "job_id": str(uuid4()), "revision_id": "rev_" + "a" * 32, "commit_sha": head,
               "history_base64": base64.b64encode(data).decode("ascii"),
               "history_sha256": hashlib.sha256(data).hexdigest()}
    assert worker._validate_history_job(payload)[2] == head
    with pytest.raises(HTTPException, match="digest"):
        worker._validate_history_job({**payload, "history_sha256": "0" * 64})
    with pytest.raises(HTTPException, match="exactly the revision"):
        worker._validate_history_job({**payload, "commit_sha": "f" * 40})
    with pytest.raises(HTTPException, match="unsupported"):
        worker._validate_history_job({**payload, "token": "x"})
    from fastapi.testclient import TestClient
    response = TestClient(worker.app).post("/v1/history-scan", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["job_id"] == payload["job_id"] and body["result"]["status"] == "complete"
    assert FAKE_TOKEN not in response.text


def test_real_scanner_finds_a_token_removed_from_the_tree(tmp_path):
    if os.environ.get("BETTERLEAKS_BIN") or shutil.which("betterleaks") is None:
        pytest.skip("the pinned Betterleaks is not installed here")
    origin, head = _repository(tmp_path)
    result = history_secrets.run_history_scan(_artifact(tmp_path, origin, head, depth=50), head)
    assert result["status"] == "complete", result
    assert result["version"] == history_secrets.VERSION
    assert any(f["file"] == "config.php" and f["line"] == 2 for f in result["findings"]), result["findings"]
    assert FAKE_TOKEN not in json.dumps(result)


def test_one_finding_per_location_at_its_newest_commit_and_generic_rules_are_warnings():
    def row(rule, file, line, commit, date):
        return {"RuleID": rule, "Description": rule, "File": file, "StartLine": line, "Commit": commit * 40,
                "Date": f"{date}T10:00:00Z", "Secret": FAKE_TOKEN}

    rows = history_secrets.normalize([
        row("generic-password", "translations/auth.yml", 7, "a", "2026-03-01"),
        row("github-pat", "auth.json", 3, "b", "2022-10-20"),
        row("github-pat", "auth.json", 3, "c", "2022-10-24"),   # same secret on another branch
        row("github-pat", "auth.json", 3, "b", "2022-10-20"),
        row("github-pat", "auth.json", 4, "b", "2022-10-20"),
    ])
    assert [(r["rule"], r["file"], r["line"], r["commit"][0], r["commits"]) for r in rows] == [
        ("github-pat", "auth.json", 3, "c", 2),          # newest commit kept, both counted
        ("github-pat", "auth.json", 4, "b", 1),
        ("generic-password", "translations/auth.yml", 7, "a", 1),   # keyword matches sort after known formats
    ]
    result = history_secrets._result("complete", None, findings=rows, commits_scanned=3)
    reported = {(f["line"], f["severity"]): f["message"] for f in history_secrets.findings(result)}
    assert set(reported) == {(4, "error"), (3, "error"), (7, "warning")}
    assert "(and 1 other commit)" in reported[(3, "error")] and "other commit" not in reported[(4, "error")]
    assert reported[(7, "warning")].startswith("Possible secret: generic-password. Committed in aaaaaaaaaaaa")
    long = dict(rows[0], description="Uncovered a token " + "with a very long scanner description " * 8 + ".")
    [message] = [f["message"] for f in history_secrets.findings({"findings": [long]})]
    assert len(message) <= 240 and message.endswith("rotate it, then remove it from history.") 
    assert "scanner… Committed in cccccccccccc on 2022-10-24 (and 1 other commit);" in message
    summary = history_secrets.summary(result)
    assert summary["finding_count"] == 3 and summary["error_count"] == 2
    assert FAKE_TOKEN not in json.dumps(result)


def test_the_module_never_names_a_framework():
    import inspect

    source = inspect.getsource(history_secrets).lower()
    assert "laravel" not in source and "symfony" not in source
