// worker.js - roda o motor de leitura direta (Python) dentro do navegador, com o Pyodide.
// Precisa ser um Web Worker: o WORKERFS lê os arquivos do disco aos pedaços (FileReaderSync), sem carregá-los na
// memória, e a tela não trava enquanto o Python trabalha.
importScripts("https://cdn.jsdelivr.net/pyodide/v0.26.4/full/pyodide.js");

const MODULES = ["innodb_carver", "page_health", "mysql_codec", "frm_parser", "dictionary_rebuild", "web_driver"];

// Recebe { files: [{name: "ibdata1" | "banco/tabela.frm", data: File}], handle: FileSystemFileHandle | null } e avisa a
// tela do que acontece: {phase}, {log}, {planned}, {tableStart}, {pages}, {table}, {done} ou {error}.
onmessage = async ({ data }) => {
  let writable = null;
  try {
    postMessage({ phase: "prep" });
    const pyodide = await loadPyodide();
    for (const name of MODULES) {
      const response = await fetch(`py/${name}.py`);
      if (!response.ok) throw new Error(`Não consegui baixar py/${name}.py (${response.status}).`);
      pyodide.FS.writeFile(`/home/pyodide/${name}.py`, await response.text());
    }
    pyodide.FS.mkdir("/data");
    pyodide.FS.mount(pyodide.FS.filesystems.WORKERFS, { blobs: data.files }, "/data");
    pyodide.globals.set("js_log", (message) => postMessage({ log: message }));
    pyodide.runPython('import json, web_driver\nrecovery = web_driver.Recovery("/data", log=js_log)');
    const recovery = pyodide.globals.get("recovery");
    recovery.on_pages = (done, total) => postMessage({ pages: { done, total } });

    // Com o arquivo de destino escolhido (Chrome/Edge) cada pedaço vai direto para o disco. Sem ele (Firefox/Safari)
    // os pedaços ficam guardados pelo navegador e viram um download no fim.
    const parts = [];
    if (data.handle) writable = await data.handle.createWritable();
    const write = async (proxy) => {
      const bytes = proxy.toJs();
      proxy.destroy();
      if (!bytes.length) return;
      if (writable) await writable.write(bytes);
      else parts.push(new Blob([bytes]));
    };

    postMessage({ phase: "dict" });
    await write(recovery.plan());
    const names = JSON.parse(pyodide.runPython("json.dumps([name for name, _ in recovery.tables])"));
    postMessage({ planned: names });
    for (let index = 0; index < names.length; index++) {
      postMessage({ tableStart: { index, total: names.length, name: names[index] } });
      await write(recovery.table(index));
      postMessage({ table: JSON.parse(recovery.info(index)) });
    }
    postMessage({ phase: "objects" });
    await write(recovery.finish());
    postMessage({ phase: "close" });
    if (writable) await writable.close();
    postMessage({ done: JSON.parse(recovery.report()), blob: writable ? null : new Blob(parts, { type: "application/sql" }) });
  } catch (error) {
    if (writable) await writable.abort().catch(() => {});
    postMessage({ error: String((error && error.message) || error) });
  }
};
