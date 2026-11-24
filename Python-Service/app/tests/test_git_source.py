from __future__ import annotations

import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from telemetry import http
import telemetry.git_source as git_source
from telemetry.git_source import GitSourceService, GitSyncError, assert_public_host, parse_remote, validate_branch, validate_repository
from telemetry.registry import SnapshotConflict, SnapshotRegistry
from telemetry.source import SourceService


def archive(content: bytes = b"<?php echo 'ok';") -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as zip_file:
        zip_file.writestr("sample/index.php", content)
    return output.getvalue()


class MemoryStorage:
    def __init__(self):
        self.objects = {}

    def put_snapshot_archive(self, key, checksum, data):
        path = f"{key}/{checksum}.zip"
        self.objects[path] = data
        return path

    def get_snapshot_archive(self, key):
        return self.objects[key]

    def delete_snapshot_archive(self, key):
        self.objects.pop(key, None)


class GitSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.registry = SnapshotRegistry(Path(self.temp.name) / "registry")
        self.storage = MemoryStorage()
        self.source_service = SourceService(self.registry, self.storage,
            indexer=lambda _workspace, _snapshot: {"chunk_count": 1, "ingested": 1, "graph_nodes": 1, "embed_fails": 0})
        commit = "a" * 40
        self.archived = archive()
        self.fetches = []

        def fetcher(repository, branch, token):
            self.fetches.append((repository, branch, token))
            return commit, self.archived

        self.git = GitSourceService(self.registry, self.source_service, fetcher)
        self.git.configure("user-1", "sample", "Owner/Repo", "main")

    def tearDown(self):
        self.temp.cleanup()

    def test_configured_sources_are_listed_for_the_scheduled_reconcile(self):
        self.git.configure("user-2", "other", "Owner/Other", "develop")
        sources = {row["project_id"]: row for row in self.registry.list_git_sources()}
        self.assertEqual(set(sources), {"sample", "other"})
        self.assertEqual((sources["other"]["repository"], sources["other"]["branch"], sources["other"]["user_id"]),
                         ("owner/other", "develop", "user-2"))

    def test_validates_fixed_github_repository_and_branch_identity(self):
        self.assertEqual(validate_repository("Owner/Repo"), "owner/repo")
        self.assertEqual(validate_branch("release/v1"), "release/v1")
        for invalid in ("https://github.com/owner/repo", "owner/repo.git", "owner/../repo", "owner/repo?x=1"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_repository(invalid)
        for invalid in ("-main", "../main", "main//x", "main.lock", "release./v1", "@"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_branch(invalid)

    def test_remote_urls_in_https_and_ssh_forms_resolve_to_one_identity(self):
        for value in ("owner/repo", "https://github.com/Owner/Repo.git", "https://github.com/owner/repo/",
                      "git@github.com:owner/repo.git", "ssh://git@github.com/owner/repo.git"):
            self.assertEqual(parse_remote(value).identity, "owner/repo", value)
        gitlab = parse_remote("git@gitlab.example.com:group/sub/app.git")
        self.assertEqual((gitlab.transport, gitlab.identity, gitlab.url),
                         ("ssh", "gitlab.example.com/group/sub/app", "ssh://git@gitlab.example.com/group/sub/app.git"))
        self.assertEqual(parse_remote("https://git.example.com:8443/team/app").url, "https://git.example.com:8443/team/app.git")
        self.assertTrue(parse_remote("https://github.com/o/r").is_default_github)
        self.assertFalse(parse_remote("git@github.com:o/r.git").is_default_github)

    def test_remote_urls_reject_credentials_unsafe_hosts_and_option_injection(self):
        for invalid in ("http://github.com/o/r", "https://user:secret@github.com/o/r", "https://token@github.com/o/r",
                        "https://github.com/o", "https://github.com/o/r?x=1", "ssh://-oProxyCommand=x/o/r",
                        "git@-oProxyCommand=x:o/r", "https://github.com/-o/r", "https://github.com/../r",
                        "file:///etc/passwd", "ftp://host/o/r", "https://github.com/a/b/c", "", "o r"):
            with self.assertRaises(ValueError, msg=invalid):
                parse_remote(invalid)

    def test_hosts_resolving_to_local_or_private_addresses_are_refused(self):
        for address in ("127.0.0.1", "169.254.169.254", "10.0.0.5"):
            with patch("telemetry.git_source.socket.getaddrinfo", return_value=[(2, 1, 6, "", (address, 0))]), \
                 self.assertRaises(GitSyncError) as raised:
                assert_public_host("internal.example")
            self.assertEqual(raised.exception.code, "git_host_blocked")
        with patch("telemetry.git_source.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("140.82.112.3", 0))]):
            assert_public_host("github.com")

    def test_failed_git_output_selects_a_fixed_message_and_is_never_exposed(self):
        from telemetry.git_source import _diagnose
        secret = "fatal: could not read Username for 'https://github.com': terminal prompts are disabled ghp_SECRET"
        code, message = _diagnose(secret.lower())
        self.assertEqual(code, "git_token_required")
        self.assertNotIn("ghp_", message)
        self.assertEqual(_diagnose("remote: repository not found.")[0], "git_repository_not_found")
        self.assertEqual(_diagnose("git@github.com: permission denied (publickey).")[0], "git_ssh_key_rejected")
        self.assertEqual(_diagnose("something unexpected")[0], "git_fetch_failed")

    def test_symlinks_are_dropped_from_a_fetched_archive_so_it_passes_validation(self):
        from telemetry.git_source import drop_symlinks
        from telemetry.source import validate_archive
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as bundle:
            bundle.writestr("repo/a.txt", "a")
            link = zipfile.ZipInfo("repo/link")
            link.external_attr = (0o120777) << 16
            bundle.writestr(link, "a.txt")
        with self.assertRaises(ValueError):
            validate_archive(buffer.getvalue())
        cleaned = drop_symlinks(buffer.getvalue())
        validate_archive(cleaned)
        self.assertEqual(zipfile.ZipFile(io.BytesIO(cleaned)).namelist(), ["repo/a.txt"])

    def test_ssh_source_keeps_its_url_and_syncs_through_it(self):
        self.git.configure("user-1", "sample", "git@gitlab.example.com:group/app.git", "main")
        source = self.git.status("user-1", "sample")
        self.assertEqual((source["repository"], source["remote_url"]),
                         ("gitlab.example.com/group/app", "ssh://git@gitlab.example.com/group/app.git"))
        self.git.synchronize("user-1", "sample")
        self.assertEqual(self.fetches[-1][0], "ssh://git@gitlab.example.com/group/app.git")

    def test_inspect_lists_branches_for_the_http_route(self):
        listing = {"repository": "owner/repo", "default_branch": "main", "branches": [{"name": "main", "commit": "a" * 40, "is_default": True}]}
        seen = []
        service = GitSourceService(self.registry, self.source_service, inspector=lambda repo, token: seen.append((repo, token)) or listing)
        app = FastAPI()
        app.include_router(http.git_router)
        client = TestClient(app)
        headers = {"x-internal-secret": "s" * 40, "x-user-id": "user-1"}
        with patch.dict("os.environ", {"RAG_CALLBACK_SECRET": "s" * 40}), \
             patch.object(git_source, "GitSourceService", return_value=service):
            self.assertEqual(client.post("/telemetry/git/inspect", json={"repository": "o/r"}).status_code, 401)
            response = client.post("/telemetry/git/inspect", headers=headers, json={"repository": "git@github.com:o/r.git", "token": "t"})
            self.assertEqual((response.status_code, response.json()), (200, listing))
            self.assertEqual(seen, [("git@github.com:o/r.git", "t")])
            bad = GitSourceService(self.registry, self.source_service, inspector=lambda *_: parse_remote("http://x/o/r"))
        with patch.dict("os.environ", {"RAG_CALLBACK_SECRET": "s" * 40}), patch.object(git_source, "GitSourceService", return_value=bad):
            self.assertEqual(client.post("/telemetry/git/inspect", headers=headers, json={"repository": "x"}).status_code, 422)

    def test_sync_creates_revision_and_queues_without_deployment(self):
        result = self.git.synchronize("user-1", "sample", "read-only-token")
        self.assertEqual(result["state"], "queued")
        self.assertRegex(result["commit_sha"], r"^[0-9a-f]{40}$")
        self.assertEqual(self.fetches, [("owner/repo", "main", "read-only-token")])
        self.assertEqual(self.registry.claim_job(), ("sample", "a" * 40))
        self.registry.finish_job("sample", "a" * 40, success=True)
        self.assertEqual(self.git.status("user-1", "sample")["status"], "ready")

    def test_reindex_rebuilds_a_ready_git_revision_through_the_worker_with_replace(self):
        calls = []

        def indexer(_workspace, _snapshot, **options):
            calls.append(options)
            return {"chunk_count": 1, "ingested": 1, "graph_nodes": 1, "embed_fails": 0}

        service = SourceService(self.registry, self.storage, indexer=indexer)
        git = GitSourceService(self.registry, service, lambda *_a: ("a" * 40, self.archived))
        git.synchronize("user-1", "sample")
        self.assertEqual(self.registry.queue_reindex("sample", "a" * 40), "busy")  # still indexing for the first time
        self.assertTrue(service.process_one())
        self.assertEqual(self.registry.get("sample", "a" * 40)["state"], "ready")
        self.assertEqual(calls, [{}])
        self.assertEqual(self.registry.queue_reindex("sample", "a" * 40), "queued")
        self.assertEqual(self.registry.get("sample", "a" * 40)["state"], "queued")
        self.assertEqual(self.registry.queue_reindex("sample", "a" * 40), "busy")
        self.assertTrue(service.process_one())
        self.assertEqual(calls, [{}, {"force_reindex": True}])
        self.assertEqual(self.registry.get("sample", "a" * 40)["state"], "ready")
        self.assertFalse(self.registry.job_reindex("sample", "a" * 40))
        self.assertEqual(self.registry.queue_reindex("sample", "b" * 40), "missing")

    def test_index_progress_follows_a_running_job_and_clears_when_it_finishes(self):
        git = GitSourceService(self.registry, self.source_service, lambda *_a: ("a" * 40, self.archived))
        revision_id = git.synchronize("user-1", "sample")["revision_id"]
        ref = "a" * 40
        self.assertEqual(self.registry.index_progress("sample", ref)["stage"], "Queued")
        self.registry.set_progress("sample", ref, "Embedding chunks", 80, "1/2")  # ignored: the job is not running yet
        self.assertEqual(self.registry.claim_job(), ("sample", ref))
        self.registry.set_progress("sample", ref, "Embedding chunks", 150, "5/10")
        self.assertEqual(self.registry.index_progress("sample", ref),
                         {"state": "indexing", "stage": "Embedding chunks", "percent": 100, "detail": "5/10"})
        self.registry.finish_job("sample", ref, success=True)
        self.assertEqual(self.registry.index_progress("sample", ref)["percent"], 100)
        self.assertEqual(self.registry.index_progress("sample", ref)["state"], "ready")
        self.assertEqual(self.registry.index_progress("sample", "c" * 40)["state"], "missing")
        self.assertTrue(revision_id)

    def test_heartbeat_reports_elapsed_time_during_a_long_stage_and_stops(self):
        from telemetry.source import _heartbeat
        seen = []
        with _heartbeat(lambda stage, percent, detail=None: seen.append((stage, percent, detail)), "Slow", 10, 50, interval=0.01):
            import time
            time.sleep(0.08)
        count = len(seen)
        self.assertGreater(count, 0)
        self.assertTrue(all(10 <= percent <= 50 and "running for" in detail for _, percent, detail in seen))
        import time
        time.sleep(0.05)
        self.assertEqual(len(seen), count)

    def test_reindex_http_route_queues_a_ready_git_revision_and_reports_conflicts(self):
        from code_quality import http as quality_http
        git = GitSourceService(self.registry, self.source_service, lambda *_a: ("a" * 40, self.archived))
        revision_id = git.synchronize("user-1", "sample")["revision_id"]
        self.assertTrue(self.source_service.process_one())
        app = FastAPI()
        app.include_router(quality_http.router)
        client = TestClient(app)
        headers = {"x-internal-secret": "s" * 40, "x-user-id": "user-1"}
        url = f"/code-quality/projects/sample/revisions/{revision_id}/reindex"
        with patch.dict("os.environ", {"RAG_CALLBACK_SECRET": "s" * 40}), \
             patch("telemetry.registry.default_registry", return_value=self.registry):
            self.assertEqual(client.post(url).status_code, 401)
            first = client.post(url, headers=headers)
            self.assertEqual((first.status_code, first.json()["queued"]), (200, True))
            self.assertEqual(client.post(url, headers=headers).status_code, 409)  # already queued
            missing = client.post("/code-quality/projects/sample/revisions/rev_" + "0" * 32 + "/reindex", headers=headers)
            self.assertEqual(missing.status_code, 404)

    def test_public_repository_sync_can_fetch_without_a_saved_token(self):
        received = []
        def anonymous_fetch(repository, branch, token):
            received.append(token)
            return "e" * 40, self.archived
        service = GitSourceService(self.registry, self.source_service, anonymous_fetch)
        result = service.synchronize("user-1", "sample")

        self.assertEqual(result["state"], "queued")
        self.assertEqual(received, [None])

    def test_repeated_fetch_reuses_immutable_ready_revision(self):
        first = self.git.synchronize("user-1", "sample", "read-only-token")
        self.registry.claim_job()
        self.registry.finish_job("sample", first["commit_sha"], success=True)
        second = self.git.synchronize("user-1", "sample", "read-only-token")
        self.assertEqual(second["revision_id"], first["revision_id"])
        self.assertEqual(second["state"], "ready")
        self.assertIsNone(self.registry.claim_job())

    def test_same_commit_with_different_source_checksum_is_rejected(self):
        first = self.git.synchronize("user-1", "sample", "read-only-token")
        with self.assertRaises(SnapshotConflict):
            self.source_service.upload_git_snapshot("sample", first["commit_sha"], archive(b"different"))

    def test_source_configuration_is_user_scoped_and_repository_is_unambiguous_for_webhooks(self):
        self.assertIsNone(self.git.status("other-user", "sample"))
        with self.assertRaisesRegex(ValueError, "already connected"):
            self.git.configure("user-2", "other-project", "owner/repo", "main")

    def test_fetcher_failure_is_safe_and_recorded_without_details(self):
        def failed_fetcher(*_args):
            raise GitSyncError("git_fetch_failed", "GitHub rejected the fetch or the repository is unavailable.")
        service = GitSourceService(self.registry, self.source_service, failed_fetcher)
        with self.assertRaisesRegex(GitSyncError, "GitHub rejected"):
            service.synchronize("user-1", "sample", "secret-token")
        status = service.status("user-1", "sample")
        self.assertEqual(status["status"], "failed")
        self.assertEqual(status["error_code"], "git_fetch_failed")
        self.assertNotIn("secret-token", str(status))

    def test_internal_http_routes_scope_source_and_redact_sync_token(self):
        used_tokens = []

        def fetcher(repository, branch, token):
            used_tokens.append(token)
            return "d" * 40, self.archived

        service = GitSourceService(self.registry, self.source_service, fetcher)
        app = FastAPI()
        app.include_router(http.git_router)
        client = TestClient(app)
        headers = {"x-internal-secret": "s" * 40, "x-user-id": "user-1"}
        with patch.dict("os.environ", {"RAG_CALLBACK_SECRET": "s" * 40}), \
             patch.object(git_source, "GitSourceService", return_value=service):
            self.assertEqual(client.get("/telemetry/git/projects/sample").status_code, 401)
            status = client.get("/telemetry/git/projects/sample", headers=headers)
            self.assertEqual(status.status_code, 200)
            self.assertNotIn("token", status.text.lower())

            response = client.post("/telemetry/git/projects/sample/sync", headers=headers,
                                   json={"token": "transient-secret"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(used_tokens, ["transient-secret"])
            self.assertNotIn("transient-secret", response.text)

            target = client.get("/telemetry/git/webhook-target?repository=owner%2Frepo",
                                headers={"x-internal-secret": "s" * 40})
            self.assertEqual(target.status_code, 200)
            self.assertEqual(target.json()["project_id"], "sample")
            self.assertEqual(target.json()["user_id"], "user-1")


if __name__ == "__main__":
    unittest.main()
