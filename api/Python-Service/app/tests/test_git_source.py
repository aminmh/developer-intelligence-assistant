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
from telemetry.git_source import GitSourceService, GitSyncError, validate_branch, validate_repository
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

    def test_sync_creates_revision_and_queues_without_deployment(self):
        result = self.git.synchronize("user-1", "sample", "read-only-token")
        self.assertEqual(result["state"], "queued")
        self.assertRegex(result["commit_sha"], r"^[0-9a-f]{40}$")
        self.assertEqual(self.fetches, [("owner/repo", "main", "read-only-token")])
        self.assertEqual(self.registry.claim_job(), ("sample", "a" * 40))
        self.registry.finish_job("sample", "a" * 40, success=True)
        self.assertEqual(self.git.status("user-1", "sample")["status"], "ready")

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
