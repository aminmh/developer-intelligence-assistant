"""Maintain SSH forwards to loopback-only ClickHouse on the selected host."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess

import asyncssh


TARGET = Path(os.getenv("TELEMETRY_RUNTIME_STATE_DIR", "/var/lib/devassistant/runtime")) / "ssh-target.json"
AGENT = os.getenv("SSH_AUTH_SOCK", "/run/devassistant/runtime-host/ssh-agent.sock")


def read_target() -> dict:
    try:
        value = json.loads(TARGET.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"mode": "local"}
    if not isinstance(value, dict) or value.get("mode") != "remote":
        return {"mode": value.get("mode", "local")} if isinstance(value, dict) else {"mode": "local"}
    if (not isinstance(value.get("host"), str) or not value["host"]
            or not isinstance(value.get("ssh_user"), str) or not value["ssh_user"]
            or not isinstance(value.get("host_fingerprint"), str)
            or not value["host_fingerprint"].startswith("SHA256:")
            or type(value.get("ssh_port")) is not int or not 1 <= value["ssh_port"] <= 65535):
        return {"mode": "invalid"}
    return value


def trusted_key(fingerprint: str, key: asyncssh.SSHKey) -> bool:
    return bool(fingerprint and key.get_fingerprint("sha256") == fingerprint)


def agent_has_identity() -> bool:
    result = subprocess.run(["ssh-add", "-l"], env={**os.environ, "SSH_AUTH_SOCK": AGENT},
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            check=False, timeout=3)
    return result.returncode == 0


async def run() -> None:
    while True:
        target = read_target()
        if target.get("mode") != "remote":
            await asyncio.sleep(2)
            continue
        connection = None
        listeners = []
        try:
            candidate = await asyncio.wait_for(asyncssh.get_server_host_key(
                target["host"], port=int(target.get("ssh_port", 22)), config=None,
            ), timeout=8)
            if candidate is None or not trusted_key(target.get("host_fingerprint", ""), candidate):
                raise RuntimeError("Remote SSH host fingerprint does not match the saved fingerprint")
            host_pattern = target["host"] if int(target.get("ssh_port", 22)) == 22 else f"[{target['host']}]:{target['ssh_port']}"
            known_hosts = asyncssh.import_known_hosts(
                f"{host_pattern} {candidate.export_public_key('openssh').decode('ascii').strip()}\n"
            )
            connection = await asyncssh.connect(
                target["host"], port=int(target.get("ssh_port", 22)), username=target["ssh_user"],
                agent_path=AGENT, known_hosts=known_hosts, config=None, connect_timeout=10,
                login_timeout=15,
            )
            for port in (8123, 9000):
                listeners.append(await connection.forward_local_port("0.0.0.0", port, "127.0.0.1", port))
            while True:
                await asyncio.sleep(2)
                current = read_target()
                if current != target or connection.is_closed() or not await asyncio.to_thread(agent_has_identity):
                    break
        except (OSError, asyncio.TimeoutError, asyncssh.Error, RuntimeError, KeyError, ValueError):
            await asyncio.sleep(3)
        finally:
            for listener in listeners:
                listener.close()
                await listener.wait_closed()
            if connection is not None:
                connection.close()
                await connection.wait_closed()


if __name__ == "__main__":
    asyncio.run(run())
