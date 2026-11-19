// harness_load.mjs - Como harness.mjs, mas no FORMATO RÁPIDO (LOAD DATA): roda o motor no mesmo Python do navegador (Pyodide), com ou sem o
// kernel, e grava a pasta <saída>/<banco>/ (estrutura.sql, carregar.sql, ..., dados/*.txt) e <saída>/restaurar.sql.
//
// Uso: node harness_load.mjs <pasta data> <pasta de saída> <kernel|python> <banco>
import { loadPyodide } from "pyodide";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const web = process.env.WEB_ROOT ? path.resolve(process.env.WEB_ROOT) : path.resolve(here, "..", "..");
const [dataDir, outDir, mode, database] = process.argv.slice(2);
if (!dataDir || !outDir || !["kernel", "python"].includes(mode) || !database) {
  console.error("Uso: node harness_load.mjs <pasta data> <pasta de saída> <kernel|python> <banco>");
  process.exit(2);
}
fs.mkdirSync(outDir, { recursive: true });

const MODULES = ["innodb_carver", "page_health", "mysql_codec", "frm_parser", "dictionary_rebuild", "stream_carver", "bigfile",
  "lost_tables", "kernel_bridge", "kernel_paths", "load_format", "web_driver"];

const started = Date.now();
const pyodide = await loadPyodide();
for (const name of MODULES) pyodide.FS.writeFile(`/home/pyodide/${name}.py`, fs.readFileSync(path.join(web, "py", `${name}.py`), "utf8"));
pyodide.FS.mkdir("/data");
pyodide.FS.mount(pyodide.FS.filesystems.NODEFS, { root: path.resolve(dataDir) }, "/data");
pyodide.FS.mkdir("/out");
pyodide.FS.mount(pyodide.FS.filesystems.NODEFS, { root: path.resolve(outDir) }, "/out");
pyodide.globals.set("db_name", database);
if (mode === "kernel") {
  await import(pathToFileURL(path.join(web, "kernel.js")).href);
  const kernel = await globalThis.loadKernel(fs.readFileSync(path.join(web, "kernel.wasm")));
  pyodide.globals.set("js_kernel", kernel);
  pyodide.runPython("import kernel_bridge; kernel_bridge.set_kernel(js_kernel)");
}
console.log(`[${((Date.now() - started) / 1000).toFixed(1)}s] Pyodide pronto (${mode})`);

const result = pyodide.runPython(`
import json, os, re, time, web_driver, load_format
safe = lambda t: re.sub(r"[^A-Za-z0-9_.-]", "_", t)
t0 = time.time()
rec = web_driver.Recovery("/data", log=lambda m: None, database=db_name, mode="load")
rec.plan()
os.makedirs("/out/%s/dados" % safe(db_name), exist_ok=True)
for index, (name, _d) in enumerate(rec.tables):
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
            rel = "%s/dados/%04d_%s.%03d.txt" % (safe(db_name), index, safe(name), len(parts))
            parts.append(rel)
            out, written = open("/out/" + rel, "wb"), 0
        out.write(chunk)
        written += len(chunk)
    if out:
        out.close()
    rec.table_load_sql(index, json.dumps(parts))
rec.finish()
for fname, text in json.loads(rec.load_files()).items():
    open("/out/%s/%s" % (safe(db_name), fname), "w", encoding="utf8", newline="\\n").write(text)
with open("/out/restaurar.sql", "w", encoding="utf8", newline="\\n") as f:
    for part in ("estrutura", "carregar", "indices", "objetos", "conferir"):
        f.write("SOURCE %s/%s.sql;\\n" % (safe(db_name), part))
report = json.loads(rec.report())
json.dumps({"s": round(time.time() - t0, 1), "rows": report["registros"]})
`);
const data = JSON.parse(result);
console.log(`[${((Date.now() - started) / 1000).toFixed(1)}s] terminou: ${data.s} s, ${data.rows} linhas`);
