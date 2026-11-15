"""Opt-in catalog acceptance tests using disposable, isolated Docker databases.

RUN_RUNTIME_SCHEMA_LIVE=1 python3 -m unittest discover -s telemetry-dev -p test_runtime_schema_live.py -v
Images must already be cached; the test never pulls images or touches existing containers.
"""
from __future__ import annotations

import json
import os
from decimal import Decimal
from pathlib import Path
import secrets
import subprocess
import tempfile
import time
import unittest

import runtime_schema as runtime
import runtime_schema_catalog as catalog


@unittest.skipUnless(os.environ.get("RUN_RUNTIME_SCHEMA_LIVE") == "1", "requires opt-in local Docker access")
class LiveCatalogTests(unittest.TestCase):
    def docker(self, *args, data=None, check=True):
        result = subprocess.run(["docker", *args], input=data, capture_output=True, timeout=60)
        if check and result.returncode:
            self.fail("Disposable database command failed: " + result.stderr.decode(errors="replace")[:1000])
        return result

    def fixture(self, image, engine):
        password = secrets.token_urlsafe(24)
        directory = tempfile.TemporaryDirectory(prefix="runtime-catalog-live-")
        self.addCleanup(directory.cleanup)
        env = Path(directory.name) / "database.env"
        if engine == "postgresql":
            settings = {"POSTGRES_USER": "schema_app", "POSTGRES_PASSWORD": password, "POSTGRES_DB": "app"}
        else:
            settings = {"MYSQL_ROOT_PASSWORD": secrets.token_urlsafe(24), "MYSQL_USER": "schema_app",
                        "MYSQL_PASSWORD": password, "MYSQL_DATABASE": "app"}
        env.write_text("".join(key + "=" + value + "\n" for key, value in settings.items()))
        env.chmod(0o600)
        name = "runtime-schema-test-" + secrets.token_hex(6)
        created = self.docker("run", "--detach", "--rm", "--pull=never", "--network=none", "--name", name,
                              "--env-file", str(env),
                              "--label", "com.devassistant.telemetry.enabled=true",
                              "--label", "com.devassistant.telemetry.project=catalog-test",
                              "--label", "com.devassistant.telemetry.environment=test",
                              "--label", "com.devassistant.telemetry.service=database", image)
        container = created.stdout.decode().strip()
        self.addCleanup(lambda: self.docker("rm", "--force", container, check=False))
        if engine == "postgresql":
            command = ["psql", "-X", "-v", "ON_ERROR_STOP=1", "-U", "schema_app", "-d", "app"]
        else:
            command = ["sh", "-c", 'export MYSQL_PWD="$MYSQL_ROOT_PASSWORD"; client=mysql; command -v mariadb >/dev/null 2>&1 && client=mariadb; exec "$client" --no-defaults -uroot --batch --raw']
        deadline = time.monotonic() + 55
        while True:
            # Official images first start a temporary bootstrap server. Wait for
            # the final server to become PID 1 before trusting a successful query.
            started = self.docker("exec", container, "sh", "-c",
                                  'case "$(cat /proc/1/comm)" in postgres|mysqld|mariadbd) exit 0;; *) exit 1;; esac', check=False)
            ready = self.docker("exec", "-i", container, *command, data=b"SELECT 1;", check=False) if started.returncode == 0 else started
            if started.returncode == 0 and ready.returncode == 0:
                break
            if time.monotonic() >= deadline:
                self.fail("Disposable " + engine + " did not become ready within 55 seconds")
            time.sleep(.5)
        inspect = json.loads(self.docker("inspect", "--format", "{{json .}}", container).stdout)
        source = {"project_id": "catalog-test", "environment": "test", "host_id": "a" * 64,
                  "container_id": container, "image_id": inspect["Image"], "service": "database", "state": "running"}
        return container, source, command, password

    def exercise(self, image, engine):
        container, source, command, password = self.fixture(image, engine)
        if engine == "postgresql":
            ddl = """
CREATE DATABASE second;
CREATE SCHEMA audit;
CREATE TABLE public.parent (tenant integer NOT NULL, id bigint GENERATED ALWAYS AS IDENTITY,
 title text DEFAULT 'hello', created_at timestamp DEFAULT now(), PRIMARY KEY (tenant,id), UNIQUE (tenant,title));
CREATE TABLE public.child (tenant integer NOT NULL, parent_id bigint NOT NULL, amount numeric(12,2) DEFAULT 0,
 CONSTRAINT child_parent FOREIGN KEY (tenant,parent_id) REFERENCES public.parent(tenant,id) ON DELETE CASCADE ON UPDATE RESTRICT);
CREATE INDEX child_amount ON public.child(amount);
CREATE INDEX parent_lower_title ON public.parent(lower(title)) WHERE title IS NOT NULL;
CREATE TABLE audit.events (id integer PRIMARY KEY);
"""
        else:
            ddl = """
CREATE DATABASE second;
GRANT SELECT ON second.* TO 'schema_app'@'%';
CREATE TABLE app.parent (tenant integer NOT NULL, id bigint NOT NULL AUTO_INCREMENT,
 title varchar(200) DEFAULT 'hello', created_at timestamp DEFAULT CURRENT_TIMESTAMP,
 PRIMARY KEY (id,tenant), UNIQUE KEY parent_tenant_title (tenant,title), UNIQUE KEY parent_tenant_id (tenant,id));
CREATE TABLE app.child (tenant integer NOT NULL, parent_id bigint NOT NULL, amount decimal(12,2) DEFAULT 0,
 CONSTRAINT child_parent FOREIGN KEY (tenant,parent_id) REFERENCES app.parent(tenant,id) ON DELETE CASCADE ON UPDATE RESTRICT);
CREATE INDEX child_amount ON app.child(amount);
CREATE INDEX parent_title_prefix ON app.parent(title(12));
CREATE TABLE second.events (id integer PRIMARY KEY);
"""
        self.docker("exec", "-i", container, *command, data=ddl.encode())
        docker = runtime.Docker(["docker"], runtime.Budget())
        config = runtime.inspect_source(docker, source)
        detected, configured = catalog.detect_engine(config)
        self.assertEqual(detected, engine)
        self.assertEqual(configured, "app")
        client = catalog.Client(docker, container, detected)
        discovery = catalog.discover(client, detected, configured)
        self.assertEqual(discovery["configured_database"], "app")
        self.assertIn("second", discovery["databases"])
        self.assertNotIn("information_schema", discovery["databases"])
        if engine == "postgresql":
            self.assertEqual(discovery["configured_schemas"], ["public"])
            self.assertIn("audit", discovery["schemas"])
        scope = ["public"] if engine == "postgresql" else ["app"]
        schema, warnings = catalog.extract(client, source, detected, "app", scope)
        self.assertEqual([table["name"] for table in schema["tables"]], ["child", "parent"])
        tables = {table["name"]: table for table in schema["tables"]}
        parent, child = tables["parent"], tables["child"]
        self.assertEqual([col["name"] for col in parent["columns"]], ["tenant", "id", "title", "created_at"])
        self.assertTrue(any(len(key["column_ids"]) == 2 for key in parent["keys"]))
        self.assertEqual(child["columns"][2]["type"], "numeric(12,2)" if engine == "postgresql" else "decimal(12,2)")
        relationship = schema["relationships"][0]
        self.assertEqual(len(relationship["source_column_ids"]), 2)
        self.assertEqual(relationship["on_delete"], "cascade")
        self.assertEqual(relationship["on_update"], "restrict")
        self.assertEqual(child["indexes"][0]["name"], "child_amount")
        self.assertTrue(warnings)
        unsupported = "parent_lower_title" if engine == "postgresql" else "parent_title_prefix"
        self.assertIn(unsupported, "\n".join(parent["inspection_metadata"]))
        self.assertNotIn(password, json.dumps(schema))
        self.assertEqual(parent["columns"][2]["default"], "hello")
        self.assertIsNone(parent["columns"][2]["default_expression"])
        self.assertEqual(Decimal(child["columns"][2]["default"]), Decimal("0"))
        self.assertIsNone(child["columns"][2]["default_expression"])
        self.assertIsNone(parent["columns"][3]["default"])
        self.assertTrue(parent["columns"][3]["default_expression"])
        if engine == "postgresql":
            self.assertEqual(parent["columns"][1]["identity"], "always")
            self.assertEqual(parent["columns"][3]["default_expression"], "now()")
            other, _ = catalog.extract(client, source, detected, "app", ["audit"])
            self.assertEqual([table["name"] for table in other["tables"]], ["events"])
        else:
            self.assertTrue(parent["columns"][1]["auto_increment"])
            other, _ = catalog.extract(client, source, detected, "second", ["second"])
            self.assertEqual([table["name"] for table in other["tables"]], ["events"])
        # A fresh run has identical source IDs and preserves all metadata through JSON transport.
        again, _ = catalog.extract(client, source, detected, "app", scope)
        self.assertEqual(schema, again)
        self.assertEqual(schema, json.loads(json.dumps(schema)))
        with self.assertRaises(runtime.SchemaError):
            runtime.inspect_source(docker, {**source, "project_id": "another-project"})

    def test_postgresql(self):
        self.exercise("postgres:16-alpine", "postgresql")

    def test_mysql(self):
        self.exercise("mysql:5.7", "mysql")

    def test_mariadb(self):
        self.exercise("mariadb:11.4", "mariadb")


if __name__ == "__main__":
    unittest.main()
