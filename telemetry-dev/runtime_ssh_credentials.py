"""Host OS credential store and platform-compatible SSH agent for Runtime ClickHouse."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import atexit
import hashlib
import tempfile


SERVICE = "com.devassistant.runtime.ssh"
ACCOUNT = "devassistant-runtime"
STATE_DIR = Path(os.environ.get("DEVASSISTANT_RUNTIME_HOST_DIR", Path.home() / ".local" / "state" / "devassistant" / "runtime-host"))
AGENT_SOCKET = STATE_DIR / "ssh-agent.sock"
AGENT_PID = STATE_DIR / "ssh-agent.pid"
ASKPASS = Path(__file__).with_name("runtime-ssh-askpass.sh")
KEYCHAIN_HELPER = Path(__file__).with_name("runtime_keychain.swift")
SHARED_AGENT_SOCKET = Path(os.environ["RUNTIME_SSH_AGENT_SOCKET"]) if os.environ.get("RUNTIME_SSH_AGENT_SOCKET") else None
PUBLIC_KEY_FILE = STATE_DIR / "runtime-ssh-key.pub"
BEARER_SERVICE = "com.devassistant.code-quality.bearer"
GITHUB_SERVICE = "com.devassistant.code-quality.github"
OUTLINE_SERVICE = "com.devassistant.code-quality.outline"
SONARQUBE_SERVICE = "com.devassistant.code-quality.sonarqube"


def _bearer_account(target_id: str) -> str:
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", target_id):
        raise ValueError("Invalid performance target identity.")
    return "target-" + hashlib.sha256(target_id.lower().encode("ascii")).hexdigest()


def _github_account(target_id: str) -> str:
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", target_id):
        raise ValueError("Invalid Git source credential identity.")
    return "git-source-" + hashlib.sha256(target_id.lower().encode("ascii")).hexdigest()


def _outline_account(target_id: str) -> str:
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", target_id):
        raise ValueError("Invalid Outline credential identity.")
    return "outline-" + hashlib.sha256(target_id.lower().encode("ascii")).hexdigest()


def _sonarqube_account(target_id: str) -> str:
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", target_id):
        raise ValueError("Invalid SonarQube credential identity.")
    return "sonarqube-" + hashlib.sha256(target_id.lower().encode("ascii")).hexdigest()


class CredentialStoreError(RuntimeError):
    def __init__(self, message: str, *, locked: bool = False):
        super().__init__(message)
        self.locked = locked


def _provider() -> str:
    if sys.platform == "darwin":
        return "keychain"
    if sys.platform == "linux" and os.environ.get("DBUS_SESSION_BUS_ADDRESS") and shutil.which("secret-tool"):
        return "secret-service"
    raise CredentialStoreError("A macOS Keychain or an unlocked Linux Secret Service keyring is required.", locked=True)


def _run(command: list[str], *, input_data: bytes | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(command, input=input_data, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, check=False, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CredentialStoreError("The OS credential store could not be reached. Unlock it and retry.", locked=True) from exc


def _keychain_request(action: str, value: dict | None = None, *, service: str = SERVICE,
                      account: str = ACCOUNT) -> dict:
    request = {"action": action, "account": account, "service": service}
    if value is not None:
        request["value"] = value
    result = _run(["/usr/bin/swift", str(KEYCHAIN_HELPER)],
                  input_data=json.dumps(request, separators=(",", ":")).encode("utf-8"))
    if result.returncode:
        diagnostic = result.stderr.decode("utf-8", "replace").lower()
        locked = "keychain-locked" in diagnostic
        message = ("Unlock the macOS Keychain, then retry." if locked
                   else "The OS credential store could not complete this operation.")
        raise CredentialStoreError(message, locked=locked)
    try:
        response = json.loads(result.stdout.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise CredentialStoreError("The macOS Keychain returned an invalid response.") from exc
    if not isinstance(response, dict):
        raise CredentialStoreError("The macOS Keychain returned an invalid response.")
    return response


def _store_secret(value: dict) -> None:
    provider = _provider()
    data = json.dumps(value, separators=(",", ":")).encode("utf-8")
    if provider == "keychain":
        _keychain_request("store", value)
        return
    else:
        result = _run(["secret-tool", "store", "--label=Developer Assistant Runtime SSH", "app", ACCOUNT, "purpose", "runtime-ssh"], input_data=data)
    if result.returncode:
        text = result.stderr.decode("utf-8", "replace").lower()
        locked = any(word in text for word in ("locked", "unlock", "interaction not allowed", "no such secret collection"))
        raise CredentialStoreError(
            "Unlock the macOS Keychain or Linux keyring, then save the SSH credential again." if locked
            else "The SSH credential could not be saved to the OS credential store.", locked=locked,
        )


def _read_secret() -> dict | None:
    provider = _provider()
    if provider == "keychain":
        response = _keychain_request("read")
        value = response.get("value") if response.get("found") is True else None
        if value is None:
            return None
        if not isinstance(value, dict):
            raise CredentialStoreError("The saved Runtime SSH credential is invalid. Remove it and save it again.")
        return value
    result = _run(["secret-tool", "lookup", "app", ACCOUNT, "purpose", "runtime-ssh"])
    if result.returncode:
        text = result.stderr.decode("utf-8", "replace").lower()
        if any(word in text for word in ("locked", "unlock", "interaction not allowed", "no such secret collection")):
            raise CredentialStoreError("Unlock the macOS Keychain or Linux keyring, then retry.", locked=True)
        return None
    try:
        value = json.loads(result.stdout.decode("utf-8").strip())
    except (UnicodeError, ValueError) as exc:
        raise CredentialStoreError("The saved Runtime SSH credential is invalid. Remove it and save it again.") from exc
    if not isinstance(value, dict):
        raise CredentialStoreError("The saved Runtime SSH credential is invalid. Remove it and save it again.")
    return value


def _delete_secret() -> None:
    provider = _provider()
    if provider == "keychain":
        _keychain_request("delete")
        return
    result = _run(["secret-tool", "clear", "app", ACCOUNT, "purpose", "runtime-ssh"])
    if result.returncode:
        text = result.stderr.decode("utf-8", "replace").lower()
        if any(word in text for word in ("locked", "unlock", "interaction not allowed", "no such secret collection")):
            raise CredentialStoreError("Unlock the macOS Keychain or Linux keyring, then retry.", locked=True)


def save_bearer_token(target_id: str, token: str) -> dict:
    """Store a benchmark token under a target-specific OS credential item."""
    if not isinstance(token, str) or not token.strip() or len(token) > 8192:
        raise ValueError("Provide a valid Bearer token up to 8192 characters.")
    account = _bearer_account(target_id)
    value = {"token": token.strip()}
    if _provider() == "keychain":
        _keychain_request("store", value, service=BEARER_SERVICE, account=account)
    else:
        result = _run(["secret-tool", "store", "--label=Developer Assistant Code Quality Bearer token",
                       "app", account, "purpose", "code-quality-bearer"],
                      input_data=json.dumps(value, separators=(",", ":")).encode("utf-8"))
        if result.returncode:
            text = result.stderr.decode("utf-8", "replace").lower()
            locked = any(word in text for word in ("locked", "unlock", "interaction not allowed", "no such secret collection"))
            raise CredentialStoreError("Unlock the OS keyring and retry." if locked else "The Bearer token could not be saved to the OS credential store.", locked=locked)
    return {"saved": True}


def bearer_token_status(target_id: str) -> dict:
    account = _bearer_account(target_id)
    if _provider() == "keychain":
        response = _keychain_request("read", service=BEARER_SERVICE, account=account)
        value = response.get("value") if response.get("found") is True else None
    else:
        result = _run(["secret-tool", "lookup", "app", account, "purpose", "code-quality-bearer"])
        if result.returncode:
            text = result.stderr.decode("utf-8", "replace").lower()
            if any(word in text for word in ("locked", "unlock", "interaction not allowed", "no such secret collection")):
                raise CredentialStoreError("Unlock the OS keyring and retry.", locked=True)
            value = None
        else:
            try:
                value = json.loads(result.stdout.decode("utf-8").strip())
            except (UnicodeError, ValueError) as exc:
                raise CredentialStoreError("The saved Bearer token is invalid. Replace it and retry.") from exc
    if value is not None and (not isinstance(value, dict) or not isinstance(value.get("token"), str)):
        raise CredentialStoreError("The saved Bearer token is invalid. Replace it and retry.")
    return {"saved": value is not None}


def use_bearer_token_for_benchmark(target_id: str) -> str:
    """Return a target-scoped token only to the one-shot benchmark operation."""
    account = _bearer_account(target_id)
    if _provider() == "keychain":
        response = _keychain_request("read", service=BEARER_SERVICE, account=account)
        value = response.get("value") if response.get("found") is True else None
    else:
        result = _run(["secret-tool", "lookup", "app", account, "purpose", "code-quality-bearer"])
        if result.returncode:
            text = result.stderr.decode("utf-8", "replace").lower()
            if any(word in text for word in ("locked", "unlock", "interaction not allowed", "no such secret collection")):
                raise CredentialStoreError("Unlock the OS keyring and retry.", locked=True)
            value = None
        else:
            try:
                value = json.loads(result.stdout.decode("utf-8").strip())
            except (UnicodeError, ValueError) as exc:
                raise CredentialStoreError("The saved Bearer token is invalid. Replace it and retry.") from exc
    token = value.get("token") if isinstance(value, dict) else None
    if not isinstance(token, str) or not token:
        raise CredentialStoreError("No Bearer token is saved for this target. Save one and retry.")
    return token


def remove_bearer_token(target_id: str) -> dict:
    account = _bearer_account(target_id)
    if _provider() == "keychain":
        _keychain_request("delete", service=BEARER_SERVICE, account=account)
    else:
        result = _run(["secret-tool", "clear", "app", account, "purpose", "code-quality-bearer"])
        if result.returncode:
            text = result.stderr.decode("utf-8", "replace").lower()
            if any(word in text for word in ("locked", "unlock", "interaction not allowed", "no such secret collection")):
                raise CredentialStoreError("Unlock the OS keyring and retry.", locked=True)
    return {"deleted": True}


def save_outline_token(target_id: str, token: str) -> dict:
    if not isinstance(token, str) or not token.strip() or len(token) > 8192:
        raise ValueError("Provide a valid Outline API token.")
    account = _outline_account(target_id)
    value = {"token": token.strip()}
    if _provider() == "keychain":
        _keychain_request("store", value, service=OUTLINE_SERVICE, account=account)
    else:
        result = _run(["secret-tool", "store", "--label=Developer Assistant Outline API token",
                       "app", account, "purpose", "code-quality-outline"],
                      input_data=json.dumps(value, separators=(",", ":")).encode("utf-8"))
        if result.returncode:
            text = result.stderr.decode("utf-8", "replace").lower()
            locked = any(word in text for word in ("locked", "unlock", "interaction not allowed", "no such secret collection"))
            raise CredentialStoreError("Unlock the OS keyring and retry." if locked else "The Outline token could not be saved to the OS credential store.", locked=locked)
    return {"saved": True}


def _read_outline_token(target_id: str) -> dict | None:
    account = _outline_account(target_id)
    if _provider() == "keychain":
        response = _keychain_request("read", service=OUTLINE_SERVICE, account=account)
        value = response.get("value") if response.get("found") is True else None
    else:
        result = _run(["secret-tool", "lookup", "app", account, "purpose", "code-quality-outline"])
        if result.returncode:
            text = result.stderr.decode("utf-8", "replace").lower()
            if any(word in text for word in ("locked", "unlock", "interaction not allowed", "no such secret collection")):
                raise CredentialStoreError("Unlock the OS keyring and retry.", locked=True)
            value = None
        else:
            try:
                value = json.loads(result.stdout.decode("utf-8").strip())
            except (UnicodeError, ValueError) as exc:
                raise CredentialStoreError("The saved Outline token is invalid. Replace it and retry.") from exc
    if value is not None and (not isinstance(value, dict) or not isinstance(value.get("token"), str)):
        raise CredentialStoreError("The saved Outline token is invalid. Replace it and retry.")
    return value


def outline_token_status(target_id: str) -> dict:
    return {"saved": _read_outline_token(target_id) is not None}


def use_outline_token(target_id: str) -> str:
    value = _read_outline_token(target_id)
    token = value.get("token") if isinstance(value, dict) else None
    if not isinstance(token, str) or not token:
        raise ValueError("No Outline API token is saved. Save one and retry.")
    return token


def remove_outline_token(target_id: str) -> dict:
    account = _outline_account(target_id)
    if _provider() == "keychain":
        _keychain_request("delete", service=OUTLINE_SERVICE, account=account)
    else:
        result = _run(["secret-tool", "clear", "app", account, "purpose", "code-quality-outline"])
        if result.returncode:
            text = result.stderr.decode("utf-8", "replace").lower()
            if any(word in text for word in ("locked", "unlock", "interaction not allowed", "no such secret collection")):
                raise CredentialStoreError("Unlock the OS keyring and retry.", locked=True)
    return {"deleted": True}


def save_sonarqube_token(target_id: str, token: str) -> dict:
    if not isinstance(token, str) or not token.strip() or len(token) > 8192:
        raise ValueError("Provide a valid SonarQube token.")
    account = _sonarqube_account(target_id)
    value = {"token": token.strip()}
    if _provider() == "keychain":
        _keychain_request("store", value, service=SONARQUBE_SERVICE, account=account)
    else:
        result = _run(["secret-tool", "store", "--label=Developer Assistant SonarQube token",
                       "app", account, "purpose", "code-quality-sonarqube"],
                      input_data=json.dumps(value, separators=(",", ":")).encode("utf-8"))
        if result.returncode:
            diagnostic = result.stderr.decode("utf-8", "replace").lower()
            locked = any(word in diagnostic for word in ("locked", "unlock", "interaction not allowed", "no such secret collection"))
            raise CredentialStoreError("Unlock the OS keyring and retry." if locked
                                       else "The SonarQube token could not be saved to the OS credential store.", locked=locked)
    return {"saved": True}


def _read_sonarqube_token(target_id: str) -> dict | None:
    account = _sonarqube_account(target_id)
    if _provider() == "keychain":
        response = _keychain_request("read", service=SONARQUBE_SERVICE, account=account)
        value = response.get("value") if response.get("found") is True else None
    else:
        result = _run(["secret-tool", "lookup", "app", account, "purpose", "code-quality-sonarqube"])
        if result.returncode:
            diagnostic = result.stderr.decode("utf-8", "replace").lower()
            if any(word in diagnostic for word in ("locked", "unlock", "interaction not allowed", "no such secret collection")):
                raise CredentialStoreError("Unlock the OS keyring and retry.", locked=True)
            value = None
        else:
            try:
                value = json.loads(result.stdout.decode("utf-8").strip())
            except (UnicodeError, ValueError) as exc:
                raise CredentialStoreError("The saved SonarQube token is invalid. Replace it and retry.") from exc
    if value is not None and (not isinstance(value, dict) or not isinstance(value.get("token"), str)):
        raise CredentialStoreError("The saved SonarQube token is invalid. Replace it and retry.")
    return value


def sonarqube_token_status(target_id: str) -> dict:
    return {"saved": _read_sonarqube_token(target_id) is not None}


def use_sonarqube_token(target_id: str) -> str:
    value = _read_sonarqube_token(target_id)
    token = value.get("token") if isinstance(value, dict) else None
    if not isinstance(token, str) or not token:
        raise ValueError("No SonarQube token is saved. Save one and retry.")
    return token


def remove_sonarqube_token(target_id: str) -> dict:
    account = _sonarqube_account(target_id)
    if _provider() == "keychain":
        _keychain_request("delete", service=SONARQUBE_SERVICE, account=account)
    else:
        result = _run(["secret-tool", "clear", "app", account, "purpose", "code-quality-sonarqube"])
        if result.returncode:
            diagnostic = result.stderr.decode("utf-8", "replace").lower()
            if any(word in diagnostic for word in ("locked", "unlock", "interaction not allowed", "no such secret collection")):
                raise CredentialStoreError("Unlock the OS keyring and retry.", locked=True)
    return {"deleted": True}


def save_github_token(target_id: str, token: str) -> dict:
    """Save a GitHub read-only token under an isolated source identity."""
    if not isinstance(token, str) or not token.strip() or len(token) > 8192:
        raise ValueError("Provide a GitHub token up to 8192 characters.")
    account = _github_account(target_id)
    value = {"token": token.strip()}
    if _provider() == "keychain":
        _keychain_request("store", value, service=GITHUB_SERVICE, account=account)
    else:
        result = _run(["secret-tool", "store", "--label=Developer Assistant GitHub source token",
                       "app", account, "purpose", "code-quality-github-source"],
                      input_data=json.dumps(value, separators=(",", ":")).encode("utf-8"))
        if result.returncode:
            text = result.stderr.decode("utf-8", "replace").lower()
            locked = any(word in text for word in ("locked", "unlock", "interaction not allowed", "no such secret collection"))
            raise CredentialStoreError("Unlock the OS keyring and retry." if locked else "The GitHub token could not be saved to the OS credential store.", locked=locked)
    return {"saved": True}


def github_token_status(target_id: str) -> dict:
    account = _github_account(target_id)
    if _provider() == "keychain":
        response = _keychain_request("read", service=GITHUB_SERVICE, account=account)
        value = response.get("value") if response.get("found") is True else None
    else:
        result = _run(["secret-tool", "lookup", "app", account, "purpose", "code-quality-github-source"])
        if result.returncode:
            text = result.stderr.decode("utf-8", "replace").lower()
            if any(word in text for word in ("locked", "unlock", "interaction not allowed", "no such secret collection")):
                raise CredentialStoreError("Unlock the OS keyring and retry.", locked=True)
            value = None
        else:
            try:
                value = json.loads(result.stdout.decode("utf-8").strip())
            except (UnicodeError, ValueError) as exc:
                raise CredentialStoreError("The saved GitHub token is invalid. Replace it and retry.") from exc
    if value is not None and (not isinstance(value, dict) or not isinstance(value.get("token"), str)):
        raise CredentialStoreError("The saved GitHub token is invalid. Replace it and retry.")
    return {"saved": value is not None}


def use_github_token_for_fetch(target_id: str) -> str:
    account = _github_account(target_id)
    if _provider() == "keychain":
        response = _keychain_request("read", service=GITHUB_SERVICE, account=account)
        value = response.get("value") if response.get("found") is True else None
    else:
        result = _run(["secret-tool", "lookup", "app", account, "purpose", "code-quality-github-source"])
        if result.returncode:
            text = result.stderr.decode("utf-8", "replace").lower()
            if any(word in text for word in ("locked", "unlock", "interaction not allowed", "no such secret collection")):
                raise CredentialStoreError("Unlock the OS keyring and retry.", locked=True)
            value = None
        else:
            try:
                value = json.loads(result.stdout.decode("utf-8").strip())
            except (UnicodeError, ValueError) as exc:
                raise CredentialStoreError("The saved GitHub token is invalid. Replace it and retry.") from exc
    token = value.get("token") if isinstance(value, dict) else None
    if not isinstance(token, str) or not token:
        raise CredentialStoreError("No GitHub token is saved for this project. Save one and retry.")
    return token


def remove_github_token(target_id: str) -> dict:
    account = _github_account(target_id)
    if _provider() == "keychain":
        _keychain_request("delete", service=GITHUB_SERVICE, account=account)
    else:
        result = _run(["secret-tool", "clear", "app", account, "purpose", "code-quality-github-source"])
        if result.returncode:
            text = result.stderr.decode("utf-8", "replace").lower()
            if any(word in text for word in ("locked", "unlock", "interaction not allowed", "no such secret collection")):
                raise CredentialStoreError("Unlock the OS keyring and retry.", locked=True)
    return {"deleted": True}


def _agent_env() -> dict[str, str]:
    return {**os.environ, "SSH_AUTH_SOCK": str(SHARED_AGENT_SOCKET or AGENT_SOCKET)}


def _agent_socket() -> Path:
    return SHARED_AGENT_SOCKET or AGENT_SOCKET


def ensure_agent() -> None:
    if sys.platform == "darwin" and SHARED_AGENT_SOCKET is None:
        raise CredentialStoreError(
            "Docker Desktop needs the macOS desktop SSH agent. Unlock the desktop session or start its SSH agent, then restart the Runtime setup helper.",
            locked=True,
        )
    if SHARED_AGENT_SOCKET is not None:
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.settimeout(0.25)
            probe.connect(str(SHARED_AGENT_SOCKET))
            return
        except OSError:
            pass
        finally:
            probe.close()
        raise CredentialStoreError(
            "The macOS SSH agent is unavailable. Unlock the desktop session or start its SSH agent, then retry.",
            locked=True,
        )
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(STATE_DIR, 0o700)
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.settimeout(0.25)
        probe.connect(str(AGENT_SOCKET))
        return
    except OSError:
        pass
    finally:
        probe.close()
    if AGENT_SOCKET.exists():
        AGENT_SOCKET.unlink()
    result = subprocess.run(["ssh-agent", "-a", str(AGENT_SOCKET), "-s"], capture_output=True, text=True, check=False, timeout=5)
    if result.returncode:
        raise CredentialStoreError("The Runtime SSH agent could not start.")
    match = re.search(r"SSH_AGENT_PID=(\d+);", result.stdout)
    if match:
        AGENT_PID.write_text(match.group(1), encoding="ascii")
        os.chmod(AGENT_PID, 0o600)


def _askpass_env(passphrase: str, askpass_once_dir: Path) -> dict[str, str]:
    env = _agent_env()
    if not passphrase:
        return env
    env.update({"SSH_ASKPASS": str(ASKPASS), "SSH_ASKPASS_REQUIRE": "force",
                "DISPLAY": env.get("DISPLAY", "devassistant:0"),
                "DEVASSISTANT_ASKPASS_PASSPHRASE": passphrase,
                "DEVASSISTANT_ASKPASS_ONCE_DIR": str(askpass_once_dir)})
    return env


def _normalize_private_key(private_key: str) -> str:
    normalized = private_key.replace("\r\n", "\n").replace("\r", "\n").strip()
    return normalized + "\n"


def _public_key(private_key: str, passphrase: str = "") -> str:
    private_key = _normalize_private_key(private_key)
    # ssh-keygen rejects piped input, and macOS OpenSSH does not reliably
    # accept askpass output when its key file is an anonymous /dev/fd path.
    # Validate through an isolated agent instead: ssh-add reads the key from
    # stdin, and the temporary agent socket is removed immediately afterward.
    with tempfile.TemporaryDirectory(prefix="da-ssh-") as directory:
        agent_socket = Path(directory) / "agent.sock"
        started = subprocess.run(
            ["ssh-agent", "-a", str(agent_socket), "-s"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=5,
        )
        match = re.search(r"SSH_AGENT_PID=(\d+);", started.stdout)
        if started.returncode or not match:
            raise CredentialStoreError("A temporary SSH agent could not be started to validate the key.")

        env = _askpass_env(passphrase, Path(directory) / "askpass-used")
        env["SSH_AUTH_SOCK"] = str(agent_socket)
        try:
            added = subprocess.run(
                ["ssh-add", "-"],
                input=private_key.encode("utf-8"),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=10,
            )
            if added.returncode:
                diagnostic = added.stderr.decode("utf-8", "replace").lower()
                passphrase_was_requested = (Path(directory) / "askpass-used").exists()
                if "incorrect passphrase" in diagnostic or "passphrase" in diagnostic or passphrase_was_requested:
                    raise CredentialStoreError(
                        "The SSH key passphrase did not unlock this key. Enter its actual passphrase, or leave this blank for an unencrypted key."
                    )
                raise CredentialStoreError(
                    "The SSH private key could not be read. Paste the complete private key, not its .pub file, and check its format."
                )
            listed = subprocess.run(
                ["ssh-add", "-L"],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=5,
            )
            if listed.returncode:
                raise CredentialStoreError("The SSH private key did not produce a valid public key.")
            value = listed.stdout.decode("ascii", "strict").strip()
        finally:
            subprocess.run(
                ["ssh-agent", "-k"],
                env={**os.environ, "SSH_AGENT_PID": match.group(1), "SSH_AUTH_SOCK": str(agent_socket)},
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=5,
            )
    if len(value.split()) < 2:
        raise CredentialStoreError("The SSH private key did not produce a valid public key.")
    return value.splitlines()[0]


def _agent_has_key(public_key: str) -> bool:
    result = subprocess.run(["ssh-add", "-L"], env=_agent_env(), stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, check=False, timeout=5)
    if result.returncode:
        return False
    identity = " ".join(public_key.split()[:2])
    return any(" ".join(line.decode("ascii", "ignore").split()[:2]) == identity
               for line in result.stdout.splitlines())


def _remove_agent_key(public_key: str | None, *, owned: bool) -> None:
    if not public_key or not owned or not SHARED_AGENT_SOCKET:
        return
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(STATE_DIR, 0o700)
    PUBLIC_KEY_FILE.write_text(public_key + "\n", encoding="ascii")
    os.chmod(PUBLIC_KEY_FILE, 0o600)
    subprocess.run(["ssh-add", "-d", str(PUBLIC_KEY_FILE)], env=_agent_env(),
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   check=False, timeout=5)
    PUBLIC_KEY_FILE.unlink(missing_ok=True)


def _load_into_agent(credentials: dict | None) -> bool:
    ensure_agent()
    if not credentials:
        if SHARED_AGENT_SOCKET is None:
            subprocess.run(["ssh-add", "-D"], env=_agent_env(), stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, check=False, timeout=5)
        return False
    key = credentials.get("private_key")
    passphrase = credentials.get("passphrase", "")
    if not isinstance(key, str) or not key.strip() or not isinstance(passphrase, str):
        raise CredentialStoreError("Saved SSH credentials are incomplete. Remove them and save them again.")
    key = _normalize_private_key(key)
    credentials["private_key"] = key
    public_key = credentials.get("public_key") or _public_key(key, passphrase)
    credentials["public_key"] = public_key
    if _agent_has_key(public_key):
        if SHARED_AGENT_SOCKET:
            credentials.setdefault("agent_identity_added_by_runtime", False)
        return True
    with tempfile.TemporaryDirectory(prefix="da-ssh-askpass-") as directory:
        env = _askpass_env(passphrase, Path(directory) / "askpass-used")
        command = ["ssh-add", "-t", "8h", "-"] if SHARED_AGENT_SOCKET else ["ssh-add", "-"]
        result = subprocess.run(command, input=key.encode("utf-8"), env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                check=False, timeout=10)
    if result.returncode:
        raise CredentialStoreError("The SSH private key could not be loaded. Check its format and passphrase.")
    if SHARED_AGENT_SOCKET:
        credentials["agent_identity_added_by_runtime"] = True
    return True


def save_credentials(private_key: str, passphrase: str = "") -> dict:
    if not isinstance(private_key, str) or not private_key.strip() or len(private_key) > 65536:
        raise ValueError("Provide a valid SSH private key no larger than 64 KiB.")
    private_key = _normalize_private_key(private_key)
    if not isinstance(passphrase, str) or len(passphrase) > 1024:
        raise ValueError("SSH key passphrase is too long.")
    ensure_agent()
    public_key = _public_key(private_key.strip(), passphrase)
    previous = _read_secret()
    credentials = {"private_key": private_key.strip(), "passphrase": passphrase, "public_key": public_key}
    _store_secret(credentials)
    try:
        loaded = _load_into_agent(credentials)
        if SHARED_AGENT_SOCKET:
            _store_secret(credentials)
        if previous and previous.get("public_key") != public_key:
            old_public_key = previous.get("public_key")
            if not old_public_key and isinstance(previous.get("private_key"), str):
                old_public_key = _public_key(previous["private_key"], previous.get("passphrase", ""))
            _remove_agent_key(old_public_key, owned=previous.get("agent_identity_added_by_runtime") is True)
    except Exception:
        _remove_agent_key(public_key, owned=credentials.get("agent_identity_added_by_runtime") is True)
        try:
            if previous:
                _store_secret(previous)
                _load_into_agent(previous)
            else:
                _delete_secret()
                _load_into_agent(None)
        except Exception:
            pass
        raise
    return {"saved": True, "loaded": loaded, "provider": credential_status()["provider"]}


def credential_status() -> dict:
    try:
        provider = _provider()
        if sys.platform == "darwin":
            ensure_agent()
        credentials = _read_secret()
        loaded = bool(credentials and isinstance(credentials.get("public_key"), str)
                      and _agent_has_key(credentials["public_key"]))
        return {"provider": provider, "saved": credentials is not None, "loaded": loaded,
                "state": "ready" if credentials and loaded else ("missing" if credentials is None else "disconnected"),
                "message": None}
    except CredentialStoreError as exc:
        return {"provider": "keychain" if sys.platform == "darwin" else "secret-service",
                "saved": False, "loaded": False, "state": "locked" if exc.locked else "unavailable",
                "message": str(exc)}


def reload_credentials() -> dict:
    credentials = _read_secret()
    loaded = _load_into_agent(credentials)
    if credentials and not credentials.get("public_key"):
        credentials["public_key"] = _public_key(credentials["private_key"], credentials.get("passphrase", ""))
    if credentials and SHARED_AGENT_SOCKET:
        credentials.setdefault("agent_identity_added_by_runtime", loaded and not _agent_has_key(credentials["public_key"]))
        _store_secret(credentials)
    return {"saved": credentials is not None, "loaded": loaded}


def remove_credentials() -> dict:
    credentials = _read_secret()
    public_key = credentials.get("public_key") if credentials else None
    if credentials and not public_key and isinstance(credentials.get("private_key"), str):
        public_key = _public_key(credentials["private_key"], credentials.get("passphrase", ""))
    _remove_agent_key(public_key, owned=bool(credentials and credentials.get("agent_identity_added_by_runtime") is True))
    _delete_secret()
    if SHARED_AGENT_SOCKET is None:
        _load_into_agent(None)
    return {"saved": False, "loaded": False}


def stop_agent() -> None:
    if SHARED_AGENT_SOCKET is not None:
        return
    pid = AGENT_PID.read_text(encoding="ascii").strip() if AGENT_PID.exists() else ""
    if pid.isdigit():
        subprocess.run(["ssh-agent", "-k"], env={**os.environ, "SSH_AGENT_PID": pid,
                       "SSH_AUTH_SOCK": str(AGENT_SOCKET)}, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, check=False, timeout=5)
    AGENT_PID.unlink(missing_ok=True)
    AGENT_SOCKET.unlink(missing_ok=True)


atexit.register(stop_agent)
