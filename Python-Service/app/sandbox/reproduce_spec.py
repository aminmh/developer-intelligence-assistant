"""ReproduceSpec — validated definition of an untrusted project reproduction.

Path discipline: every project path must resolve inside JOBS_ROOT (env var,
default ``/jobs``). The orchestrator and the nested sandbox daemon mount the
same named volume at the same path, so a path that resolves on the
orchestrator resolves identically on the daemon that actually executes the
job. Without this discipline, a host bind-mount path embedded in a compose
config would silently resolve against the *inner* daemon's filesystem and
mount an empty directory — the reproduction would then succeed against
nothing.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


class ProjectRootError(ValueError):
    """Raised when a project path escapes (or is not inside) JOBS_ROOT."""


def jobs_root() -> Path:
    """The single source of truth for job storage on this host.

    Mirrors ``JOBS_ROOT`` from the environment; defaults to ``/jobs`` so the
    same default holds in the orchestrator and the nested sandbox daemon.
    """
    return Path(os.environ.get("JOBS_ROOT", "/jobs"))


@dataclass(frozen=True)
class ReproduceSpec:
    """All inputs needed to reproduce an untrusted project in a sandbox.

    ``project_root`` is the orchestrator-side path to the project directory.
    It must be JOBS_ROOT-relative after symlink resolution; anything else (an
    absolute path elsewhere, a symlink that escapes JOBS_ROOT, or JOBS_ROOT
    itself) is rejected so the nested daemon can never be pointed at a
    directory that does not exist inside its own mount of JOBS_ROOT.
    """

    project_id: str
    project_root: str
    command: list[str] = field(default_factory=list)
    read_only: bool = True
    cap_drop: tuple[str, ...] = ("ALL",)
    pids_limit: int = 256
    mem_limit: str = "1g"
    tmpfs: tuple[str, ...] = ("/tmp",)

    def __post_init__(self) -> None:
        # Reject absolute project_id and ../ tricks before they can escape
        # JOBS_ROOT in compose generation.
        if Path(self.project_id).is_absolute() or ".." in Path(self.project_id).parts:
            raise ProjectRootError(
                f"project_id must be a relative path, got {self.project_id!r}"
            )
        self._validate_project_root()

    def _validate_project_root(self) -> None:
        root = jobs_root().resolve()
        candidate = Path(self.project_root).resolve()
        if candidate == root:
            raise ProjectRootError(
                f"project_root must be a subdirectory of JOBS_ROOT ({root})"
            )
        try:
            candidate.relative_to(root)
        except ValueError:
            raise ProjectRootError(
                f"project_root {self.project_root!r} resolves to {candidate}, "
                f"which is outside JOBS_ROOT ({root}). All project paths must "
                "be JOBS_ROOT-relative so the nested sandbox daemon (which "
                "mounts the same volume at the same path) resolves them "
                "identically."
            ) from None

    @property
    def sandbox_project_root(self) -> str:
        """Project path as seen by the sandbox daemon.

        Identical to the resolved JOBS_ROOT-relative path on the orchestrator
        because both sides mount the jobs volume at JOBS_ROOT.
        """
        root = jobs_root().resolve()
        return str(root / Path(self.project_root).resolve().relative_to(root))
