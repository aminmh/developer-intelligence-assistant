"""Live acceptance checks. A failed credential never triggers a password reset."""
from __future__ import annotations

import base64
import io
from html.parser import HTMLParser
from http.cookiejar import CookieJar
import json
import ssl
import subprocess
import time
import uuid
import zipfile
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urljoin
from urllib.request import Request, build_opener, HTTPSHandler, HTTPCookieProcessor, ProxyHandler

from config import SERVICES, selected, url, validate
from state import ROOT, STATE, atomic, env_read, load, save, redact
from system import desktop, run


def browser():
    caroot = run("mkcert", "-CAROOT")
    context = ssl.create_default_context(cafile=caroot + "/rootCA.pem")
    cookies = CookieJar()
    return build_opener(ProxyHandler({}), HTTPSHandler(context=context), HTTPCookieProcessor(cookies)), cookies


def request(opener, address, *, data=None, headers=None, method=None):
    req = Request(address, data=data, headers=headers or {}, method=method)
    try:
        with opener.open(req, timeout=30) as response:
            return response.status, response.read(), response.headers
    except HTTPError as response:
        return response.code, response.read(), response.headers


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


class LoginForm(HTMLParser):
    def __init__(self):
        super().__init__()
        self.action = ""
        self.fields = {}

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "form" and values.get("id") == "kc-form-login":
            self.action = values.get("action", "")
        if tag == "input" and values.get("type") == "hidden" and values.get("name"):
            self.fields[values["name"]] = values.get("value", "")


def configure_oidc(opener, c: dict, web: dict) -> None:
    base = url(c, "keycloak")
    status, body, _ = request(opener, base + "/realms/master/protocol/openid-connect/token", data=urlencode({
        "grant_type": "password", "client_id": "admin-cli", "username": web["KEYCLOAK_ADMIN_USERNAME"],
        "password": web["KEYCLOAK_ADMIN_PASSWORD"]}).encode())
    require(status == 200, "Stored Keycloak administration credentials do not authenticate. Existing account/data preserved; reconcile web/.env with the current administrator.")
    headers = {"Authorization": "Bearer " + json.loads(body)["access_token"], "Content-Type": "application/json"}
    clients = [(web["OIDC_CLIENT_ID"], web["OIDC_CLIENT_SECRET"], web["OIDC_REDIRECT_URI"], url(c, "app"))]
    if "outline" in c["optional"]:
        clients.append(("developer-assistant-outline", web["OUTLINE_OIDC_CLIENT_SECRET"], url(c, "outline") + "/auth/oidc.callback", url(c, "outline")))
    for client_id, secret, redirect, origin in clients:
        endpoint = base + "/admin/realms/developer-assistant/clients"
        status, body, _ = request(opener, endpoint + "?" + urlencode({"clientId": client_id}), headers=headers)
        require(status == 200, "Could not read the existing Keycloak clients")
        rows = json.loads(body)
        if rows:
            current = rows[0]
            status, body, _ = request(opener, endpoint + "/" + current["id"] + "/client-secret", headers=headers)
            require(status == 200 and json.loads(body).get("value") == secret,
                    "Stored OIDC client secret differs from the persisted realm. No secret was reset; reconcile web/.env and resume.")
            current.update(redirectUris=[redirect], webOrigins=[origin])
            current.setdefault("attributes", {})["post.logout.redirect.uris"] = origin + "/*"
            status, _, _ = request(opener, endpoint + "/" + current["id"], headers=headers, data=json.dumps(current).encode(), method="PUT")
        else:
            document = {"clientId": client_id, "secret": secret, "enabled": True, "publicClient": False,
                        "protocol": "openid-connect", "standardFlowEnabled": True, "directAccessGrantsEnabled": False,
                        "redirectUris": [redirect], "webOrigins": [origin], "attributes": {"post.logout.redirect.uris": origin + "/*"}}
            status, _, _ = request(opener, endpoint, headers=headers, data=json.dumps(document).encode())
        require(status in (201, 204), "Keycloak client address configuration failed")


def login(opener, cookies, c: dict, web: dict) -> None:
    app = url(c, "app")
    status, page, _ = request(opener, app + "/api/auth/login")
    form = LoginForm()
    form.feed(page.decode())
    require(status == 200 and bool(form.action), "OIDC authorization flow did not return the Keycloak login form")
    require(urlsplit(form.action).netloc == urlsplit(url(c, "keycloak")).netloc, "OIDC login form points at an unexpected host")
    form.fields.update(username="developer-admin", password=web["KEYCLOAK_BOOTSTRAP_PASSWORD"], credentialId="")
    status, _, _ = request(opener, form.action, data=urlencode(form.fields).encode())
    require(status == 200, "Application bootstrap login failed; existing password was preserved")
    status, body, _ = request(opener, app + "/api/auth/me")
    require(status == 200 and json.loads(body).get("user", {}).get("sub") == web["OIDC_BOOTSTRAP_ADMIN_SUB"],
            "Stored application bootstrap password does not authenticate against the existing realm; no account was reset")
    require(any(cookie.name == "devassistant_session" and cookie.secure for cookie in cookies), "Application session cookie is missing or not secure")


def logout(opener, c: dict) -> None:
    app = url(c, "app")
    status, _, _ = request(opener, app + "/api/auth/logout", data=b"{}", headers={"Content-Type": "application/json", "Origin": app})
    require(status == 200, "Application logout failed")
    status, _, _ = request(opener, app + "/api/auth/me")
    require(status == 401, "Application session remained active after logout")


def upload_check(opener, c: dict) -> None:
    """Exercise session issuance, TUS creation/resume and the MinIO finish hook."""
    from cli import compose
    fixture = io.BytesIO()
    with zipfile.ZipFile(fixture, "w") as archive:
        archive.writestr("README.txt", "Installer upload readiness probe\n")
    payload = fixture.getvalue()
    project = "installer-probe-" + uuid.uuid4().hex
    headers = {"Content-Type": "application/json", "Origin": url(c, "app")}
    status, body, _ = request(opener, url(c, "app") + "/api/rag/upload", headers=headers,
                              data=json.dumps({"filename": "installer-probe.zip", "size": len(payload), "project_id": project}).encode())
    require(status == 200, "Authenticated upload session issuance failed")
    session = json.loads(body)
    endpoint = url(c, "uploads") + "/files/"
    require(session["tus_endpoint"] == endpoint, "Upload session returned an incorrect public HTTPS endpoint")
    status, _, preflight = request(opener, endpoint, method="OPTIONS", headers={
        "Origin": url(c, "app"), "Access-Control-Request-Method": "PATCH",
        "Access-Control-Request-Headers": "authorization,tus-resumable,upload-offset,content-type"})
    require(status in (200, 204) and preflight.get("Access-Control-Allow-Origin") in (url(c, "app"), "*"),
            "Browser upload CORS preflight failed")
    allowed = preflight.get("Access-Control-Allow-Headers", "").lower()
    require(all(name in allowed for name in ("authorization", "tus-resumable", "upload-offset", "content-type")),
            "Browser upload preflight omitted required TUS headers")
    metadata = {"filename": "installer-probe.zip", "upload_kind": "rag-document", "project_id": project,
                "upload_session_id": session["upload_session_id"], "upload_token": session["upload_token"]}
    tus = {"Tus-Resumable": "1.0.0", "Authorization": "Bearer " + session["upload_token"], "Origin": url(c, "app")}
    create = dict(tus, **{"Upload-Length": str(len(payload)), "Upload-Metadata": ",".join(
        key + " " + base64.b64encode(value.encode()).decode() for key, value in metadata.items())})
    status, _, response_headers = request(opener, endpoint, data=b"", headers=create)
    require(status == 201, "TUS upload creation failed")
    require(response_headers.get("Access-Control-Allow-Origin") in (url(c, "app"), "*"), "Upload creation omitted browser CORS access")
    location = urljoin(endpoint, response_headers.get("Location", ""))
    require(location.startswith(endpoint) and location != endpoint, "TUS returned an incorrect public upload location")
    # Two PATCHes with an intervening HEAD exercise the browser's resume protocol.
    split = len(payload) // 2
    for offset, chunk in ((0, payload[:split]), (split, payload[split:])):
        status, _, head = request(opener, location, headers=tus, method="HEAD")
        require(status == 200 and head.get("Upload-Offset") == str(offset), "TUS resume offset mismatch")
        patch = dict(tus, **{"Upload-Offset": str(offset), "Content-Type": "application/offset+octet-stream"})
        status, _, _ = request(opener, location, headers=patch, data=chunk, method="PATCH")
        require(status == 204, "TUS upload PATCH failed")
    key = "rag-uploads/" + session["upload_session_id"] + "/installer-probe.zip"
    # Probe only our unique object. No existing uploads or application accounts change.
    code = ("import os,time; from minio import Minio; "
            "c=Minio('minio:9000',access_key=os.environ['AWS_ACCESS_KEY_ID'],secret_key=os.environ['AWS_SECRET_ACCESS_KEY'],secure=False)\n"
            f"key={key!r}\nexpected={payload!r}\n"
            "for attempt in range(30):\n"
            " try:\n"
            "  response=c.get_object('tus-uploads',key)\n"
            "  try: actual=response.read()\n"
            "  finally: response.close(); response.release_conn()\n"
            "  assert actual==expected\n"
            "  c.remove_object('tus-uploads',key)\n"
            "  break\n"
            " except Exception:\n"
            "  if attempt==29: raise\n"
            "  time.sleep(1)\n")
    compose("api", "exec", "-T", "python-api", "python", "-c", code, capture=True)


def sonarqube_check(opener, c: dict) -> None:
    password = env_read(ROOT / "api/.env")["SONARQUBE_ADMIN_PASSWORD"]
    base = url(c, "sonarqube")

    def auth(value):
        return {"Authorization": "Basic " + base64.b64encode(("admin:" + value).encode()).decode()}

    def valid(value):
        status, body, _ = request(opener, base + "/api/authentication/validate", headers=auth(value))
        return status == 200 and json.loads(body).get("valid") is True

    if not valid(password):
        require(load(STATE / "sonarqube-bootstrap.json", {}).get("fresh") is True,
                "Stored SonarQube credentials do not authenticate. Existing account preserved; reconcile api/.env SONARQUBE_ADMIN_PASSWORD.")
        require(valid("admin"), "Fresh SonarQube administration login failed; no password was reset")
        status, _, _ = request(opener, base + "/api/users/change_password", headers=auth("admin"),
                                data=urlencode({"login": "admin", "previousPassword": "admin", "password": password}).encode())
        require(status in (200, 204) and valid(password), "Fresh SonarQube password initialization failed")
    save(STATE / "sonarqube-bootstrap.json", {"fresh": False})


def report(statuses: dict, note: str) -> None:
    c = validate(load(STATE / "choices.json"))
    api, web, proxy = env_read(ROOT / "api/.env"), env_read(ROOT / "web/.env"), env_read(STATE / "proxy.env")
    credentials = {
        "app": ("developer-admin", web.get("KEYCLOAK_BOOTSTRAP_PASSWORD", "unknown"), "OIDC application login"),
        "keycloak": (web.get("KEYCLOAK_ADMIN_USERNAME", "admin"), web.get("KEYCLOAK_ADMIN_PASSWORD", "unknown"), "Keycloak administration (separate account)"),
        "qdrant": ("N/A", proxy.get("QDRANT_PUBLIC_API_KEY", "unknown"), "api-key header at HTTPS proxy; private peers have internal access"),
        "neo4j": (api.get("NEO4J_USER", "neo4j"), api.get("NEO4J_PASSWORD", "unknown"), "database password"),
        "minio": (api.get("MINIO_ROOT_USER", "unknown"), api.get("MINIO_ROOT_PASSWORD", "unknown"), "MinIO root console login"),
        "s3": (api.get("MINIO_ROOT_USER", "unknown"), api.get("MINIO_ROOT_PASSWORD", "unknown"), "S3 access key / secret key (SigV4)"),
        "uploads": ("N/A", "N/A", "application session JWT verified by finish hook; no standalone login"),
        "outline": ("application OIDC user", "same application login", "OIDC"),
        "drawio": ("N/A", "N/A", "no standalone authentication; loopback only"),
        "sonarqube": ("admin", api.get("SONARQUBE_ADMIN_PASSWORD", "unknown"), "SonarQube own account; verify status before using stored bootstrap value"),
    }
    rows = [note, "Status is from this installation run, not a continuous monitor.", "", "Service | Status | Hostname | Full address | Published / internal ports | Username | Password / API key | Authentication"]
    for name in selected(c):
        user, password, method = credentials[name]
        rows.append(f"{name} | {statuses.get(name, 'PENDING')} | {c['hosts'][name]['hostname']} | {url(c, name)} | {c['hosts'][name]['port']} / {SERVICES[name][1]} | {user} | {password} | {method}")
    rows += [f"neo4j-bolt | {statuses.get('bolt', 'PENDING')} | {c['hosts']['neo4j']['hostname']} | bolt+s://{c['hosts']['neo4j']['hostname']}:{c['bolt_port']} | {c['bolt_port']} / 7687 | {api.get('NEO4J_USER', 'neo4j')} | {api.get('NEO4J_PASSWORD', 'unknown')} | TLS + database password",
             f"qdrant-grpc | {statuses.get('grpc', 'PENDING')} | {c['hosts']['qdrant']['hostname']} | https://{c['hosts']['qdrant']['hostname']}:{c['grpc_port']} | {c['grpc_port']} / 6334 | N/A | {proxy.get('QDRANT_PUBLIC_API_KEY', 'unknown')} | TLS + api-key metadata",
             "python-api | " + statuses.get("api", "PENDING") + " | python-api | http://python-api:9002 | unpublished / 9002 | N/A | N/A | Internal only",
             "Runtime ClickHouse | mode-dependent | telemetry-clickhouse | http://telemetry-clickhouse:8123 | unpublished / 8123 | " + api.get("TELEMETRY_CLICKHOUSE_USER", "telemetry") + " | " + api.get("TELEMETRY_CLICKHOUSE_PASSWORD", "unknown") + " | Internal only; password",
             "Runtime router/tunnel/reloader | mode-dependent | telemetry-clickhouse-router / runtime-ssh-tunnel / telemetry-collector-reloader | private Compose network | unpublished | N/A | N/A | Internal only; existing Runtime policy",
             "Runtime helper | " + statuses.get("runtime", "PENDING") + " | localhost | http://127.0.0.1:47831/health | 47831 / host | N/A | N/A | host approval helper",
             "System monitor | " + statuses.get("sysmon", "PENDING") + " | localhost | http://127.0.0.1:8765/stats | 8765 / host | N/A | N/A | local metrics",
             "Ollama | " + statuses.get("models", "PENDING") + " | host.docker.internal | http://host.docker.internal:11434 | 11434 / host | N/A | N/A | local model endpoint"]
    if "outline" in c["optional"]:
        rows += [f"Outline PostgreSQL | container readiness | outline-postgres | postgres://outline-postgres:5432/outline | unpublished / 5432 | outline | {web['OUTLINE_DATABASE_PASSWORD']} | Internal only; password",
                 "Outline Redis | container readiness | outline-redis | redis://outline-redis:6379 | unpublished / 6379 | N/A | N/A | Internal only; no authentication"]
    if "sonarqube" in c["optional"]:
        rows += [f"Analyzer PostgreSQL | container readiness | analyzer-db | postgres://analyzer-db:5432/sonarqube | unpublished / 5432 | sonar | {api['ANALYZER_POSTGRES_PASSWORD']} | Internal only; password",
                 "Analyzer worker | container readiness | analyzer-worker | https://analyzer-worker:9443 | unpublished / 9443 | N/A | host-managed client certificate | Internal only; mTLS"]
    rows += ["", "Stored bootstrap values may differ from existing database accounts; FAILED/PENDING authentication is never a verified credential.",
             "Headless limitations: no native approval dialogs, browser launching or desktop credential store." if not desktop() else "Desktop integration is available; third-party browser trust may require mkcert -install in that browser's profile.",
             'Recovery: bash install.sh. Credentials: bash install.sh info, or just install-info. If Just was installed locally, first run: export PATH="$PWD/.install/bin:$PATH"',
             "Completed caches/layers/models are reusable; an interrupted individual operation may restart."]
    atomic(STATE / "services.txt", "\n".join(rows) + "\n")


def verify() -> None:
    c = validate(load(STATE / "choices.json"))
    statuses = {}
    failures = []
    opener, cookies = browser()

    def check(name, action):
        for attempt in range(3):
            try:
                action()
                statuses[name] = "READY"
                print(f"{name}: verified")
                return
            except Exception as error:
                if attempt < 2:
                    time.sleep(2 * (attempt + 1))
                    continue
                statuses[name] = "FAILED"
                failures.append(name)
                print(f"{name}: FAILED; inspect service configuration/credentials, then resume (no account reset)")
                if isinstance(error, RuntimeError):
                    print(redact(str(error)))

    def https(name):
        # Native curl checks OS/default trust, not just the explicitly loaded CA.
        address = url(c, name)
        if name == "s3":
            address += "/minio/health/live"
        elif name == "uploads":
            address += "/files/"
        result = subprocess.run(["curl", "--noproxy", "*", "--silent", "--show-error", "--max-time", "30", "-o", "/dev/null", "-w", "%{http_code}", address], capture_output=True, text=True)
        allowed = {"200", "204", "301", "302", "303", "307", "308"}
        if name == "uploads":
            allowed |= {"404", "405"}
        if name == "qdrant":
            allowed = {"401"}
        require(result.returncode == 0 and result.stdout in allowed, "HTTPS trust or route failed")

    for name in selected(c):
        check(name, lambda name=name: https(name))
    web = env_read(ROOT / "web/.env")
    check("oidc-config", lambda: configure_oidc(opener, c, web))
    check("login", lambda: login(opener, cookies, c, web))

    def qdrant():
        key = env_read(STATE / "proxy.env")["QDRANT_PUBLIC_API_KEY"]
        status, body, _ = request(opener, url(c, "qdrant") + "/collections", headers={"api-key": key})
        require(status == 200 and json.loads(body).get("status") == "ok", "Qdrant authentication failed")
    check("qdrant-auth", qdrant)
    check("upload-transfer", lambda: upload_check(opener, c))

    def container(code):
        from cli import compose
        compose("api", "exec", "-T", "python-api", "python", "-c", code, capture=True)

    check("api", lambda: container("import urllib.request; urllib.request.urlopen('http://127.0.0.1:9002/health', timeout=20).read()"))
    check("models", lambda: container("import json, os, urllib.request; u='http://host.docker.internal:11434/api/embed'; r=urllib.request.Request(u, data=json.dumps({'model':os.environ['OLLAMA_EMBED_MODEL'], 'input':'installation readiness'}).encode(),headers={'Content-Type':'application/json'}); d=json.load(urllib.request.urlopen(r,timeout=120)); assert d['embeddings']"))
    check("minio-auth", lambda: container("import os; from minio import Minio; c=Minio('minio:9000',access_key=os.environ['AWS_ACCESS_KEY_ID'],secret_key=os.environ['AWS_SECRET_ACCESS_KEY'],secure=False); assert c.bucket_exists('tus-uploads')"))
    check("bolt", lambda: bolt_check(c))
    check("grpc", lambda: grpc_check(c))
    if "sonarqube" in c["optional"]:
        check("sonarqube-auth", lambda: sonarqube_check(opener, c))
        check("analyzer-mtls", lambda: container("import os,ssl,urllib.request; c=ssl.create_default_context(cafile=os.environ['ANALYZER_WORKER_CA_FILE']); c.load_cert_chain(os.environ['ANALYZER_WORKER_CLIENT_CERT_FILE'],os.environ['ANALYZER_WORKER_CLIENT_KEY_FILE']); r=urllib.request.urlopen('https://analyzer-worker:9443/v1/capabilities',context=c,timeout=30); assert r.status==200"))
    check("runtime", lambda: require(json.loads(request(opener, "http://127.0.0.1:47831/health")[1]).get("api_version") == 28, "Runtime helper version mismatch"))
    check("sysmon", lambda: require(request(opener, "http://127.0.0.1:8765/stats")[0] == 200, "System monitor unavailable"))
    check("logout", lambda: logout(opener, c))
    if "login" in failures or "logout" in failures:
        statuses["app"] = "FAILED application credentials/OIDC"
    if "oidc-config" in failures:
        statuses["keycloak"] = "FAILED administration/client credentials"
    if "bolt" in failures:
        statuses["neo4j"] = "FAILED Bolt authentication/TLS"
    if "qdrant-auth" in failures:
        statuses["qdrant"] = "FAILED credentials"
    if "upload-transfer" in failures:
        statuses["uploads"] = "FAILED upload transfer"
    if "sonarqube-auth" in failures:
        statuses["sonarqube"] = "FAILED credentials"
    if "minio-auth" in failures:
        statuses["minio"] = statuses["s3"] = "FAILED credentials"
    save(STATE / "verification.json", {"statuses": statuses, "failed_checks": failures})
    report(statuses, "Installation verified" if not failures else "Installation incomplete; failed checks: " + ", ".join(failures))
    require(not failures, "Live verification failed. Private details: just install-info. Reconcile stored credentials if accounts changed; resume: bash install.sh")


def bolt_check(c):
    from neo4j import GraphDatabase, TrustCustomCAs
    api = env_read(ROOT / "api/.env")
    address = f"bolt://{c['hosts']['neo4j']['hostname']}:{c['bolt_port']}"
    with GraphDatabase.driver(address, auth=(api["NEO4J_USER"], api["NEO4J_PASSWORD"]), encrypted=True,
                              trusted_certificates=TrustCustomCAs(run("mkcert", "-CAROOT") + "/rootCA.pem"),
                              connection_timeout=20) as driver:
        driver.verify_connectivity()
        with driver.session() as session:
            require(session.run("RETURN 1 AS ready").single()["ready"] == 1, "Public encrypted Bolt authentication failed")


def grpc_check(c):
    # The host client connects to the actual loopback proxy with CA verification.
    import os
    ca = run("mkcert", "-CAROOT") + "/rootCA.pem"
    os.environ["GRPC_DEFAULT_SSL_ROOTS_FILE_PATH"] = ca
    from qdrant_client import QdrantClient
    key = env_read(STATE / "proxy.env")["QDRANT_PUBLIC_API_KEY"]
    host = c["hosts"]["qdrant"]["hostname"]
    with QdrantClient(host=host, port=c["hosts"]["qdrant"]["port"], grpc_port=c["grpc_port"],
                      https=True, api_key=key, prefer_grpc=True, verify=ca, timeout=30) as client:
        client.get_collections()
    import grpc
    denied = False
    try:
        with QdrantClient(host=host, port=c["hosts"]["qdrant"]["port"], grpc_port=c["grpc_port"],
                          https=True, prefer_grpc=True, verify=ca, timeout=30) as anonymous:
            anonymous.get_collections()
    except grpc.RpcError as error:
        denied = error.code() == grpc.StatusCode.UNAUTHENTICATED
    require(denied, "Public gRPC did not reject anonymous access with UNAUTHENTICATED")
