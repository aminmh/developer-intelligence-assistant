"""SSH preflight and ClickHouse provisioning for Runtime storage."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import re
import secrets
import shlex

import asyncssh

from telemetry.runtime_storage import (
    clickhouse_credentials,
    clickhouse_credentials_status,
    save_storage_target,
    validate_remote_target,
    _validate_host,
)


_SUDO_HELP = (
    "Ask a remote administrator to add this account to the sudo or admin group, "
    "or grant it an approved sudo policy. Then sign out and back in, and verify "
    "with `sudo -v` and `sudo -l`. Do not enable passwordless sudo for all commands."
)


class HostKeyMismatch(Exception):
    pass


async def _get_server_host_key(host: str, port: int):
    return await asyncio.wait_for(
        asyncssh.get_server_host_key(host, port=port, config=None),
        timeout=8,
    )


def _remote_target(payload: dict) -> dict:
    return validate_remote_target(payload)


async def scan_host_key(payload: dict) -> dict:
    """Read the SSH host fingerprint without sending user authentication."""
    host = _validate_host(str(payload.get("host", "")))
    try:
        port = int(payload.get("ssh_port", 22))
    except (TypeError, ValueError) as exc:
        raise ValueError("SSH port must be between 1 and 65535") from exc
    if not 1 <= port <= 65535:
        raise ValueError("SSH port must be between 1 and 65535")
    try:
        host_key = await _get_server_host_key(host, port)
    except (OSError, asyncio.TimeoutError, asyncssh.Error) as exc:
        raise RuntimeError("SSH host could not be reached") from exc
    if host_key is None:
        raise RuntimeError("SSH server did not provide a verifiable host key")
    return {"fingerprint": host_key.get_fingerprint("sha256"), "host": host, "port": port}


async def _connect(target: dict):
    try:
        host_key = await _get_server_host_key(target["host"], target["ssh_port"])
        fingerprint = host_key.get_fingerprint("sha256") if host_key else ""
        if not fingerprint or not secrets.compare_digest(fingerprint, target["host_fingerprint"]):
            raise HostKeyMismatch("SSH host fingerprint changed")
        host_pattern = target["host"] if target["ssh_port"] == 22 else f"[{target['host']}]:{target['ssh_port']}"
        public_key = host_key.export_public_key("openssh").decode("ascii").strip()
        trusted_host = asyncssh.import_known_hosts(f"{host_pattern} {public_key}\n")
        agent_path = __import__("os").environ.get("SSH_AUTH_SOCK", "/run/devassistant/runtime-host/ssh-agent.sock")
        connection = await asyncssh.connect(
            target["host"], port=target["ssh_port"], username=target["ssh_user"],
            known_hosts=trusted_host, agent_path=agent_path,
            connect_timeout=8, login_timeout=12, config=None,
        )
    except HostKeyMismatch:
        raise
    except asyncssh.PermissionDenied as exc:
        raise RuntimeError("SSH key authentication failed. Confirm the account and public key on the remote host.") from exc
    except (OSError, asyncio.TimeoutError, asyncssh.Error) as exc:
        raise RuntimeError("SSH connection failed. Check the host, port, and network access.") from exc
    return connection, fingerprint


async def _run(connection, command: str, *, input_data: str | None = None, timeout: int = 20):
    result = await connection.run(command, input=input_data, check=False, timeout=timeout)
    return result


async def _sudo_check(connection, password: str) -> dict:
    if not password:
        result = await _run(connection, "sudo -n sh -c 'test \"$(id -u)\" = 0'", timeout=12)
        if result.exit_status == 0:
            return {"ok": True, "password_required": False, "message": "SSH and sudo access verified."}
        return {
            "ok": False,
            "password_required": True,
            "check": "sudo",
            "message": "SSH connected, but sudo needs a password or an administrator must grant sudo access.",
            "help": _SUDO_HELP,
        }
    result = await _run(connection, "sudo -S -p '' sh -c 'test \"$(id -u)\" = 0'", input_data=password + "\n", timeout=12)
    if result.exit_status == 0:
        return {"ok": True, "password_required": True, "message": "SSH and sudo access verified. The password was not stored."}
    return {
        "ok": False,
        "password_required": True,
        "check": "sudo",
        "message": "The sudo password was rejected or this account has no sudo rights.",
        "help": _SUDO_HELP,
    }


async def _system_preflight(connection, sudo_password: str) -> dict:
    command = "cat /etc/os-release; printf '\\n--ARCH--\\n'; uname -m; printf '\\n--DISK--\\n'; df -Pk / | tail -n 1; printf '\\n--APT--\\n'; command -v apt-get; printf '\\n--NETWORK--\\n'; getent hosts packages.clickhouse.com >/dev/null && bash -c ':</dev/tcp/packages.clickhouse.com/443' && echo reachable"
    prefix = "sudo -n " if not sudo_password else "sudo -S -p '' "
    result = await _run(connection, prefix + "sh -c " + shlex.quote(command), input_data=(sudo_password + "\n") if sudo_password else None, timeout=20)
    if result.exit_status != 0:
        return {"ok": False, "check": "system-preflight", "message": "Could not read the remote host's operating system, disk, and network preflight."}
    output = result.stdout or ""
    match = re.search(r"(?:^|\n)ID=(?:\"?)(ubuntu|debian)(?:\"?)(?:\n|$)", output, re.IGNORECASE)
    arch_match = re.search(r"--ARCH--\s*([A-Za-z0-9_]+)", output)
    disk_match = re.search(r"--DISK--\s*\S+\s+(\d+)\s+\d+\s+\d+", output)
    supported_arch = {"x86_64", "aarch64"}
    os_name = match.group(1).lower() if match else None
    arch = arch_match.group(1) if arch_match else None
    free_kib = int(disk_match.group(1)) if disk_match else 0
    if os_name not in {"ubuntu", "debian"}:
        return {"ok": False, "check": "operating-system", "message": "Remote installation supports Ubuntu and Debian only."}
    if arch not in supported_arch:
        return {"ok": False, "check": "architecture", "message": "Remote installation supports x86_64 and ARM64 hosts only."}
    if free_kib < 2 * 1024 * 1024:
        return {"ok": False, "check": "disk-space", "message": "At least 2 GiB of free space is required on the remote root filesystem."}
    if "--NETWORK--" not in output or not output.rstrip().endswith("reachable"):
        return {"ok": False, "check": "network", "message": "The remote host cannot resolve packages.clickhouse.com."}
    return {"ok": True, "os": os_name, "architecture": arch, "free_bytes": free_kib * 1024}


async def test_ssh_and_sudo(payload: dict) -> dict:
    target = _remote_target(payload)
    connection, fingerprint = await _connect(target)
    try:
        result = await _sudo_check(connection, target["sudo_password"])
        credentials = clickhouse_credentials_status()
        if result["ok"] and not credentials["ready"]:
            return {
                **result,
                "ok": False,
                "check": "clickhouse-credentials",
                "storage_ready": False,
                "storage_help": credentials["message"],
                "message": "SSH and sudo passed, but Remote ClickHouse credentials are not ready.",
                "host": target["host"],
                "fingerprint": fingerprint,
            }
        return {**result, "storage_ready": credentials["ready"], "storage_help": credentials["message"], "host": target["host"], "fingerprint": fingerprint}
    finally:
        connection.close()
        await connection.wait_closed()


def _clickhouse_install_script(target: dict, username: str, password_hash: str) -> str:
    database_q = shlex.quote(target["database"])
    database = target["database"]
    return f"""set -euo pipefail
exec 2>&1
export DEBIAN_FRONTEND=noninteractive
work_dir=$(mktemp -d)
trap 'rm -rf "$work_dir"' EXIT
stage() {{ printf '__DEVASSISTANT_INSTALL_STAGE__=%s\\n' "$1"; }}
stage prerequisites
apt-get update
apt-get install -y --no-install-recommends ca-certificates curl gnupg
install -d -m 0755 /usr/share/keyrings
stage clickhouse-signing-key
curl -fsSL --retry 2 --connect-timeout 10 --max-time 60 \\
  https://packages.clickhouse.com/rpm/lts/repodata/repomd.xml.key \\
  -o "$work_dir/clickhouse-signing-key.asc"
gpg --dearmor --yes --output "$work_dir/clickhouse-keyring.gpg" "$work_dir/clickhouse-signing-key.asc"
test -s "$work_dir/clickhouse-keyring.gpg"
install -m 0644 "$work_dir/clickhouse-keyring.gpg" /usr/share/keyrings/clickhouse-keyring.gpg
chmod 0644 /usr/share/keyrings/clickhouse-keyring.gpg
stage package-repository
echo 'deb [signed-by=/usr/share/keyrings/clickhouse-keyring.gpg] https://packages.clickhouse.com/deb stable main' > /etc/apt/sources.list.d/clickhouse.list
apt-get update
stage clickhouse-packages
apt-get install -y --no-install-recommends clickhouse-server clickhouse-client
stage server-configuration
install -d -m 0750 /etc/clickhouse-server/config.d /etc/clickhouse-server/users.d
cat > /etc/clickhouse-server/config.d/devassistant-runtime.xml <<'XML'
<clickhouse>
  <listen_host replace="replace">127.0.0.1</listen_host>
  <listen_host>::1</listen_host>
  <http_port>8123</http_port>
  <tcp_port>9000</tcp_port>
  <path>/var/lib/clickhouse/</path>
</clickhouse>
XML
cat > /etc/clickhouse-server/users.d/devassistant-runtime.xml <<'XML'
<clickhouse><users><default><networks replace="replace"><ip>127.0.0.1</ip><ip>::1</ip></networks></default><{username}><password_sha256_hex>{password_hash}</password_sha256_hex><networks replace="replace"><ip>127.0.0.1</ip><ip>::1</ip></networks><profile>default</profile><quota>default</quota><access_management>0</access_management><grants><query>GRANT ALL ON {database}.*</query></grants></{username}></users></clickhouse>
XML
chmod 0640 /etc/clickhouse-server/config.d/devassistant-runtime.xml /etc/clickhouse-server/users.d/devassistant-runtime.xml
chown clickhouse:clickhouse /etc/clickhouse-server/config.d/devassistant-runtime.xml /etc/clickhouse-server/users.d/devassistant-runtime.xml
stage start-clickhouse
systemctl enable clickhouse-server
systemctl restart clickhouse-server
for i in $(seq 1 60); do clickhouse-client --query 'SELECT 1' >/dev/null 2>&1 && break; sleep 2; done
stage initialize-database
clickhouse-client --query 'CREATE DATABASE IF NOT EXISTS {database_q}'
"""


async def install_clickhouse(payload: dict, registry=None) -> dict:
    target = _remote_target(payload)
    access, _ = await _connect(target)
    try:
        sudo = await _sudo_check(access, target["sudo_password"])
        if not sudo["ok"]:
            raise PermissionError(sudo["message"] + " " + sudo["help"])
        preflight = await _system_preflight(access, target["sudo_password"])
        if not preflight["ok"]:
            raise RuntimeError(preflight["message"])
    finally:
        access.close()
        await access.wait_closed()

    username, password = clickhouse_credentials(require_remote_strength=True)
    password_hash = hashlib.sha256(password.encode("utf-8")).hexdigest()
    install_script = _clickhouse_install_script(target, username, password_hash)
    connection, _ = await _connect(target)
    try:
        command = "sudo -n bash -s" if not target["sudo_password"] else "sudo -S -p '' -k bash -s"
        script_input = install_script if not target["sudo_password"] else target["sudo_password"] + "\n" + install_script
        result = await _run(connection, command, input_data=script_input, timeout=900)
        if result.exit_status != 0:
            stages = re.findall(r"^__DEVASSISTANT_INSTALL_STAGE__=([a-z-]+)$", result.stdout or "", re.MULTILINE)
            failed_stage = stages[-1] if stages else "unknown"
            stage_label = failed_stage.replace("-", " ")
            raise RuntimeError(
                f"ClickHouse installation failed during {stage_label}. Check the remote system logs and retry; "
                "the active Runtime target was not changed."
            )
        target_data = {
            "mode": "remote",
            "host": target["host"],
            "ssh_port": target["ssh_port"],
            "ssh_user": target["ssh_user"],
            "host_fingerprint": target["host_fingerprint"],
            "database": target["database"],
            "username": username,
            "health": "verified",
            "verified_at": None,
            "last_error": None,
        }
        saved = save_storage_target(target_data, registry)
        restarted = saved.pop("collector_restart_requested", False)
        return {"target": saved, "preflight": preflight, "collector_restart_requested": restarted}
    finally:
        connection.close()
        await connection.wait_closed()
