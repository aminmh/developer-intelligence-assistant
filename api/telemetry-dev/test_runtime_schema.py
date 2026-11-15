from __future__ import annotations

import base64
import atexit
import hashlib
import hmac
import json
import io
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import runtime_schema as runtime
import runtime_schema_catalog as catalog
import runtime_setup as setup
import runtime_ssh_credentials as credentials

atexit.unregister(credentials.stop_agent)


SOURCE = {"project_id":"project","environment":"dev","host_id":"a"*64,"container_id":"b"*64,
          "image_id":"sha256:"+"c"*64,"service":"database","state":"running","updated_at":"2026-11-14T10:00:00Z"}


def token(body, *, operation="extract", **changes):
    claims = {"aud":"runtime-helper","origin":"http://localhost:3000","sub":"admin","exp":time.time()+120,
              "jti":"unique-token","operation":"runtime-schema-"+operation if operation else None,
              **{key:body["source"][key] for key in ("project_id","environment","host_id","container_id","image_id")},
              "database":body.get("database") or "","schemas":body.get("schemas") or [],**changes}
    encode = lambda value:base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
    header, payload = encode({"alg":"HS256"}), encode(claims)
    signature = base64.urlsafe_b64encode(hmac.new(b"test-key",(header+"."+payload).encode(),hashlib.sha256).digest()).decode().rstrip("=")
    return header+"."+payload+"."+signature


class RuntimeSchemaTests(unittest.TestCase):
    def setUp(self):
        self.body = {"source":dict(SOURCE),"database":"app","schemas":["public"]}
        self.signing = patch.object(setup,"helper_secret",return_value=b"test-key")
        self.signing.start()
        self.addCleanup(self.signing.stop)

    def authorize(self, body=None, encoded=None, operation="extract"):
        body = body or self.body
        return runtime.authorize(operation,body,encoded or token(body,operation=operation),"http://localhost:3000",setup)

    def test_scope_requires_every_exact_identity_and_catalog_selection(self):
        encoded = token(self.body)
        self.assertEqual(self.authorize(encoded=encoded)["sub"],"admin")
        for key in ("project_id","environment","host_id","container_id","image_id"):
            changed = {**self.body,"source":{**SOURCE,key:"d"*64}}
            with self.subTest(key=key), self.assertRaises(runtime.SchemaError):
                self.authorize(changed,encoded)
        for changed in ({**self.body,"database":"other"},{**self.body,"schemas":["other"]}):
            with self.assertRaises(runtime.SchemaError):
                self.authorize(changed,encoded)

    def test_rejects_setup_token_expiry_missing_user_and_short_container(self):
        for changes in ({"operation":None},{"exp":time.time()-1},{"sub":""},{"jti":None}):
            with self.subTest(changes=changes),self.assertRaises(runtime.SchemaError):
                self.authorize(encoded=token(self.body,**changes))
        self.body["source"]["container_id"]="b"*12
        with self.assertRaises(runtime.SchemaError):
            self.authorize()

    def test_origin_and_unsigned_scope_cannot_be_replayed(self):
        with self.assertRaises(runtime.SchemaError):
            runtime.authorize("extract",self.body,token(self.body),"https://evil.test",setup)
        with self.assertRaises(runtime.SchemaError):
            self.authorize(encoded=token(self.body),operation="discover")

    def test_inspection_refuses_stale_label_image_and_running_state(self):
        labels={"com.devassistant.telemetry."+k:v for k,v in {"enabled":"true","project":"project","environment":"dev","service":"database"}.items()}
        record={"Id":SOURCE["container_id"],"Image":SOURCE["image_id"],"State":{"Running":True},"Config":{"Labels":labels}}
        docker=Mock()
        docker.run.return_value=json.dumps(record)
        runtime.inspect_source(docker,SOURCE)
        for bad in ({**record,"Image":"replaced"},{**record,"State":{"Running":False}},
                    {**record,"Config":{"Labels":{**labels,"com.devassistant.telemetry.enabled":"false"}}}):
            docker.run.return_value=json.dumps(bad)
            with self.assertRaises(runtime.SchemaError) as raised:
                runtime.inspect_source(docker,SOURCE)
            self.assertEqual(raised.exception.status,409)

    def test_subprocess_diagnostics_are_never_exposed(self):
        with self.assertRaises(runtime.SchemaError) as raised:
            runtime.Budget().run([sys.executable,"-c","import sys; print('PASSWORD=top-secret',file=sys.stderr); sys.exit(1)"])
        self.assertNotIn("top-secret",str(raised.exception))

    def test_output_budget_refuses_without_truncating(self):
        with patch.object(runtime,"MAX_BYTES",64),self.assertRaises(runtime.SchemaError) as raised:
            runtime.Budget().run([sys.executable,"-c","print('x'*1000)"])
        self.assertEqual(raised.exception.status,413)
        with patch.object(runtime,"MAX_BYTES",64):
            budget=runtime.Budget()
            budget.run([sys.executable,"-c","print('x'*40)"])
            with self.assertRaises(runtime.SchemaError):
                budget.run([sys.executable,"-c","print('x'*40)"])

    def test_elapsed_deadline_terminates_process(self):
        with patch.object(runtime,"TIMEOUT",.1),self.assertRaises(runtime.SchemaError):
            runtime.Budget().run([sys.executable,"-c","import time; time.sleep(5)"])

    def test_receipt_covers_schema_provenance_user_origin_and_expiry(self):
        claims=self.authorize()
        schema={"version":1,"tables":[],"relationships":[]}
        with patch.object(setup,"shared_secret",return_value=b"shared-secret"):
            envelope=runtime.signed_result(schema,{"database":"app"},claims,setup)
        receipt=envelope.pop("receipt")
        key=hmac.new(b"shared-secret",b"devassistant-runtime-schema-result-v1",hashlib.sha256).digest()
        self.assertEqual(receipt,hmac.new(key,runtime.canonical(envelope),hashlib.sha256).hexdigest())
        envelope["sub"]="other"
        self.assertNotEqual(receipt,hmac.new(key,runtime.canonical(envelope),hashlib.sha256).hexdigest())

    def test_engine_requires_image_or_configuration_evidence(self):
        with self.assertRaises(runtime.SchemaError):
            catalog.detect_engine({"Image":"nginx:1","Labels":{"service":"postgres"}})
        self.assertEqual(catalog.detect_engine({"Image":"postgres:17","Env":["POSTGRES_DB=app","POSTGRES_PASSWORD=secret"]}),("postgresql","app"))
        self.assertEqual(catalog.detect_engine({"Image":"mariadb:11"})[0],"mariadb")

    def test_discovery_does_not_extract_and_supports_database_schema_choices(self):
        client=Mock()
        client.query.side_effect=[[{"kind":"version","value":"17"},{"kind":"database","value":"app"}],
                                  [{"kind":"schema","value":"public"},{"kind":"schema","value":"audit"}]]
        result=catalog.discover(client,"postgresql","app","app")
        self.assertEqual(result["schemas"],["public","audit"])
        self.assertEqual(result["configured_schemas"],["public"])
        self.assertEqual(client.query.call_count,2)
        self.assertEqual(client.query.call_args_list[0].args[1],"app")

    def test_mysql_uses_application_account_even_with_root_bootstrap_credentials(self):
        docker=Mock()
        docker.run.return_value=""
        catalog.Client(docker,SOURCE["container_id"],"mysql").query("SELECT 1;")
        script=docker.run.call_args.args[0][5]
        with tempfile.TemporaryDirectory() as directory:
            for name in ("mysql","mariadb"):
                executable=Path(directory)/name
                executable.write_text('#!/bin/sh\nprintf "%s\\n" "$MYSQL_PWD" "$@"\n')
                executable.chmod(0o700)
            password_file=Path(directory)/"password"
            password_file.write_text("file-password\n")
            for family in ("MYSQL","MARIADB"):
                for use_file in (False,True):
                    env={"PATH":directory+":/bin:/usr/bin",family+"_USER":"app-user",family+"_ROOT_PASSWORD":"root-password"}
                    env[family+("_PASSWORD_FILE" if use_file else "_PASSWORD")]=str(password_file) if use_file else "app-password"
                    with self.subTest(family=family,use_file=use_file):
                        result=subprocess.run(["/bin/sh","-c",script],env=env,capture_output=True,text=True,check=True)
                        self.assertEqual(result.stdout.splitlines()[0],"file-password" if use_file else "app-password")
                        self.assertIn("--user=app-user",result.stdout.splitlines())
                        self.assertNotIn("root-password",result.stdout)

    def test_postgres_discovery_uses_configured_database_before_selection(self):
        client=Mock()
        client.query.side_effect=[[{"kind":"version","value":"16"},{"kind":"database","value":"app"}],[]]
        catalog.discover(client,"postgresql","app")
        self.assertEqual(client.query.call_args_list[0].args[1],"app")

    def test_container_client_credentials_stay_inside_container_and_transactions_are_read_only(self):
        for engine in ("postgresql","mysql","mariadb"):
            docker=Mock()
            docker.run.return_value='{"kind":"version","value":"17"}\n'
            catalog.Client(docker,SOURCE["container_id"],engine).query("SELECT 1;","app")
            call=docker.run.call_args
            self.assertIn(b"READ ONLY",call.kwargs["input_data"])
            self.assertNotIn("super-secret",str(call.args))
            self.assertEqual(call.args[0][:3],["exec","-i",SOURCE["container_id"]])
            if engine == "postgresql":
                self.assertIn('PGDATABASE="$1"',call.args[0][5])
                self.assertNotIn('--dbname=',call.args[0][5])
            else:
                self.assertIn('--no-defaults',call.args[0][5])
                self.assertIn('--protocol=SOCKET',call.args[0][5])

    def test_ssh_scan_reports_real_fingerprint_and_rejects_changed_key(self):
        helper=SimpleNamespace(resolve_ssh_config_target=lambda payload:{"host":"db.example","ssh_user":"deploy","ssh_port":2222})
        budget=Mock()
        budget.run.return_value="[db.example]:2222 ssh-ed25519 "+base64.b64encode(b"host-key").decode()+"\n"
        scanned=runtime.scan_ssh({},helper,budget)
        expected="SHA256:"+base64.b64encode(hashlib.sha256(b"host-key").digest()).decode().rstrip("=")
        self.assertEqual(scanned["fingerprint"],expected)
        self.assertIn("2222",budget.run.call_args.args[0])
        body={"source":SOURCE,"ssh":{"host":"db.example","confirmed":True,"host_fingerprint":"SHA256:changed"}}
        with patch.object(setup,"resolve_ssh_config_target",helper.resolve_ssh_config_target),patch.object(runtime,"scan_ssh",return_value=scanned),patch.object(setup,"cert_fingerprint",return_value="f"*64):
            with self.assertRaises(runtime.SchemaError) as raised:
                runtime.handle("associate",body,token(body,operation="associate"),"http://localhost:3000",setup)
        self.assertEqual(raised.exception.status,409)

    def test_remote_certificate_must_match_reporting_host(self):
        docker=Mock()
        docker.run.side_effect=["e"*64+"\n","f"*64]
        with self.assertRaises(runtime.SchemaError) as raised:
            runtime.verify_reporting_host(docker,SOURCE)
        self.assertEqual(raised.exception.status,409)
        docker.run.side_effect=["e"*64+"\n",SOURCE["host_id"]]
        runtime.verify_reporting_host(docker,SOURCE)

    def test_ssh_association_persists_only_after_identity_and_container_checks(self):
        target={"host":"db.example","ssh_user":"deploy","ssh_port":22,"fingerprint":"SHA256:verified","known_host":"db.example ssh-ed25519 a2V5"}
        body={"source":SOURCE,"ssh":{"host":"db.example","confirmed":True,"host_fingerprint":target["fingerprint"]}}
        with tempfile.TemporaryDirectory() as directory,patch.object(setup,"state_path",return_value=Path(directory)/"setup.json"),patch.object(setup,"cert_fingerprint",return_value="f"*64),patch.object(runtime,"scan_ssh",return_value=target),patch.object(runtime,"remote_docker",return_value=Mock()),patch.object(runtime,"inspect_source",return_value={}) as inspected,patch.object(runtime,"verify_reporting_host",side_effect=runtime.SchemaError("certificate mismatch")) as verified:
            with self.assertRaises(runtime.SchemaError):
                runtime.handle("associate",body,token(body,operation="associate"),"http://localhost:3000",setup)
            self.assertFalse(runtime.association_path(setup).exists())
            inspected.assert_not_called()
            verified.side_effect=None
            result=runtime.handle("associate",body,token(body,operation="associate"),"http://localhost:3000",setup)
            self.assertTrue(result["associated"])
            self.assertEqual(runtime.associations(setup)[SOURCE["host_id"]],target)
            self.assertEqual(runtime.association_path(setup).stat().st_mode & 0o777,0o600)
            verified.reset_mock()
            with patch.object(catalog,"detect_engine",return_value=("postgresql","app")),patch.object(catalog,"discover",return_value={"engine":"postgresql"}):
                runtime.handle("discover",self.body,token(self.body,operation="discover"),"http://localhost:3000",setup)
            verified.assert_called_once()

    def test_http_route_rejects_origin_host_and_plain_setup_authorization(self):
        import runtime_ssh_credentials
        import working_tree_sync
        server=Mock()
        with patch.object(setup,"ThreadingHTTPServer",return_value=server) as make_server,patch.object(runtime_ssh_credentials,"reload_credentials"),patch.object(working_tree_sync,"read_grants",return_value=[]),patch.object(setup.signal,"signal"):
            setup.serve_helper()
        handler_class=make_server.call_args.args[1]
        for origin,host,encoded in (("https://evil.test","127.0.0.1:47831",token(self.body)),
                                    ("http://localhost:3000","evil.test:47831",token(self.body)),
                                    ("http://localhost:3000","127.0.0.1:47831",token(self.body,operation=None))):
            handler=object.__new__(handler_class)
            raw=json.dumps(self.body).encode()
            handler.headers={"Content-Length":str(len(raw)),"Origin":origin,"Host":host,"Authorization":"Bearer "+encoded}
            handler.rfile=io.BytesIO(raw)
            handler.path="/runtime-schema/extract"
            handler._json=Mock()
            with patch.object(setup,"helper_origin",return_value="http://localhost:3000"),patch.object(runtime,"handle") as operation:
                handler.do_POST()
            self.assertEqual(handler._json.call_args.args[0],403)
            operation.assert_not_called()

    def test_remote_ssh_pins_confirmed_key_and_agent_without_forwarding(self):
        import runtime_ssh_credentials as credentials
        target={"host":"db.example","ssh_user":"deploy","ssh_port":2222,"known_host":"db.example ssh-ed25519 a2V5"}
        with tempfile.TemporaryDirectory() as directory,patch.object(credentials,"ensure_agent"),patch.object(credentials,"_agent_env",return_value={"SSH_AUTH_SOCK":"/agent"}):
            docker=runtime.remote_docker(target,Mock(),directory)
            self.assertIn("StrictHostKeyChecking=yes",docker.ssh)
            self.assertIn("GlobalKnownHostsFile=/dev/null",docker.ssh)
            self.assertNotIn("-A",docker.ssh)
            self.assertEqual(docker.env,{"SSH_AUTH_SOCK":"/agent"})
            self.assertEqual((Path(directory)/"known_hosts").read_text(),target["known_host"]+"\n")

    def test_normalizer_preserves_composite_order_expressions_identity_and_unsupported_features(self):
        rows=[{"kind":"table","namespace":"public","table":"items","relation_kind":"r"}]
        rows += [{"kind":"column","namespace":"public","table":"items","name":name,"type":"numeric(12,3)","nullable":False,"identity":"a","default_expression":"nextval('ids'::regclass)"} for name in ("a","b")]
        rows += [{"kind":"constraint","namespace":"public","table":"items","name":"pk","constraint_type":"p","columns":["b","a"]},
                 {"kind":"constraint","namespace":"public","table":"items","name":"positive","constraint_type":"c","definition":"CHECK (a > 0)"}]
        schema,warnings=catalog.normalize(rows,SOURCE,"postgresql","app")
        table=schema["tables"][0]
        self.assertEqual(table["keys"][0]["column_ids"],[table["columns"][1]["id"],table["columns"][0]["id"]])
        self.assertIsNone(table["columns"][0]["default"])
        self.assertEqual(table["columns"][0]["identity"],"always")
        self.assertIn("CHECK (a > 0)",table["inspection_metadata"])
        self.assertTrue(any("generation/default" in item for item in table["inspection_metadata"]))
        self.assertTrue(warnings)
        self.assertEqual(schema,catalog.normalize(rows,SOURCE,"postgresql","app")[0])
        self.assertNotEqual(table["id"],catalog.normalize(rows,{**SOURCE,"container_id":"f"*64},"postgresql","app")[0]["tables"][0]["id"])

    def test_catalog_defaults_separate_literals_from_expressions_and_retain_originals(self):
        cases = [
            ("postgresql", "text", "'it''s ready'::text", "", "it's ready", None),
            ("postgresql", "text", "''::character varying", "", "", None),
            ("postgresql", "numeric", "'9007199254740993.01230'::numeric", "", "9007199254740993.01230", None),
            ("postgresql", "numeric", "-12.3400", "", "-12.3400", None),
            ("postgresql", "integer", "(42)", "", "42", None),
            ("postgresql", "boolean", "false", "", False, None),
            ("postgresql", "boolean", "'true'::boolean", "", True, None),
            ("postgresql", "text", "NULL::text", "", None, None),
            ("postgresql", "timestamp", "now()", "", None, "now()"),
            ("postgresql", "integer", "nextval('ids'::regclass)", "", None, "nextval('ids'::regclass)"),
            ("postgresql", "text", "'abc'::custom_type", "", None, "'abc'::custom_type"),
            ("postgresql", "text", "'abc'::character(1)", "", None, "'abc'::character(1)"),
            ("mysql", "varchar(255)", "CURRENT_TIMESTAMP", "", "CURRENT_TIMESTAMP", None),
            ("mysql", "varchar(255)", "it's\\ready", "", "it's\\ready", None),
            ("mysql", "varchar(255)", "NULL", "", "NULL", None),
            ("mysql", "varchar(255)", "", "", "", None),
            ("mysql", "decimal(30,5)", "9007199254740993.01230", "", "9007199254740993.01230", None),
            ("mysql", "timestamp(6)", "CURRENT_TIMESTAMP(6)", "", None, "CURRENT_TIMESTAMP(6)"),
            ("mysql", "varchar(255)", "uuid()", "DEFAULT_GENERATED", None, "uuid()"),
            ("mariadb", "varchar(255)", "'it''s ready'", "", "it's ready", None),
            ("mariadb", "varchar(255)", "'NULL'", "", "NULL", None),
            ("mariadb", "varchar(255)", "''", "", "", None),
            ("mariadb", "decimal(30,5)", "9007199254740993.01230", "", "9007199254740993.01230", None),
            ("mariadb", "varchar(255)", "concat('a','b')", "", None, "concat('a','b')"),
            ("mariadb", "varchar(255)", "'a\\b'", "", None, "'a\\b'"),
            ("mariadb", "timestamp", "current_timestamp()", "", None, "current_timestamp()"),
        ]
        for engine, data_type, original, extra, expected, expression in cases:
            with self.subTest(engine=engine,original=original):
                rows=[{"kind":"table","namespace":"public","table":"items","relation_kind":"r"},
                      {"kind":"column","namespace":"public","table":"items","name":"value","type":data_type,"nullable":True,"default_expression":original,"extra":extra}]
                schema,warnings=catalog.normalize(rows,SOURCE,engine,"app")
                table=schema["tables"][0]
                self.assertEqual(table["columns"][0]["default"],expected)
                self.assertEqual(table["columns"][0]["default_expression"],expression)
                retained="\n".join(table["inspection_metadata"])
                self.assertTrue(original in retained or json.dumps(original) in retained)
                self.assertTrue(warnings)

    def test_postgres_generated_expression_is_retained_without_becoming_default(self):
        rows=[{"kind":"table","namespace":"public","table":"items","relation_kind":"r"},
              {"kind":"column","namespace":"public","table":"items","name":"total","type":"integer","nullable":True,"generated":"s","default_expression":"(price * quantity)"}]
        schema,warnings=catalog.normalize(rows,SOURCE,"postgresql","app")
        table=schema["tables"][0]
        self.assertIsNone(table["columns"][0]["default"])
        self.assertIsNone(table["columns"][0]["default_expression"])
        self.assertIn("Generated column total: (price * quantity)",table["inspection_metadata"])
        self.assertTrue(warnings)

    def test_extract_flow_signs_normalized_catalog_without_container_environment(self):
        labels={"com.devassistant.telemetry."+k:v for k,v in {"enabled":"true","project":"project","environment":"dev","service":"database"}.items()}
        config={"Labels":labels,"Image":"postgres:17","Env":["POSTGRES_DB=app","POSTGRES_PASSWORD=private-container-password"]}
        record={"Id":SOURCE["container_id"],"Image":SOURCE["image_id"],"State":{"Running":True},"Config":config}
        rows=[{"kind":"table","namespace":"public","table":"items","relation_kind":"r"},
              {"kind":"column","namespace":"public","table":"items","name":"id","type":"integer","nullable":False}]
        docker=Mock()
        docker.run.side_effect=[json.dumps(record),
                               '\n'.join(json.dumps(row) for row in [{"kind":"version","value":"17"},{"kind":"database","value":"app"}]),
                               json.dumps({"kind":"schema","value":"public"}),
                               '\n'.join(json.dumps(row) for row in rows),json.dumps(record)]
        with patch.object(setup,"cert_fingerprint",return_value=SOURCE["host_id"]),patch.object(runtime,"local_docker",return_value=docker),patch.object(setup,"shared_secret",return_value=b"shared-secret"):
            result=runtime.handle("extract",self.body,token(self.body),"http://localhost:3000",setup)
        self.assertEqual(result["schema"]["tables"][0]["columns"][0]["type"],"integer")
        self.assertEqual(result["provenance"]["coverage"],{"warnings":[],"complete":True})
        self.assertEqual(result["provenance"]["container_id"],SOURCE["container_id"])
        self.assertNotIn("private-container-password",json.dumps(result))
        self.assertEqual(len(result["receipt"]),64)


if __name__ == "__main__":
    unittest.main()
