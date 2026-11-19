"""loadflow.py - Faz, em Python comum e gravando numa pasta, o mesmo que o worker.js faz no formato rápido (LOAD DATA).
Uso: python loadflow.py <pasta data> <banco> <pasta de saída> [sql]
Grava <saída>/<banco>/{estrutura,carregar,indices,objetos,conferir}.sql e <saída>/<banco>/dados/*.txt, mais <saída>/restaurar.sql.
Com o 4º argumento "sql" grava também <saída>/dump.sql (o formato comum), para comparar os dois."""
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "py"))
import load_format
import web_driver


def safe(text):
    return re.sub(r"[^A-Za-z0-9_.-]", "_", text)


def main(data, db, outdir, also_sql=False):
    folder = os.path.join(outdir, safe(db))
    os.makedirs(os.path.join(folder, "dados"), exist_ok=True)
    t0 = time.time()
    rec = web_driver.Recovery(data, log=lambda m: None, database=db, mode="load")
    rec.plan()
    for index, (name, _dict_only) in enumerate(rec.tables):
        parts, out, written = [], None, 0
        rec.begin_table(index)
        while True:
            chunk = rec.next_chunk()
            if chunk is None:
                break
            if not chunk:
                continue
            if out is None or written >= load_format.PART_BYTES:
                if out:
                    out.close()
                rel = f"{safe(db)}/dados/{index:04d}_{safe(name)}.{len(parts):03d}.txt"
                parts.append(rel)
                out, written = open(os.path.join(outdir, rel), "wb"), 0
            out.write(chunk)
            written += len(chunk)
        if out:
            out.close()
        rec.table_load_sql(index, json.dumps(parts))
    rec.finish()
    for name, text in json.loads(rec.load_files()).items():
        open(os.path.join(folder, name), "w", encoding="utf8", newline="\n").write(text)
    with open(os.path.join(outdir, "restaurar.sql"), "w", encoding="utf8", newline="\n") as f:
        for part in ("estrutura", "carregar", "indices", "objetos", "conferir"):
            f.write(f"SOURCE {safe(db)}/{part}.sql;\n")
    print(f"formato rápido: {time.time() - t0:.0f}s")
    if also_sql:
        t0 = time.time()
        rec = web_driver.Recovery(data, log=lambda m: None, database=db)
        with open(os.path.join(outdir, "dump.sql"), "wb") as f:
            f.write(rec.plan())
            for index in range(len(rec.tables)):
                rec.begin_table(index)
                for chunk in iter(rec.next_chunk, None):
                    f.write(chunk)
            f.write(rec.finish())
        print(f"formato comum: {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], sys.argv[3], len(sys.argv) > 4 and sys.argv[4] == "sql")
