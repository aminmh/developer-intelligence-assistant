"""Mode-aware Docker Compose orchestration for local Runtime telemetry."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import subprocess
import sys

API_DIR = Path(__file__).resolve().parents[1]
MODES = {"disabled", "local", "remote"}


def compose_env() -> dict[str, str]:
    env = os.environ.copy()
    agent_socket = Path.home() / ".local/state/devassistant/runtime-host/ssh-agent.sock"
    env["SSH_AUTH_SOCK"] = str(agent_socket)
    env["RUNTIME_SSH_AGENT_SOURCE"] = (
        "/run/host-services/ssh-auth.sock" if platform.system() == "Darwin" else str(agent_socket)
    )
    return env


def compose(*args: str, profile: str | None = None, capture_output: bool = False) -> subprocess.CompletedProcess[str]:
    command = ["docker", "compose"]
    if profile:
        command.extend(["--profile", profile])
    command.extend(args)
    return subprocess.run(
        command, cwd=API_DIR, env=compose_env(), check=True, text=True, capture_output=capture_output
    )


def api_python(code: str) -> str:
    result = compose("exec", "-T", "python-api", "python", "-c", code, capture_output=True)
    return result.stdout.strip()


def read_mode() -> str:
    mode = json.loads(api_python(
        "import json; from telemetry.registry import default_registry; "
        "from telemetry.runtime_storage import get_storage_target; "
        "print(json.dumps(get_storage_target(default_registry())['mode']))"
    ))
    if mode not in MODES:
        raise RuntimeError(f"API returned an unsupported Runtime storage mode: {mode!r}")
    return mode


def apply_mode(mode: str) -> None:
    api_python(
        "from telemetry.registry import default_registry; "
        "from telemetry.runtime_storage import set_storage_mode; "
        f"set_storage_mode({mode!r}, default_registry())"
    )


def verify_backend(mode: str) -> None:
    if mode == "local":
        function = "verify_local_clickhouse_connection"
    elif mode == "remote":
        function = "verify_router_clickhouse_connection"
    else:
        return
    api_python("from telemetry.runtime_storage import " + function + "; " + function + "()")


def start_bridge() -> None:
    compose("up", "-d", "python-api", "telemetry-clickhouse-router", "runtime-ssh-tunnel", profile="runtime")


def start_local_clickhouse() -> None:
    compose("up", "-d", "--wait", "--wait-timeout", "120", "telemetry-clickhouse", profile="runtime-local")


def stop_local_clickhouse() -> None:
    compose("stop", "telemetry-clickhouse", profile="runtime-local")


def start_collector() -> None:
    compose("up", "-d", "telemetry-collector-runtime", profile="runtime")
    compose("up", "-d", "--force-recreate", "--no-deps", "telemetry-collector-reloader", profile="runtime")


def stop_collector() -> None:
    compose("stop", "telemetry-collector-reloader", "telemetry-collector-runtime", profile="runtime")


def start_saved_mode() -> str:
    """Start only the selected backend; never leave ingestion running before verification."""
    mode = read_mode()
    stop_collector()
    start_bridge()
    if mode == "local":
        start_local_clickhouse()
        verify_backend(mode)
        start_collector()
    elif mode == "remote":
        stop_local_clickhouse()
        verify_backend(mode)
        start_collector()
    else:
        stop_local_clickhouse()
    return mode


def select_mode(mode: str) -> None:
    if mode not in MODES:
        raise ValueError("Storage mode must be disabled, local, or remote")
    stop_collector()
    start_bridge()
    if mode == "local":
        start_local_clickhouse()
        apply_mode(mode)
        start_collector()
    elif mode == "remote":
        stop_local_clickhouse()
        # set_storage_mode writes the SSH/router target before checking through
        # the tunnel; the already-running tunnel follows that shared target.
        apply_mode(mode)
        start_collector()
    else:
        apply_mode(mode)
        stop_local_clickhouse()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("start", help="start services for the saved Runtime storage mode")
    select_parser = subparsers.add_parser("select", help="start and select a Runtime storage mode")
    select_parser.add_argument("mode", choices=sorted(MODES))
    args = parser.parse_args(argv)
    try:
        if args.command == "start":
            mode = start_saved_mode()
            print(f"Runtime storage mode {mode}: selected services are ready.")
        else:
            select_mode(args.mode)
            print(f"Runtime storage mode set to {args.mode}.")
    except (subprocess.CalledProcessError, RuntimeError, ValueError) as exc:
        print(f"Runtime startup failed: {exc}", file=sys.stderr)
        print("The selected mode was not changed after a failed verification; no backend fallback was attempted.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
