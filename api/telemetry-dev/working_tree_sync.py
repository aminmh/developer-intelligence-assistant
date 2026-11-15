"""Desktop helper side of working-tree sync (revision-sync plan phase 6). Stdlib only.

The browser hands the helper a grant for one project. The helper asks the user, in native
dialogs, to choose a folder and approve syncing it; the grant token goes to the OS keychain
and the folder to the helper's state file. A watcher thread then polls each granted folder
(mtime and size scan) and, after 30 seconds without changes and at least 3 minutes since
the last upload, packages it as a deterministic ZIP and uploads it to the web app with the
grant. An unchanged tree is never re-uploaded. A 401 or 403 (revoked grant, or the granting
user lost access) stops that folder's sync.

Packaging: ``git ls-files -co --exclude-standard`` in a Git folder (tracked plus untracked,
honouring .gitignore), otherwise a walk with common dependency/build folders skipped; secret
files are always left out (the same rules as the code tools); entries are sorted with fixed
timestamps and permissions so identical trees give identical bytes. The folder's ``HEAD``
is sent as lineage metadata only.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
QUIET_SECONDS = 30
MIN_UPLOAD_INTERVAL_SECONDS = 180
POLL_SECONDS = 5
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_FILES = 20_000
KEYCHAIN_SERVICE = "devassistant-working-tree"
GRANT_ID = re.compile(r"wtg_[0-9a-f]{32}")
PROJECT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
SKIPPED_DIRS = {".git", "node_modules", "vendor", "dist", "build", "target", "__pycache__", ".venv", "venv",
                ".idea", ".vscode", ".next", ".nuxt", "coverage", ".cache", ".gradle", "bin", "obj"}
FIXED_TIME = (1980, 1, 1, 0, 0, 0)

_lock = threading.Lock()
_watcher: threading.Thread | None = None


class SyncError(RuntimeError):
    pass


def _secret_rule():
    """The server's secret-file rule (agent_tools.source.is_secret_path), so both sides agree."""
    app = ROOT / "api" / "Python-Service" / "app"
    if str(app) not in sys.path:
        sys.path.insert(0, str(app))
    from agent_tools.source import is_secret_path

    return is_secret_path


# --- state and keychain -------------------------------------------------------------------

def state_path() -> Path:
    base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return base / "devassistant" / "runtime" / "working-tree.json"


def read_grants() -> list[dict]:
    try:
        data = json.loads(state_path().read_text())
    except (OSError, ValueError):
        return []
    grants = data.get("grants") if isinstance(data, dict) else None
    return [g for g in grants if isinstance(g, dict) and GRANT_ID.fullmatch(str(g.get("grant_id", "")))] \
        if isinstance(grants, list) else []


def write_grants(grants: list[dict]) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_suffix(".tmp")
    staging.write_text(json.dumps({"grants": grants}, indent=2))
    os.chmod(staging, 0o600)
    os.replace(staging, path)


def _update_grant(grant_id: str, **changes) -> None:
    with _lock:
        grants = read_grants()
        for grant in grants:
            if grant["grant_id"] == grant_id:
                grant.update(changes)
        write_grants(grants)


def save_token(grant_id: str, token: str) -> None:
    import runtime_ssh_credentials as store

    value = {"token": token}
    if store._provider() == "keychain":
        store._keychain_request("store", value, service=KEYCHAIN_SERVICE, account=grant_id)
        return
    result = store._run(["secret-tool", "store", "--label=Developer Assistant working-tree grant",
                         "app", grant_id, "purpose", "working-tree-grant"],
                        input_data=json.dumps(value).encode())
    if result.returncode:
        raise SyncError("The grant could not be saved to the OS keyring. Unlock it and retry.")


def read_token(grant_id: str) -> str | None:
    import runtime_ssh_credentials as store

    if store._provider() == "keychain":
        response = store._keychain_request("read", service=KEYCHAIN_SERVICE, account=grant_id)
        value = response.get("value") if response.get("found") is True else None
    else:
        result = store._run(["secret-tool", "lookup", "app", grant_id, "purpose", "working-tree-grant"])
        try:
            value = json.loads(result.stdout.decode()) if not result.returncode else None
        except ValueError:
            value = None
    token = value.get("token") if isinstance(value, dict) else None
    return token if isinstance(token, str) and token else None


def remove_token(grant_id: str) -> None:
    import runtime_ssh_credentials as store

    try:
        if store._provider() == "keychain":
            store._keychain_request("delete", service=KEYCHAIN_SERVICE, account=grant_id)
        else:
            store._run(["secret-tool", "clear", "app", grant_id, "purpose", "working-tree-grant"])
    except Exception:  # noqa: BLE001 — the grant is revoked server-side either way
        pass


# --- folder choice ------------------------------------------------------------------------

def choose_folder(project_id: str) -> Path:
    """A native folder chooser; the user's choice, never a path from the browser."""
    prompt = f"Choose the folder to sync to the Developer Assistant project “{project_id}”"
    if sys.platform == "darwin" and shutil.which("osascript"):
        command = ["osascript", "-e", "on run argv\nactivate\nreturn POSIX path of (choose folder with prompt (item 1 of argv))\nend run", prompt]
    elif (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")) and shutil.which("zenity"):
        command = ["zenity", "--file-selection", "--directory", "--title", prompt]
    else:
        raise SyncError("No desktop session is available to choose a folder.")
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired as exc:
        raise SyncError("Choosing a folder timed out; nothing was synced.") from exc
    folder = result.stdout.strip()
    if result.returncode != 0 or not folder:
        raise SyncError("No folder was chosen; nothing was synced.")
    path = Path(folder).expanduser().resolve()
    if not path.is_dir() or path == Path.home() or path == Path("/"):
        raise SyncError("Choose a project folder, not your home folder or the disk root.")
    return path


# --- packaging ----------------------------------------------------------------------------

def _git(folder: Path, *args: str) -> subprocess.CompletedProcess | None:
    if not shutil.which("git"):
        return None
    try:
        return subprocess.run(["git", "-C", str(folder), *args], capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None


def list_files(folder: Path) -> list[str]:
    """Relative POSIX paths to package, sorted; secrets and symlinks never included."""
    is_secret = _secret_rule()
    listed = _git(folder, "ls-files", "-co", "--exclude-standard", "-z")
    if listed is not None and listed.returncode == 0:
        names = [n for n in listed.stdout.decode("utf-8", "replace").split("\0") if n]
    else:
        names = []
        for dirpath, dirnames, filenames in os.walk(folder):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIPPED_DIRS and not d.startswith(".") or d in {".github"})
            for name in filenames:
                names.append(Path(dirpath, name).relative_to(folder).as_posix())
    result = []
    for name in names:
        path = folder / name
        if (is_secret(name) or path.is_symlink() or not path.is_file()
                or any(part in SKIPPED_DIRS for part in Path(name).parts[:-1])):
            continue
        result.append(name)
        if len(result) > MAX_FILES:
            raise SyncError(f"The folder has more than {MAX_FILES} files to sync.")
    return sorted(set(result))


def tree_signature(folder: Path, names: list[str]) -> str:
    """Cheap change detector: path, size and mtime of every packaged file."""
    digest = hashlib.sha256()
    for name in names:
        try:
            info = (folder / name).stat()
        except OSError:
            continue
        digest.update(f"{name}\0{info.st_size}\0{info.st_mtime_ns}\n".encode())
    return digest.hexdigest()


def package(folder: Path, names: list[str]) -> bytes:
    """Deterministic ZIP: sorted entries, fixed time and permissions."""
    buffer = io.BytesIO()
    total = 0
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as bundle:
        for name in names:
            try:
                data = (folder / name).read_bytes()
            except OSError:
                continue  # removed while packaging; the next poll catches up
            total += len(data)
            if total > 4 * MAX_ARCHIVE_BYTES:
                raise SyncError("The folder is too large to sync.")
            entry = zipfile.ZipInfo(name, FIXED_TIME)
            entry.external_attr = 0o100644 << 16
            entry.compress_type = zipfile.ZIP_DEFLATED
            bundle.writestr(entry, data)
    payload = buffer.getvalue()
    if len(payload) > MAX_ARCHIVE_BYTES:
        raise SyncError("The working tree exceeds the 64 MiB upload limit.")
    return payload


def base_commit(folder: Path) -> str | None:
    result = _git(folder, "rev-parse", "HEAD")
    value = result.stdout.decode().strip() if result is not None and result.returncode == 0 else ""
    return value if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value) else None


# --- upload and watch ---------------------------------------------------------------------

def upload(grant: dict, payload: bytes, commit: str | None) -> dict:
    token = read_token(grant["grant_id"])
    if not token:
        raise SyncError("The grant token is missing from the keychain; grant the folder again.")
    headers = {"Content-Type": "application/zip", "Authorization": f"WorkingTreeGrant {grant['grant_id']}:{token}"}
    if commit:
        headers["X-Base-Commit"] = commit
    request = urllib.request.Request(f"{grant['web_origin']}/api/working-tree/upload", data=payload,
                                     headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=120) as response:
        return json.loads(response.read() or b"{}")


def sync_once(grant: dict, *, now: float | None = None) -> str:
    """One poll of one granted folder; returns what happened (for status and tests)."""
    now = time.time() if now is None else now
    folder = Path(grant["folder"])
    if not folder.is_dir():
        _update_grant(grant["grant_id"], status="folder_missing")
        return "folder_missing"
    names = list_files(folder)
    signature = tree_signature(folder, names)
    if signature != grant.get("pending_signature"):
        _update_grant(grant["grant_id"], pending_signature=signature, changed_at=now)
        return "changed"
    if signature == grant.get("uploaded_signature"):
        return "unchanged"
    if now - float(grant.get("changed_at") or now) < QUIET_SECONDS:
        return "settling"
    if now - float(grant.get("uploaded_at") or 0) < MIN_UPLOAD_INTERVAL_SECONDS:
        return "waiting"
    payload = package(folder, names)
    archive_sha = hashlib.sha256(payload).hexdigest()
    if archive_sha == grant.get("uploaded_archive_sha256"):
        _update_grant(grant["grant_id"], uploaded_signature=signature)  # only mtimes changed
        return "unchanged"
    try:
        result = upload(grant, payload, base_commit(folder))
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            _update_grant(grant["grant_id"], status="revoked")
            remove_token(grant["grant_id"])
            return "revoked"
        _update_grant(grant["grant_id"], status=f"http_{exc.code}", uploaded_at=now)
        return f"http_{exc.code}"
    except (urllib.error.URLError, OSError):
        _update_grant(grant["grant_id"], status="web_unreachable", uploaded_at=now)
        return "web_unreachable"
    revision = result.get("revision") or {}
    _update_grant(grant["grant_id"], status="uploaded", uploaded_at=now, uploaded_signature=signature,
                  uploaded_archive_sha256=archive_sha, revision_id=revision.get("revision_id"))
    return "uploaded"


def _watch() -> None:
    while True:
        for grant in read_grants():
            if grant.get("status") == "revoked":
                continue
            try:
                sync_once(grant)
            except Exception as exc:  # noqa: BLE001 — one folder's failure never stops the others
                _update_grant(grant["grant_id"], status="error", error=str(exc)[:300])
        time.sleep(POLL_SECONDS)


def start_watcher() -> None:
    global _watcher
    with _lock:
        if _watcher is None or not _watcher.is_alive():
            _watcher = threading.Thread(target=_watch, name="working-tree-sync", daemon=True)
            _watcher.start()


def add_grant(project_id: str, grant_id: str, token: str, web_origin: str, approve) -> dict:
    """Choose and approve a folder for a grant from the browser, then start syncing it."""
    if not PROJECT_ID.fullmatch(project_id or "") or not GRANT_ID.fullmatch(grant_id or "") \
            or not isinstance(token, str) or not 20 <= len(token) <= 200:
        raise SyncError("Invalid working-tree grant.")
    folder = choose_folder(project_id)
    names = list_files(folder)
    approve("Sync a working-tree folder", [
        f"Folder: {folder}",
        f"Project: {project_id}",
        f"Files: {len(names)} (tracked and untracked, honouring .gitignore; secret files such as .env are never sent)",
        f"Destination: {web_origin}, indexed and readable by the chat and its external model when the project allows it",
        "Uploads happen a minute after your edits settle, at most every 3 minutes, until you revoke the grant.",
    ], "Developer Assistant will upload this folder's current state, including uncommitted edits.")
    save_token(grant_id, token)
    with _lock:
        grants = [g for g in read_grants() if not (g.get("project_id") == project_id and str(g.get("folder")) == str(folder))]
        grants.append({"grant_id": grant_id, "project_id": project_id, "folder": str(folder),
                       "web_origin": web_origin, "status": "granted"})
        write_grants(grants)
    start_watcher()
    return {"folder": str(folder), "files": len(names)}
