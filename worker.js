// worker.js - roda o motor de leitura direta (Python) dentro do navegador, com o Pyodide.
// Precisa ser um Web Worker: o WORKERFS lê os arquivos do disco aos pedaços (FileReaderSync), sem carregá-los na
// memória, e a tela não trava enquanto o Python trabalha.
importScripts("https://cdn.jsdelivr.net/pyodide/v0.26.4/full/pyodide.js");

const MODULES = ["innodb_carver", "page_health", "mysql_codec", "frm_parser", "dictionary_rebuild", "stream_carver", "lost_tables", "web_driver"];

// Recebe { files: [{name: "ibdata1" | "banco/tabela.frm", data: File}], handle: FileSystemFileHandle | null,
//   dumpDir } e avisa a tela do que acontece: {phase}, {log}, {planned}, {tableStart}, {pages}, {table}, {done} ou {error}.
// O dump vai para o arquivo que o usuário escolheu (handle: Chrome e Edge) ou, sem ele, para um arquivo do disco do
//   navegador (OPFS, na pasta dumpDir) que a tela baixa no fim. Nunca fica na memória: um dump de banco grande tem GBs.
onmessage = async ({ data }) => {
  let writable = null, staged = null;
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
    // streamPages (só nos testes de desenvolvimento) troca o limite de folhas a partir do qual a tabela é lida em lotes.
    pyodide.globals.set("stream_pages", data.streamPages === undefined ? null : data.streamPages);
    pyodide.runPython('import json, web_driver\nrecovery = web_driver.Recovery("/data", log=js_log, stream_pages=stream_pages)');
    const recovery = pyodide.globals.get("recovery");
    recovery.on_pages = (done, total) => postMessage({ pages: { done, total } });

    let write;
    if (data.handle) {
      writable = await data.handle.createWritable();
      write = (bytes) => writable.write(bytes);
    } else {
      if (!navigator.storage || !navigator.storage.getDirectory) {
        throw new Error("Este navegador não deixa escolher onde salvar nem tem disco privado (OPFS) para guardar o dump. Use o Chrome ou o Edge.");
      }
      const folder = await (await navigator.storage.getDirectory()).getDirectoryHandle(data.dumpDir, { create: true });
      const handle = await folder.getFileHandle("dump.sql", { create: true });
      const access = await handle.createSyncAccessHandle();
      let position = 0;
      staged = { handle, access };
      write = (bytes) => { access.write(bytes, { at: position }); position += bytes.byteLength; };
    }
    const put = async (proxy) => {
      const bytes = proxy.toJs();
      proxy.destroy();
      if (bytes.length) await write(bytes);
    };

    postMessage({ phase: "dict" });
    await put(recovery.plan());
    const names = JSON.parse(pyodide.runPython("json.dumps([name for name, _ in recovery.tables])"));
    postMessage({ planned: names });
    for (let index = 0; index < names.length; index++) {
      postMessage({ tableStart: { index, total: names.length, name: names[index] } });
      // A tabela sai em pedaços: cada um vai para o disco antes de o Python montar o próximo.
      recovery.begin_table(index);
      for (let chunk = recovery.next_chunk(); chunk !== undefined; chunk = recovery.next_chunk()) await put(chunk);
      postMessage({ table: JSON.parse(recovery.info(index)) });
    }
    postMessage({ phase: "objects" });
    await put(recovery.finish());
    postMessage({ phase: "close" });
    let blob = null;
    if (writable) await writable.close();
    else { staged.access.flush(); staged.access.close(); blob = await staged.handle.getFile(); }
    // A memória do WebAssembly só cresce: o tamanho no fim é o pico que o Python usou.
    postMessage({ done: JSON.parse(recovery.report()), blob, heapMB: Math.round(pyodide._module.HEAPU8.length / 1048576) });
  } catch (error) {
    if (writable) await writable.abort().catch(() => {});
    if (staged) { try { staged.access.close(); } catch (e) { /* já fechado */ } }
    const quota = error && error.name === "QuotaExceededError";
    postMessage({ error: quota ? "O disco do navegador encheu ao guardar o dump. Libere espaço no disco ou use o Chrome ou o Edge, que gravam direto no arquivo escolhido."
      : String((error && error.message) || error) });
  }
};
