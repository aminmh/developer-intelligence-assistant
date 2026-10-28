"""Bounded GitHub branch synchronization into immutable source snapshots."""
from __future__ import annotations

import os
from pathlib import Path
import re
import selectors
import subprocess
import tempfile
import time
from typing import Any
from uuid import uuid4

from telemetry.registry import SnapshotRegistry, default_registry
from telemetry.source import SourceService

REPOSITORY = re.compile(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}\Z")
BRANCH = re.compile(r"[A-Za-z0-9_./-]{1,200}\Z")
MAX_GIT_ARCHIVE_BYTES = 64 * 1024 * 1024


class GitSyncError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def validate_repository(repository: object) -> str:
    if not isinstance(repository, str) or not REPOSITORY.fullmatch(repository):
        raise ValueError("Enter a GitHub repository as owner/name.")
    owner, name = repository.split("/", 1)
    if owner in {".", ".."} or name in {".", ".."} or name.endswith(".git"):
        raise ValueError("Enter a GitHub repository as owner/name without a URL or suffix.")
    return f"{owner.lower()}/{name.lower()}"


def validate_branch(branch: object) -> str:
    if not isinstance(branch, str) or not BRANCH.fullmatch(branch):
        raise ValueError("Enter a valid Git branch name.")
    if branch in {"", "@"} or branch.startswith("-") or branch.endswith(("/", ".")) or ".." in branch or "//" in branch:
        raise ValueError("Enter a valid Git branch name.")
    if any(part.startswith(".") or part.endswith((".", ".lock")) for part in branch.split("/")):
        raise ValueError("Enter a valid Git branch name.")
    return branch


def _git(args: list[str], cwd: str, env: dict[str, str], *, timeout: int = 90,
         max_output: int = 2 * 1024 * 1024) -> bytes:
    try:
        process = subprocess.Popen(["git", *args], cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
    except FileNotFoundError as exc:
        raise GitSyncError("git_unavailable", "Git is unavailable in the API runtime.") from exc
    deadline = time.monotonic() + timeout
    output = bytearray()
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(["git", *args], timeout)
                for key, _ in selector.select(min(remaining, 1.0)):
                    chunk = os.read(key.fileobj.fileno(), 64 * 1024)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    output.extend(chunk)
                    if len(output) > max_output:
                        raise GitSyncError("git_output_limit", "The fetched source exceeds the configured size limits.")
        return_code = process.wait(timeout=max(0.1, deadline - time.monotonic()))
    except subprocess.TimeoutExpired as exc:
        process.kill()
        process.wait()
        raise GitSyncError("git_fetch_timeout", "GitHub did not complete the fetch in time.") from exc
    except Exception:
        process.kill()
        process.wait()
        raise
    if return_code:
        # Never expose Git's remote output: it can contain provider-controlled details.
        raise GitSyncError("git_fetch_failed", "GitHub rejected the fetch or the repository is unavailable.")
    return bytes(output)


def fetch_branch_archive(repository: str, branch: str, token: str | None = None) -> tuple[str, bytes]:
    """Fetch one branch head from fixed github.com over HTTPS and export its tree."""
    repository = validate_repository(repository)
    branch = validate_branch(branch)
    if token is not None and (not isinstance(token, str) or len(token) > 8192):
        raise GitSyncError("git_credential_invalid", "The saved GitHub token is invalid. Replace it and retry.")
    token = token.strip() if isinstance(token, str) else ""

    with tempfile.TemporaryDirectory(prefix="code-quality-git-") as temp:
        root = Path(temp)
        work = root / "repo"
        work.mkdir(mode=0o700)
        env = {key: value for key, value in os.environ.items()
               if key in {"PATH", "HOME", "TMPDIR", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY"}}
        env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_CONFIG_GLOBAL": os.devnull})
        if token:
            askpass = root / "askpass.py"
            askpass.write_text(
                "#!/usr/bin/env python3\nimport os, sys\n"
                "prompt = sys.argv[1].lower() if len(sys.argv) > 1 else ''\n"
                "print('x-access-token' if 'username' in prompt else os.environ['DA_GITHUB_TOKEN'])\n",
                encoding="utf-8",
            )
            askpass.chmod(0o700)
            env.update({"GIT_ASKPASS": str(askpass), "GIT_ASKPASS_REQUIRE": "force",
                        "DA_GITHUB_TOKEN": token})
        _git(["init", "--quiet"], str(work), env)
        _git(["remote", "add", "origin", f"https://github.com/{repository}.git"], str(work), env)
        _git(["fetch", "--quiet", "--depth=1", "origin", f"refs/heads/{branch}"], str(work), env)
        commit = _git(["rev-parse", "--verify", "FETCH_HEAD^{commit}"], str(work), env).decode().strip()
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit):
            raise GitSyncError("git_commit_invalid", "GitHub returned an invalid commit identity.")
        if _git(["ls-tree", "-r", commit], str(work), env).find(b"160000 commit ") >= 0:
            raise GitSyncError("git_submodules_unsupported", "Repositories with Git submodules are not supported yet.")
        archive = _git(["archive", "--format=zip", f"--prefix={repository.split('/')[1]}/", commit],
                       str(work), env, max_output=MAX_GIT_ARCHIVE_BYTES)
        if not archive or len(archive) > MAX_GIT_ARCHIVE_BYTES:
            raise GitSyncError("git_archive_limit", "The fetched source exceeds the 64 MiB archive limit.")
        return commit, archive


class GitSourceService:
    def __init__(self, registry: SnapshotRegistry | None = None, sources: SourceService | None = None,
                 fetcher=fetch_branch_archive):
        self.registry = registry or default_registry()
        if sources is None:
            from index.storage import storage
            sources = SourceService(self.registry, storage)
        self.sources = sources
        self.fetcher = fetcher

    def configure(self, user_id: str, project_id: str, repository: object, branch: object) -> dict[str, Any]:
        repo = validate_repository(repository)
        selected_branch = validate_branch(branch)
        existing = self.registry.get_git_source(user_id, project_id)
        credential_id = existing["credential_id"] if existing and existing["repository"] == repo else str(uuid4())
        return self.registry.save_git_source(user_id, project_id, repo, selected_branch, credential_id)

    def status(self, user_id: str, project_id: str) -> dict[str, Any] | None:
        source = self.registry.get_git_source(user_id, project_id)
        if source is None:
            return None
        commit = source.get("last_commit_sha")
        if commit:
            indexed = self.registry.get(project_id, commit)
            if indexed.get("state") in {"queued", "indexing"}:
                source["status"] = "indexing"
            elif indexed.get("state") == "ready":
                source["status"] = "ready"
            elif indexed.get("state") == "failed":
                source["status"] = "failed"
                source["error_code"] = indexed.get("error_code") or "snapshot_indexing_failed"
        return {key: source.get(key) for key in (
            "project_id", "repository", "branch", "status", "last_commit_sha",
            "last_revision_id", "error_code", "updated_at", "credential_id")}

    def source_for_repository(self, repository: object) -> dict[str, Any] | None:
        repo = validate_repository(repository)
        return self.registry.git_source_for_repository(repo)

    def synchronize(self, user_id: str, project_id: str, token: str | None = None) -> dict[str, Any]:
        source = self.registry.get_git_source(user_id, project_id)
        if source is None:
            raise GitSyncError("git_source_missing", "Configure a GitHub repository before synchronizing.")
        self.registry.update_git_source_status(user_id, project_id, status="fetching", error_code=None)
        try:
            commit_sha, archive = self.fetcher(source["repository"], source["branch"], token)
            result = self.sources.upload_git_snapshot(project_id, commit_sha, archive)
            self.registry.update_git_source_status(
                user_id, project_id,
                status="ready" if result.get("state") == "ready" else "indexing",
                commit_sha=commit_sha, revision_id=result.get("revision_id"), error_code=None,
            )
            return {"project_id": project_id, "repository": source["repository"], "branch": source["branch"],
                    "commit_sha": commit_sha, "revision_id": result.get("revision_id"), "state": result.get("state")}
        except GitSyncError as exc:
            self.registry.update_git_source_status(user_id, project_id, status="failed", error_code=exc.code)
            raise
        except Exception as exc:
            # Do not log exception text because it can contain provider output.
            self.registry.update_git_source_status(user_id, project_id, status="failed", error_code="git_sync_failed")
            raise GitSyncError("git_sync_failed", "Git synchronization failed. Check access and retry.") from exc
