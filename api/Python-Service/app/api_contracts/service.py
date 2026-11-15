"""Bring contract documents into the store from their three sources.

Spec: @.archcore/frontend-assist/api-contract-sources-and-versions.spec.md.

- ``upload``: the user sends the document.
- ``repo_path``: the document is a file inside the owning (back-end) project; it is read
  from each new committed or uploaded revision. Working-tree revisions are skipped, so
  a developer's uncommitted edit never becomes the contract other projects follow.
- ``url``: the document is downloaded from a public HTTPS address, on request or on a
  configured interval.
"""

from __future__ import annotations

import io
import logging
import posixpath
import re
import zipfile
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from api_contracts.fetch import FetchFailed, FetchRefused, fetch_contract, validate_url
from api_contracts.model import MAX_DOCUMENT_BYTES, ContractInvalid, load_contract
from api_contracts.store import ContractStore, default_store

log = logging.getLogger(__name__)

_NAME = re.compile(r"^[^\x00-\x1f\x7f]{1,120}$")
_CONTRACT_SUFFIXES = (".json", ".yaml", ".yml")
MIN_REFRESH_MINUTES = 15
MAX_REFRESH_MINUTES = 7 * 24 * 60
_IGNORED_TOP_LEVEL = {"__MACOSX", ".DS_Store"}


class ContractInputError(ValueError):
    """The caller's request is wrong; the message is safe to show."""


def clean_repo_path(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 500:
        raise ContractInputError("Enter the contract file's path inside the project (at most 500 characters).")
    path = posixpath.normpath(value.strip().replace("\\", "/"))
    if path.startswith("/") or path == ".." or path.startswith("../") or path == ".":
        raise ContractInputError("The contract path must be relative to the project root.")
    if not path.lower().endswith(_CONTRACT_SUFFIXES):
        raise ContractInputError("The contract file must end in .json, .yaml, or .yml.")
    return path


def clean_source_input(kind: Any, name: Any, body: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    """Validate a new source's fields and return ``(kind, name, config)``."""
    if kind not in ("upload", "repo_path", "url"):
        raise ContractInputError("kind must be upload, repo_path, or url.")
    if not isinstance(name, str) or not _NAME.match(name.strip()):
        raise ContractInputError("Enter a name of 1 to 120 characters.")
    config: dict[str, Any] = {}
    if kind == "repo_path":
        config["path"] = clean_repo_path(body.get("path"))
    elif kind == "url":
        try:
            config["url"] = validate_url(body.get("url"))[0]
        except FetchRefused as exc:
            raise ContractInputError(str(exc)) from exc
        minutes = body.get("refresh_minutes")
        if minutes is not None:
            if type(minutes) is not int or not MIN_REFRESH_MINUTES <= minutes <= MAX_REFRESH_MINUTES:
                raise ContractInputError("The refresh interval must be between 15 minutes and 7 days.")
            config["refresh_minutes"] = minutes
    return kind, name.strip(), config


def _archive_reader(archive: bytes) -> Callable[[str], bytes | None]:
    """Read one project-relative file from a revision ZIP without extracting it."""
    bundle = zipfile.ZipFile(io.BytesIO(archive))
    entries = {info.filename: info for info in bundle.infolist() if not info.is_dir()}
    top_level = {name.split("/", 1)[0] for name in entries} - _IGNORED_TOP_LEVEL
    # Archives often wrap the project in one folder (repo-main/...).
    wrapper = next(iter(top_level)) + "/" if len(top_level) == 1 and all(
        "/" in name for name in entries if name.split("/", 1)[0] not in _IGNORED_TOP_LEVEL) else ""

    def read(path: str) -> bytes | None:
        info = entries.get(path) or (entries.get(wrapper + path) if wrapper else None)
        if info is None:
            return None
        if (info.external_attr >> 16) & 0o170000 == 0o120000:  # a symbolic link
            return None
        if info.file_size > MAX_DOCUMENT_BYTES:
            raise ContractInvalid([f"{path} exceeds the 10 MB limit."])
        return bundle.read(info)

    return read


class ContractService:
    def __init__(self, store: ContractStore | None = None, *, registry: Any = None, storage: Any = None,
                 fetcher: Callable[[str], bytes] = fetch_contract) -> None:
        self._store = store
        self._registry = registry
        self._storage = storage
        self._fetcher = fetcher

    @property
    def store(self) -> ContractStore:
        if self._store is None:
            self._store = default_store()
        return self._store

    @property
    def registry(self) -> Any:
        if self._registry is None:
            from telemetry.registry import default_registry
            self._registry = default_registry()
        return self._registry

    @property
    def storage(self) -> Any:
        if self._storage is None:
            from index.storage import storage
            self._storage = storage
        return self._storage

    # ── ingestion ────────────────────────────────────────────────────────────

    def _ingest(self, source: dict[str, Any], data: bytes, *, revision_id: str | None = None,
                resolver: Callable[[str], bytes | None] | None = None, base_path: str = "") -> dict[str, Any]:
        try:
            model, digest = load_contract(data, resolver=resolver, base_path=base_path)
        except ContractInvalid as exc:
            attempt = self.store.record_failure(source["source_id"], "failed", exc.errors, revision_id)
            return {"outcome": "failed", "version": attempt}
        version, created = self.store.record_valid(source["source_id"], model, digest, revision_id)
        return {"outcome": "created" if created else "unchanged", "version": version}

    def ingest_upload(self, source_id: str, data: bytes) -> dict[str, Any]:
        source = self.store.get_source(source_id)
        if source["kind"] != "upload":
            raise ContractInputError("Only an upload contract accepts an uploaded document.")
        if len(data) > MAX_DOCUMENT_BYTES:
            raise ContractInputError("The document exceeds the 10 MB limit.")
        return self._ingest(source, data)

    def ingest_revision(self, source: dict[str, Any], revision: dict[str, Any]) -> dict[str, Any]:
        """Read a repo_path source's file from one ready revision of its project."""
        revision_id = revision.get("revision_id")
        path = source["config"]["path"]
        reference = revision.get("source_ref") or revision.get("commit_sha")
        key = self.registry.archive_key(source["project_id"], reference) if reference else None
        if not key:
            attempt = self.store.record_failure(
                source["source_id"], "failed", ["The revision's source archive is no longer available."], revision_id)
            return {"outcome": "failed", "version": attempt}
        try:
            reader = _archive_reader(self.storage.get_snapshot_archive(key))
            data = reader(path)
        except ContractInvalid as exc:
            return {"outcome": "failed",
                    "version": self.store.record_failure(source["source_id"], "failed", exc.errors, revision_id)}
        except (zipfile.BadZipFile, OSError, RuntimeError) as exc:
            log.warning("contract_archive_unreadable source=%s error_type=%s", source["source_id"], type(exc).__name__)
            attempt = self.store.record_failure(
                source["source_id"], "failed", ["The revision's source archive could not be read."], revision_id)
            return {"outcome": "failed", "version": attempt}
        if data is None:
            attempt = self.store.record_failure(
                source["source_id"], "missing", [f"{path} is not in this revision."], revision_id)
            return {"outcome": "missing", "version": attempt}
        return self._ingest(source, data, revision_id=revision_id, resolver=reader, base_path=path)

    def refresh(self, source_id: str) -> dict[str, Any]:
        """Check a source now: download a URL, or read the path from the project's newest revision."""
        source = self.store.get_source(source_id)
        if source["kind"] == "url":
            try:
                data = self._fetcher(source["config"]["url"])
            except (FetchRefused, FetchFailed) as exc:
                attempt = self.store.record_failure(source_id, "failed", [str(exc)])
                return {"outcome": "failed", "retriable": isinstance(exc, FetchFailed), "version": attempt}
            return self._ingest(source, data)
        if source["kind"] == "repo_path":
            revision = self.registry.latest_ready(source["project_id"], ("upload", "git"))
            if revision is None:
                raise ContractInputError("The project has no indexed revision to read the contract from yet.")
            return self.ingest_revision(source, revision)
        raise ContractInputError("An upload contract changes when you upload a new document.")

    def on_revision_ready(self, project_id: str, source_ref: str) -> int:
        """Called when a revision finishes indexing. Never raises: indexing must not fail because of a contract."""
        handled = 0
        try:
            sources = self.store.list_sources(project_id=project_id, kind="repo_path")
            if not sources:
                return 0
            revision = self.registry.get(project_id, source_ref)
            if revision.get("state") != "ready" or revision.get("source_kind") == "working_tree":
                return 0
            for source in sources:
                try:
                    self.ingest_revision(source, revision)
                    handled += 1
                except Exception as exc:  # noqa: BLE001 — one broken source must not stop the others
                    log.warning("contract_revision_ingest_failed source=%s error_type=%s",
                                source["source_id"], type(exc).__name__)
        except Exception as exc:  # noqa: BLE001
            log.warning("contract_revision_hook_failed project=%s error_type=%s", project_id, type(exc).__name__)
        return handled

    def refresh_due_urls(self, now: datetime | None = None) -> int:
        """Refresh every URL source whose interval has passed. Returns how many were checked."""
        now = now or datetime.now(timezone.utc)
        checked = 0
        for source in self.store.list_sources(kind="url"):
            minutes = source["config"].get("refresh_minutes")
            if not minutes:
                continue
            last = source.get("last_checked_at")
            try:
                due = last is None or datetime.fromisoformat(last) + timedelta(minutes=minutes) <= now
            except ValueError:
                due = True
            if not due:
                continue
            try:
                self.refresh(source["source_id"])
                checked += 1
            except Exception as exc:  # noqa: BLE001 — keep the schedule alive
                log.warning("contract_url_refresh_failed source=%s error_type=%s", source["source_id"], type(exc).__name__)
        return checked

    # ── reading ──────────────────────────────────────────────────────────────

    def describe_source(self, source: dict[str, Any]) -> dict[str, Any]:
        newest = self.store.newest_valid_version(source["source_id"])
        return {**source, "current_version": newest,
                "attachments": self.store.list_attachments(source_id=source["source_id"])}

    def describe_attachment(self, attachment: dict[str, Any]) -> dict[str, Any]:
        source = self.store.get_source(attachment["source_id"])
        version = self.store.resolve(attachment)
        return {**attachment, "source": {key: source[key] for key in ("source_id", "project_id", "kind", "name", "status")},
                "resolved_version": version, "available": version is not None}

    def project_overview(self, project_id: str) -> dict[str, Any]:
        return {
            "project_id": project_id,
            "sources": [self.describe_source(source) for source in self.store.list_sources(project_id=project_id)],
            "attachments": [self.describe_attachment(row) for row in self.store.list_attachments(project_id=project_id)],
        }

    def version_for_revision(self, source_id: str, revision_id: str) -> dict[str, Any]:
        """The contract a named back-end revision carried, or an explicit unavailable state."""
        found = self.store.version_for_revision(source_id, revision_id)
        if found is None:
            return {"available": False, "state": "not_ingested", "version": None}
        if found["state"] != "valid" or not found["version_id"]:
            return {"available": False, "state": found["state"], "version": None}
        return {"available": True, "state": "valid", "version": self.store.get_version(found["version_id"], with_model=False)}


    # ── code generation ──────────────────────────────────────────────────────

    def generate_code(self, source_id: str, version_id: str, target: Any, *, base_url: Any = None,
                      search: Any = None) -> dict[str, Any]:
        """Typed client code or mocks for one valid contract version."""
        from api_contracts.codegen import GenerationError, generate

        version = self.store.get_version(version_id, with_model=True)
        if version["source_id"] != source_id or version["state"] != "valid" or not version.get("model"):
            raise ContractInputError("Choose a valid version of this contract.")
        if not isinstance(target, str) or (base_url is not None and not isinstance(base_url, str)) \
                or (search is not None and not isinstance(search, str)):
            raise ContractInputError("target, base_url, and search must be text.")
        try:
            result = generate(version["model"], target, base_url=base_url, search=search)
        except GenerationError as exc:
            raise ContractInputError(str(exc)) from exc
        return {"source_id": source_id, "version_id": version_id, "title": version["title"],
                "api_version": version["api_version"], **result}

    # ── conformance (@.archcore/frontend-assist/contract-conformance-findings.spec.md) ──

    def _consumer_inputs(self, attachment_id: str, revision_id: str | None, ui_map_loader: Any = None) -> tuple[dict, dict, dict]:
        attachment = self.store.get_attachment(attachment_id)
        if attachment["role"] != "consumer":
            raise ContractInputError("Change impact is for projects that consume the contract.")
        if ui_map_loader is None:
            from projects.frontend.service import ui_map_for_revision as ui_map_loader
        from projects.frontend.service import UiMapUnavailable
        try:
            ui_map = ui_map_loader(attachment["project_id"], revision_id)
        except UiMapUnavailable as exc:
            raise ContractInputError(str(exc)) from exc
        return attachment, self.store.get_source(attachment["source_id"]), ui_map

    @staticmethod
    def _header(attachment: dict, source: dict, ui_map: dict) -> dict[str, Any]:
        return {"project_id": attachment["project_id"], "revision_id": ui_map.get("revision_id"),
                "attachment_id": attachment["attachment_id"], "role": "consumer",
                "contract": {"source_id": source["source_id"], "name": source["name"], "project_id": source["project_id"]},
                "framework": ui_map.get("meta_framework") or ui_map.get("framework")}

    def _provider_conformance(self, attachment_id: str, revision_id: str | None, routes_loader: Any = None) -> dict[str, Any]:
        """Check a back-end revision's routes against the contract it provides."""
        from api_contracts.conformance import provider_report
        from projects.backend_routes import RoutesUnavailable

        if routes_loader is None:
            from projects.backend_routes import routes_for_revision as routes_loader
        attachment = self.store.get_attachment(attachment_id)
        source = self.store.get_source(attachment["source_id"])
        try:
            routes = routes_loader(attachment["project_id"], revision_id)
        except RoutesUnavailable as exc:
            raise ContractInputError(str(exc)) from exc
        header = {"project_id": attachment["project_id"], "revision_id": routes.get("revision_id"),
                  "attachment_id": attachment_id, "role": "provider", "framework": routes.get("framework"),
                  "contract": {"source_id": source["source_id"], "name": source["name"], "project_id": source["project_id"]}}
        # A contract kept in this project's own code is compared with the version read from the same revision.
        version = None
        if source["kind"] == "repo_path" and source["project_id"] == attachment["project_id"] and routes.get("revision_id"):
            own = self.store.version_for_revision(source["source_id"], routes["revision_id"])
            if own and own["state"] == "valid" and own["version_id"]:
                version = self.store.get_version(own["version_id"], with_model=True)
        version = version or self.store.resolve(attachment, with_model=True)
        if version is None:
            return {**header, "available": False, "version": None, "findings": [],
                    "notes": ["This contract has no valid version yet, so there is nothing to check against."]}
        model = version.pop("model")
        return {**header, "available": True, "version": version, **provider_report(routes, model)}

    def runtime_conformance(self, attachment_id: str, revision_id: str | None = None, *,
                            observations_loader: Any = None) -> dict[str, Any]:
        """Compare what the running back end answered (routes and status codes) with the contract it provides."""
        from api_contracts.conformance import runtime_report
        from api_contracts.runtime_observations import RuntimeUnavailable

        attachment = self.store.get_attachment(attachment_id)
        if attachment["role"] != "provider":
            raise ContractInputError("Live traffic is compared for the project that provides the contract.")
        if observations_loader is None:
            from api_contracts.runtime_observations import observed_responses as observations_loader
        source = self.store.get_source(attachment["source_id"])
        revision = (self.registry.get_by_revision_id(attachment["project_id"], revision_id) if revision_id
                    else self.registry.latest_ready(attachment["project_id"]))
        if not revision or revision.get("state") != "ready":
            raise ContractInputError("The project has no indexed revision yet. Index the project first.")
        header = {"project_id": attachment["project_id"], "revision_id": revision["revision_id"],
                  "attachment_id": attachment_id, "role": "provider",
                  "contract": {"source_id": source["source_id"], "name": source["name"], "project_id": source["project_id"]}}
        version = self.store.resolve(attachment, with_model=True)
        if version is None:
            return {**header, "available": False, "version": None, "findings": [],
                    "notes": ["This contract has no valid version yet, so there is nothing to check against."]}
        try:
            observations = observations_loader(attachment["project_id"], revision["revision_id"])
        except RuntimeUnavailable as exc:
            version.pop("model", None)
            return {**header, "available": False, "version": version, "findings": [], "notes": [str(exc)]}
        model = version.pop("model")
        return {**header, "available": True, "version": version, **runtime_report(observations, model)}

    def conformance(self, attachment_id: str, revision_id: str | None = None, *, ui_map_loader: Any = None,
                    routes_loader: Any = None) -> dict[str, Any]:
        """Check a project against its attached contract: a consumer's requests and models, or a provider's routes."""
        from api_contracts.conformance import consumer_report

        if self.store.get_attachment(attachment_id)["role"] == "provider":
            return self._provider_conformance(attachment_id, revision_id, routes_loader)
        attachment, source, ui_map = self._consumer_inputs(attachment_id, revision_id, ui_map_loader)
        header = self._header(attachment, source, ui_map)
        version = self.store.resolve(attachment, with_model=True)
        if version is None:
            return {**header, "available": False, "version": None, "findings": [],
                    "notes": ["This contract has no valid version yet, so there is nothing to check against."]}
        model = version.pop("model")
        return {**header, "available": True, "version": version, **consumer_report(ui_map, model)}

    def impact(self, attachment_id: str, from_version_id: str | None = None, to_version_id: str | None = None,
               revision_id: str | None = None, *, ui_map_loader: Any = None) -> dict[str, Any]:
        """What moving the attachment from one contract version to another touches in the consumer project.

        Defaults: from the version the attachment uses now, to the source's newest valid version. When those
        are the same, the comparison is the newest version against the one before it.
        """
        from api_contracts.conformance import impact_report

        attachment, source, ui_map = self._consumer_inputs(attachment_id, revision_id, ui_map_loader)
        header = self._header(attachment, source, ui_map)
        valid = [row for row in self.store.list_versions(source["source_id"]) if row["state"] == "valid"]

        def pick(version_id: str) -> dict[str, Any]:
            if not any(row["version_id"] == version_id for row in valid):
                raise ContractInputError("Choose two valid versions of this contract.")
            return self.store.get_version(version_id, with_model=True)

        if not valid:
            return {**header, "available": False, "entries": [], "versions": [],
                    "notes": ["This contract has no valid version yet."]}
        current = self.store.resolve(attachment)
        newer = pick(to_version_id) if to_version_id else self.store.get_version(valid[0]["version_id"], with_model=True)
        if from_version_id:
            older = pick(from_version_id)
        elif current and current["version_id"] != newer["version_id"]:
            older = self.store.get_version(current["version_id"], with_model=True)
        else:
            position = next(i for i, row in enumerate(valid) if row["version_id"] == newer["version_id"])
            if position + 1 >= len(valid):
                return {**header, "available": False, "entries": [], "versions": valid,
                        "notes": ["This contract has only one version, so there is no change to assess yet."]}
            older = self.store.get_version(valid[position + 1]["version_id"], with_model=True)
        old_model, new_model = older.pop("model"), newer.pop("model")
        return {**header, "available": True, "from_version": older, "to_version": newer, "versions": valid,
                **impact_report(ui_map, old_model, new_model)}


contract_service = ContractService()
