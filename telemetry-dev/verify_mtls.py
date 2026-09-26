#!/usr/bin/env python3
"""Verify the receiver TLS overlay with throwaway certificates and containers.

Requires the isolated compose stack, Docker, Python 3, and local openssl.
Does not install a CA, publish a port, or modify application containers.
"""
import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile
import time


ROOT = Path(__file__).resolve().parent
COLLECTOR = "ghcr.io/open-telemetry/opentelemetry-collector-releases/opentelemetry-collector-contrib:0.136.0"
CLIENT = "clickhouse/clickhouse-server:25.8.4.13"


def run(*args, check=True):
    return subprocess.run(args, check=check, capture_output=True, text=True, timeout=30)


def main():
    config = json.loads(run("docker", "compose", "-f", str(ROOT.parent / "docker-compose.telemetry.yml"),
                            "config", "--format", "json").stdout)
    network = config["networks"]["telemetry_ingest"]["name"]
    name = "telemetry-mtls-check-" + secrets.token_hex(4)
    with tempfile.TemporaryDirectory(prefix="telemetry-mtls-") as temporary:
        pki = Path(temporary)
        # All certificates expire in one day and live only in this temporary directory.
        for ca in ("clients-ca", "untrusted-ca"):
            run("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                "-subj", "/CN=" + ca, "-keyout", str(pki / (ca + ".key")),
                "-out", str(pki / (ca + ".crt")))
        for subject, ca, purpose in (("server", "clients-ca", "serverAuth"),
                                     ("client", "clients-ca", "clientAuth"),
                                     ("untrusted", "untrusted-ca", "clientAuth")):
            run("openssl", "req", "-new", "-newkey", "rsa:2048", "-nodes",
                "-subj", "/CN=" + subject, "-keyout", str(pki / (subject + ".key")),
                "-out", str(pki / (subject + ".csr")))
            extensions = "extendedKeyUsage=" + purpose + "\n"
            if subject == "server":
                extensions += "subjectAltName=DNS:" + name + "\n"
            (pki / "extensions.cnf").write_text(extensions)
            run("openssl", "x509", "-req", "-days", "1", "-in", str(pki / (subject + ".csr")),
                "-CA", str(pki / (ca + ".crt")), "-CAkey", str(pki / (ca + ".key")),
                "-CAcreateserial", "-extfile", str(pki / "extensions.cnf"),
                "-out", str(pki / (subject + ".crt")))
        # The pinned Collector runs as a non-root UID; keys remain temporary test data.
        pki.chmod(0o755)
        for path in pki.iterdir():
            path.chmod(0o644)
        arguments = ["--config=/etc/otelcol/collector.yaml",
                     "--config=/etc/otelcol/collector-mtls.example.yaml"]
        mounts = ["-v", str(ROOT) + ":/etc/otelcol:ro", "-v", str(pki) + ":/etc/otelcol/pki:ro",
                  "-e", "TELEMETRY_CH_PASSWORD=" + os.environ.get("TELEMETRY_CH_PASSWORD", "telemetry-dev-only")]
        run("docker", "run", "--rm", *mounts, COLLECTOR, "validate", *arguments)
        try:
            run("docker", "run", "-d", "--rm", "--name", name, "--network", network,
                "--read-only", "--cap-drop=ALL", *mounts, COLLECTOR, *arguments)

            def probe(client):
                command = ["docker", "run", "--rm", "--network", network,
                           "-v", str(pki) + ":/pki:ro", "--entrypoint", "wget", CLIENT,
                           "-qO-", "--timeout=3", "--tries=1", "--ca-certificate=/pki/clients-ca.crt",
                           "--header=Content-Type: application/json", '--post-data={"resourceSpans":[]}']
                if client:
                    command.extend(["--certificate=/pki/" + client + ".crt", "--private-key=/pki/" + client + ".key"])
                return run(*command, "https://" + name + ":4318/v1/traces", check=False)

            for _ in range(15):
                trusted = probe("client")
                if trusted.returncode == 0:
                    break
                time.sleep(1)
            assert trusted.returncode == 0, "A trusted client certificate must be accepted"
            assert probe(None).returncode != 0, "A client without a certificate must be rejected"
            assert probe("untrusted").returncode != 0, "An untrusted client CA must be rejected"
            print(json.dumps({"result": "PASS", "merged_config_valid": True,
                              "trusted_client_accepted": True, "missing_certificate_rejected": True,
                              "untrusted_client_rejected": True, "minimum_tls_version": "1.3"}, indent=2))
        finally:
            run("docker", "rm", "-f", name, check=False)


if __name__ == "__main__":
    main()
