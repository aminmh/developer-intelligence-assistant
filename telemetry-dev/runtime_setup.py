#!/usr/bin/env python3
"""Interactive local Docker Runtime setup using native OS dialogs."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import platform
import base64
import hmac
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
API = ROOT / "api"
CERT_DIR = API / "telemetry-dev" / "host" / "certs"
ENV_FILE = API / ".env"
LABEL_PREFIX = "com.devassistant.telemetry."
LABELS = {
    "enabled": LABEL_PREFIX + "enabled",
    "project": LABEL_PREFIX + "project",
    "environment": LABEL_PREFIX + "environment",
    "commit": LABEL_PREFIX + "commit",
    "service": LABEL_PREFIX + "service",
}
SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
COMMIT = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
HELPER_HOST = "127.0.0.1"
HELPER_PORT = 47831
HELPER_COMMAND_TIMEOUT_SECONDS = 600
NATIVE_DIALOG_TIMEOUT_SECONDS = 180
LOCAL_DOCKER_COMMAND_TIMEOUT_SECONDS = 120
HELPER_STATE: dict = {"status": "idle", "message": "Ready", "updated": time.time()}
HELPER_LOCK = threading.Lock()
HELPER_USED_TOKENS: set[str] = set()


class ApprovalCancelled(RuntimeError):
    """The operator explicitly cancelled a grouped approval dialog."""


class NativeDialogError(RuntimeError):
    """A native dialog could not be executed."""


def state_path() -> Path:
    base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return base / "devassistant" / "runtime" / "local-setup.json"


def read_state() -> dict | None:
    path = state_path()
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Cannot read setup state at {path}: {exc}") from exc


def helper_secret() -> bytes:
    value = os.environ.get("RAG_CALLBACK_SECRET", "")
    if not value:
        env_path = API / ".env"
        if env_path.is_file():
            for line in env_path.read_text().splitlines():
                if line.startswith("RAG_CALLBACK_SECRET="):
                    value = line.split("=", 1)[1].strip().strip("\"'")
                    break
    if len(value) < 32:
        raise RuntimeError("Runtime helper authorization is unavailable: configure the shared RAG_CALLBACK_SECRET in api/.env and web/.env.")
    return hmac.new(value.encode(), b"devassistant-runtime-helper-v1", hashlib.sha256).digest()


def helper_origin() -> str:
    value = os.environ.get("WEB_ORIGIN", "")
    if not value:
        env_path = ROOT / "web" / ".env"
        if env_path.is_file():
            for line in env_path.read_text().splitlines():
                if line.startswith("WEB_ORIGIN="):
                    value = line.split("=", 1)[1].strip().strip("\"'")
                    break
    return value or "http://localhost:3000"


def verify_helper_token(token: str, origin: str, *, operation: str | None = None,
                        project_id: str | None = None, unscoped: bool = False) -> bool:
    try:
        head, body, signature = token.split(".")
        expected = base64.urlsafe_b64encode(hmac.new(helper_secret(), f"{head}.{body}".encode(), hashlib.sha256).digest()).decode().rstrip("=")
        if not hmac.compare_digest(expected, signature): return False
        claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        return (claims.get("aud") == "runtime-helper" and claims.get("origin") == origin
                and isinstance(claims.get("exp"), (int, float)) and claims["exp"] >= time.time()
                and (operation is None or claims.get("operation") == operation)
                and (project_id is None or claims.get("project_id") == project_id)
                and (not unscoped or (claims.get("operation") is None and claims.get("project_id") is None)))
    except (ValueError, OSError, RuntimeError, json.JSONDecodeError):
        return False


def serve_helper() -> None:
    class Handler(BaseHTTPRequestHandler):
        server_version = "RuntimeSetupHelper/1"

        def _json(self, status: int, data: dict) -> None:
            payload = json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            origin = self.headers.get("Origin", "")
            allowed = helper_origin()
            if origin == allowed:
                self.send_header("Access-Control-Allow-Origin", allowed)
                self.send_header("Vary", "Origin")
            self.end_headers()
            self.wfile.write(payload)

        def _authorized(self, *, operation: str | None = None,
                        project_id: str | None = None, unscoped: bool = False) -> bool:
            origin = self.headers.get("Origin", "")
            allowed = helper_origin()
            host = self.headers.get("Host", "")
            if origin != allowed or host not in {f"127.0.0.1:{HELPER_PORT}", f"localhost:{HELPER_PORT}"}:
                return False
            scheme, _, authority = origin.partition("://")
            if scheme not in {"http", "https"} or not authority or authority.startswith("["):
                return False
            token = self.headers.get("Authorization", "")
            return token.startswith("Bearer ") and verify_helper_token(
                token[7:], origin, operation=operation, project_id=project_id, unscoped=unscoped)

        def do_OPTIONS(self) -> None:
            origin = self.headers.get("Origin", "")
            if origin != helper_origin():
                return self._json(403, {"detail": "Invalid browser origin."})
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Max-Age", "60")
            self.end_headers()

        def do_POST(self) -> None:
            if self.path not in {"/setup", "/cleanup", "/remove-project-containers"}:
                return self._json(404, {"detail": "Not found."})
            if not self._authorized(): return self._json(403, {"detail": "Runtime setup authorization is invalid or expired."})
            if self.path in {"/setup", "/cleanup"} and not self._authorized(unscoped=True):
                return self._json(403, {"detail": "This authorization is not valid for Runtime setup or cleanup."})
            project_name = None
            project_id = None
            if self.path in {"/setup", "/remove-project-containers"}:
                length = self.headers.get("Content-Length", "0")
                if not length.isdigit() or int(length) > 4096:
                    return self._json(400, {"detail": "Invalid setup request."})
                try:
                    body = json.loads(self.rfile.read(int(length)) or b"{}")
                except (ValueError, json.JSONDecodeError):
                    return self._json(400, {"detail": "Invalid setup request."})
                if not isinstance(body, dict):
                    return self._json(400, {"detail": "Invalid Runtime operation request."})
                if self.path == "/setup":
                    if (body.get("project_name") is not None and
                            (not isinstance(body["project_name"], str) or
                             not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", body["project_name"]))):
                        return self._json(400, {"detail": "Invalid Compose project selection."})
                    project_name = body.get("project_name")
                else:
                    project_id = body.get("project_id")
                    if not isinstance(project_id, str) or not SLUG.fullmatch(project_id):
                        return self._json(400, {"detail": "Invalid Runtime project ID."})
                    if not self._authorized(operation="runtime-local-container-removal", project_id=project_id):
                        return self._json(403, {"detail": "Runtime removal authorization is not scoped to this project."})
            token_id = hashlib.sha256(self.headers.get("Authorization", "").encode()).hexdigest()
            with HELPER_LOCK:
                if token_id in HELPER_USED_TOKENS:
                    return self._json(403, {"detail": "This Runtime setup authorization has already been used."})
                if HELPER_STATE["status"] in {"starting", "running"}:
                    return self._json(409, {"detail": "A Runtime operation is already running. Wait for it to finish before starting another."})
                HELPER_USED_TOKENS.add(token_id)
                action = ("cleanup" if self.path == "/cleanup" else
                          "remove local containers" if self.path == "/remove-project-containers" else "setup")
                HELPER_STATE.update(status="starting", message=f"Starting Runtime {action} dialogs", updated=time.time())
                for key in ("project_id", "removed_count", "target_count", "already_absent", "failed_containers"):
                    HELPER_STATE.pop(key, None)
                if project_id is not None:
                    HELPER_STATE["project_id"] = project_id
            def execute() -> None:
                with HELPER_LOCK: HELPER_STATE.update(status="running", message=f"Runtime {action} is running", updated=time.time())
                try:
                    result = (cleanup() if action == "cleanup" else
                              remove_project_containers(project_id) if project_id is not None else setup(project_name))
                    outcome = result.get("status", "complete") if isinstance(result, dict) else "complete"
                    message = result.get("message", f"Runtime {action} completed") if isinstance(result, dict) else f"Runtime {action} completed"
                    with HELPER_LOCK:
                        HELPER_STATE.update(status=outcome, message=message, updated=time.time())
                        if isinstance(result, dict):
                            HELPER_STATE.update({key: result[key] for key in
                                ("project_id", "removed_count", "target_count", "already_absent", "failed_containers")
                                if key in result})
                except Exception as exc:
                    with HELPER_LOCK:
                        message = str(exc)
                        cancelled = isinstance(exc, ApprovalCancelled) or "cancel" in message.lower() or "not approved" in message.lower()
                        HELPER_STATE.update(status="cancelled" if cancelled else "error", message=message, updated=time.time())
            threading.Thread(target=execute, name="runtime-setup", daemon=True).start()
            return self._json(202, {"status": "starting"})

        def do_GET(self) -> None:
            if self.path == "/health": return self._json(200, {"ok": True, "api_version": 19})
            if self.path not in {"/status", "/setup-state"}: return self._json(404, {"detail": "Not found."})
            if self.path == "/setup-state":
                if not self._authorized(unscoped=True):
                    return self._json(403, {"detail": "Runtime setup authorization is invalid or expired."})
                try:
                    saved = read_state()
                    if not saved: return self._json(200, {"configured": False})
                    project = saved.get("project") if isinstance(saved.get("project"), dict) else {}
                    return self._json(200, {"configured": True, "phase": saved.get("phase", "incomplete"),
                        "project_id": saved.get("project_id"), "environment": saved.get("environment"),
                        "commit": saved.get("commit"), "compose_project": project.get("name"),
                        "services": saved.get("services", [])})
                except RuntimeError as exc:
                    return self._json(500, {"detail": str(exc)})
            with HELPER_LOCK: state = dict(HELPER_STATE)
            project_id = state.get("project_id")
            if project_id:
                authorized = self._authorized(operation="runtime-local-container-removal", project_id=project_id)
            else:
                authorized = self._authorized(unscoped=True)
            if not authorized:
                return self._json(403, {"detail": "Runtime operation status authorization is invalid or expired."})
            return self._json(200, state)

        def log_message(self, _format: str, *_args) -> None: return

    try:
        helper_secret()
        server = ThreadingHTTPServer((HELPER_HOST, HELPER_PORT), Handler)
    except OSError as exc:
        raise RuntimeError(f"Cannot bind Runtime setup helper to loopback port {HELPER_PORT}: {exc}") from exc
    print(f"Runtime setup helper listening on http://{HELPER_HOST}:{HELPER_PORT}")
    server.serve_forever(poll_interval=0.5)


def write_state(value: dict | None) -> None:
    path = state_path()
    if value is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp_name = tempfile.mkstemp(prefix="setup-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2)
            stream.write("\n")
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def dialog_backend() -> tuple[str, str]:
    """Return the native dialog executable, or fail with an actionable error."""
    if sys.platform == "darwin":
        if shutil.which("swift"):
            return "mac", shutil.which("swift")
    elif os.name == "nt":
        if shutil.which("powershell") or shutil.which("pwsh"):
            return "windows", shutil.which("powershell") or shutil.which("pwsh")
    elif sys.platform.startswith("linux"):
        for name in ("zenity", "kdialog"):
            if shutil.which(name):
                return name, name
    system = platform.system() or sys.platform
    required = {"Darwin": "Swift command-line tools", "Windows": "PowerShell", "Linux": "Zenity or KDialog"}.get(system, "a supported OS dialog facility")
    raise RuntimeError(f"Runtime setup does not support {system}, or its native dialog tool is unavailable (requires {required}).")


def macos_native_dialog_script(kind: str, title: str, text: str, *, default: str = "", choices: list[str] | None = None) -> str:
    """Build native AppKit dialogs without interpolating user text as source code."""
    if kind not in {"confirm", "entry", "checklist", "multiselect", "radiolist"}:
        raise ValueError(f"Unsupported macOS Runtime dialog type: {kind}")
    payload = base64.b64encode(json.dumps({
        "kind": kind,
        "title": title,
        "text": text,
        "default": default,
        "choices": choices or [],
        "radio_mode": kind == "radiolist",
        "require_all": kind == "checklist",
    }, ensure_ascii=False).encode("utf-8")).decode("ascii")
    script = r'''import AppKit
import Foundation
import Darwin

let encodedPayload = "__PAYLOAD__"
let payloadData = Data(base64Encoded: encodedPayload)!
let dialog = try! JSONSerialization.jsonObject(with: payloadData) as! [String: Any]
let kind = dialog["kind"] as! String
let title = dialog["title"] as! String
let message = dialog["text"] as! String
let defaultAnswer = dialog["default"] as! String
let choices = dialog["choices"] as! [String]
let radioMode = dialog["radio_mode"] as! Bool
let requireAll = dialog["require_all"] as! Bool

final class ChecklistTarget: NSObject {
    let boxes: [NSButton]
    let continueButton: NSButton
    let radioMode: Bool
    let requireAll: Bool

    init(boxes: [NSButton], continueButton: NSButton, radioMode: Bool, requireAll: Bool) {
        self.boxes = boxes
        self.continueButton = continueButton
        self.radioMode = radioMode
        self.requireAll = requireAll
    }

    @objc func selectionChanged(_ sender: NSButton) {
        if radioMode && sender.state == .on {
            for box in boxes where box !== sender { box.state = .off }
        }
        let checked = boxes.filter { $0.state == .on }.count
        if radioMode {
            continueButton.isEnabled = checked == 1
        } else if requireAll {
            continueButton.isEnabled = !boxes.isEmpty && checked == boxes.count
        } else {
            continueButton.isEnabled = checked > 0
        }
    }
}

let alert = NSAlert()
alert.messageText = title
alert.informativeText = message
alert.alertStyle = kind == "confirm" ? .warning : .informational
alert.addButton(withTitle: "Cancel")
alert.addButton(withTitle: "Continue")
let continueButton = alert.buttons[1]
continueButton.isEnabled = kind == "confirm" || kind == "entry"

var entryField: NSTextField? = nil
var boxes: [NSButton] = []
var checklistTarget: ChecklistTarget? = nil
if kind == "entry" {
    let field = NSTextField(string: defaultAnswer)
    field.setFrameSize(NSSize(width: 420, height: 24))
    entryField = field
    alert.accessoryView = field
    alert.window.initialFirstResponder = field
} else if kind == "checklist" || kind == "multiselect" || kind == "radiolist" {
    boxes = choices.enumerated().map { index, choice in
        let box = radioMode
            ? NSButton(radioButtonWithTitle: choice, target: nil, action: nil)
            : NSButton(checkboxWithTitle: choice, target: nil, action: nil)
        box.tag = index
        return box
    }
    let target = ChecklistTarget(boxes: boxes, continueButton: continueButton,
                                 radioMode: radioMode, requireAll: requireAll)
    checklistTarget = target
    for box in boxes {
        box.target = target
        box.action = #selector(ChecklistTarget.selectionChanged(_:))
    }
    let contentHeight = max(28, choices.count * 30)
    let listHeight = min(contentHeight, 360)
    let stack = NSStackView()
    stack.orientation = .vertical
    stack.alignment = .leading
    stack.distribution = .fill
    stack.spacing = 4
    stack.frame = NSRect(x: 0, y: 0, width: 560, height: CGFloat(contentHeight))
    for box in boxes {
        box.setFrameSize(NSSize(width: 548, height: 26))
        stack.addArrangedSubview(box)
    }
    let scrollView = NSScrollView(frame: NSRect(x: 0, y: 0, width: 560, height: CGFloat(listHeight)))
    scrollView.documentView = stack
    scrollView.hasVerticalScroller = contentHeight > listHeight
    scrollView.drawsBackground = false
    alert.accessoryView = scrollView
}

let application = NSApplication.shared
application.setActivationPolicy(.accessory)
application.activate(ignoringOtherApps: true)
let response = withExtendedLifetime(checklistTarget) { alert.runModal() }
if response == .alertSecondButtonReturn {
    switch kind {
    case "confirm": print("yes")
    case "entry": print(entryField?.stringValue ?? "")
    default:
        let selected = boxes.enumerated().compactMap { index, box in box.state == .on ? String(index + 1) : nil }
        print(selected.joined(separator: ","))
    }
} else {
    print("__DEVASSISTANT_DIALOG_CANCELLED__")
}
'''
    return script.replace("__PAYLOAD__", payload)


def _dialog(kind: str, title: str, text: str, *, default: str = "", choices: list[str] | None = None) -> str | None:
    backend, executable = dialog_backend()
    script_input = None
    if backend == "mac":
        swift = executable if Path(executable).name == "swift" else shutil.which("swift")
        if not swift:
            raise NativeDialogError("macOS native dialogs require the Swift command-line tool.")
        args = [swift, "-"]
        script_input = macos_native_dialog_script(kind, title, text, default=default, choices=choices)
    elif backend == "zenity":
        if kind == "confirm":
            args = [executable, "--question", "--title", title, "--text", text, "--icon=dialog-warning"]
        elif kind == "entry":
            args = [executable, "--entry", "--title", title, "--text", text, "--entry-text", default, "--icon=dialog-information"]
        else:
            args = [executable, "--list", "--checklist" if kind in {"checklist", "multiselect"} else "--radiolist", "--title", title, "--text", text, "--column", "Select", "--column", "Choice", "--print-column", "2", "--separator", ",", "--icon=dialog-question"]
            for i, choice in enumerate(choices or []): args.extend(["TRUE" if i == 0 and kind == "radiolist" else "FALSE", choice])
    elif backend == "kdialog":
        if kind == "confirm": args = [executable, "--icon", "dialog-warning", "--yesno", text, "--title", title]
        elif kind == "entry": args = [executable, "--icon", "dialog-information", "--title", title, "--inputbox", text, default]
        elif kind in {"checklist", "multiselect"}: args = [executable, "--icon", "dialog-question", "--title", title, "--checklist", text, *sum(([str(i + 1), c, "off"] for i, c in enumerate(choices or [])), [])]
        else: args = [executable, "--icon", "dialog-question", "--title", title, "--menu", text, *sum(([str(i + 1), c] for i, c in enumerate(choices or [])), [])]
    else:
        # Windows Forms gives standard native message, input, and checked-list dialogs.
        payload = base64.b64encode(json.dumps({"kind":kind,"title":title,"text":text,"default":default,"choices":choices or []}).encode()).decode()
        script = "$ErrorActionPreference='Stop'; Add-Type -AssemblyName System.Windows.Forms; $d=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('"+payload+"'))|ConvertFrom-Json;"
        script += "if($d.kind -eq 'confirm'){if([Windows.Forms.MessageBox]::Show($d.text,$d.title,'YesNo','Warning') -eq 'Yes'){'yes'}}"
        script += "elseif($d.kind -eq 'entry'){$f=New-Object Windows.Forms.Form;$f.Text=$d.title;$f.Icon=[System.Drawing.SystemIcons]::Information;$f.Width=520;$f.Height=160;$l=New-Object Windows.Forms.Label;$l.Text=$d.text;$l.AutoSize=$true;$l.Top=15;$f.Controls.Add($l);$t=New-Object Windows.Forms.TextBox;$t.Text=$d.default;$t.Top=45;$t.Width=470;$f.Controls.Add($t);$b=New-Object Windows.Forms.Button;$b.Text='Continue';$b.Top=80;$b.DialogResult='OK';$f.Controls.Add($b);$f.AcceptButton=$b;if($f.ShowDialog() -eq 'OK'){$t.Text}else{exit 2}}"
        script += "else{$f=New-Object Windows.Forms.Form;$f.Text=$d.title;$f.Icon=[System.Drawing.SystemIcons]::Question;$f.Width=520;$f.Height=420;$l=New-Object Windows.Forms.Label;$l.Text=$d.text;$l.AutoSize=$true;$f.Controls.Add($l);if($d.kind -eq 'radiolist'){$c=New-Object Windows.Forms.ListBox;$c.SelectionMode='One'}else{$c=New-Object Windows.Forms.CheckedListBox};$c.Top=35;$c.Width=470;$c.Height=290;foreach($x in $d.choices){[void]$c.Items.Add($x)};$f.Controls.Add($c);$b=New-Object Windows.Forms.Button;$b.Text='Continue';$b.Top=335;$b.DialogResult='OK';$f.Controls.Add($b);if($d.kind -eq 'checklist' -or $d.kind -eq 'multiselect'){$b.Enabled=$false;$c.Add_ItemCheck({param($sender,$event);$count=$sender.CheckedItems.Count;if($event.NewValue -eq 'Checked'){$count++}elseif($event.NewValue -eq 'Unchecked'){$count--};if($d.kind -eq 'checklist'){$b.Enabled=($count -eq $sender.Items.Count)}else{$b.Enabled=($count -gt 0)}})};if($f.ShowDialog() -eq 'OK'){if($d.kind -eq 'radiolist'){if($null -eq $c.SelectedItem){exit 2};$c.SelectedItem}else{if(($d.kind -eq 'checklist' -and $c.CheckedItems.Count -ne $c.Items.Count) -or ($d.kind -eq 'multiselect' -and $c.CheckedItems.Count -lt 1)){exit 2};($c.CheckedItems -join ',')}}else{exit 2}}"
        args = [executable, "-NoProfile", "-STA", "-Command", script]
    try:
        result = subprocess.run(args, input=script_input, text=True, capture_output=True,
                                timeout=NATIVE_DIALOG_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        raise NativeDialogError(
            f"{backend} native approval dialog timed out after {NATIVE_DIALOG_TIMEOUT_SECONDS} seconds."
        ) from exc
    if result.returncode != 0:
        if backend == "mac":
            detail = (result.stderr or result.stdout or "").strip()
            if "(-128)" in detail or "User canceled" in detail or "User cancelled" in detail:
                raise ApprovalCancelled("Runtime setup approval was cancelled in the native dialog.")
            raise NativeDialogError(f"macOS native dialog failed: {detail or 'Swift exited unsuccessfully'}")
        if kind in {"checklist", "multiselect", "confirm", "radiolist", "entry"} and result.returncode in {1, 2}:
            raise ApprovalCancelled("Runtime setup approval was cancelled in the native dialog.")
        raise NativeDialogError(f"{backend} native dialog failed: {(result.stderr or result.stdout or '').strip() or 'dialog exited unsuccessfully'}")
    output = (result.stdout or "").strip()
    if backend == "mac" and output == "__DEVASSISTANT_DIALOG_CANCELLED__": raise ApprovalCancelled("Runtime setup approval was cancelled in the native dialog.")
    if kind == "confirm":
        if backend in {"zenity", "kdialog"}: return "yes"
        return "yes" if output.lower() in {"yes", "continue"} else None
    return output


def required_approval(title: str, items: list[str], summary: str = "") -> None:
    """Require one explicit approval covering every listed operation."""
    if not items:
        return
    if sys.stdin.isatty():
        print(f"\n{title}")
        if summary:
            print(summary)
        for index, item in enumerate(items, 1):
            print(f"  [ ] {index}. {item}")
        answer = prompt("Type the item numbers in order (for example 1,2,3) to accept every checklist item")
        expected = ",".join(str(index) for index in range(1, len(items) + 1))
        normalized = ",".join(part.strip() for part in answer.split(",") if part.strip())
        if normalized != expected:
            raise ApprovalCancelled("Runtime setup approval was cancelled; every checklist item must be accepted.")
        return
    text = (summary + "\n\n" if summary else "") + "\n".join(f"☐ {item}" for item in items)
    try:
        output = _dialog("checklist", title, text, choices=items)
    except ApprovalCancelled:
        raise
    except NativeDialogError:
        raise
    if output is None:
        raise ApprovalCancelled("Runtime setup approval was cancelled in the native dialog.")
    picked = {part.strip() for part in output.replace("\n", ",").split(",") if part.strip()}
    if len(picked) != len(items) or not all(item in picked or str(index) in picked for index, item in enumerate(items, 1)):
        raise ApprovalCancelled("Runtime setup approval requires every checklist item to be checked.")


def prompt(text: str, default: str | None = None) -> str:
    if sys.stdin.isatty():
        suffix = f" [{default}]" if default is not None else ""
        return input(f"{text}{suffix}: ").strip() or (default or "")
    answer = _dialog("entry", "Developer Assistant Runtime Setup", text, default=default or "")
    if answer is None: raise RuntimeError(f"Runtime setup was cancelled at the input: {text}")
    return answer.strip() or (default or "")


def confirm(text: str) -> bool:
    if sys.stdin.isatty(): return prompt(f"{text} [y/N]").lower() in {"y", "yes"}
    return _dialog("confirm", "Developer Assistant Runtime Setup", text) == "yes"


def require_terminal() -> None:
    if not sys.stdin.isatty(): dialog_backend()


def docker_socket_group_id(socket_path: Path) -> str:
    # Docker Desktop and OrbStack expose the host socket inside Linux containers
    # as root:root, even when the host socket has a different group ID.
    if sys.platform == "darwin":
        return "0"
    return str(socket_path.stat().st_gid)


def run(args: list[str], *, cwd: Path | None = None, env: dict | None = None,
        capture: bool = False, check: bool = True,
        timeout: float = HELPER_COMMAND_TIMEOUT_SECONDS) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(args, cwd=cwd, env=env, text=True,
                                stdout=subprocess.PIPE if capture else None,
                                stderr=subprocess.PIPE if capture else None, check=False,
                                timeout=timeout)
    except FileNotFoundError as exc:
        raise RuntimeError(f"Required command not found: {args[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Command timed out after {timeout:g} seconds: {args[0]}") from exc
    if check and result.returncode:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"Command failed ({result.returncode}): {' '.join(args)}\n{detail}")
    return result


def local_docker_command_prefix() -> list[str]:
    """Pin Docker commands to a local Unix socket or Windows named pipe."""
    docker_host = os.environ.get("DOCKER_HOST", "").strip()
    if docker_host:
        endpoint = docker_host
    else:
        context_name = os.environ.get("DOCKER_CONTEXT", "").strip()
        if not context_name:
            context_name = run(["docker", "context", "show"], capture=True,
                               timeout=LOCAL_DOCKER_COMMAND_TIMEOUT_SECONDS).stdout.strip()
        if not context_name:
            raise RuntimeError("A local Docker context could not be identified.")
        try:
            contexts = json.loads(run(["docker", "context", "inspect", context_name], capture=True,
                                      timeout=LOCAL_DOCKER_COMMAND_TIMEOUT_SECONDS).stdout)
            endpoint = contexts[0]["Endpoints"]["docker"]["Host"]
        except (AttributeError, IndexError, KeyError, TypeError, ValueError, RuntimeError):
            raise RuntimeError("The selected Docker context could not be verified as local.") from None
    if not isinstance(endpoint, str):
        raise RuntimeError("The selected Docker endpoint could not be verified as local.")
    scheme, separator, socket_path = endpoint.partition("://")
    is_local_socket = separator and scheme.lower() == "unix" and socket_path.startswith("/")
    is_local_named_pipe = separator and scheme.lower() == "npipe" and socket_path.startswith("/")
    if not (is_local_socket or is_local_named_pipe):
        raise RuntimeError("The selected Docker endpoint is not local. Switch to a local socket or named-pipe context and retry.")
    # Pin every following command to the endpoint that was just validated so
    # a concurrent Docker context switch cannot redirect a mutation.
    return ["docker", "--host", endpoint]


def inspect_local_runtime_container(docker: list[str], container_id: str) -> dict | None:
    """Read and validate one container without exposing Docker errors to the UI."""
    result = run(docker + ["inspect", "--format", "{{json .}}", container_id], capture=True, check=False,
                 timeout=LOCAL_DOCKER_COMMAND_TIMEOUT_SECONDS)
    if result.returncode:
        detail = (result.stderr or "").lower()
        if "no such object" in detail or "not found" in detail:
            return None
        raise RuntimeError("Docker could not inspect a local Runtime container.")
    try:
        record = json.loads(result.stdout)
        if isinstance(record, list):
            record = record[0]
        if not isinstance(record, dict) or record.get("Id") != container_id:
            raise ValueError
        labels = record.get("Config", {}).get("Labels") or {}
        if not isinstance(labels, dict):
            labels = {}
        return {
            "container_id": container_id,
            "enabled": labels.get(LABELS["enabled"]) == "true",
            "project_id": labels.get(LABELS["project"]),
            "running": bool(record.get("State", {}).get("Running")),
        }
    except (IndexError, AttributeError, TypeError, ValueError, json.JSONDecodeError):
        raise RuntimeError("Docker returned unreadable metadata for a local container.") from None


def discover_local_runtime_containers(docker: list[str], project_id: str) -> list[dict]:
    result = run(docker + ["ps", "--all", "--quiet", "--no-trunc",
                           "--filter", f"label={LABELS['enabled']}=true",
                           "--filter", f"label={LABELS['project']}={project_id}"], capture=True,
                 timeout=LOCAL_DOCKER_COMMAND_TIMEOUT_SECONDS)
    containers = []
    for container_id in dict.fromkeys(line.strip() for line in result.stdout.splitlines() if line.strip()):
        record = inspect_local_runtime_container(docker, container_id)
        if record and record["enabled"] and record["project_id"] == project_id:
            containers.append(record)
    return containers


def remove_project_containers(project_id: str) -> dict:
    """Stop and remove only this project's opted-in containers on the local daemon."""
    require_terminal()
    docker = local_docker_command_prefix()
    targets = discover_local_runtime_containers(docker, project_id)
    if not targets:
        return {"status": "complete", "project_id": project_id, "removed_count": 0,
                "target_count": 0, "already_absent": 0, "failed_containers": [],
                "message": f"No matching local Runtime containers were found for {project_id}; nothing was changed."}

    running_count = sum(bool(item["running"]) for item in targets)
    stopped_count = len(targets) - running_count
    approval_item = (f"Stop and remove {len(targets)} local containers for project {project_id} "
                     f"({running_count} running, {stopped_count} stopped). Container writable-layer data will be lost.")
    summary = (f"Project: {project_id}\nLocal Docker endpoint: verified socket or named pipe\n"
               f"Matching containers: {len(targets)}\n"
               "Only exact Runtime-enabled and project labels are in scope. Images, volumes, Runtime history, and remote hosts remain.")
    required_approval("Remove local Runtime containers", [approval_item], summary=summary)

    approved_ids = {item["container_id"] for item in targets}
    current = discover_local_runtime_containers(docker, project_id)
    added_ids = {item["container_id"] for item in current} - approved_ids
    if added_ids:
        return {"status": "error", "project_id": project_id, "removed_count": 0,
                "target_count": len(targets), "already_absent": 0,
                "failed_containers": sorted(added_ids),
                "message": "New matching local containers appeared after approval. Nothing was removed; refresh and approve again."}

    removed = []
    already_absent = 0
    failures = []
    for container_id in sorted(approved_ids):
        try:
            before_stop = inspect_local_runtime_container(docker, container_id)
            if before_stop is None:
                already_absent += 1
                continue
            if not before_stop["enabled"] or before_stop["project_id"] != project_id:
                failures.append(container_id)
                continue
            if before_stop["running"]:
                stopped = run(docker + ["stop", container_id], capture=True, check=False,
                              timeout=LOCAL_DOCKER_COMMAND_TIMEOUT_SECONDS)
                if stopped.returncode:
                    failures.append(container_id)
                    continue
            before_remove = inspect_local_runtime_container(docker, container_id)
            if before_remove is None:
                already_absent += 1
                continue
            if not before_remove["enabled"] or before_remove["project_id"] != project_id:
                failures.append(container_id)
                continue
            removed_result = run(docker + ["rm", container_id], capture=True, check=False,
                                 timeout=LOCAL_DOCKER_COMMAND_TIMEOUT_SECONDS)
            if removed_result.returncode:
                failures.append(container_id)
                continue
            removed.append(container_id)
        except RuntimeError:
            failures.append(container_id)

    if failures:
        return {"status": "error", "project_id": project_id, "removed_count": len(removed),
                "target_count": len(targets), "already_absent": already_absent,
                "failed_containers": failures,
                "message": f"Removed {len(removed)} of {len(targets)} approved local containers for {project_id}; "
                           f"{len(failures)} could not be safely removed. Refresh and retry."}
    return {"status": "complete", "project_id": project_id, "removed_count": len(removed),
            "target_count": len(targets), "already_absent": already_absent,
            "failed_containers": [],
            "message": f"Removed {len(removed)} local containers for {project_id}; "
                       f"{already_absent} were already absent. Runtime history and remote containers were preserved."}


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def yaml_module():
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("Local setup needs PyYAML in the Python 3 environment. Install it with: python3 -m pip install PyYAML") from exc
    return yaml


def yaml_services(text: str) -> dict:
    yaml = yaml_module()
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise RuntimeError(f"Compose YAML cannot be safely edited: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("services"), dict):
        raise RuntimeError("Compose file has no top-level services mapping")
    return data["services"]


def yaml_node(text: str):
    yaml = yaml_module()
    try:
        return yaml.compose(text)
    except yaml.YAMLError as exc:
        raise RuntimeError(f"Compose YAML cannot be safely edited: {exc}") from exc


def map_pair(node, key: str):
    yaml = yaml_module()
    if not isinstance(node, yaml.MappingNode):
        return None
    for key_node, value_node in node.value:
        if isinstance(key_node, yaml.ScalarNode) and key_node.value == key:
            return key_node, value_node
    return None


def line_offset(text: str, line: int) -> int:
    lines = text.splitlines(keepends=True)
    return sum(len(value) for value in lines[:min(line, len(lines))])


def line_indent(node, fallback: int) -> int:
    yaml = yaml_module()
    if isinstance(node, yaml.MappingNode):
        columns = [key.start_mark.column for key, _ in node.value if isinstance(key, yaml.ScalarNode)]
        if columns:
            return min(columns)
    if isinstance(node, yaml.SequenceNode):
        # Scalar marks in block sequences point after the dash and its space.
        columns = [max(0, item.start_mark.column - 2) for item in node.value]
        if columns:
            return min(columns)
    return fallback


def child_indent_step(node, parent_column: int) -> int:
    yaml = yaml_module()
    if isinstance(node, yaml.MappingNode):
        columns = [key.start_mark.column for key, _ in node.value if isinstance(key, yaml.ScalarNode)]
        if columns and min(columns) > parent_column:
            return min(columns) - parent_column
    return 2


def add_service_labels(text: str, service: str, values: dict[str, str]) -> str:
    """Patch only YAML scalar spans, preserving comments and unrelated formatting."""
    yaml = yaml_module()
    root = yaml_node(text)
    services_pair = map_pair(root, "services")
    service_pair = map_pair(services_pair[1], service) if services_pair else None
    if not service_pair:
        return text
    service_node = service_pair[1]
    if not isinstance(service_node, yaml.MappingNode) or service_node.flow_style:
        raise RuntimeError(f"Service {service} is not a block mapping; refusing an unsafe edit")
    service_indent = line_indent(service_node, service_pair[0].start_mark.column + 2)
    service_step = max(1, service_indent - service_pair[0].start_mark.column)
    labels_pair = map_pair(service_node, "labels")
    edits: list[tuple[int, int, str]] = []
    missing = dict(values)

    def append_block_lines(line: int, rows: list[str]) -> None:
        offset = line_offset(text, line)
        prefix = ""
        if offset == len(text) and text and not text.endswith(("\n", "\r")):
            prefix = "\n"
        edits.append((offset, offset, prefix + "".join(rows)))

    if labels_pair:
        labels_node = labels_pair[1]
        if isinstance(labels_node, yaml.MappingNode):
            for key_node, value_node in labels_node.value:
                if not isinstance(key_node, yaml.ScalarNode) or key_node.value not in values:
                    continue
                if not isinstance(value_node, yaml.ScalarNode):
                    raise RuntimeError(f"Service {service} has a complex value for {key_node.value}; refusing an unsafe edit")
                edits.append((value_node.start_mark.index, value_node.end_mark.index,
                              json.dumps(values[key_node.value])))
                missing.pop(key_node.value, None)
            if missing and labels_node.flow_style:
                start = labels_node.end_mark.index - 1
                items = ", ".join(f"{json.dumps(k)}: {json.dumps(v)}" for k, v in missing.items())
                separator = ", " if labels_node.value else ""
                edits.append((start, start, separator + items))
            elif missing:
                step = child_indent_step(service_node, service_pair[0].start_mark.column)
                indent = line_indent(labels_node, labels_pair[0].start_mark.column + step)
                newline = "\r\n" if "\r\n" in text else "\n"
                append_block_lines(labels_node.end_mark.line,
                                   [" " * indent + f"{json.dumps(k)}: {json.dumps(v)}" + newline
                                    for k, v in missing.items()])
        elif isinstance(labels_node, yaml.SequenceNode):
            for item in labels_node.value:
                if not isinstance(item, yaml.ScalarNode):
                    raise RuntimeError(f"Service {service} has a complex labels list; refusing an unsafe edit")
                key = item.value.split("=", 1)[0]
                if key not in values:
                    continue
                edits.append((item.start_mark.index, item.end_mark.index,
                              json.dumps(f"{key}={values[key]}")))
                missing.pop(key, None)
            if missing and labels_node.flow_style:
                start = labels_node.end_mark.index - 1
                items = ", ".join(json.dumps(f"{k}={v}") for k, v in missing.items())
                separator = ", " if labels_node.value else ""
                edits.append((start, start, separator + items))
            elif missing:
                step = child_indent_step(service_node, service_pair[0].start_mark.column)
                indent = line_indent(labels_node, labels_pair[0].start_mark.column + step)
                newline = "\r\n" if "\r\n" in text else "\n"
                append_block_lines(labels_node.end_mark.line,
                                   [" " * indent + "- " + json.dumps(f"{k}={v}") + newline
                                    for k, v in missing.items()])
        else:
            raise RuntimeError(f"Service {service} uses an unsupported labels format; refusing an unsafe edit")
    elif values:
        indent = service_indent
        newline = "\r\n" if "\r\n" in text else "\n"
        lines = [" " * indent + "labels:" + newline]
        lines.extend(" " * (indent + service_step) + f"{json.dumps(k)}: {json.dumps(v)}" + newline
                     for k, v in values.items())
        append_block_lines(service_node.end_mark.line, lines)

    updated = text
    for start, end, replacement in sorted(edits, key=lambda edit: edit[0], reverse=True):
        updated = updated[:start] + replacement + updated[end:]
    try:
        yaml.safe_load(updated)
    except yaml.YAMLError as exc:
        raise RuntimeError(f"Proposed labels for service {service} would make Compose YAML invalid: {exc}") from exc
    return updated


def compose_args(project: dict) -> list[str]:
    args = ["docker", "compose", "--project-directory", project["directory"], "-p", project["name"]]
    for path in project["files"]:
        args.extend(["-f", path])
    return args


def discover_projects() -> list[dict]:
    result = run(["docker", "compose", "ls", "--all", "--format", "json"], capture=True)
    try:
        rows = json.loads(result.stdout or "[]")
    except ValueError as exc:
        raise RuntimeError("Docker Compose returned an unreadable project list") from exc
    projects = []
    for row in rows:
        config_files = row.get("ConfigFiles") or row.get("configFiles") or ""
        if isinstance(config_files, list):
            files = [str(part).strip() for part in config_files if str(part).strip()]
        else:
            files = [part.strip() for part in str(config_files).split(",") if part.strip()]
        if files:
            projects.append({"name": row.get("Name") or row.get("name"),
                             "status": row.get("Status") or row.get("status") or "unknown",
                             "files": [str(Path(path).expanduser().resolve()) for path in files],
                             "directory": str(Path(files[0]).expanduser().resolve().parent)})
    return projects


def choose_project(projects: list[dict]) -> dict:
    if not projects:
        raise RuntimeError("Docker Compose did not report any local projects with config files")
    if not sys.stdin.isatty():
        answer = _dialog("radiolist", "Choose Runtime Project", "Select the Compose project whose services should report.", choices=[f"{item['name']} ({item['status']}) — {', '.join(item['files'])}" for item in projects])
        if answer is None: raise RuntimeError("Runtime setup was cancelled while choosing a Compose project")
        try: return projects[int(answer) - 1]
        except (ValueError, IndexError):
            match = next((item for item in projects if answer.startswith(str(item["name"]) + " (")), None)
            if match: return match
            raise RuntimeError("The native dialog returned an unknown project selection")
    for index, item in enumerate(projects, 1):
        print(f"  {index}. {item['name']} ({item['status']}) — {', '.join(item['files'])}")
    while True:
        answer = prompt("Choose a project number")
        if answer.isdigit() and 1 <= int(answer) <= len(projects):
            return projects[int(answer) - 1]
        print("Enter one of the listed numbers.")


def update_env_scope(fingerprint: str, project_id: str, environment: str,
                     *, remove: bool = False) -> tuple[str, str]:
    before = ENV_FILE.read_text() if ENV_FILE.exists() else ""
    lines = before.splitlines(True)
    found = None
    for i, line in enumerate(lines):
        if line.startswith("TELEMETRY_HOST_SCOPES="):
            found = i
            break
    raw = lines[found].split("=", 1)[1].strip() if found is not None else "{}"
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        raw = raw[1:-1]
    try:
        scopes = json.loads(raw or "{}")
    except ValueError as exc:
        raise RuntimeError("api/.env has invalid TELEMETRY_HOST_SCOPES JSON; it was left unchanged") from exc
    if not isinstance(scopes, dict):
        raise RuntimeError("TELEMETRY_HOST_SCOPES must be a JSON object")
    old_scope = json.loads(json.dumps(scopes.get(fingerprint)))
    if remove:
        host = scopes.get(fingerprint)
        projects = host.get("projects", {}) if isinstance(host, dict) else {}
        envs = projects.get(project_id, []) if isinstance(projects, dict) else []
        if environment in envs:
            envs.remove(environment)
        if isinstance(projects, dict):
            if envs:
                projects[project_id] = envs
            else:
                projects.pop(project_id, None)
            if not projects:
                scopes.pop(fingerprint, None)
    else:
        host = scopes.setdefault(fingerprint, {"projects": {}})
        if not isinstance(host, dict) or not isinstance(host.get("projects", {}), dict):
            raise RuntimeError("Existing host scope has an unsupported structure; it was left unchanged")
        projects = host.setdefault("projects", {})
        envs = projects.setdefault(project_id, [])
        if not isinstance(envs, list) or not all(isinstance(value, str) for value in envs):
            raise RuntimeError("Existing project scope has an unsupported structure; it was left unchanged")
        if environment not in envs:
            envs.append(environment)
    value = json.dumps(scopes, separators=(",", ":"), sort_keys=True)
    line = f"TELEMETRY_HOST_SCOPES={value}\n"
    if found is None:
        after = before + ("" if not before or before.endswith("\n") else "\n") + line
    else:
        lines[found] = line
        after = "".join(lines)
    return before, after


def write_text_atomic(path: Path, text: str, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(text)
        os.chmod(temp_name, mode if mode is not None else (path.stat().st_mode & 0o777 if path.exists() else 0o600))
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def cert_fingerprint() -> str:
    cert = CERT_DIR / "agent" / "host.crt"
    result = run(["openssl", "x509", "-in", str(cert), "-noout", "-fingerprint", "-sha256"], capture=True)
    value = result.stdout.strip().split("=", 1)[-1].replace(":", "").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise RuntimeError("Could not read a valid local host certificate fingerprint")
    return value


def cert_directory_is_empty_scaffold() -> bool:
    if not CERT_DIR.exists():
        return True
    if CERT_DIR.is_symlink() or not CERT_DIR.is_dir():
        return False
    return all(path.is_dir() and not path.is_symlink() for path in CERT_DIR.rglob("*"))


def start_ingress_and_agent() -> tuple[dict, str]:
    created_certs = cert_directory_is_empty_scaffold()
    if created_certs:
        run([str(API / "telemetry-dev" / "host" / "bootstrap-dev-certs.sh"), str(CERT_DIR)])
    needed = [CERT_DIR / "ingress" / name for name in ("ca.pem", "server.crt", "server.key")]
    needed += [CERT_DIR / "agent" / name for name in ("ca.pem", "host.crt", "host.key")]
    if not all(path.is_file() for path in needed):
        raise RuntimeError(f"Local identity directory is incomplete: {CERT_DIR}")
    fingerprint = cert_fingerprint()
    CURRENT_SETUP.update({"fingerprint": fingerprint, "certificates_created": created_certs,
                          "scope_added": False, "scope_original": False})
    write_state(CURRENT_SETUP)
    before, after = update_env_scope(fingerprint, CURRENT_SETUP["project_id"], CURRENT_SETUP["environment"])
    if before != after:
        CURRENT_SETUP.update({"scope_added": True, "scope_original": True,
                              "scope_file_before": before, "scope_file_after_hash": digest(after)})
        write_state(CURRENT_SETUP)
        write_text_atomic(ENV_FILE, after)
    CURRENT_SETUP.update({"scope_added": before != after, "scope_original": before != after})
    write_state(CURRENT_SETUP)
    print("\nStarting local API ingress and host agent…")
    run(["docker", "compose", "--profile", "runtime-host", "up", "-d", "python-api", "telemetry-caddy"], cwd=API)
    socket_path = Path(os.environ.get("DOCKER_SOCK_PATH", "/var/run/docker.sock"))
    if not socket_path.exists():
        raise RuntimeError(f"Docker socket not found at {socket_path}; set DOCKER_SOCK_PATH and retry")
    env = os.environ.copy()
    env.update({"HOST_IDENTITY_DIR": str(CERT_DIR / "agent"),
                "BACKEND_URL": "https://telemetry-caddy:9443",
                "DOCKER_SOCK_PATH": str(socket_path),
                "DOCKER_SOCK_GID": docker_socket_group_id(socket_path)})
    run(["docker", "compose", "-f", "telemetry-agent/docker-compose.yml", "-f",
         "telemetry-agent/docker-compose.local.yml", "up", "-d", "--build"], cwd=API, env=env)
    return CURRENT_SETUP, fingerprint


CURRENT_SETUP: dict = {}


def setup(project_name: str | None = None) -> None:
    require_terminal()
    yaml_module()
    if read_state():
        raise RuntimeError(f"A local setup record already exists at {state_path()}. Run runtime status or cleanup first.")
    print("Runtime setup runs in this terminal. The browser cannot access your Docker daemon or project files.")
    print("The command requests permission before reading Docker metadata and the selected project files.")
    if not confirm("Allow this command to query Docker for local Compose project metadata?"):
        raise RuntimeError("Setup cancelled before Docker metadata was read.")
    projects = discover_projects()
    if project_name is None:
        project = choose_project(projects)
    else:
        project = next((item for item in projects if item["name"] == project_name), None)
        if project is None:
            raise RuntimeError("The selected Compose project is no longer available. Reload the project list and try again.")
    print(f"Selected project directory: {project['directory']}")
    base = compose_args(project)
    result = run(base + ["config", "--services"], cwd=Path(project["directory"]), capture=True)
    services = [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]
    if not services:
        raise RuntimeError("The selected Compose project has no services")
    if not sys.stdin.isatty():
        answer = _dialog("multiselect", "Choose Runtime Services", "Select one or more services to track, then click Continue.", choices=services)
        if answer is None: raise RuntimeError("Runtime setup was cancelled while choosing services")
        picked = {part.strip() for part in answer.replace("\n", ",").replace(" ", ",").split(",") if part.strip()}
        selected = [service for i, service in enumerate(services, 1) if service in picked or str(i) in picked]
        if not selected: raise RuntimeError("Choose at least one service")
    else:
        selected = []
    for index, service in enumerate(services, 1):
        print(f"  {index}. {service}")
    while sys.stdin.isatty():
        answer = prompt("Services to track (comma-separated numbers)")
        try:
            indexes = sorted({int(part.strip()) for part in answer.split(",")})
            if indexes and all(1 <= value <= len(services) for value in indexes):
                selected = [services[value - 1] for value in indexes]
                break
        except ValueError:
            pass
        print("Choose one or more listed service numbers, separated by commas.")
    while True:
        project_id = prompt("Runtime project ID (letters, numbers, dot, dash, underscore)")
        if SLUG.fullmatch(project_id): break
        print("Project ID must be 1–128 letters, numbers, dots, dashes, or underscores.")
    while True:
        environment = prompt("Environment", "development")
        if SLUG.fullmatch(environment): break
        print("Environment must be 1–128 letters, numbers, dots, dashes, or underscores.")
    commit_result = run(["git", "rev-parse", "HEAD"], cwd=Path(project["directory"]), capture=True, check=False)
    proposed = commit_result.stdout.strip().lower() if commit_result.returncode == 0 else ""
    print(f"Detected Git commit: {proposed or 'not available'}")
    if proposed and COMMIT.fullmatch(proposed):
        commit = proposed
    else:
        while True:
            commit = prompt("Enter the full lowercase Git commit SHA-1 or SHA-256")
            if COMMIT.fullmatch(commit): break
            print("Commit must be a full lowercase 40-character SHA-1 or 64-character SHA-256.")

    files = [Path(path) for path in project["files"]]
    for path in files:
        if not path.is_file():
            raise RuntimeError(f"Compose file no longer exists: {path}")
    contents = {path: path.read_text() for path in files}
    services_by_file = {}
    for path in files:
        try:
            services_by_file[path] = yaml_services(contents[path])
        except RuntimeError as exc:
            raise RuntimeError(f"Could not safely read Compose file {path}: {exc}") from exc
    owners = {}
    for service in selected:
        for path in files:
            if service in services_by_file[path]:
                owners[service] = path
    if set(owners) != set(selected):
        raise RuntimeError("Could not locate all selected service definitions in the Compose files")
    CURRENT_SETUP.update({"version": 1, "project_id": project_id, "environment": environment,
                          "commit": commit, "project": project, "services": selected,
                          "compose_originals": {}, "compose_hashes": {}, "phase": "starting"})
    labels = {}
    proposed_texts = dict(contents)
    for service in selected:
        values = {LABELS["enabled"]: "true", LABELS["project"]: project_id,
                  LABELS["environment"]: environment, LABELS["commit"]: commit, LABELS["service"]: service}
        path = owners[service]
        try:
            proposed_texts[path] = add_service_labels(proposed_texts[path], service, values)
        except RuntimeError as exc:
            raise RuntimeError(f"Could not safely propose labels for {service} in {path}: {exc}") from exc
        labels[service] = values
    changed = [path for path in files if contents[path] != proposed_texts[path]]
    if not changed:
        raise RuntimeError("Selected services already have the requested telemetry labels")
    changed_paths = ", ".join(str(path) for path in changed)
    print(f"\nRuntime setup summary: Compose project {project['name']}; services {', '.join(selected)}; files {changed_paths}; selected services will be recreated.")
    required_approval("Approve Runtime setup", [
        f"Read the selected Compose files and Git commit ({', '.join(str(path) for path in files)})",
        f"Create local certificates under {CERT_DIR}" if cert_directory_is_empty_scaffold() else "Reuse the existing local certificates",
        "Update API host authorization scope",
        f"Apply telemetry labels to selected services: {', '.join(selected)}",
        f"Recreate selected services in Compose project {project['name']}",
        "Start or refresh the local API ingress and telemetry agent",
    ], summary=f"Compose project: {project['name']}\nServices: {', '.join(selected)}\nAffected files: {changed_paths}")
    CURRENT_SETUP["label_values"] = labels
    for path in changed:
        CURRENT_SETUP["compose_originals"][str(path)] = contents[path]
        CURRENT_SETUP["compose_hashes"][str(path)] = digest(proposed_texts[path])
    write_state(CURRENT_SETUP)
    try:
        for path in changed:
            if digest(path.read_text()) != digest(contents[path]):
                raise RuntimeError(f"Compose file changed after approval: {path}; no further edits were applied")
            write_text_atomic(path, proposed_texts[path])
    except Exception:
        print(f"\nSetup stopped. Recovery copies are recorded at {state_path()}.")
        raise
    CURRENT_SETUP["phase"] = "compose-labeled"
    write_state(CURRENT_SETUP)
    try:
        _, fingerprint = start_ingress_and_agent()
    except Exception:
        print(f"\nSetup stopped. Recovery copies and progress are recorded at {state_path()}.")
        print("Run ./devassistant.sh runtime status for the current setup state.")
        raise

    recreate = base + ["up", "-d", "--no-deps", *selected]
    run(recreate, cwd=Path(project["directory"]))
    CURRENT_SETUP["phase"] = "active"
    write_state(CURRENT_SETUP)
    print("\nSetup complete.")
    print(f"Runtime project: {project_id} · {environment} · {commit}")
    print("Open Runtime and select the project/commit. Container reporting needs no source snapshot.")
    print("Optional: add source snapshots for code matching, and spans for routes and database activity.")
    print("Check setup: ./devassistant.sh runtime status")
    print("Remove setup: ./devassistant.sh runtime cleanup")


def status() -> None:
    require_terminal()
    state = read_state()
    if not state:
        print("No local Runtime tracking setup is recorded.")
        return
    print(f"Runtime setup: {state.get('phase', 'incomplete')}")
    print(f"Project: {state.get('project_id')} · {state.get('environment')} · {state.get('commit')}")
    print(f"Compose project: {state.get('project', {}).get('name')}")
    print(f"Services: {', '.join(state.get('services', []))}")
    print(f"Host certificate: {state.get('fingerprint', 'not provisioned')}")
    print(f"Recovery state: {state_path()}")
    if confirm("Allow Docker status inspection for the local API ingress and host agent?"):
        run(["docker", "compose", "--profile", "runtime-host", "ps", "python-api", "telemetry-caddy"], cwd=API, check=False)
        env = os.environ.copy()
        env.update({"HOST_IDENTITY_DIR": str(CERT_DIR / "agent"), "BACKEND_URL": "https://telemetry-caddy:9443"})
        run(["docker", "compose", "-f", "telemetry-agent/docker-compose.yml", "-f",
             "telemetry-agent/docker-compose.local.yml", "ps"], cwd=API, env=env, check=False)


def managed_agent_environment(*, require_active: bool = True) -> tuple[dict, dict] | tuple[None, None]:
    state = read_state()
    if not state:
        return None, None
    if require_active and state.get("phase") not in {"active", "labels-applied-services-not-recreated"}:
        print(f"Local Runtime setup is {state.get('phase', 'incomplete')}; recover it with runtime status/cleanup before starting services.")
        return None, None
    required = [CERT_DIR / "agent" / name for name in ("ca.pem", "host.crt", "host.key")]
    if not all(path.is_file() for path in required):
        print(f"Local host identity is incomplete at {CERT_DIR}; the telemetry agent was not started.")
        return None, None
    socket_path = Path(os.environ.get("DOCKER_SOCK_PATH", "/var/run/docker.sock"))
    if not socket_path.exists():
        print(f"Docker socket not found at {socket_path}; the telemetry agent was not started.")
        return None, None
    env = os.environ.copy()
    env.update({"HOST_IDENTITY_DIR": str(CERT_DIR / "agent"),
                "BACKEND_URL": "https://telemetry-caddy:9443",
                "DOCKER_SOCK_PATH": str(socket_path),
                "DOCKER_SOCK_GID": docker_socket_group_id(socket_path)})
    args = ["docker", "compose", "-f", "telemetry-agent/docker-compose.yml", "-f",
            "telemetry-agent/docker-compose.local.yml"]
    return {"env": env, "cwd": API}, {"args": args}


def ensure_host_services() -> None:
    command, compose = managed_agent_environment()
    if command is None:
        # Clean up a Caddy container left behind by older default-profile startup.
        run(["docker", "compose", "--profile", "runtime-host", "stop", "telemetry-caddy"], cwd=API, check=False)
        if not read_state():
            print("No configured local Runtime agent to start.")
        return
    print("==> Starting local Runtime ingress and host agent")
    run(["docker", "compose", "--profile", "runtime-host", "up", "-d", "python-api", "telemetry-caddy"],
        cwd=API)
    run(compose["args"] + ["up", "-d"], cwd=command["cwd"], env=command["env"])


def stop_host_services() -> None:
    run(["docker", "compose", "--profile", "runtime-host", "stop", "telemetry-caddy"], cwd=API, check=False)
    command, compose = managed_agent_environment(require_active=False)
    if command is None:
        if not read_state():
            print("No configured local Runtime agent to stop.")
        return
    print("==> Stopping local Runtime host agent")
    run(compose["args"] + ["down"], cwd=command["cwd"], env=command["env"])


def restore_compose(state: dict) -> bool:
    safe = True
    for raw_path, original in state.get("compose_originals", {}).items():
        path = Path(raw_path)
        if not path.exists():
            print(f"Compose file is missing; recovery copy remains in state: {path}")
            safe = False
            continue
        write_text_atomic(path, original)
    return safe


def cleanup() -> dict:
    require_terminal()
    state = read_state()
    if not state:
        print("No local Runtime tracking setup is recorded.")
        return {"status": "complete", "message": "No local Runtime setup record was present."}
    compose_files = list(state.get("compose_originals", {}).keys())
    certs_applicable = bool(state.get("certificates_created") and CERT_DIR.exists())
    actions = [
        "Stop the telemetry agent and Runtime ingress",
        f"Restore every saved Compose file ({', '.join(compose_files) or 'none'})",
        f"Recreate selected Compose services without Runtime labels ({', '.join(state.get('services', [])) or 'none'})",
        "Remove the host/project/environment authorization",
        "Refresh python-api when required",
    ]
    if certs_applicable:
        actions.append(f"Permanently remove generated development certificates under {CERT_DIR}")
    print(f"\nRuntime cleanup summary: files {', '.join(compose_files) or 'none'}; project {state.get('project_id')}; services {', '.join(state.get('services', [])) or 'none'}.")
    required_approval("Approve Runtime cleanup", actions,
                      summary=f"Project: {state.get('project_id')} · {state.get('environment')}\nAffected files: {', '.join(compose_files) or 'none'}")
    try:
        project = state.get("project") if isinstance(state.get("project"), dict) else None
        services = [item for item in state.get("services", []) if isinstance(item, str) and SLUG.fullmatch(item)]
        if not project or not services:
            raise RuntimeError("Saved Runtime project metadata is incomplete; selected services could not be recreated safely")
        compose_ok = restore_compose(state)
        if not compose_ok:
            raise RuntimeError("One or more saved Compose files could not be restored")
        run(compose_args(project) + ["up", "-d", "--no-deps", *services], cwd=Path(project["directory"]))
        env = os.environ.copy()
        env.update({"HOST_IDENTITY_DIR": str(CERT_DIR / "agent"), "BACKEND_URL": "https://telemetry-caddy:9443"})
        run(["docker", "compose", "-f", "telemetry-agent/docker-compose.yml", "-f",
             "telemetry-agent/docker-compose.local.yml", "down"], cwd=API, env=env, check=False)
        run(["docker", "compose", "--profile", "runtime-host", "stop", "telemetry-caddy"], cwd=API, check=False)
        fingerprint = state.get("fingerprint")
        if fingerprint and state.get("scope_original"):
            before, after = update_env_scope(fingerprint, state["project_id"], state["environment"], remove=True)
            if before != after:
                write_text_atomic(ENV_FILE, after)
                run(["docker", "compose", "up", "-d", "python-api"], cwd=API, check=False)
        if certs_applicable:
            shutil.rmtree(CERT_DIR)
    except Exception as exc:
        state["phase"] = "cleanup-incomplete"
        state["cleanup_error"] = str(exc)
        write_state(state)
        return {"status": "error", "message": f"Cleanup is incomplete; recovery state was retained: {exc}"}
    write_state(None)
    return {"status": "complete", "message": "Local Runtime setup cleaned up."}


def main() -> int:
    commands = {"setup": setup, "status": status, "cleanup": cleanup,
                "ensure-host-services": ensure_host_services, "stop-host-services": stop_host_services,
                "helper": serve_helper}
    if len(sys.argv) != 2 or sys.argv[1] not in commands:
        print("Usage: ./devassistant.sh runtime {setup|status|cleanup|ensure-host-services|stop-host-services|helper}", file=sys.stderr)
        return 2
    try:
        commands[sys.argv[1]]()
        return 0
    except (RuntimeError, OSError, KeyboardInterrupt) as exc:
        if isinstance(exc, KeyboardInterrupt):
            print("\nInterrupted. Any completed changes remain recorded for recovery.", file=sys.stderr)
            return 130
        print(f"Runtime setup error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
