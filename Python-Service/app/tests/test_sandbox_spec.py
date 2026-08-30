"""JOBS_ROOT path-discipline guard + compose generation tests.

The guard is the fix for the nested-daemon path bug: a host bind-mount path
embedded in a compose config silently resolves against the *inner* daemon's
filesystem and mounts an empty directory. Every project path must therefore
be JOBS_ROOT-relative after symlink resolution.
"""


import pytest

from sandbox.compose_generator import build_compose
from sandbox.reproduce_spec import ProjectRootError, ReproduceSpec, jobs_root


@pytest.fixture
def jobs(tmp_path, monkeypatch):
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()
    monkeypatch.setenv("JOBS_ROOT", str(jobs_dir))
    return jobs_dir


def test_jobs_root_defaults_to_jobs(monkeypatch):
    monkeypatch.delenv("JOBS_ROOT", raising=False)
    assert str(jobs_root()) == "/jobs"


def test_valid_project_root_inside_jobs(jobs):
    project = jobs / "p1"
    project.mkdir()
    spec = ReproduceSpec(project_id="p1", project_root=str(project))
    assert spec.sandbox_project_root == str(jobs / "p1")


def test_project_root_equal_to_jobs_root_rejected(jobs):
    with pytest.raises(ProjectRootError):
        ReproduceSpec(project_id="p1", project_root=str(jobs))


def test_project_root_outside_jobs_rejected(jobs, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(ProjectRootError):
        ReproduceSpec(project_id="p1", project_root=str(outside))


def test_symlink_escape_rejected(jobs, tmp_path):
    """A project_root that resolves through a symlink out of JOBS_ROOT must
    be rejected even though the literal path sits inside JOBS_ROOT."""
    outside = tmp_path / "secret"
    outside.mkdir()
    link = jobs / "escape"
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ProjectRootError):
        ReproduceSpec(project_id="escape", project_root=str(link))


def test_project_id_with_dotdot_rejected(jobs):
    with pytest.raises(ProjectRootError):
        ReproduceSpec(project_id="../escape", project_root=str(jobs / "x"))


def test_absolute_project_id_rejected(jobs):
    with pytest.raises(ProjectRootError):
        ReproduceSpec(project_id="/etc", project_root=str(jobs / "x"))


def test_compose_uses_jobs_root_derived_source(jobs):
    project = jobs / "p1"
    project.mkdir()
    spec = ReproduceSpec(
        project_id="p1",
        project_root=str(project),
        command=["python", "run.py"],
    )
    doc = build_compose(spec)
    job = doc["services"]["job"]
    assert job["runtime"] == "runsc"
    assert job["volumes"] == [f"{jobs / 'p1'}:/work:ro"]
    assert job["read_only"] is True
    assert job["cap_drop"] == ["ALL"]
    assert job["pids_limit"] == 256
    assert job["mem_limit"] == "1g"
    assert job["tmpfs"] == ["/tmp"]


def test_compose_read_write_mount_when_read_only_false(jobs):
    project = jobs / "p1"
    project.mkdir()
    spec = ReproduceSpec(
        project_id="p1", project_root=str(project), read_only=False
    )
    assert build_compose(spec)["services"]["job"]["volumes"] == [
        f"{jobs / 'p1'}:/work"
    ]


def test_compose_source_escapes_rejected(jobs):
    """project_id that would place the mount source outside JOBS_ROOT must be
    rejected at compose time, even if the spec was constructed elsewhere."""
    spec = ReproduceSpec(project_id="ok", project_root=str(jobs / "ok"))
    # Mutate the frozen field through object.__setattr__ to simulate a spec
    # built by code that bypassed the dataclass (defensive path).
    object.__setattr__(spec, "project_id", "../../etc")
    with pytest.raises(ProjectRootError):
        build_compose(spec)
