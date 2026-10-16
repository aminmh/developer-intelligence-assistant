"""Temporary read-only host metrics for the configured Remote ClickHouse target."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import math
import re
import threading
import subprocess
from typing import Any

from telemetry import runtime_provisioning, runtime_storage, ssh_tunnel


METRICS_COMMAND = r"""set -eu
LC_ALL=C
export LC_ALL
awk '$1 == "cpu" { total = 0; for (i = 2; i <= 9; i++) total += $i; idle = $5 + $6; printf "cpu %.0f %.0f\n", total, idle; found = 1 } END { if (!found) exit 1 }' /proc/stat
awk '$1 == "MemTotal:" { total = $2; have_total = 1 } $1 == "MemAvailable:" { available = $2; have_available = 1 } END { if (!have_total || !have_available || available > total) exit 1; printf "memory %.0f %.0f\n", total * 1024, (total - available) * 1024 }' /proc/meminfo
awk 'NR == 1 { if (NF < 3) exit 1; printf "load %.6f %.6f %.6f\n", $1, $2, $3; found = 1 } END { if (!found) exit 1 }' /proc/loadavg
df -P -B1 /var/lib/clickhouse | awk 'NR == 2 { if ($2 !~ /^[0-9]+$/ || $4 !~ /^[0-9]+$/) exit 1; printf "disk %s %s\n", $2, $4; found = 1 } END { if (!found) exit 1 }'
"""
METRICS_TIMEOUT_SECONDS = 8


@dataclass(frozen=True)
class HostSample:
    sampled_at: str
    cpu_percent: float | None
    memory_used_bytes: int
    memory_total_bytes: int
    load_average: tuple[float, float, float]
    disk_free_bytes: int
    disk_total_bytes: int


_collection_lock = threading.Lock()
_latest_sample: HostSample | None = None
_cpu_counters: tuple[int, int] | None = None
_target_identity: tuple[str, int, str, str] | None = None


def _empty_response(mode: str) -> dict[str, Any]:
    return {
        "mode": mode,
        "available": False,
        "stale": False,
        "sampled_at": None,
        "ssh_health": "not-required",
        "ssh_message": None,
        "clickhouse_health": "not-required",
        "clickhouse_message": None,
        "metrics": None,
    }


def _sample_metrics(sample: HostSample) -> dict[str, Any]:
    return {
        "cpu_percent": sample.cpu_percent,
        "memory_used_bytes": sample.memory_used_bytes,
        "memory_total_bytes": sample.memory_total_bytes,
        "load_average": list(sample.load_average),
        "disk_free_bytes": sample.disk_free_bytes,
        "disk_total_bytes": sample.disk_total_bytes,
    }


def _failed_response(message: str, *, ssh_health: str = "unavailable") -> dict[str, Any]:
    sample = _latest_sample
    clickhouse_message = (
        "ClickHouse health could not be checked after the metrics query failed."
        if ssh_health == "connected"
        else "ClickHouse health could not be checked because SSH is unavailable."
    )
    return {
        "mode": "remote",
        "available": False,
        "stale": sample is not None,
        "sampled_at": sample.sampled_at if sample else None,
        "ssh_health": ssh_health,
        "ssh_message": message,
        "clickhouse_health": "unknown",
        "clickhouse_message": clickhouse_message,
        "metrics": _sample_metrics(sample) if sample else None,
    }


def _parse_metrics_output(output: str) -> tuple[tuple[int, int], dict[str, Any]]:
    if not isinstance(output, str) or len(output) > 4096:
        raise ValueError("invalid metrics output")
    lines = output.splitlines()
    if len(lines) != 4:
        raise ValueError("invalid metrics output")

    cpu = re.fullmatch(r"cpu ([0-9]+) ([0-9]+)", lines[0])
    memory = re.fullmatch(r"memory ([0-9]+) ([0-9]+)", lines[1])
    load = re.fullmatch(r"load ([0-9]+(?:\.[0-9]+)?) ([0-9]+(?:\.[0-9]+)?) ([0-9]+(?:\.[0-9]+)?)", lines[2])
    disk = re.fullmatch(r"disk ([0-9]+) ([0-9]+)", lines[3])
    if not cpu or not memory or not load or not disk:
        raise ValueError("invalid metrics output")

    counters = (int(cpu.group(1)), int(cpu.group(2)))
    memory_total, memory_used = int(memory.group(1)), int(memory.group(2))
    load_average = tuple(float(load.group(i)) for i in range(1, 4))
    disk_total, disk_free = int(disk.group(1)), int(disk.group(2))
    if (counters[0] <= 0 or counters[1] > counters[0]
            or memory_total <= 0 or memory_used > memory_total
            or disk_total <= 0 or disk_free > disk_total
            or not all(math.isfinite(value) and value >= 0 for value in load_average)):
        raise ValueError("invalid metrics values")
    return counters, {
        "memory_total_bytes": memory_total,
        "memory_used_bytes": memory_used,
        "load_average": load_average,
        "disk_total_bytes": disk_total,
        "disk_free_bytes": disk_free,
    }


def _cpu_percentage(previous: tuple[int, int] | None, current: tuple[int, int]) -> float | None:
    if previous is None:
        return None
    total_delta = current[0] - previous[0]
    idle_delta = current[1] - previous[1]
    if total_delta <= 0 or idle_delta < 0 or idle_delta > total_delta:
        return None
    return round((total_delta - idle_delta) * 100 / total_delta, 1)


async def _has_ssh_identity() -> bool:
    try:
        return await asyncio.to_thread(ssh_tunnel.agent_has_identity)
    except (OSError, subprocess.TimeoutExpired):
        return False


async def _collect_remote(target: dict, registry: Any) -> dict[str, Any]:
    global _latest_sample, _cpu_counters

    if not await _has_ssh_identity():
        return _failed_response("Unlock the OS keychain or keyring, or restore the saved SSH key, then retry.")

    connection = None
    try:
        try:
            connection, _fingerprint = await runtime_provisioning._connect(target)
        except runtime_provisioning.HostKeyMismatch:
            return _failed_response("The saved SSH host fingerprint changed. Verify the host identity in Runtime storage settings.")
        except RuntimeError as exc:
            if "SSH key authentication failed" in str(exc):
                return _failed_response("SSH authentication failed. Recheck the saved SSH account and key, then retry.")
            return _failed_response("The SSH host could not be reached. Check its address, port, and network access.")

        try:
            result = await asyncio.wait_for(
                connection.run(METRICS_COMMAND, check=False),
                timeout=METRICS_TIMEOUT_SECONDS,
            )
        except (asyncio.TimeoutError, TimeoutError):
            return _failed_response(
                "The SSH metrics query timed out. Check remote host load and SSH responsiveness.",
                ssh_health="connected",
            )
        if result.exit_status != 0:
            return _failed_response(
                "The remote host returned invalid metrics. Confirm Linux proc data and /var/lib/clickhouse are available.",
                ssh_health="connected",
            )

        try:
            counters, values = _parse_metrics_output(result.stdout)
        except (AttributeError, TypeError, ValueError):
            return _failed_response(
                "The remote host returned malformed metrics. Confirm it uses the supported Linux setup.",
                ssh_health="connected",
            )

        clickhouse_health = "unavailable"
        clickhouse_message = "ClickHouse did not respond through the configured Runtime connection. Check ClickHouse and the SSH tunnel."
        try:
            status = await asyncio.to_thread(runtime_storage.storage_status, registry)
            if status.get("health") == "verified":
                clickhouse_health = "available"
                clickhouse_message = None
        except Exception:
            # Keep host resource metrics useful when ClickHouse itself is down.
            pass

        cpu_percent = _cpu_percentage(_cpu_counters, counters)
        _cpu_counters = counters
        sample = HostSample(
            sampled_at=datetime.now(timezone.utc).isoformat(),
            cpu_percent=cpu_percent,
            **values,
        )
        _latest_sample = sample
        return {
            "mode": "remote",
            "available": True,
            "stale": False,
            "sampled_at": sample.sampled_at,
            "ssh_health": "connected",
            "ssh_message": None,
            "clickhouse_health": clickhouse_health,
            "clickhouse_message": clickhouse_message,
            "metrics": _sample_metrics(sample),
        }
    except Exception:
        return _failed_response("Remote host metrics are unavailable. Check SSH and keyring status, then retry.")
    finally:
        if connection is not None:
            try:
                connection.close()
                await connection.wait_closed()
            except Exception:
                pass


def collect_host_metrics(registry=None) -> dict[str, Any]:
    """Return one current sample without retaining a history."""
    global _latest_sample, _cpu_counters, _target_identity

    with _collection_lock:
        target = runtime_storage.get_storage_target(registry)
        mode = target.get("mode") if isinstance(target, dict) else "invalid"
        if mode != "remote":
            _latest_sample = None
            _cpu_counters = None
            _target_identity = None
            return _empty_response(mode if mode in {"disabled", "local"} else "unavailable")

        target_identity = (
            str(target.get("host", "")),
            int(target.get("ssh_port", 0)) if str(target.get("ssh_port", "")).isdigit() else 0,
            str(target.get("ssh_user", "")),
            str(target.get("host_fingerprint", "")),
        )
        if _target_identity != target_identity:
            _latest_sample = None
            _cpu_counters = None
            _target_identity = target_identity
        return asyncio.run(_collect_remote(target, registry))
