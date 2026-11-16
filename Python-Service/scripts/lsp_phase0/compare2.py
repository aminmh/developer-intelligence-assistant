import json, random, sys
from collections import Counter, defaultdict
from pathlib import Path
SRC = Path("src").resolve()
P = json.load(open("inspect.json")); L = json.load(open("lsp.json"))
short = lambda f: f.rsplit("\\",1)[-1]
rel = lambda p: str(Path(p).resolve().relative_to(SRC))
# member names defined in the project (parser defs with owner + LSP defs)
pmembers = defaultdict(set)
for d in P["definitions"]:
    if d.get("owner"): pmembers[d["name"]].add(short(d["owner"]))
for d in L["defs"]:
    if d["kind"] in (6,9,14,22) and d["container"]: pmembers["__construct" if d["kind"]==9 else d["name"]].add(d["container"])
A = {}   # (file,line,member) -> parser class (receiver/declared) ; only resolved, member defined in project
Aun = {} # unresolved parser refs to project-defined member names
for r in P["references"]:
    m = r.get("to_member")
    if not m or m not in pmembers: continue
    k = (rel(r["file"]), r["line"], m)
    if r.get("unresolved") or not r.get("to_fqn"): Aun[k] = r
    else: A.setdefault(k, set()).add(short(r["to_fqn"]))
B = {}
for r in L["refs"]:
    t = r["target"]
    if t["kind"] in (6,9,14,22) and t["container"]:
        m = "__construct" if t["kind"]==9 else t["name"]
        B.setdefault((r["file"], r["line"], m), set()).add(t["container"])
both = A.keys() & B.keys()
agree = sum(1 for k in both if A[k] & B[k])
print("parser resolved member refs to project names:", len(A), " unresolved:", len(Aun))
print("lsp member refs:", len(B))
print("both:", len(both), " class agrees:", agree, " class differs:", len(both)-agree)
print("parser only:", len(A.keys()-B.keys()), " lsp only:", len(B.keys()-A.keys()))
print("lsp-only that parser had as UNRESOLVED:", len((B.keys()-A.keys()) & Aun.keys()))
print("parser-unresolved neither found:", len(Aun.keys()-B.keys()))
lines={}
def text(f,n):
    if f not in lines: lines[f]=(SRC/f).read_text(errors="replace").splitlines()
    return lines[f][n-1].strip()[:170]
random.seed(11)
po = sorted(A.keys()-B.keys()); lo = sorted(B.keys()-A.keys()-Aun.keys()); lu = sorted((B.keys()-A.keys()) & Aun.keys())
diff = sorted(k for k in both if not (A[k]&B[k]))
print("parser-only by member:", Counter(k[2] for k in po).most_common(12))
print("lsp-only(not in parser at all) by member:", Counter(k[2] for k in lo).most_common(12))
out = {}
for name, s, n in (("parser_only", po, 15), ("lsp_only_new", lo, 15), ("lsp_resolves_unresolved", lu, 10), ("class_differs", diff, 10), ("both", sorted(both), 10)):
    out[name] = [dict(file=k[0], line=k[1], member=k[2], parser=sorted(A.get(k,[])), lsp=sorted(B.get(k,[])), code=text(k[0],k[1])) for k in random.sample(s, min(n,len(s)))]
json.dump(out, open("samples.json","w"), indent=1)
