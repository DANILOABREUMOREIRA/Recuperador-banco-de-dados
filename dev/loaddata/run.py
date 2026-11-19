import subprocess, sys, time, os
M = r"C:\Users\danil\Desktop\IAS\recuperacao de banco\programa\sistema\mysql\bin\mysql.exe"
def imp(port, sqlfile, cwd, extra=()):
    cmd = [M, "-h127.0.0.1", f"-P{port}", "-uroot", "--default-character-set=utf8", "--max_allowed_packet=64M", *extra]
    t = time.time()
    with open(os.path.join(cwd, sqlfile), "rb") as f:
        r = subprocess.run(cmd, stdin=f, cwd=cwd, capture_output=True, text=True)
    dt = time.time() - t
    print(f"{sqlfile:14s} porta {port}: {dt:7.1f} s  rc={r.returncode} {r.stderr.strip()[:300]}", flush=True)
    return dt
if __name__ == "__main__":
    mode, out = sys.argv[1], sys.argv[2]
    if mode == "A": imp(3391, "dump.sql", out)
    if mode == "B":
        open(os.path.join(out, "dump_b.sql"), "wb").write(b"SET autocommit=0;\n" + open(os.path.join(out, "dump.sql"), "rb").read() + b"\nCOMMIT;\n")
        imp(3392, "dump_b.sql", out)
    if mode == "C":
        a = imp(3393, "estrutura.sql", out); b = imp(3393, "carregar.sql", out, ["--local-infile=1"]); print(f"  total C: {a+b:.1f} s")
