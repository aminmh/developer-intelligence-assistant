"""Database client execution and catalog normalization; no application rows are read."""
from __future__ import annotations

import hashlib
import json
import re


def error(message):
    from runtime_schema import SchemaError
    return SchemaError(message)


def literal(value):
    # UTF-8 hex avoids SQL modes/backslash escaping in both supported catalog families.
    return "convert_from(decode('" + value.encode().hex() + "','hex'),'UTF8')"


def mysql_literal(value):
    return "CONVERT(0x" + value.encode().hex() + " USING utf8mb4)"


def detect_engine(config):
    env = dict(item.split("=", 1) for item in config.get("Env", []) if "=" in item)
    image = str(config.get("Image", "")).lower()
    entry = " ".join(str(item) for item in (config.get("Entrypoint") or []) + (config.get("Cmd") or [])).lower()
    evidence = image + " " + entry
    if re.search(r"(?:^|[/ :_-])postgres(?:ql)?(?:[ :/_-]|$)", evidence) or "PG_MAJOR" in env:
        return "postgresql", env.get("POSTGRES_DB") or env.get("POSTGRES_USER") or "postgres"
    if "mariadb" in evidence or "MARIADB_VERSION" in env:
        return "mariadb", env.get("MARIADB_DATABASE") or env.get("MYSQL_DATABASE")
    if "mysql" in evidence or "MYSQL_MAJOR" in env:
        return "mysql", env.get("MYSQL_DATABASE")
    raise error("This container is not a verified PostgreSQL, MySQL, or MariaDB server. Choose a reporting database container with its native client installed.")


class Client:
    def __init__(self, docker, container_id, engine):
        self.docker, self.container_id, self.engine = docker, container_id, engine

    def query(self, sql, database=None, *, raw_output=False):
        if self.engine == "postgresql":
            script = ('export PGPASSWORD="${POSTGRES_PASSWORD:-${PGPASSWORD:-}}"; '
                      'if [ -n "${POSTGRES_PASSWORD_FILE:-}" ]; then export PGPASSWORD="$(cat "$POSTGRES_PASSWORD_FILE")"; fi; '
                      'unset PGHOSTADDR PGSERVICE PGSERVICEFILE; '
                      'case "${PGHOST:-}" in /*) ;; *) PGHOST=/var/run/postgresql;; esac; export PGHOST; '
                      'export PGDATABASE="$1"; '
                      'export PGCONNECT_TIMEOUT=5 PGOPTIONS="-c default_transaction_read_only=on -c statement_timeout=45000 -c lock_timeout=2000"; '
                      'exec psql -X -q -A -t -v ON_ERROR_STOP=1 -U "${POSTGRES_USER:-postgres}"')
            transaction = "BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;\n" + sql + "\nCOMMIT;\n"
            args = ["sh", "-c", script, "runtime-catalog", database or "postgres"]
        else:
            script = ('if [ -n "${MARIADB_USER:-}" ]; then '
                      'catalog_user="$MARIADB_USER"; MYSQL_PWD="${MARIADB_PASSWORD:-${MYSQL_PASSWORD:-}}"; catalog_password_file="${MARIADB_PASSWORD_FILE:-${MYSQL_PASSWORD_FILE:-}}"; '
                      'elif [ -n "${MYSQL_USER:-}" ]; then '
                      'catalog_user="$MYSQL_USER"; MYSQL_PWD="${MYSQL_PASSWORD:-}"; catalog_password_file="${MYSQL_PASSWORD_FILE:-}"; '
                      'else catalog_user=root; MYSQL_PWD="${MARIADB_ROOT_PASSWORD:-${MYSQL_ROOT_PASSWORD:-}}"; catalog_password_file="${MARIADB_ROOT_PASSWORD_FILE:-${MYSQL_ROOT_PASSWORD_FILE:-}}"; fi; '
                      'if [ -n "$catalog_password_file" ]; then MYSQL_PWD="$(cat "$catalog_password_file")" || exit 1; fi; export MYSQL_PWD; '
                      'client=mysql; command -v mariadb >/dev/null 2>&1 && client=mariadb; '
                      'export MYSQL_TEST_LOGIN_FILE=/dev/null; '
                      'exec "$client" --no-defaults --batch --raw --skip-column-names --default-character-set=utf8mb4 --connect-timeout=5 --protocol=SOCKET --user="$catalog_user"')
            transaction = "SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ; START TRANSACTION READ ONLY;\n" + sql + "\nCOMMIT;\n"
            args = ["sh", "-c", script]
        raw = self.docker.run(["exec", "-i", self.container_id, *args], input_data=transaction.encode())
        if raw_output:
            return raw
        try:
            rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
            if any(not isinstance(row, dict) for row in rows):
                raise ValueError
            return rows
        except ValueError:
            raise error("The database client returned unreadable catalog metadata. Check its version and client configuration.") from None

    def show_create(self, qualified_table):
        raw = self.query("SHOW CREATE TABLE " + qualified_table + ";", raw_output=True)
        _, separator, definition = raw.partition("\t")
        if not separator or not definition.strip():
            raise error("A complete table definition could not be read. Check catalog inspection permissions.")
        return definition.strip()


def discover(client, engine, configured, database=None):
    if engine == "postgresql":
        sql = "SELECT json_build_object('kind','version','value',current_setting('server_version')); SELECT json_build_object('kind','database','value',datname) FROM pg_database WHERE datallowconn AND NOT datistemplate AND has_database_privilege(datname,'CONNECT') ORDER BY datname;"
    else:
        sql = "SELECT JSON_OBJECT('kind','version','value',VERSION()); SELECT JSON_OBJECT('kind','database','value',SCHEMA_NAME) FROM information_schema.SCHEMATA WHERE SCHEMA_NAME NOT IN ('mysql','sys','performance_schema','information_schema') ORDER BY SCHEMA_NAME;"
    rows = client.query(sql, (database or configured) if engine == "postgresql" else None)
    databases = [row["value"] for row in rows if row.get("kind") == "database"]
    version = next((row["value"] for row in rows if row.get("kind") == "version"), "")
    if not version:
        raise error("The database server version could not be verified.")
    if engine == "postgresql":
        major = re.match(r"\d+", version)
        if not major or int(major.group()) < 12:
            raise error("Runtime catalog loading requires PostgreSQL 12 or newer to preserve identity and generated column metadata.")
    if engine != "postgresql" and "mariadb" in version.lower():
        engine = "mariadb"
    if database and database not in databases:
        raise error("The selected database is unavailable. Refresh discovery and choose an accessible database.")
    selected = database or (configured if configured in databases else None)
    if engine == "postgresql" and selected:
        rows = client.query("SELECT json_build_object('kind','schema','value',nspname) FROM pg_namespace WHERE nspname <> 'information_schema' AND nspname NOT LIKE 'pg\\_%' ESCAPE '\\' AND has_schema_privilege(oid,'USAGE') ORDER BY nspname;", selected)
        schemas = [row["value"] for row in rows if row.get("kind") == "schema"]
        defaults = ["public"] if "public" in schemas else []
    elif engine != "postgresql" and selected:
        schemas, defaults = [selected], [selected]
    else:
        schemas, defaults = [], []
    return {"engine": engine, "version": version, "databases": databases,
            "configured_database": configured if configured in databases else None,
            "schemas": schemas, "configured_schemas": defaults}


def postgres_sql(schemas):
    scope = ",".join(literal(item) for item in schemas)
    prefix = "WITH selected AS (SELECT c.*,n.nspname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname IN (" + scope + ") AND c.relkind IN ('r','p','v','m','f')) "
    queries = [
        "SELECT json_build_object('kind','table','namespace',nspname,'table',relname,'relation_kind',relkind,'partition',relispartition,'row_security',relrowsecurity,'options',reloptions,'inherits',EXISTS(SELECT 1 FROM pg_inherits WHERE inhrelid=c.oid),'partition_key',pg_get_partkeydef(c.oid),'partition_bound',pg_get_expr(c.relpartbound,c.oid),'parents',(SELECT json_agg(inhparent::regclass::text ORDER BY inhseqno) FROM pg_inherits WHERE inhrelid=c.oid)) FROM selected c ORDER BY nspname,relname",
        "SELECT json_build_object('kind','column','namespace',c.nspname,'table',c.relname,'name',a.attname,'type',format_type(a.atttypid,a.atttypmod),'nullable',NOT a.attnotnull,'default_expression',pg_get_expr(d.adbin,d.adrelid),'identity',a.attidentity,'generated',a.attgenerated,'collation',CASE WHEN a.attcollation<>t.typcollation THEN co.collname END) FROM selected c JOIN pg_attribute a ON a.attrelid=c.oid JOIN pg_type t ON t.oid=a.atttypid LEFT JOIN pg_attrdef d ON d.adrelid=c.oid AND d.adnum=a.attnum LEFT JOIN pg_collation co ON co.oid=a.attcollation WHERE a.attnum>0 AND NOT a.attisdropped ORDER BY c.nspname,c.relname,a.attnum",
        "SELECT json_build_object('kind','constraint','namespace',c.nspname,'table',c.relname,'name',k.conname,'constraint_type',k.contype,'columns',(SELECT json_agg(a.attname ORDER BY u.ord) FROM unnest(k.conkey) WITH ORDINALITY u(num,ord) JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=u.num),'target_namespace',tn.nspname,'target_table',tc.relname,'target_columns',(SELECT json_agg(a.attname ORDER BY u.ord) FROM unnest(k.confkey) WITH ORDINALITY u(num,ord) JOIN pg_attribute a ON a.attrelid=k.confrelid AND a.attnum=u.num),'on_delete',k.confdeltype,'on_update',k.confupdtype,'definition',pg_get_constraintdef(k.oid,true),'deferred',k.condeferrable,'validated',k.convalidated) FROM selected c JOIN pg_constraint k ON k.conrelid=c.oid LEFT JOIN pg_class tc ON tc.oid=k.confrelid LEFT JOIN pg_namespace tn ON tn.oid=tc.relnamespace ORDER BY c.nspname,c.relname,k.conname",
        "SELECT json_build_object('kind','index','namespace',c.nspname,'table',c.relname,'name',ic.relname,'unique',i.indisunique,'columns',(SELECT json_agg(a.attname ORDER BY u.ord) FROM unnest(i.indkey) WITH ORDINALITY u(num,ord) LEFT JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=u.num),'definition',pg_get_indexdef(i.indexrelid),'special',i.indexprs IS NOT NULL OR i.indpred IS NOT NULL OR am.amname<>'btree' OR i.indnatts<>i.indnkeyatts OR NOT i.indisvalid OR coalesce((to_jsonb(i)->>'indnullsnotdistinct')::boolean,false) OR EXISTS(SELECT 1 FROM unnest(i.indoption) opt WHERE opt<>0)) FROM selected c JOIN pg_index i ON i.indrelid=c.oid JOIN pg_class ic ON ic.oid=i.indexrelid JOIN pg_am am ON am.oid=ic.relam WHERE NOT EXISTS(SELECT 1 FROM pg_constraint k WHERE k.conindid=i.indexrelid) ORDER BY c.nspname,c.relname,ic.relname",
        "SELECT json_build_object('kind','metadata','namespace',c.nspname,'table',c.relname,'definition',pg_get_triggerdef(t.oid,true)) FROM selected c JOIN pg_trigger t ON t.tgrelid=c.oid WHERE NOT t.tgisinternal",
        "SELECT json_build_object('kind','metadata','namespace',c.nspname,'table',c.relname,'definition',pg_get_viewdef(c.oid,true)) FROM selected c WHERE c.relkind IN ('v','m')",
        "SELECT json_build_object('kind','metadata','namespace',c.nspname,'table',c.relname,'definition','policy: '||p.polname||' USING '||coalesce(pg_get_expr(p.polqual,p.polrelid),'')||' CHECK '||coalesce(pg_get_expr(p.polwithcheck,p.polrelid),'')) FROM selected c JOIN pg_policy p ON p.polrelid=c.oid",
        "SELECT json_build_object('kind','metadata','namespace',c.nspname,'table',c.relname,'definition','User-defined type: '||tn.nspname||'.'||t.typname||' kind='||t.typtype::text||' base='||format_type(t.typbasetype,t.typtypmod)||' labels='||coalesce((SELECT string_agg(e.enumlabel,', ' ORDER BY e.enumsortorder) FROM pg_enum e WHERE e.enumtypid=t.oid),'')||' constraints='||coalesce((SELECT string_agg(pg_get_constraintdef(k.oid,true),'; ') FROM pg_constraint k WHERE k.contypid=t.oid),'')) FROM selected c JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum>0 AND NOT a.attisdropped JOIN pg_type t ON t.oid=a.atttypid JOIN pg_namespace tn ON tn.oid=t.typnamespace WHERE tn.nspname<>'pg_catalog'",
        "SELECT json_build_object('kind','metadata','namespace',c.nspname,'table',c.relname,'definition','Sequence for '||a.attname||': '||row_to_json(s)::text) FROM selected c JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum>0 AND NOT a.attisdropped JOIN pg_sequence s ON s.seqrelid=pg_get_serial_sequence(format('%I.%I',c.nspname,c.relname),a.attname)::regclass",
    ]
    return ";\n".join(prefix + query for query in queries) + ";"


def mysql_sql(database):
    scope = mysql_literal(database)
    return f"""
SELECT JSON_OBJECT('kind','table','namespace',TABLE_SCHEMA,'table',TABLE_NAME,'relation_kind',TABLE_TYPE,'engine',ENGINE,'collation',TABLE_COLLATION,'options',CREATE_OPTIONS,'comment',TABLE_COMMENT) FROM information_schema.TABLES WHERE TABLE_SCHEMA={scope} ORDER BY TABLE_NAME;
SELECT JSON_OBJECT('kind','column','namespace',TABLE_SCHEMA,'table',TABLE_NAME,'name',COLUMN_NAME,'type',COLUMN_TYPE,'nullable',IS_NULLABLE='YES','default_expression',COLUMN_DEFAULT,'extra',EXTRA,'generated',GENERATION_EXPRESSION,'collation',COLLATION_NAME,'charset',CHARACTER_SET_NAME,'comment',COLUMN_COMMENT) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA={scope} ORDER BY TABLE_NAME,ORDINAL_POSITION;
SELECT JSON_OBJECT('kind','index_part','namespace',TABLE_SCHEMA,'table',TABLE_NAME,'name',INDEX_NAME,'column',COLUMN_NAME,'unique',NON_UNIQUE=0,'ordinal',SEQ_IN_INDEX,'prefix',SUB_PART,'index_type',INDEX_TYPE,'direction',COLLATION) FROM information_schema.STATISTICS WHERE TABLE_SCHEMA={scope} ORDER BY TABLE_NAME,INDEX_NAME,SEQ_IN_INDEX;
SELECT JSON_OBJECT('kind','fk_part','namespace',k.TABLE_SCHEMA,'table',k.TABLE_NAME,'name',k.CONSTRAINT_NAME,'column',k.COLUMN_NAME,'ordinal',k.ORDINAL_POSITION,'target_namespace',k.REFERENCED_TABLE_SCHEMA,'target_table',k.REFERENCED_TABLE_NAME,'target_column',k.REFERENCED_COLUMN_NAME,'on_delete',r.DELETE_RULE,'on_update',r.UPDATE_RULE) FROM information_schema.KEY_COLUMN_USAGE k JOIN information_schema.REFERENTIAL_CONSTRAINTS r ON r.CONSTRAINT_SCHEMA=k.CONSTRAINT_SCHEMA AND r.TABLE_NAME=k.TABLE_NAME AND r.CONSTRAINT_NAME=k.CONSTRAINT_NAME WHERE k.TABLE_SCHEMA={scope} AND k.REFERENCED_TABLE_NAME IS NOT NULL ORDER BY k.TABLE_NAME,k.CONSTRAINT_NAME,k.ORDINAL_POSITION;
SELECT JSON_OBJECT('kind','metadata','namespace',EVENT_OBJECT_SCHEMA,'table',EVENT_OBJECT_TABLE,'definition',CONCAT('TRIGGER ',TRIGGER_NAME,' ',ACTION_TIMING,' ',EVENT_MANIPULATION,' ',ACTION_STATEMENT)) FROM information_schema.TRIGGERS WHERE EVENT_OBJECT_SCHEMA={scope};
SELECT JSON_OBJECT('kind','metadata','namespace',TABLE_SCHEMA,'table',TABLE_NAME,'definition',CONCAT('VIEW ',COALESCE(VIEW_DEFINITION,'[definition unavailable]'))) FROM information_schema.VIEWS WHERE TABLE_SCHEMA={scope};
SELECT JSON_OBJECT('kind','metadata','namespace',TABLE_SCHEMA,'table',TABLE_NAME,'definition',CONCAT('PARTITION ',PARTITION_NAME,' ',COALESCE(PARTITION_EXPRESSION,''),' ',COALESCE(PARTITION_DESCRIPTION,''))) FROM information_schema.PARTITIONS WHERE TABLE_SCHEMA={scope} AND PARTITION_NAME IS NOT NULL;
"""


def extract(client, source, engine, database, schemas):
    rows = client.query(postgres_sql(schemas) if engine == "postgresql" else mysql_sql(database), database)
    if engine != "postgresql":
        # SHOW CREATE is catalog-only, and preserves CHECK constraints and vendor attributes
        # whose information_schema contracts differ between MySQL and MariaDB releases.
        for row in list(rows):
            if row.get("kind") == "table" and row.get("relation_kind") == "BASE TABLE":
                quoted = "`" + database.replace("`", "``") + "`.`" + row["table"].replace("`", "``") + "`"
                # Native SHOW output is not JSON; use a second bounded client method.
                definition = client.show_create(quoted)
                if definition:
                    rows.append({"kind": "metadata", "namespace": database, "table": row["table"], "definition": definition})
    return normalize(rows, source, engine, database)


def constant_default(expression, *, postgres=False):
    """Recognize catalog constants without evaluating SQL or coercing decimal precision."""
    number = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
    pattern = r"\s*(?P<open>\(*)\s*(?P<value>'(?:[^']|'')*'|" + number + r"|true|false|null)\s*(?P<close>\)*)(?P<cast>.*)"
    match = re.fullmatch(pattern, expression, re.IGNORECASE | re.DOTALL)
    if not match or len(match["open"]) != len(match["close"]):
        return False, None
    cast = match["cast"].strip()
    if cast:
        if not postgres or not re.fullmatch(r"::\s*(?:pg_catalog\.)?[a-z][a-z0-9 ]*", cast, re.IGNORECASE):
            return False, None
        cast = re.sub(r"\s+", " ", cast[2:].strip().lower()).removeprefix("pg_catalog.")
        if cast not in {"text", "varchar", "character varying", "uuid",
                        "smallint", "integer", "bigint", "int2", "int4", "int8", "numeric", "decimal", "real",
                        "double precision", "float4", "float8", "boolean", "bool", "date", "time", "timetz",
                        "timestamp", "timestamptz", "time with time zone", "time without time zone",
                        "timestamp with time zone", "timestamp without time zone", "json", "jsonb"}:
            return False, None
    value = match["value"]
    if value.startswith("'"):
        # Backslash semantics depend on SQL mode; retain those definitions as expressions.
        if "\\" in value:
            return False, None
        value = value[1:-1].replace("''", "'")
        if cast in {"bool", "boolean"}:
            if value.lower() not in {"true", "false", "t", "f"}:
                return False, None
            return True, value.lower() in {"true", "t"}
        return True, value
    if value.lower() in {"true", "false"}:
        if cast and cast not in {"bool", "boolean"}:
            return False, None
        return True, value.lower() == "true"
    if cast in {"bool", "boolean"} and value.lower() != "null":
        return False, None
    return True, None if value.lower() == "null" else value


def column_default(row, engine):
    original = row.get("default_expression")
    if original is None or (engine == "postgresql" and row.get("generated")):
        return None, None
    expression = str(original)
    if engine == "mysql":
        temporal = re.match(r"^(timestamp|datetime)\b", row["type"], re.IGNORECASE)
        if "DEFAULT_GENERATED" in (row.get("extra") or "").upper() or (temporal and re.fullmatch(r"current_timestamp(?:\(\d*\))?", expression, re.IGNORECASE)):
            return None, expression
        return expression, None
    recognized, value = constant_default(expression, postgres=engine == "postgresql")
    return (value, None) if recognized else (None, expression)


def normalize(rows, source, engine, database):
    identity = [source[key] for key in ("project_id", "environment", "host_id", "container_id")] + [database]
    def ident(kind, *parts):
        return kind + "_" + hashlib.sha256(json.dumps(identity + list(parts), separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()
    tables, warnings = {}, []
    for row in rows:
        if row.get("kind") == "table":
            name = (row["namespace"], row["table"])
            table = {"id": ident("table", *name), "namespace": name[0], "name": name[1], "columns": [], "keys": [], "indexes": [], "inspection_metadata": []}
            tables[name] = table
            if engine == "postgresql":
                if row.get("relation_kind") != "r" or row.get("partition") or row.get("row_security") or row.get("inherits") or row.get("options"):
                    table["inspection_metadata"].append("PostgreSQL relation features: " + json.dumps({k:v for k,v in row.items() if k not in {"kind","namespace","table"}}, sort_keys=True))
            # MySQL/MariaDB tables keep their full SHOW CREATE TABLE text as one note (see extract),
            # which already carries engine, collation, options and per-column attributes.
    columns = {}
    for row in rows:
        if row.get("kind") != "column":
            continue
        key = (row["namespace"], row["table"])
        if key not in tables:
            continue
        default, default_expression = column_default(row, engine)
        col = {"id": ident("column", *key, row["name"]), "name": row["name"], "type": row["type"], "nullable": bool(row["nullable"]),
               "default": default, "default_expression": default_expression, "identity": {"a":"always","d":"by default"}.get(row.get("identity")), "collation": row.get("collation")}
        if engine != "postgresql":
            col["auto_increment"] = "auto_increment" in (row.get("extra") or "").lower()
            # Plain columns need no note; keep the catalog row only when it carries a default,
            # generation, comment or other attribute that the column model does not represent.
            extra = re.sub(r"\bauto_increment\b", "", row.get("extra") or "", flags=re.IGNORECASE).strip()
            if row.get("default_expression") is not None or extra or row.get("generated") or row.get("comment"):
                tables[key]["inspection_metadata"].append("Column catalog: " + json.dumps(row, sort_keys=True))
        elif row.get("generated"):
            tables[key]["inspection_metadata"].append("Generated column " + row["name"] + ": " + str(row.get("default_expression")))
        elif row.get("identity") or row.get("default_expression") is not None:
            tables[key]["inspection_metadata"].append("Column generation/default expression " + row["name"] + ": " + str(row.get("identity") or row.get("default_expression")))
        tables[key]["columns"].append(col)
        columns[(*key,row["name"])] = col["id"]
    relationships = []
    index_parts, fk_parts = {}, {}
    actions = {"a":"no action","r":"restrict","c":"cascade","n":"set null","d":"set default"}
    for row in rows:
        key = (row.get("namespace"), row.get("table"))
        table = tables.get(key)
        if table is None:
            continue
        kind = row.get("kind")
        if kind == "metadata":
            table["inspection_metadata"].append(str(row["definition"]))
        elif kind in {"index_part","fk_part"}:
            (index_parts if kind == "index_part" else fk_parts).setdefault((*key,row["name"]), []).append(row)
        elif kind in {"index","constraint"}:
            names = row.get("columns") or []
            ids = [columns.get((*key,name)) for name in names]
            constraint_type = row.get("constraint_type")
            special = row.get("special") or row.get("deferred") or row.get("validated") is False or "NULLS NOT DISTINCT" in row.get("definition", "") or "MATCH FULL" in row.get("definition", "")
            if not names or None in ids or special or (kind == "constraint" and constraint_type not in {"p","u","f"}):
                table["inspection_metadata"].append(row.get("definition") or "Unsupported catalog constraint/index")
                continue
            item = {"id":ident(kind,*key,row["name"]), "name":row["name"], "column_ids":ids}
            if kind == "index":
                table["indexes"].append({**item,"unique":bool(row.get("unique"))})
            elif constraint_type in {"p","u"}:
                table["keys"].append({**item,"kind":"primary" if constraint_type=="p" else "unique"})
            else:
                target_key = (row.get("target_namespace"),row.get("target_table"))
                target_ids = [columns.get((*target_key,name)) for name in row.get("target_columns") or []]
                if target_key not in tables or not target_ids or None in target_ids:
                    table["inspection_metadata"].append("Foreign key outside selected schemas: " + row.get("definition",row["name"]))
                else:
                    relationships.append({"id":ident("relationship",*key,row["name"]),"name":row["name"],"source_table_id":table["id"],"source_column_ids":ids,"target_table_id":tables[target_key]["id"],"target_column_ids":target_ids,"on_delete":actions.get(row.get("on_delete")),"on_update":actions.get(row.get("on_update"))})
    for key, parts in index_parts.items():
        parts.sort(key=lambda row:row["ordinal"])
        table = tables[key[:2]]
        ids = [columns.get((*key[:2],row["column"])) for row in parts]
        if None in ids or any(row.get("prefix") or row.get("index_type") != "BTREE" or row.get("direction") == "D" for row in parts):
            table["inspection_metadata"].append("Index catalog: " + json.dumps(parts,sort_keys=True))
        elif key[2] == "PRIMARY" or parts[0]["unique"]:
            table["keys"].append({"id":ident("key",*key),"name":key[2],"kind":"primary" if key[2]=="PRIMARY" else "unique","column_ids":ids})
        else:
            table["indexes"].append({"id":ident("index",*key),"name":key[2],"unique":False,"column_ids":ids})
    for key, parts in fk_parts.items():
        parts.sort(key=lambda row:row["ordinal"])
        row = parts[0]
        table = tables[key[:2]]
        target_key = (row["target_namespace"],row["target_table"])
        if target_key not in tables:
            table["inspection_metadata"].append("Foreign key outside selection: " + json.dumps(parts,sort_keys=True))
            continue
        relationships.append({"id":ident("relationship",*key),"name":key[2],"source_table_id":table["id"],"source_column_ids":[columns[(*key[:2],p["column"])] for p in parts],"target_table_id":tables[target_key]["id"],"target_column_ids":[columns[(*target_key,p["target_column"])] for p in parts],"on_delete":row["on_delete"].lower(),"on_update":row["on_update"].lower()})
    for table in tables.values():
        if table["inspection_metadata"]:
            warnings.append("Inspect retained catalog features for " + table["namespace"] + "." + table["name"] + " before editing; migration generation is restricted for this table.")
    return {"version":1,"tables":list(tables.values()),"relationships":relationships}, warnings
