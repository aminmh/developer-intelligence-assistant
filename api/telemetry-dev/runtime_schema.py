"""Scoped, read-only Runtime catalog inspection on the desktop helper."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import selectors
import shlex
import signal
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import runtime_schema_catalog as catalog

MAX_BYTES = 4 * 1024 * 1024
TIMEOUT = 60
HEX_ID = re.compile(r"[0-9a-f]{64}")
ASSOCIATION_LOCK = threading.Lock()


class SchemaError(RuntimeError):
    def __init__(self, message, status=422):
        super().__init__(message)
        self.status = status


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def fingerprint(value):
    return str(value).replace(":", "").lower()


class Budget:
    def __init__(self):
        self.deadline = time.monotonic() + TIMEOUT
        self.bytes = 0

    def remaining(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise SchemaError("Catalog inspection exceeded 60 seconds. Select fewer schemas and retry.", 408)
        return remaining

    def run(self, args, *, input_data=None, env=None, check=True):
        """Drain bounded pipes; neither diagnostics nor command arguments reach browser errors."""
        self.remaining()
        with tempfile.TemporaryFile() as incoming:
            incoming.write(input_data or b"")
            incoming.seek(0)
            try:
                process = subprocess.Popen(args, stdin=incoming, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                           env=env, start_new_session=True)
            except OSError:
                raise SchemaError("A required Docker, SSH, or database client is unavailable.") from None
            output = {"stdout": bytearray(), "stderr": bytearray()}
            try:
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
                    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
                    while selector.get_map():
                        for key, _ in selector.select(min(self.remaining(), .2)):
                            chunk = os.read(key.fileobj.fileno(), 65536)
                            if not chunk:
                                selector.unregister(key.fileobj)
                                continue
                            self.bytes += len(chunk)
                            if self.bytes > MAX_BYTES:
                                raise SchemaError("Catalog output exceeded 4 MiB. Select fewer schemas and retry.", 413)
                            output[key.data].extend(chunk)
                    process.wait(timeout=self.remaining())
                if check and process.returncode:
                    raise SchemaError("Catalog access failed. Verify the container client, database permissions, and SSH access; no changes were made.")
                return bytes(output["stdout"]).decode("utf-8", "strict")
            except (UnicodeError, subprocess.TimeoutExpired):
                raise SchemaError("Catalog inspection timed out or returned unreadable metadata.") from None
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                process.stdout.close()
                process.stderr.close()


def authorize(operation, body, token, origin, helper):
    if not helper.verify_helper_token(token, origin, operation="runtime-schema-" + operation):
        raise SchemaError("Runtime schema authorization is invalid or expired.", 403)
    try:
        head, encoded, _ = token.split(".")
        header = json.loads(base64.urlsafe_b64decode(head + "=" * (-len(head) % 4)))
        claims = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        source = body["source"]
        if header.get("alg") != "HS256" or not isinstance(source, dict):
            raise ValueError
        if not isinstance(claims.get("sub"), str) or not claims["sub"] or not claims.get("jti"):
            raise ValueError
        for field in ("project_id", "environment", "host_id", "container_id", "image_id"):
            if not isinstance(source.get(field), str) or not source[field] or claims.get(field) != source[field]:
                raise ValueError
        if not HEX_ID.fullmatch(source["container_id"]) or source.get("state") != "running":
            raise ValueError
        if not HEX_ID.fullmatch(fingerprint(source["host_id"])):
            raise ValueError
        database, schemas = body.get("database") or "", body.get("schemas") or []
        if not isinstance(database, str) or len(database) > 256 or "\0" in database:
            raise ValueError
        if (not isinstance(schemas, list) or len(schemas) > 128 or len(set(schemas)) != len(schemas)
                or any(not isinstance(item, str) or not item or len(item) > 256 or "\0" in item for item in schemas)):
            raise ValueError
        if claims.get("database", "") != database or claims.get("schemas", []) != schemas:
            raise ValueError
        return claims
    except (ValueError, KeyError, TypeError):
        raise SchemaError("Runtime schema authorization does not match the selected source or catalog scope.", 403) from None


def association_path(helper):
    return helper.state_path().with_name("schema-hosts.json")


def associations(helper):
    try:
        value = json.loads(association_path(helper).read_text())
        if not isinstance(value, dict):
            raise ValueError
        return value
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        raise SchemaError("Saved Runtime SSH associations cannot be read. Repair the helper state and retry.") from None


def scan_ssh(payload, helper, budget):
    try:
        target = helper.resolve_ssh_config_target(payload)
    except (ValueError, RuntimeError):
        raise SchemaError("OpenSSH could not resolve this direct host. Check the host alias, account, and port.") from None
    budget.remaining()
    if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,252}", target.get("host", ""))
            or not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}\$?", target.get("ssh_user", ""))):
        raise SchemaError("OpenSSH resolved an unsupported host or account name.")
    raw = budget.run(["ssh-keyscan", "-T", "5", "-p", str(target["ssh_port"]), target["host"]])
    keys = []
    for line in raw.splitlines():
        parts = line.split()
        if len(parts) != 3 or parts[0].startswith("#"):
            continue
        try:
            digest = hashlib.sha256(base64.b64decode(parts[2], validate=True)).digest()
        except ValueError:
            continue
        value = "SHA256:" + base64.b64encode(digest).decode().rstrip("=")
        keys.append((parts[1] != "ssh-ed25519", parts[1], line, value))
    if not keys:
        raise SchemaError("No SSH host key was received. Check reachability and the SSH port.")
    _, _, key, value = sorted(keys)[0]
    return {**target, "fingerprint": value, "known_host": key}


class Docker:
    def __init__(self, prefix, budget, *, ssh=None, env=None):
        self.prefix, self.budget, self.ssh, self.env = prefix, budget, ssh, env

    def run(self, args, *, input_data=None, check=True):
        command = self.prefix + args
        if self.ssh:
            command = self.ssh + [shlex.join(command)]
        return self.budget.run(command, input_data=input_data, env=self.env, check=check)


def remote_docker(target, budget, directory):
    import runtime_ssh_credentials as credentials
    try:
        credentials.ensure_agent()
    except Exception:
        raise SchemaError("The desktop SSH agent is unavailable. Unlock saved Runtime SSH credentials and retry.") from None
    known = Path(directory) / "known_hosts"
    known.write_text(target["known_host"] + "\n")
    command = ["ssh", "-F", "/dev/null", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
               "-o", "UserKnownHostsFile=" + str(known), "-o", "GlobalKnownHostsFile=/dev/null",
               "-o", "ConnectTimeout=8", "-o", "ConnectionAttempts=1", "-p", str(target["ssh_port"]),
               "-l", target["ssh_user"], target["host"]]
    return Docker(["docker", "--host", "unix:///var/run/docker.sock"], budget,
                  ssh=command, env=credentials._agent_env())


def verify_reporting_host(docker, source):
    agents = docker.run(["ps", "--quiet", "--no-trunc", "--filter", "label=com.docker.compose.service=agent"])
    script = "import hashlib,os,ssl; print(hashlib.sha256(ssl.PEM_cert_to_DER_cert(open(os.environ['HOST_CERT']).read())).hexdigest())"
    for agent in agents.splitlines()[:32]:
        if HEX_ID.fullmatch(agent):
            reported = docker.run(["exec", agent, "python", "-c", script], check=False).strip()
            if reported == fingerprint(source["host_id"]):
                return
    raise SchemaError("This SSH host does not present the selected reporting host certificate. Choose the correct host and associate it again.", 409)


def inspect_source(docker, source):
    try:
        record = json.loads(docker.run(["inspect", "--format", "{{json .}}", source["container_id"]]))
        config = record["Config"]
        labels = config.get("Labels") or {}
        expected = {"enabled": "true", "project": source["project_id"], "environment": source["environment"], "service": source["service"]}
        if (record["Id"] != source["container_id"] or not record["State"]["Running"]
                or record["Image"] != source["image_id"]
                or any(labels.get("com.devassistant.telemetry." + key) != val for key, val in expected.items())):
            raise ValueError
        return config
    except (ValueError, KeyError, TypeError):
        raise SchemaError("The reporting container changed or stopped. Refresh Runtime sources and select it again.", 409) from None


def local_docker(helper, budget):
    # Reuse the helper's Unix socket/named-pipe validation; bound its context lookups.
    try:
        prefix = helper.local_docker_command_prefix(timeout=min(8, budget.remaining() / 2))
    except Exception:
        raise SchemaError("A local Docker socket could not be verified. Select a local Docker context and retry.") from None
    budget.remaining()
    return Docker(prefix, budget)


def signed_result(schema, provenance, claims, helper):
    envelope = {"schema": schema, "provenance": provenance, "sub": claims["sub"], "origin": claims["origin"],
                "expires_at": int(time.time()) + 120}
    key = hmac.new(helper.shared_secret(), b"devassistant-runtime-schema-result-v1", hashlib.sha256).digest()
    envelope["receipt"] = hmac.new(key, canonical(envelope), hashlib.sha256).hexdigest()
    if len(canonical(envelope)) > MAX_BYTES:
        raise SchemaError("The normalized schema exceeds 4 MiB. Select fewer schemas and retry.", 413)
    return envelope


def handle(operation, body, token, origin, helper):
    budget = Budget()
    claims = authorize(operation, body, token, origin, helper)
    source = body["source"]
    if operation == "ssh-scan":
        scanned = scan_ssh(body.get("ssh") or {}, helper, budget)
        return {key: scanned[key] for key in ("host", "ssh_user", "ssh_port", "fingerprint")}
    try:
        is_local = fingerprint(helper.cert_fingerprint(timeout=min(8, budget.remaining()))) == fingerprint(source["host_id"])
    except Exception:
        is_local = False
    with tempfile.TemporaryDirectory(prefix="runtime-schema-ssh-") as directory:
        if operation == "associate":
            ssh = body.get("ssh") or {}
            scanned = scan_ssh(ssh, helper, budget)
            if ssh.get("confirmed") is not True or ssh.get("host_fingerprint") != scanned["fingerprint"]:
                raise SchemaError("Confirm the displayed SSH host fingerprint before associating this host.", 409)
            docker = remote_docker(scanned, budget, directory)
            verify_reporting_host(docker, source)
            inspect_source(docker, source)
            with ASSOCIATION_LOCK:
                saved = associations(helper)
                saved[source["host_id"]] = {key: scanned[key] for key in ("host", "ssh_user", "ssh_port", "fingerprint", "known_host")}
                path = association_path(helper)
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                with tempfile.NamedTemporaryFile(dir=path.parent, mode="w", delete=False) as stream:
                    temp = Path(stream.name)
                    json.dump(saved, stream)
                os.chmod(temp, 0o600)
                os.replace(temp, path)
            return {"associated": True, "host_id": source["host_id"]}
        if is_local:
            docker = local_docker(helper, budget)
        else:
            with ASSOCIATION_LOCK:
                target = associations(helper).get(source["host_id"])
            if not target:
                raise SchemaError("Associate this reporting host with an SSH host before inspecting its databases.", 409)
            docker = remote_docker(target, budget, directory)
            verify_reporting_host(docker, source)
        config = inspect_source(docker, source)
        engine, configured = catalog.detect_engine(config)
        query = catalog.Client(docker, source["container_id"], engine)
        discovery = catalog.discover(query, engine, configured, body.get("database"))
        engine = discovery["engine"]
        if operation == "discover":
            return {"source": source, **discovery}
        database = body.get("database")
        if not database or database not in discovery["databases"]:
            raise SchemaError("Choose a discovered database before loading its schema.")
        schemas = body.get("schemas") or discovery["configured_schemas"]
        if not schemas or not set(schemas) <= set(discovery["schemas"]):
            raise SchemaError("Choose existing non-system schemas from the discovered database.")
        schema, warnings = catalog.extract(query, source, engine, database, schemas)
        inspect_source(docker, source)
        budget.remaining()
        provenance = {key: source[key] for key in ("project_id", "environment", "host_id", "container_id", "image_id")}
        provenance.update(engine=engine, engine_version=discovery["version"], database=database, schemas=schemas,
                          extracted_at=datetime.now(timezone.utc).isoformat(), coverage={"warnings": warnings, "complete": not warnings})
        return signed_result(schema, provenance, claims, helper)
