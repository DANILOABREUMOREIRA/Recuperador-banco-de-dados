// harness.mjs - Roda a recuperação no MESMO Python do navegador (Pyodide, WebAssembly), só que no Node, com ou sem o kernel,
// e grava o .sql. Serve para medir o tempo de verdade e conferir que o texto das linhas sai igual.
//
// Uso: node harness.mjs <pasta data> <saida.sql> <kernel|python> [banco]
import { loadPyodide } from "pyodide";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const web = process.env.WEB_ROOT ? path.resolve(process.env.WEB_ROOT) : path.resolve(here, "..", "..");
const [dataDir, outSql, mode, database] = process.argv.slice(2);
if (!dataDir || !outSql || !["kernel", "python"].includes(mode)) {
  console.error("Uso: node harness.mjs <pasta data> <saida.sql> <kernel|python>");
  process.exit(2);
}

const MODULES = ["innodb_carver", "page_health", "mysql_codec", "frm_parser", "dictionary_rebuild", "stream_carver", "bigfile",
  "lost_tables", "kernel_bridge", "kernel_paths", "load_format", "web_driver"];

const started = Date.now();
const pyodide = await loadPyodide();
for (const name of MODULES.filter((n) => fs.existsSync(path.join(web, "py", `${n}.py`)))) pyodide.FS.writeFile(`/home/pyodide/${name}.py`, fs.readFileSync(path.join(web, "py", `${name}.py`), "utf8"));
pyodide.FS.mkdir("/data");
pyodide.FS.mount(pyodide.FS.filesystems.NODEFS, { root: path.resolve(dataDir) }, "/data");
const outDir = path.dirname(path.resolve(outSql));
pyodide.FS.mkdir("/out");
pyodide.FS.mount(pyodide.FS.filesystems.NODEFS, { root: outDir }, "/out");
pyodide.globals.set("out_name", path.basename(outSql));
pyodide.globals.set("db_name", database || null);

if (mode === "kernel") {
  await import(pathToFileURL(path.join(web, "kernel.js")).href);
  const kernel = await globalThis.loadKernel(fs.readFileSync(path.join(web, "kernel.wasm")));
  pyodide.globals.set("js_kernel", kernel);
  pyodide.runPython("import kernel_bridge; kernel_bridge.set_kernel(js_kernel)");
}
console.log(`[${((Date.now() - started) / 1000).toFixed(1)}s] Pyodide pronto (${mode})`);

// PROFILE=1: roda sob cProfile e imprime as 30 funções que mais gastam tempo próprio (tottime).
const profile = process.env.PROFILE ? "True" : "False";
// PRE=arquivo.py: Python extra executado antes da recuperação (contadores, ajustes de teste).
if (process.env.PRE) pyodide.runPython(fs.readFileSync(process.env.PRE, "utf8"));
const result = pyodide.runPython(`
import json, time, web_driver, cProfile, pstats, io
PROFILE = ${profile}
prof = cProfile.Profile() if PROFILE else None
if prof: prof.enable()
t0 = time.time()
rec = web_driver.Recovery("/data", log=lambda m: None, database=db_name)
f = open("/out/" + out_name, "wb")
f.write(rec.plan())
t1 = time.time()
times = {}
for i in range(len(rec.tables)):
    ts = time.time()
    rec.begin_table(i)
    while True:
        c = rec.next_chunk()
        if c is None:
            break
        f.write(c)
    times[rec.tables[i][0]] = round(time.time() - ts, 2)
f.write(rec.finish())
if PROFILE or True:
    import builtins
    getattr(builtins, "_dump", lambda: None)()
f.close()
report = json.loads(rec.report())
open("/out/" + out_name + ".report.json", "w", encoding="utf8").write(json.dumps(report, ensure_ascii=False))
if prof:
    prof.disable()
    buf = io.StringIO()
    pstats.Stats(prof, stream=buf).sort_stats("tottime").print_stats(30)
    open("/out/" + out_name + ".prof.txt", "w", encoding="utf8").write(buf.getvalue())
json.dumps({"plan_s": round(t1 - t0, 1), "tables_s": round(time.time() - t1, 1), "rows": report["registros"], "tables": times})
`);
const data = JSON.parse(result);
console.log(`[${((Date.now() - started) / 1000).toFixed(1)}s] terminou: dicionário ${data.plan_s} s, tabelas ${data.tables_s} s, ${data.rows} linhas`);
const top = Object.entries(data.tables).sort((a, b) => b[1] - a[1]).slice(0, 5).map(([n, t]) => `${n} ${t}s`).join(", ");
console.log("mais lentas: " + top);
