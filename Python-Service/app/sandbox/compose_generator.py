"""Compose configuration for untrusted project reproduction.

Bind-mount sources are always derived from JOBS_ROOT (env, default ``/jobs``)
plus the spec's ``project_id`` — never from a free-form host path. The nested
sandbox daemon mounts the same named volume at the same path, so a source
written here resolves correctly no matter which daemon executes the job.
"""

from __future__ import annotations

import json

from .reproduce_spec import ProjectRootError, ReproduceSpec, jobs_root


def build_compose(spec: ReproduceSpec) -> dict:
    """Return the compose document (as a dict) that runs ``spec`` under runsc.

    The service intentionally mirrors the hardening the rest of the pipeline
    relies on: read_only rootfs, ALL capabilities dropped, tmpfs mounts,
    pids_limit and mem_limit. Do not weaken these to make a backend work.
    """
    root = jobs_root().resolve()
    # Resolve before checking: is_relative_to is purely lexical and would
    # accept project_id like "../../etc" whose parts merely share the prefix.
    candidate = (root / spec.project_id).resolve()
    if candidate == root or not candidate.is_relative_to(root):
        raise ProjectRootError(
            f"project_id {spec.project_id!r} escapes JOBS_ROOT ({root})"
        )
    source = candidate

    command = spec.command or ["/bin/sh"]
    return {
        "services": {
            "job": {
                "image": "alpine:3.20.3",
                "command": command,
                "working_dir": "/work",
                "volumes": [
                    f"{source}:/work:ro" if spec.read_only else f"{source}:/work"
                ],
                "runtime": "runsc",
                "read_only": spec.read_only,
                "cap_drop": list(spec.cap_drop),
                "tmpfs": list(spec.tmpfs),
                "pids_limit": spec.pids_limit,
                "mem_limit": spec.mem_limit,
            }
        }
    }


def render_compose(spec: ReproduceSpec) -> str:
    """Render ``build_compose`` as a compose document string.

    JSON is emitted (compose accepts it natively) so no YAML dependency is
    needed.
    """
    return json.dumps(build_compose(spec), indent=2)
