"""Replay operations of one check: probe sets, settings, runs, and the report refresh after a run.

Contract: @.archcore/regression/regression-check.spec.md clauses 10-18. Targets are the user's saved
benchmark targets of the project (``PerformanceStore``); a Bearer token comes with the request and
is never stored.
"""

from __future__ import annotations

from typing import Any, Callable

from regression.probes import ProbeError, build_from_routes, hint_excerpts, import_requests, validate_probes
from regression.recordings import Recordings
from regression.replay import ReplayError, merge, span_deltas, span_summary
from regression.store import RegressionStore

MAX_RESET_PATH = 200


def _targets() -> Any:
    from code_quality.performance import PerformanceStore

    return PerformanceStore()


def target_for(user_id: str, project_id: str, target_id: Any, targets: Any = None) -> dict[str, Any]:
    if not isinstance(target_id, str) or not target_id:
        raise ProbeError("Choose a saved target.")
    target = (targets or _targets()).get_target(user_id, project_id, target_id)
    if target is None:
        raise LookupError("The target was not found for this project.")
    if target.get("unassigned"):
        raise ProbeError("Save this target under the current project before using it for replay.")
    return target


def save_settings(store: RegressionStore, project_id: str, check_id: str, value: Any) -> dict[str, Any]:
    """Targets, the disposable flag, a reset path and ignore rules (developer edits)."""
    from code_quality.performance import PerformanceError, validate_route
    from regression.diff import compile_rules

    if not isinstance(value, dict):
        raise ProbeError("Replay settings must be an object.")
    settings: dict[str, Any] = {"disposable": bool(value.get("disposable")), "reset_path": None, "ignore": []}
    for key in ("baseline_target_id", "candidate_target_id"):
        if isinstance(value.get(key), str) and len(value[key]) <= 100:
            settings[key] = value[key]
    if value.get("reset_path"):
        try:
            settings["reset_path"] = validate_route(str(value["reset_path"])[:MAX_RESET_PATH])
        except PerformanceError as exc:
            raise ProbeError(str(exc)) from exc
    rules = value.get("ignore") if isinstance(value.get("ignore"), list) else []
    if len(rules) > 50 or len(compile_rules(rules)) != len(rules):
        raise ProbeError("Ignore rules are JSON paths starting with $, such as $.data[*].updated_at; at most 50.")
    settings["ignore"] = rules
    return store.set_settings(project_id, check_id, settings)


def state(store: RegressionStore, project_id: str, check_id: str) -> dict[str, Any]:
    return {"probes": store.get_probes(project_id, check_id), "settings": store.get_settings(project_id, check_id),
            "runs": store.runs(project_id, check_id)}


def _candidate_hints(project_id: str, revision_id: str) -> list[dict[str, Any]]:
    """Excerpts of the files ``PROBE_HINTS`` names on the candidate revision."""
    import io
    import zipfile

    from agent.core.capability import Capability
    from code_quality.framework_evidence import archive_context, collect_capabilities
    from regression.service import _load_revision

    revision = _load_revision(project_id, revision_id)
    archive = revision.archive_loader()
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        names = [info.filename for info in bundle.infolist() if not info.is_dir()]
    with archive_context(archive, names) as context:
        hints = collect_capabilities(context, {Capability.PROBE_HINTS})[Capability.PROBE_HINTS].records
        return hint_excerpts(hints, context.read_text)


def generate(store: RegressionStore, project_id: str, check_id: str, *,
             hints: Callable[[str, str], list[dict[str, Any]]] = _candidate_hints,
             model_call: Callable[..., Any] | None = None) -> dict[str, Any]:
    """Clause 10: probes for the routes of a finished static report; the developer reviews before saving."""
    from regression.equivalence import _provider

    check = store.get(project_id, check_id)
    if check["state"] != "done":
        raise ProbeError("Run the static check first; probes are built from its routes.")
    try:
        excerpts = hints(project_id, check["candidate_revision_id"])
    except Exception:  # noqa: BLE001 — probes can still be built from route templates
        excerpts = []
    return build_from_routes(check["report"].get("routes", []), excerpts, model_call=model_call,
                             provider=_provider(project_id)) | {"hint_files": [e["file"] for e in excerpts]}


def import_into(store: RegressionStore, project_id: str, check_id: str, text: Any, target: dict[str, Any],
                mode: str = "append") -> dict[str, Any]:
    """Clause 11: imported requests appended to (or replacing) the probe set."""
    if not isinstance(text, str) or not text or len(text) > 20 * 1024 * 1024:
        raise ProbeError("Import a HAR file or request list of at most 20 MB.")
    imported = import_requests(text, target["origin"])
    existing = store.get_probes(project_id, check_id) if mode == "append" else []
    probes = validate_probes([*existing, *imported["probes"]][:200])
    store.set_probes(project_id, check_id, probes)
    return {"probes": probes, "imported": len(imported["probes"]), "skipped": imported["skipped"]}


def refresh_report(store: RegressionStore, project_id: str, check_id: str, *,
                   spans: Callable[[str, str, str], Any] = span_summary) -> dict[str, Any]:
    """Merge the newest candidate run into the saved report (clauses 17-18)."""
    check = store.get(project_id, check_id)
    candidate = store.latest_run(project_id, check_id, "candidate")
    if candidate is None or check["state"] != "done":
        return check
    baseline_id = candidate["summary"].get("baseline_run_id")
    deltas = {}
    if baseline_id:
        deltas = span_deltas(spans(project_id, check["base_revision_id"], baseline_id),
                             spans(project_id, check["candidate_revision_id"], candidate["run_id"]))
    report = merge(check["report"], candidate, deltas)
    return store.save(project_id, check_id, state="done", report=report)


def run(store: RegressionStore, user_id: str, project_id: str, check_id: str, body: dict[str, Any], *,
        targets: Any = None, recordings: Recordings | None = None, background: bool = True,
        sender: Any = None) -> dict[str, Any]:
    """Start a baseline or candidate replay with the saved target for that kind."""
    from regression import replay

    kind = body.get("kind")
    if kind not in ("baseline", "candidate"):
        raise ProbeError("kind must be baseline or candidate.")
    settings = store.get_settings(project_id, check_id)
    target = target_for(user_id, project_id, body.get("target_id") or settings.get(f"{kind}_target_id"), targets)
    token = body.get("bearer_token") if target.get("auth_mode") == "bearer" else None
    if target.get("auth_mode") == "bearer" and not (isinstance(token, str) and token):
        raise ProbeError("This target needs its Bearer token.")
    options: dict[str, Any] = {"token": token, "recordings": recordings, "background": background,
                               "on_done": lambda p, c: refresh_report(store, p, c) if kind == "candidate" else None}
    if sender is not None:
        options["sender"] = sender
    return replay.start(store, project_id, check_id, kind, target, **options)


def _base_material(project_id: str, revision_id: str, symbol_ids: list[str]) -> tuple[list[Any], dict[str, Any]]:
    """Test templates and changed-symbol source of the base revision: the tests describe the base version."""
    import io
    import zipfile

    from agent.core.capability import Capability
    from code_quality.framework_evidence import archive_context, collect_capabilities
    from regression.equivalence import symbol_bodies
    from regression.service import _load_revision

    revision = _load_revision(project_id, revision_id)
    archive = revision.archive_loader()
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        names = [info.filename for info in bundle.infolist() if not info.is_dir()]
    with archive_context(archive, names) as context:
        templates = collect_capabilities(context, {Capability.CHARACTERIZATION_TESTS})[Capability.CHARACTERIZATION_TESTS].records
    return list(templates), symbol_bodies(archive, revision.definitions, symbol_ids)


def test_bundle(store: RegressionStore, project_id: str, check_id: str, *,
                material: Callable[[str, str, list[str]], tuple[list[Any], dict[str, Any]]] = _base_material,
                recordings: Recordings | None = None, model_call: Callable[..., Any] | None = None) -> dict[str, Any]:
    """Clauses 19-21: characterization tests and the request file for a finished check."""
    from regression.characterize import build_bundle
    from regression.equivalence import _provider

    check = store.get(project_id, check_id)
    if check["state"] != "done":
        raise ProbeError("Run the static check first; tests are built from its report.")
    recordings = recordings or Recordings()
    ids = [s["canonical_symbol_id"] for s in check["report"].get("symbols", []) if s.get("canonical_symbol_id")]
    notes = []
    try:
        templates, bodies = material(project_id, check["base_revision_id"], ids)
    except Exception:  # noqa: BLE001 — the request file still works without templates
        templates, bodies = [], {}
        notes.append("The base revision's source could not be read, so only the request file is included.")
    bundle = build_bundle(check, store.get_probes(project_id, check_id), store.latest_run(project_id, check_id, "baseline"),
                          lambda run_id, probe_id: recordings.load(project_id, check_id, run_id, probe_id, 1),
                          store.get_settings(project_id, check_id), templates, symbol_bodies=bodies,
                          model_call=model_call, provider=_provider(project_id), notes=notes)
    return {**bundle, "notes": notes, "check_id": check_id}


__all__ = ["ProbeError", "ReplayError", "generate", "test_bundle", "import_into", "refresh_report", "run", "save_settings", "state",
           "target_for"]
