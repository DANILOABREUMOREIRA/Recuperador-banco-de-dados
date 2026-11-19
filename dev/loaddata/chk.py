import subprocess, sys, json
M = r"C:\Users\danil\Desktop\IAS\recuperacao de banco\programa\sistema\mysql\bin\mysql.exe"
def q(port, sql, db=None):
    c = [M, "-h127.0.0.1", f"-P{port}", "-uroot", "-N", "-B", "--default-character-set=utf8"] + ([f"--database={db}"] if db else []) + ["-e", sql]
    r = subprocess.run(c, capture_output=True, text=True, encoding="utf8", errors="replace"); 
    if r.returncode: raise SystemExit(r.stderr)
    return r.stdout
db, ports, out = sys.argv[1], sys.argv[2].split(","), sys.argv[3]
tables = [l for l in q(ports[0], "show tables", db).split("\n") if l]
res = {p: {} for p in ports}
for p in ports:
    for i in range(0, len(tables), 40):
        sql = "; ".join(f"checksum table `{t}`" for t in tables[i:i+40])
        lines = [l.split("\t") for l in q(p, sql, db).split("\n") if l]
        for name, cs in lines: res[p][name.split(".",1)[1]] = cs
cnt = json.load(open(out + "/contagens.json"))
bad = [t for t in tables if len({res[p].get(t) for p in ports}) != 1]
print(f"{len(tables)} tabelas, checksums diferentes entre {ports}: {len(bad)}", bad[:10])
for p in ports[1:2]:
    pass
# contagem de linhas das tabelas com dados no último
last = ports[-1]
mism = []
for t, n in cnt.items():
    got = int(q(last, f"select count(*) from `{t}`", db).strip() or 0)
    if got != n: mism.append((t, n, got))
print("contagens divergentes da esperada:", len(mism), mism[:10])
