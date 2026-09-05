"""Phase 2 tests — sandbox executor (two-tier, image-resolver policy).

Docker daemon traffic is fully mocked: DockerApi is monkeypatched with a fake
recording calls, and runtime config is pointed at a small fixture yml. No real
containers are ever created.
"""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

import pytest

from sandbox import executor as ex
from sandbox.executor import SandboxExecutor

FIXTURE_YML = """\
runtimes:
  php:
    image: composer:2
    installer:
      - composer
      - install
      - --no-interaction
      - --prefer-dist
      - --no-dev
    probes:
      routes:
        - php
        - artisan
        - route:list
        - --json
      schedule:
        - php
        - artisan
        - schedule:list
"""


class FakeDockerApi:
    """Scriptable stand-in for DockerApi."""

    def __init__(self) -> None:
        self.images = ["composer:2"]
        self.pull_ok = False
        self.run_result = {"status": "done", "exit_code": 0, "stdout": "[]"}
        self.calls: list[str] = []
        self.created: list[dict] = []

    def ping(self) -> bool:
        return True

    def list_images(self) -> list[dict]:
        return [{"Id": "sha256:x", "RepoTags": list(self.images)}]

    def image_exists(self, reference: str) -> bool:
        repo, _, tag = reference.partition(":")
        for rt in self.images:
            r, _, t = rt.rpartition(":")
            if r == repo and (t == tag or t == "latest"):
                return True
        return False

    def pull_image(self, reference: str, timeout: float = 300.0) -> bool:
        self.calls.append(f"pull:{reference}")
        if self.pull_ok:
            self.images.append(reference)
        return self.pull_ok

    def create_container(self, spec: dict) -> str:
        self.created.append(spec)
        return f"cid-{len(self.created)}"

    def start(self, container_id: str) -> None:
        self.calls.append(f"start:{container_id}")

    def wait(self, container_id: str) -> int:
        return self.run_result.get("exit_code", 0)

    def logs(self, container_id: str) -> str:
        return self.run_result.get("stdout", "")

    def remove(self, container_id: str, force: bool = True) -> None:
        self.calls.append(f"rm:{container_id}")

    def put_archive(self, container_id: str, path: str, tar_bytes: bytes) -> None:
        self.calls.append(f"put:{path}")
        self.tar_bytes = tar_bytes

    def run_once(self, **kw: object) -> dict:
        self.calls.append(f"run:{kw.get('image')}:{' '.join(kw.get('argv') or [])}")
        self.last_run = kw
        return dict(self.run_result)


@pytest.fixture
def fake_api(monkeypatch, tmp_path):
    fake = FakeDockerApi()
    monkeypatch.setattr(ex, "DockerApi", lambda *a, **k: fake)
    cfg = tmp_path / "runtimes.yml"
    cfg.write_text(FIXTURE_YML, encoding="utf-8")
    yield ex, SandboxExecutor(docker_host="tcp://sandbox-runner:2375",
                              runtime_config_path=str(cfg))


def _workspace(tmp_path) -> Path:
    root = tmp_path / "proj"
    root.mkdir(parents=True)
    (root / "composer.json").write_text(json.dumps({"name": "x/test"}), encoding="utf-8")
    (root / "routes").mkdir()
    (root / "routes" / "web.php").write_text("<?php\n", encoding="utf-8")
    return root


class TestRuntimeConfig:
    def test_fixture_parses(self, fake_api):
        _, executor = fake_api
        rt = executor.runtime_for("php")
        assert rt["image"] == "composer:2"
        assert rt["installer"][0] == "composer"
        assert rt["probes"]["routes"] == ["php", "artisan", "route:list", "--json"]

    def test_default_config_loads(self):
        rt = ex.load_runtime_yml()
        assert "php" in rt["runtimes"]
        assert rt["runtimes"]["php"]["probes"]["routes"]

    def test_missing_framework(self, fake_api):
        _m, executor = fake_api
        assert executor.runtime_for("cobol") is None


class TestStaging:
    def test_stages_project_tree(self, fake_api, tmp_path):
        ex_module, executor = fake_api
        root = _workspace(tmp_path)
        src = executor.stage_project(str(root), "proj-1")
        fake = ex_module.DockerApi()
        assert src.endswith("/jobs/proj-1/src")
        assert any(c.startswith("put:/jobs") for c in fake.calls)
        # tar content check: entries prefixed proj-1/src/
        names = []
        with tarfile.open(fileobj=io.BytesIO(fake.tar_bytes)) as tar:
            names = tar.getnames()
        assert "proj-1/src/composer.json" in names
        assert "proj-1/src/routes/web.php" in names

    def test_unsafe_project_id_rejected(self, fake_api):
        _m, executor = fake_api
        with pytest.raises(ValueError):
            executor.stage_project("/tmp/x", "../escape")
        with pytest.raises(ValueError):
            executor.stage_project("/tmp/x", "a/b")


class TestInstallTier:
    def test_install_done_when_image_present(self, fake_api, tmp_path):
        ex_module, executor = fake_api
        result = executor.install_dependencies("proj-1", "php")
        assert result["status"] == "done"
        assert result["exit_code"] == 0
        fake = ex_module.DockerApi()
        # tier-1 runs a cache-warm-and-offline-install shell wrapper
        assert any(c.startswith("run:composer:2:sh -c") for c in fake.calls)
        run_call = next(c for c in fake.calls if c.startswith("run:composer:2:sh -c"))
        assert "composer install" in run_call
        assert "COMPOSER_DISABLE_NETWORK=1" in run_call
        # writable bind (no :ro), runsc NOT requested for install tier
        hc = fake.last_run["host_config"]
        assert hc["Binds"] == [f"{ex.jobs_root()}/proj-1/src:/app"]
        assert "Runtime" not in hc

    def test_install_unavailable_without_image_or_egress(self, fake_api):
        ex_module, executor = fake_api
        fake = ex_module.DockerApi()
        fake.images = []  # composer:2 gone; pull disabled
        result = executor.install_dependencies("proj-1", "php")
        assert result["status"] == "unavailable"
        assert "no egress" in result["reason"]
        assert fake.calls[-1] == "pull:composer:2"

    def test_install_pulls_when_egress_exists(self, fake_api):
        ex_module, executor = fake_api
        fake = ex_module.DockerApi()
        fake.images = []
        fake.pull_ok = True
        result = executor.install_dependencies("proj-1", "php")
        assert result["status"] == "done"  # pulled then ran


class TestProbeTier:
    def test_probe_runs_hardened_runsc(self, fake_api):
        ex_module, executor = fake_api
        record = executor.run_probe("proj-1", "routes", "php")
        assert record["status"] == "done"
        assert record["exit_code"] == 0
        fake = ex_module.DockerApi()
        hc = fake.last_run["host_config"]
        assert hc["Runtime"] == "runsc"
        assert hc["Binds"] == [f"{ex.jobs_root()}/proj-1/src:/app:ro"]
        assert hc["NetworkMode"] == "none"
        assert hc["ReadonlyRootfs"] is True
        assert hc["CapDrop"] == ["ALL"]
        env = {e.split("=", 1)[0] for e in fake.last_run["env"]}
        assert {"APP_ENV", "DB_CONNECTION", "QUEUE_CONNECTION"} <= env

    def test_probe_unavailable_when_image_missing(self, fake_api):
        ex_module, executor = fake_api
        ex_module.DockerApi().images = []
        record = executor.run_probe("proj-1", "routes", "php")
        assert record["status"] == "unavailable"
        assert "not pulled" in record["error"]

    def test_probe_free_text_argv_override(self, fake_api):
        _, executor = fake_api
        record = executor.run_probe(
            "proj-1", "custom", "php",
            argv=["php", "artisan", "about"],
        )
        assert record["command"] == ["php", "artisan", "about"]
        assert record["name"] == "custom"

    def test_probe_missing_template_skipped(self, fake_api):
        _m, executor = fake_api
        record = executor.run_probe("proj-1", "nope", "php")
        assert record["status"] == "skipped"


class TestAnalyzeDriver:
    def test_full_flow(self, fake_api, tmp_path):
        _, executor = fake_api
        root = _workspace(tmp_path)
        out = executor.analyze(str(root), "proj-1", "php")
        assert out["staged"] is True
        assert out["install"]["status"] == "done"
        assert [p["name"] for p in out["probes"]] == ["routes", "schedule"]
        assert all(p["status"] == "done" for p in out["probes"])
        assert out["runtime_image"] == "composer:2"

    def test_probes_skipped_on_install_failure(self, fake_api, tmp_path):
        ex_module, executor = fake_api
        ex_module.DockerApi().run_result = {"status": "done", "exit_code": 7,
                                            "stdout": "composer error"}
        out = executor.analyze(str(_workspace(tmp_path)), "proj-1", "php")
        assert out["install"]["exit_code"] == 7
        assert all(p["status"] == "skipped" for p in out["probes"])
        assert "exit 7" in out["probes"][0]["reason"]

    def test_stage_failure_reported(self, fake_api, tmp_path):
        ex_module, executor = fake_api

        def boom_create(spec):
            raise RuntimeError("daemon down")
        ex_module.DockerApi().create_container = boom_create
        out = executor.analyze(str(_workspace(tmp_path)), "proj-1", "php")
        assert out["staged"] is False
        assert out["install"]["status"] == "failed"
        assert "stage" in out["install"]["error"]


class TestShellCommand:
    def _staged_fake(self, ex_module, stdout: str = "yes"):
        fake = ex_module.DockerApi()
        fake.run_result = {"status": "done", "exit_code": 0, "stdout": stdout}
        return fake

    def test_run_command_shell_semantics(self, fake_api):
        ex_module, executor = fake_api
        fake = self._staged_fake(ex_module)
        record = executor.run_command("proj-1", "php artisan app:code-parse --json", framework="php")
        assert record["status"] == "done"
        assert record["command"] == "php artisan app:code-parse --json"
        assert record["image"] == "composer:2"
        assert record["working_dir"] == "/app"
        # sh -c wrapper, writable bind, networked (no runsc), hardened caps
        run_call = next(c for c in fake.calls if c.startswith("run:composer:2:sh -c"))
        assert "php artisan app:code-parse --json" in run_call
        hc = fake.last_run["host_config"]
        assert hc["Binds"] == [f"{ex.jobs_root()}/proj-1/src:/app"]
        assert "Runtime" not in hc
        assert hc["ReadonlyRootfs"] is False
        assert hc["CapDrop"] == ["ALL"]
        env = fake.last_run["env"]
        assert env["APP_ENV"] == "testing" and env["DB_CONNECTION"] == "sqlite"
        # project-staged liveness check ran against alpine first
        assert any(c.startswith("run:alpine:3.20.3:sh -c") for c in fake.calls)

    def test_run_command_honours_staged_rel_dir(self, fake_api):
        ex_module, executor = fake_api
        self._staged_fake(ex_module)
        executor._rel["proj-1"] = "Archive"
        record = executor.run_command("proj-1", "ls -la", framework="php")
        assert record["working_dir"] == "/app/Archive"

    def test_run_command_skipped_when_not_staged(self, fake_api):
        ex_module, executor = fake_api
        # daemon has no staged source → alpine liveness exits non-zero
        ex_module.DockerApi().run_result = {"status": "done", "exit_code": 1, "stdout": ""}
        record = executor.run_command("proj-1", "ls", framework="php")
        assert record["status"] == "skipped"
        assert "not staged" in record["reason"]
        # only the alpine liveness container ran — never the composer image shell
        assert not any(c.startswith("run:composer:2:") for c in ex_module.DockerApi().calls)

    def test_run_command_no_runtime_framework(self, fake_api):
        _, executor = fake_api
        record = executor.run_command("proj-1", "ls", framework="cobol")
        assert record["status"] == "skipped"
        assert "cobol" in record["reason"]

    def test_project_staged_liveness(self, fake_api):
        ex_module, executor = fake_api
        self._staged_fake(ex_module)
        assert executor.project_staged("proj-1") is True
        ex_module.DockerApi().run_result = {"status": "done", "exit_code": 1, "stdout": ""}
        assert executor.project_staged("proj-1") is False

    def test_liveness_trusts_exit_code_over_logs(self, fake_api):
        # Tiny containers can exit before logs are readable — exit 0 with
        # empty stdout must still count as staged.
        ex_module, executor = fake_api
        ex_module.DockerApi().run_result = {"status": "done", "exit_code": 0, "stdout": ""}
        assert executor.project_staged("proj-1") is True

    def test_run_command_discovery_wrapper_when_rel_unknown(self, fake_api):
        ex_module, executor = fake_api
        self._staged_fake(ex_module)
        record = executor.run_command("proj-1", "php artisan app:code-parse --json", framework="php")
        run_call = next(c for c in ex_module.DockerApi().calls if c.startswith("run:composer:2:sh -c"))
        assert "cd /app || exit 9" in run_call
        assert "composer.json" in run_call
        assert record["working_dir"] == "/app"
