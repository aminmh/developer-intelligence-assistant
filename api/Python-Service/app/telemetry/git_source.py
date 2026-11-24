"""Bounded GitHub branch synchronization into immutable source snapshots."""
from __future__ import annotations

import io
import ipaddress
import os
import stat
import zipfile
from dataclasses import dataclass
from pathlib import Path
import re
import selectors
import socket
import subprocess
import tempfile
import time
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from telemetry.registry import SnapshotRegistry, default_registry
from telemetry.source import SourceService

REPOSITORY = re.compile(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}\Z")
BRANCH = re.compile(r"[A-Za-z0-9_./-]{1,200}\Z")
MAX_GIT_ARCHIVE_BYTES = 64 * 1024 * 1024
HOST = re.compile(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?\Z")
PATH_SEGMENT = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}\Z")
SSH_USER = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}\Z")
SCP_LIKE = re.compile(r"(?:(?P<user>[A-Za-z0-9_][A-Za-z0-9_.-]{0,63})@)?(?P<host>[A-Za-z0-9.-]{1,253}):(?!//)(?P<path>[^\s:]+)\Z")
MAX_LISTED_BRANCHES = 500


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


@dataclass(frozen=True)
class Remote:
    transport: str  # "https" or "ssh"
    host: str
    path: str  # "owner/name", or "group/subgroup/name" on hosts that nest
    user: str = "git"
    port: int | None = None

    @property
    def identity(self) -> str:
        """The stored repository key: ``owner/name`` on github.com (as before), ``host/path`` elsewhere."""
        return self.path if self.host == "github.com" else f"{self.host}/{self.path}".lower()

    @property
    def name(self) -> str:
        return self.path.rsplit("/", 1)[1]

    @property
    def url(self) -> str:
        if self.transport == "https":
            return f"https://{self.host}{f':{self.port}' if self.port else ''}/{self.path}.git"
        return f"ssh://{self.user}@{self.host}{f':{self.port}' if self.port else ''}/{self.path}.git"

    @property
    def is_default_github(self) -> bool:
        """What the legacy ``owner/name`` form already means, so it needs no stored URL."""
        return self.transport == "https" and self.host == "github.com" and self.port is None


def _clean_path(raw: str, host: str) -> str:
    path = raw.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    segments = path.split("/")
    if not 2 <= len(segments) <= 10 or any(
            not PATH_SEGMENT.fullmatch(part) or part in {".", ".."} or part.endswith((".git", ".lock")) for part in segments):
        raise ValueError("Enter a repository URL such as https://host/owner/name.git or git@host:owner/name.git.")
    if host == "github.com" and len(segments) != 2:
        raise ValueError("A github.com repository must be owner/name.")
    return "/".join(segments).lower() if host == "github.com" else "/".join(segments)


def parse_remote(value: object) -> Remote:
    """Accept ``owner/name`` (github.com), an HTTPS URL, or an SSH URL (``ssh://`` or ``git@host:path``).

    Credentials never belong in the URL: userinfo on HTTPS is rejected, and the SSH user is only a login name.
    """
    invalid = ValueError("Enter a repository URL such as https://host/owner/name.git or git@host:owner/name.git.")
    if not isinstance(value, str) or not value.strip() or len(value) > 500 or any(c.isspace() or ord(c) < 32 for c in value.strip()):
        raise invalid
    text = value.strip()
    if REPOSITORY.fullmatch(text):
        return Remote("https", "github.com", _clean_path(text, "github.com"))
    if "://" not in text:
        match = SCP_LIKE.fullmatch(text)
        if not match or "." not in match["host"] and match["host"] != "localhost":
            raise invalid
        host = match["host"].lower()
        if not HOST.fullmatch(host):
            raise invalid
        return Remote("ssh", host, _clean_path(match["path"], host), match["user"] or "git")
    parts = urlsplit(text)
    if parts.scheme == "http":
        raise ValueError("Use an https:// or ssh:// URL; plain http is not supported.")
    if parts.scheme not in {"https", "ssh"} or parts.query or parts.fragment or not parts.hostname:
        raise invalid
    try:
        port = parts.port
    except ValueError as exc:
        raise invalid from exc
    host = parts.hostname.lower()
    if not HOST.fullmatch(host):
        raise invalid
    if parts.scheme == "https":
        if parts.username or parts.password:
            raise ValueError("Do not put credentials in the URL. Save an access token instead.")
        return Remote("https", host, _clean_path(parts.path, host), port=port)
    if parts.password or (parts.username and not SSH_USER.fullmatch(parts.username)):
        raise invalid
    return Remote("ssh", host, _clean_path(parts.path, host), parts.username or "git", port)


def assert_public_host(host: str) -> None:
    """Refuse hosts that resolve to loopback, link-local or (unless allowed) private addresses.

    The API fetches whatever host a user names, so internal services must not be reachable this way.
    Set GIT_SOURCE_ALLOW_PRIVATE_HOSTS=1 to permit a self-hosted Git server on a private network.
    """
    allow_private = os.environ.get("GIT_SOURCE_ALLOW_PRIVATE_HOSTS") == "1"
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)}
    except OSError as exc:
        raise GitSyncError("git_host_unresolved", "The Git host could not be resolved.") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
        if ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast or ip.is_reserved or (ip.is_private and not allow_private):
            raise GitSyncError("git_host_blocked", "That Git host resolves to a private or local address and is not allowed.")


_DIAGNOSES = (
    (("permission denied (publickey", "no supported authentication methods"), "git_ssh_key_rejected",
     "The Git host refused your SSH key. Make sure your ssh-agent holds a key added to your account (or the repository as a deploy key)."),
    (("host key verification failed", "remote host identification has changed"), "git_host_key_mismatch",
     "The SSH host key did not match the one remembered for this host."),
    (("could not resolve host", "name or service not known", "temporary failure in name resolution"), "git_host_unresolved",
     "The Git host could not be resolved."),
    (("could not read username", "terminal prompts are disabled", "could not read password"), "git_token_required",
     "The repository needs an access token. Paste one into Access token and fetch again."),
    (("authentication failed", "invalid username or password", "bad credentials", "returned error: 401"), "git_token_rejected",
     "The Git host rejected the access token. It may be expired, mistyped, or missing read access to the repository."),
    (("saml", "single sign-on", "returned error: 403", "access denied", "forbidden"), "git_access_forbidden",
     "Access was forbidden. The token may lack read access to this repository, or the organization requires the token to be authorized (SSO) or approved."),
    (("repository not found", "returned error: 404", "not found"), "git_repository_not_found",
     "The repository was not found, or the credential cannot see it. Check the URL; for a private repository, check the token's access to it."),
    (("connection timed out", "operation timed out", "connection refused", "network is unreachable"), "git_host_unreachable",
     "The Git host could not be reached."),
)


def _diagnose(stderr: str) -> tuple[str, str]:
    for needles, code, message in _DIAGNOSES:
        if any(needle in stderr for needle in needles):
            return code, message
    return "git_fetch_failed", ("The Git host rejected the request or the repository is unavailable. For SSH, check that your "
                                "ssh-agent holds a key with access; for a private HTTPS repository, provide an access token.")


def _git(args: list[str], cwd: str, env: dict[str, str], *, timeout: int = 90,
         max_output: int = 2 * 1024 * 1024) -> bytes:
    errors = tempfile.TemporaryFile()
    try:
        process = subprocess.Popen(["git", *args], cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=errors, bufsize=0)
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
        raise GitSyncError("git_fetch_timeout", "The Git host did not respond in time.") from exc
    except Exception:
        process.kill()
        process.wait()
        raise
    if return_code:
        # Never expose Git's remote output (it can contain provider-controlled text): it only selects a fixed message.
        errors.seek(0)
        code, message = _diagnose(errors.read(8192).decode("utf-8", "replace").lower())
        raise GitSyncError(code, message)
    return bytes(output)


def fetch_branch_archive(repository: str, branch: str, token: str | None = None) -> tuple[str, bytes]:
    """Fetch one branch head from fixed github.com over HTTPS and export its tree."""
    commit, archive, _history = _fetch(repository, branch, token, with_history=False)
    return commit, archive


def fetch_branch_snapshot(repository: str, branch: str, token: str | None = None) -> tuple[str, bytes, bytes | None]:
    """The branch head, its tree, and its recent history for the secret scan (None when unavailable)."""
    return _fetch(repository, branch, token, with_history=True)


def drop_symlinks(archive: bytes) -> bytes:
    """Rebuild the archive without symlink entries.

    Source revisions never contain links (they could point outside the snapshot), so a repository that has
    some is indexed without them instead of being refused as a whole.
    """
    source = zipfile.ZipFile(io.BytesIO(archive))
    if not any(stat.S_ISLNK(info.external_attr >> 16) for info in source.infolist()):
        return archive
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as target:
        for info in source.infolist():
            if not stat.S_ISLNK(info.external_attr >> 16):
                target.writestr(info, source.read(info))
    return out.getvalue()


def _git_env(root: Path, remote: Remote, token: str) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items()
           if key in {"PATH", "HOME", "TMPDIR", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "SSH_AUTH_SOCK"}}
    env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_ALLOW_PROTOCOL": "https:ssh"})
    if remote.transport == "ssh":
        # Keys come only from the forwarded ssh-agent; host keys are remembered on first use.
        known_hosts = os.environ.get("GIT_SOURCE_KNOWN_HOSTS") or str(default_registry().root / "known_hosts")
        env["GIT_SSH_COMMAND"] = ("ssh -o BatchMode=yes -o ConnectTimeout=15 -o StrictHostKeyChecking=accept-new "
                                  f"-o UserKnownHostsFile={known_hosts} -o IdentitiesOnly=no")
    elif token:
        askpass = root / "askpass.py"
        askpass.write_text(
            "#!/usr/bin/env python3\nimport os, sys\n"
            "prompt = sys.argv[1].lower() if len(sys.argv) > 1 else ''\n"
            "print('x-access-token' if 'username' in prompt else os.environ['DA_GITHUB_TOKEN'])\n",
            encoding="utf-8",
        )
        askpass.chmod(0o700)
        env.update({"GIT_ASKPASS": str(askpass), "GIT_ASKPASS_REQUIRE": "force", "DA_GITHUB_TOKEN": token})
    return env


def _clean_token(token: str | None) -> str:
    if token is not None and (not isinstance(token, str) or len(token) > 8192):
        raise GitSyncError("git_credential_invalid", "The saved GitHub token is invalid. Replace it and retry.")
    return token.strip() if isinstance(token, str) else ""


_GIT_REMOTE_OPTIONS = ["-c", "http.followRedirects=false"]


def _fetch(repository: str, branch: str, token: str | None, *, with_history: bool) -> tuple[str, bytes, bytes | None]:
    remote = parse_remote(repository)
    branch = validate_branch(branch)
    token = _clean_token(token)
    assert_public_host(remote.host)

    with tempfile.TemporaryDirectory(prefix="code-quality-git-") as temp:
        root = Path(temp)
        work = root / "repo"
        work.mkdir(mode=0o700)
        env = _git_env(root, remote, token)
        _git(["init", "--quiet"], str(work), env)
        _git(["remote", "add", "origin", remote.url], str(work), env)
        _git([*_GIT_REMOTE_OPTIONS, "fetch", "--quiet", "--depth=1", "origin", f"refs/heads/{branch}"], str(work), env)
        commit = _git(["rev-parse", "--verify", "FETCH_HEAD^{commit}"], str(work), env).decode().strip()
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit):
            raise GitSyncError("git_commit_invalid", "The Git host returned an invalid commit identity.")
        if _git(["ls-tree", "-r", commit], str(work), env).find(b"160000 commit ") >= 0:
            raise GitSyncError("git_submodules_unsupported", "Repositories with Git submodules are not supported yet.")
        archive = drop_symlinks(_git(["archive", "--format=zip", f"--prefix={remote.name}/", commit],
                                     str(work), env, max_output=MAX_GIT_ARCHIVE_BYTES))
        if not archive or len(archive) > MAX_GIT_ARCHIVE_BYTES:
            raise GitSyncError("git_archive_limit", "The fetched source exceeds the 64 MiB archive limit.")
        return commit, archive, (_fetch_history(work, env, commit) if with_history else None)


def inspect_remote(repository: str, token: str | None = None) -> dict[str, Any]:
    """List a remote's branches without cloning it, so a developer can choose one to fetch."""
    remote = parse_remote(repository)
    token = _clean_token(token)
    assert_public_host(remote.host)
    with tempfile.TemporaryDirectory(prefix="code-quality-git-") as temp:
        root = Path(temp)
        env = _git_env(root, remote, token)
        output = _git([*_GIT_REMOTE_OPTIONS, "ls-remote", "--symref", remote.url, "HEAD", "refs/heads/*", "refs/tags/*"],
                      str(root), env, timeout=45).decode("utf-8", "replace")
    default_branch = None
    head_commit = None
    branches: dict[str, str] = {}
    tags = 0
    for line in output.splitlines():
        if line.startswith("ref: "):
            target = line[5:].split("\t", 1)[0]
            if target.startswith("refs/heads/"):
                default_branch = target[len("refs/heads/"):]
            continue
        sha, _, ref = line.partition("\t")
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha):
            continue
        if ref == "HEAD":
            head_commit = sha
        elif ref.startswith("refs/heads/"):
            name = ref[len("refs/heads/"):]
            try:
                branches[validate_branch(name)] = sha
            except ValueError:
                continue  # a branch name that could not be configured anyway
        elif ref.startswith("refs/tags/") and not ref.endswith("^{}"):
            tags += 1
    if default_branch is None and head_commit:
        default_branch = next((name for name, sha in branches.items() if sha == head_commit and name in {"main", "master"}), None) \
            or next((name for name, sha in branches.items() if sha == head_commit), None)
    ordered = sorted(branches, key=lambda name: (name != default_branch, name.lower()))
    return {
        "repository": remote.identity, "host": remote.host, "name": remote.name, "transport": remote.transport,
        "url": remote.url, "default_branch": default_branch, "branch_count": len(branches), "tag_count": tags,
        "head_commit": head_commit,
        "branches": [{"name": name, "commit": branches[name], "is_default": name == default_branch}
                     for name in ordered[:MAX_LISTED_BRANCHES]],
        "truncated": len(branches) > MAX_LISTED_BRANCHES,
    }


def _fetch_history(work: Path, env: dict[str, str], commit: str) -> bytes | None:
    """Up to HISTORY_DEPTH commits behind ``commit`` as a history artifact (code_quality.history_secrets).

    Best effort: a failure or an oversized history leaves the revision without a history scan,
    never fails the synchronization.
    """
    from code_quality.history_secrets import HISTORY_DEPTH, HISTORY_REF, MAX_HISTORY_BYTES, pack_history

    try:
        _git(["fetch", "--quiet", f"--depth={HISTORY_DEPTH}", "origin", commit], str(work), env, timeout=180)
        _git(["update-ref", HISTORY_REF, commit], str(work), env)
        bundle = _git(["bundle", "create", "--quiet", "-", HISTORY_REF], str(work), env, timeout=180,
                      max_output=MAX_HISTORY_BYTES)
        shallow_file = work / ".git" / "shallow"
        shallow = shallow_file.read_text(encoding="ascii").split() if shallow_file.is_file() else []
        return pack_history(bundle, shallow)
    except (GitSyncError, OSError, UnicodeError, ValueError):
        return None


class GitSourceService:
    def __init__(self, registry: SnapshotRegistry | None = None, sources: SourceService | None = None,
                 fetcher=fetch_branch_snapshot, inspector=inspect_remote):
        self.registry = registry or default_registry()
        if sources is None:
            from index.storage import storage
            sources = SourceService(self.registry, storage)
        self.sources = sources
        self.fetcher = fetcher
        self.inspector = inspector

    def configure(self, user_id: str, project_id: str, repository: object, branch: object) -> dict[str, Any]:
        remote = parse_remote(repository)
        repo = remote.identity
        selected_branch = validate_branch(branch)
        existing = self.registry.get_git_source(user_id, project_id)
        # A different transport is a different credential (token vs ssh-agent), so it gets a new id too.
        same = existing and existing["repository"] == repo and (existing.get("remote_url") or None) == (None if remote.is_default_github else remote.url)
        credential_id = existing["credential_id"] if same else str(uuid4())
        return self.registry.save_git_source(user_id, project_id, repo, selected_branch, credential_id,
                                             None if remote.is_default_github else remote.url)

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
            "last_revision_id", "error_code", "updated_at", "credential_id", "remote_url")}

    def inspect(self, repository: object, token: str | None = None) -> dict[str, Any]:
        return self.inspector(str(repository), token)

    def source_for_repository(self, repository: object) -> dict[str, Any] | None:
        repo = validate_repository(repository)
        return self.registry.git_source_for_repository(repo)

    def _store_history(self, project_id: str, commit_sha: str, history: bytes | None) -> bool:
        """Keep the history artifact next to the source archive; a failure only skips the history scan."""
        from code_quality.history_secrets import history_key

        put = getattr(self.sources.storage, "put_snapshot_history", None)
        archive_key = self.registry.archive_key(project_id, commit_sha)
        if history is None or put is None or not archive_key:
            return False
        try:
            put(history_key(archive_key, commit_sha), history)
            return True
        except Exception:  # noqa: BLE001 — never log storage errors with provider details
            return False

    def synchronize(self, user_id: str, project_id: str, token: str | None = None) -> dict[str, Any]:
        source = self.registry.get_git_source(user_id, project_id)
        if source is None:
            raise GitSyncError("git_source_missing", "Configure a GitHub repository before synchronizing.")
        self.registry.update_git_source_status(user_id, project_id, status="fetching", error_code=None)
        try:
            fetched = self.fetcher(source.get("remote_url") or source["repository"], source["branch"], token)
            commit_sha, archive = fetched[0], fetched[1]
            result = self.sources.upload_git_snapshot(project_id, commit_sha, archive)
            history_stored = self._store_history(project_id, commit_sha, fetched[2] if len(fetched) > 2 else None)
            self.registry.update_git_source_status(
                user_id, project_id,
                status="ready" if result.get("state") == "ready" else "indexing",
                commit_sha=commit_sha, revision_id=result.get("revision_id"), error_code=None,
            )
            return {"project_id": project_id, "repository": source["repository"], "branch": source["branch"],
                    "commit_sha": commit_sha, "revision_id": result.get("revision_id"), "state": result.get("state"),
                    "history_available": history_stored}
        except GitSyncError as exc:
            self.registry.update_git_source_status(user_id, project_id, status="failed", error_code=exc.code)
            raise
        except Exception as exc:
            # Do not log exception text because it can contain provider output.
            self.registry.update_git_source_status(user_id, project_id, status="failed", error_code="git_sync_failed")
            raise GitSyncError("git_sync_failed", "Git synchronization failed. Check access and retry.") from exc
