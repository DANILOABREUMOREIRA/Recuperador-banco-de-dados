import re, sys, os, time, subprocess
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import run
out = sys.argv[1]; port = int(sys.argv[2])
src = open(os.path.join(out, "estrutura.sql"), encoding="utf8").read().split("\n")
res, adds, i, table = [], [], 0, None
while i < len(src):
    line = src[i]
    m = re.match(r"CREATE TABLE `([^`]+)`", line)
    if not m:
        res.append(line); i += 1; continue
    table = m.group(1); block = [line]; i += 1
    body = []
    while not src[i].startswith(")"):
        body.append(src[i]); i += 1
    keep, keys = [], []
    for b in body:
        s = b.rstrip().rstrip(",")
        if re.match(r"\s*(UNIQUE )?KEY ", s) or re.match(r"\s*(FULLTEXT )?KEY ", s):
            keys.append(s.strip())
        else:
            keep.append(s)
    block += [k + ("," if n < len(keep) - 1 else "") for n, k in enumerate(keep)]
    block.append(src[i]); i += 1
    res += block
    if keys:
        adds.append(f"ALTER TABLE `{table}` " + ", ".join("ADD " + k for k in keys) + ";")
open(os.path.join(out, "estrutura2.sql"), "w", encoding="utf8").write("\n".join(res))
open(os.path.join(out, "indices.sql"), "w", encoding="utf8").write("SET FOREIGN_KEY_CHECKS=0;\nSET UNIQUE_CHECKS=0;\nUSE `%s`;\n" % sys.argv[3] + "\n".join(adds) + "\n")
if os.environ.get("PREP_ONLY"): sys.exit(0)
a = run.imp(port, "estrutura2.sql", out); b = run.imp(port, "carregar.sql", out, ["--local-infile=1"]); c = run.imp(port, "indices.sql", out)
print("  total LOAD + indices no fim: %.1f" % (a + b + c))
