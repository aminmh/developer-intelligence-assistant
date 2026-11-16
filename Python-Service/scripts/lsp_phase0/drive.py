"""Phase 0 spike: drive phpactor over a revision with documentSymbol + references.

Usage: python3 drive.py <src_dir> <out.json> [max_defs]
"""
import json, os, subprocess, sys, threading, time
from pathlib import Path

PA = "/Volumes/Projects/Projects/Developer Assistant/api/Python-Service/php/lsp/vendor/bin/phpactor"
SRC = Path(sys.argv[1]).resolve()
OUT = sys.argv[2]
MAX_DEFS = int(sys.argv[3]) if len(sys.argv) > 3 else 100000

proc = subprocess.Popen(["php", PA, "language-server"], cwd=SRC, stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
pending, lock, nid = {}, threading.Lock(), [0]


def send(msg):
    body = json.dumps(msg).encode()
    proc.stdin.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
    proc.stdin.flush()


def reader():
    f = proc.stdout
    while True:
        headers = {}
        while True:
            line = f.readline()
            if not line:
                return
            line = line.strip()
            if not line:
                break
            k, _, v = line.decode().partition(":")
            headers[k.lower()] = v.strip()
        msg = json.loads(f.read(int(headers["content-length"])))
        if "id" in msg and "method" in msg:  # server -> client request
            result = [None] * len(msg.get("params", {}).get("items", [])) if msg["method"] == "workspace/configuration" else None
            send({"jsonrpc": "2.0", "id": msg["id"], "result": result})
        elif "id" in msg:
            with lock:
                ev = pending.get(msg["id"])
            if ev:
                ev[1] = msg
                ev[0].set()


threading.Thread(target=reader, daemon=True).start()


def request(method, params, timeout=60):
    with lock:
        nid[0] += 1
        i = nid[0]
        ev = [threading.Event(), None]
        pending[i] = ev
    send({"jsonrpc": "2.0", "id": i, "method": method, "params": params})
    if not ev[0].wait(timeout):
        return {"error": "timeout"}
    return ev[1]


def notify(method, params):
    send({"jsonrpc": "2.0", "method": method, "params": params})


uri = lambda p: Path(p).as_uri()
t0 = time.time()
init = request("initialize", {
    "processId": os.getpid(), "rootUri": uri(SRC), "capabilities": {},
    "initializationOptions": {
        "composer.enable": False,             # never load the project's vendor/autoload.php
        "indexer.enabled_watchers": [],
        "language_server_phpstan.enabled": False,
        "language_server_psalm.enabled": False,
        "language_server_php_cs_fixer.enabled": False,
        "php_code_sniffer.enabled": False,
        "language_server.diagnostics_on_update": False,
        "language_server.diagnostics_on_save": False,
        "language_server.diagnostics_on_open": False,
    },
}, timeout=120)
notify("initialized", {})
caps = (init.get("result") or {}).get("capabilities", {})
t_init = time.time() - t0

files = sorted(p for p in SRC.rglob("*.php"))
defs = []  # {name, kind, file, line, char}
t1 = time.time()


def walk(items, file, parent=""):
    for s in items or []:
        name = s["name"]
        full = f"{parent}::{name}" if parent else name
        sel = s.get("selectionRange") or s.get("location", {}).get("range")
        defs.append({"name": name, "container": parent, "kind": s["kind"], "file": str(file.relative_to(SRC)),
                     "line": sel["start"]["line"] + 1, "char": sel["start"]["character"]})
        walk(s.get("children"), file, name if s["kind"] in (5, 10, 11, 23) else parent)


for f in files:
    text = f.read_text(errors="replace")
    notify("textDocument/didOpen", {"textDocument": {"uri": uri(f), "languageId": "php", "version": 1, "text": text}})
    r = request("textDocument/documentSymbol", {"textDocument": {"uri": uri(f)}})
    walk(r.get("result"), f)
    notify("textDocument/didClose", {"textDocument": {"uri": uri(f)}})
t_syms = time.time() - t1

# references for classes(5), interfaces(11), enums(10), methods(6), functions(12), constructors(9), enum members(22), constants(14)
targets = [d for d in defs if d["kind"] in (5, 6, 9, 10, 11, 12, 14, 22)][:MAX_DEFS]
refs, timeouts, errors = [], 0, 0
t2 = time.time()
per = []
for d in targets:
    f = SRC / d["file"]
    notify("textDocument/didOpen", {"textDocument": {"uri": uri(f), "languageId": "php", "version": 1, "text": f.read_text(errors="replace")}})
    s = time.time()
    r = request("textDocument/references", {"textDocument": {"uri": uri(f)},
                "position": {"line": d["line"] - 1, "character": d["char"]},
                "context": {"includeDeclaration": False}}, timeout=30)
    per.append(time.time() - s); d["ms"] = round((time.time() - s) * 1000)
    notify("textDocument/didClose", {"textDocument": {"uri": uri(f)}})
    if r.get("error") == "timeout":
        timeouts += 1
        continue
    if "error" in r:
        errors += 1
        continue
    for loc in r.get("result") or []:
        p = loc["uri"].replace("file://", "")
        try:
            rel = str(Path(p).resolve().relative_to(SRC))
        except ValueError:
            rel = "OUTSIDE:" + p
        refs.append({"target": d, "file": rel, "line": loc["range"]["start"]["line"] + 1})
t_refs = time.time() - t2

request("shutdown", None, timeout=10)
notify("exit", None)
per.sort()
json.dump({"timed": sorted(targets, key=lambda d: -d.get("ms", 0))[:15], "capabilities": sorted(k for k, v in caps.items() if v), "files": len(files), "defs": defs, "refs": refs,
           "stats": {"init_s": round(t_init, 2), "symbols_s": round(t_syms, 2), "refs_s": round(t_refs, 2),
                     "targets": len(targets), "timeouts": timeouts, "errors": errors,
                     "p50_ms": round(per[len(per) // 2] * 1000) if per else None,
                     "p95_ms": round(per[int(len(per) * .95)] * 1000) if per else None,
                     "max_ms": round(per[-1] * 1000) if per else None}}, open(OUT, "w"), indent=1)
print(json.dumps(json.load(open(OUT))["stats"]), len(defs), "defs", len(refs), "refs")
