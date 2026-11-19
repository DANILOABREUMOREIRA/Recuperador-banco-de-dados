// worker.js - roda o motor de leitura direta (Python) dentro do navegador, com o Pyodide.
// Precisa ser um Web Worker: o WORKERFS lê os arquivos do disco aos pedaços (FileReaderSync), sem carregá-los na
// memória, e a tela não trava enquanto o Python trabalha.
importScripts("https://cdn.jsdelivr.net/pyodide/v0.26.4/full/pyodide.js");
importScripts("kernel.js");   // define loadKernel: o laço quente (ler registros e escrever o SQL) compilado para WebAssembly

const MODULES = ["innodb_carver", "page_health", "mysql_codec", "frm_parser", "dictionary_rebuild", "stream_carver", "bigfile", "lost_tables",
  "kernel_bridge", "kernel_paths", "load_format", "web_driver"];

// DEBUG-PROVISORIO: registro técnico para a aba Depuração (a tela mostra tudo o que chega como {dbg}).
const dbg = (text) => postMessage({ dbg: text });
const mbytes = (n) => (n / 1048576).toFixed(1).replace(".", ",") + " MB";

// Recebe { files: [{name: "ibdata1" | "banco/tabela.frm", data: File}], handle: FileSystemFileHandle | null,
//   dumpDir, mode } e avisa a tela do que acontece: {phase}, {log}, {planned}, {tableStart}, {pages}, {table}, {done} ou {error}.
// O dump vai para o arquivo que o usuário escolheu (handle: Chrome e Edge) ou, sem ele, para um arquivo do disco do
//   navegador (OPFS, na pasta dumpDir) que a tela baixa no fim. Nunca fica na memória: um dump de banco grande tem GBs.
// O resultado é sempre um .zip com um .sql por banco da pasta (montado em fluxo, durante a recuperação) e um LEIA-ME.
onmessage = async ({ data }) => {
  try {
    postMessage({ phase: "prep" });
    dbg(`worker iniciado: ${data.files.length} arquivos, modo=${data.mode}, kernel ${data.noKernel ? "desligado (?semkernel=1)" : "pedido"}, ` +
        `destino=${data.handle ? "arquivo escolhido (" + data.handle.name + ")" : "disco do navegador (OPFS, pasta " + data.dumpDir + ")"}`);
    const pyStart = Date.now();
    const pyodide = await loadPyodide();
    dbg(`Pyodide ${pyodide.version} carregado em ${Date.now() - pyStart} ms`);
    const moduleSizes = [];
    for (const name of MODULES) {
      const response = await fetch(`py/${name}.py`);
      if (!response.ok) throw new Error(`Não consegui baixar py/${name}.py (${response.status}).`);
      const source = await response.text();
      pyodide.FS.writeFile(`/home/pyodide/${name}.py`, source);
      moduleSizes.push(`${name}=${source.length}`);
    }
    dbg(`módulos Python (caracteres): ${moduleSizes.join(" ")}`);
    pyodide.FS.mkdir("/data");
    pyodide.FS.mount(pyodide.FS.filesystems.WORKERFS, { blobs: data.files }, "/data");
    dbg(`WORKERFS: /data montado com ${data.files.length} arquivos, ${mbytes(data.files.reduce((sum, f) => sum + f.data.size, 0))}`);
    pyodide.globals.set("js_log", (message) => postMessage({ log: message }));
    // streamPages (só nos testes de desenvolvimento) troca o limite de folhas a partir do qual a tabela é lida em lotes.
    pyodide.globals.set("stream_pages", data.streamPages === undefined ? null : data.streamPages);
    pyodide.runPython("import json, web_driver, kernel_bridge");
    // Andamento da varredura do ibdata1 (a barra fina da etapa do dicionário): o Python avisa a cada 32 MB lidos.
    pyodide.globals.set("js_scan", (done, total) => postMessage({ scan: { done: done * 16384, total: total * 16384 } }));
    pyodide.runPython("import innodb_carver\ninnodb_carver.SCAN_PROGRESS = js_scan");
    // O kernel WebAssembly acelera a leitura das tabelas. Se não carregar (ou se a tela pediu ?semkernel=1), o Python faz tudo
    //   sozinho, do jeito antigo e mais lento, com o mesmo resultado.
    if (data.noKernel) {
      postMessage({ log: "Kernel WebAssembly desligado (?semkernel=1): leitura só em Python." });
    } else {
      try {
        const response = await fetch("kernel.wasm");
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const wasm = await response.arrayBuffer();
        dbg(`kernel.wasm baixado: ${wasm.byteLength} bytes`);
        pyodide.globals.set("js_kernel", await loadKernel(wasm));
        pyodide.runPython("kernel_bridge.set_kernel(js_kernel)");
        postMessage({ log: "Kernel WebAssembly ligado: as tabelas são lidas pelo código compilado." });
      } catch (error) {
        postMessage({ log: `Kernel WebAssembly indisponível (${error.message}): leitura só em Python, mais lenta.` });
      }
    }
    const databases = JSON.parse(pyodide.runPython('json.dumps(list(web_driver.list_databases("/data")))'));
    if (!databases.length) throw new Error("Nenhum banco de usuário com arquivos .frm foi encontrado na pasta escolhida.");
    dbg(`bancos com .frm (banco: quantidade): ${pyodide.runPython('json.dumps(web_driver.list_databases("/data"))')}`);
    dbg("saída: .zip, um .sql por banco");
    const result = await recoverMany(pyodide, data, databases, data.mode === "load");
    postMessage({ done: result.report, blob: result.blob, heapMB: Math.round(pyodide._module.HEAPU8.length / 1048576) });
  } catch (error) {
    const quota = error && error.name === "QuotaExceededError";
    const text = String((error && error.message) || error);
    dbg(`ERRO no worker (${(error && error.name) || typeof error}): ${(error && error.stack) || text}`);
    // Erro do Python: o traceback inteiro vai para o log; a tela mostra só a última linha (a mensagem).
    const lines = text.trim().split("\n");
    if (lines[0].startsWith("Traceback")) postMessage({ log: text });
    const message = lines[0].startsWith("Traceback") ? lines[lines.length - 1].replace(/^(RuntimeError|CarverError|[\w.]+\.CarverError): /, "") : text;
    postMessage({ error: quota ? "O disco do navegador encheu ao guardar o dump. Libere espaço no disco ou use o Chrome ou o Edge, que gravam direto no arquivo escolhido."
      : message });
  }
};

// Um arquivo no disco privado do navegador (OPFS), gravado em sequência.
async function opfsFile(dir, name) {
  if (!navigator.storage || !navigator.storage.getDirectory) {
    throw new Error("Este navegador não deixa escolher onde salvar nem tem disco privado (OPFS) para guardar o dump. Use o Chrome ou o Edge.");
  }
  const folder = await (await navigator.storage.getDirectory()).getDirectoryHandle(dir, { create: true });
  const handle = await folder.getFileHandle(name, { create: true });
  const access = await handle.createSyncAccessHandle();
  let position = 0;
  return { handle, access, write(bytes) { access.write(bytes, { at: position }); position += bytes.byteLength; } };
}

// VÁRIOS bancos na pasta: recupera todos, um .sql por banco, direto para dentro de um .zip.
async function recoverMany(pyodide, data, databases, load) {
  const zip = await import("./vendor/zip/zip.min.js");
  zip.configure({ useWebWorkers: false });   // este já é um worker
  const started = Date.now();
  // O destino passa por `sink`, que a gente controla: num erro ele é abortado direto (o ZipWriter trava o stream e não deixa abortá-lo
  //   por fora), e o arquivo escolhido não fica aberto e incompleto até a aba fechar.
  let file = null, sink;
  if (data.handle) {
    const out = (await data.handle.createWritable()).getWriter();
    sink = { write: (chunk) => out.write(chunk), close: () => out.close(), abort: () => out.abort() };
  } else {
    file = await opfsFile(data.dumpDir, "dump.zip");
    sink = {
      write(chunk) { file.write(chunk); },
      close() { file.access.flush(); file.access.close(); },
      abort() { file.access.close(); },
    };
  }
  try {
    return await zipAll(pyodide, data, databases, load, zip, sink, file, started);
  } catch (error) {
    try { await sink.abort(); } catch (e) { /* já fechado */ }
    throw error;
  }
}

async function zipAll(pyodide, data, databases, load, zip, sink, file, started) {
  const writer = new zip.ZipWriter(new WritableStream(sink), { zip64: true });   // o tamanho de cada .sql só se sabe no fim

  postMessage({ phase: "dict" });
  postMessage({ log: load ? `Formato rápido: o .zip leva as linhas em arquivos de texto, que o MySQL carrega com LOAD DATA.`
    : `${databases.length} banco(s) na pasta: ${databases.join(", ")}. Saem num .zip, um .sql por banco.` });
  pyodide.globals.set("load_mode", !!load);
  pyodide.runPython("carver, found, databases = web_driver.prepare_all('/data', js_log)\nrecoveries = {}");
  postMessage({ log: `Dicionário e tabelas perdidas: ${((Date.now() - started) / 1000).toFixed(1)} s.` });
  const all = JSON.parse(pyodide.runPython("json.dumps(databases)"));   // inclui _sem_banco, se o dicionário achou tabelas sem dono
  const plans = [];
  for (const db of all) {
    pyodide.globals.set("db_name", db);
    pyodide.runPython('recoveries[db_name] = web_driver.recovery_for("/data", db_name, carver, found, js_log, stream_pages, \'load\' if load_mode else \'sql\')');
    const recovery = pyodide.runPython("recoveries[db_name]");
    recovery.on_pages = (done, total) => postMessage({ pages: { done, total } });
    recovery.debug = dbg;   // DEBUG-PROVISORIO
    const header = recovery.plan();   // cada banco lista as tabelas dele; o cabeçalho do .sql espera a vez do banco
    const tables = JSON.parse(pyodide.runPython("json.dumps([name for name, _ in recoveries[db_name].tables])"));
    plans.push({ db, recovery, header, tables });
  }
  const labels = plans.flatMap(p => p.tables.map(t => `${p.db}.${t}`));
  postMessage({ planned: labels, weights: plans.flatMap(p => p.tables.map((_, i) => p.recovery.leaf_count(i))) });

  const reports = {};
  let counter = 0;
  for (const plan of plans) {
    const { db, recovery } = plan;
    if (load) {
      counter = await writeFast(zip, writer, plan, labels, counter);
      reports[db] = JSON.parse(recovery.report());
      postMessage({ log: `${db}: ${reports[db].registros.toLocaleString("pt-BR")} registros em ${Object.keys(reports[db].tabelas).length} tabelas.` });
      continue;
    }
    const stream = new TransformStream();
    const adding = writer.add(`${db}.sql`, stream.readable);
    let addError = null;
    adding.catch(e => { addError = e; });
    const out = stream.writable.getWriter();
    let sent = 0;   // DEBUG-PROVISORIO: bytes entregues ao .zip (tem de ser igual aos que o Python emitiu)
    const put = async (proxy) => {
      const bytes = proxy.toJs();
      proxy.destroy();
      if (bytes.length) { await out.write(bytes); sent += bytes.length; }
    };
    dbg(`${db}.sql: começando a gravar no .zip`);
    await put(plan.header);
    for (let index = 0; index < plan.tables.length; index++, counter++) {
      postMessage({ tableStart: { index: counter, total: labels.length, name: labels[counter] } });
      recovery.begin_table(index);
      for (let chunk = recovery.next_chunk(); chunk !== undefined; chunk = recovery.next_chunk()) await put(chunk);
      postMessage({ table: { ...JSON.parse(recovery.info(index)), name: labels[counter] } });
      if (sent !== recovery.sql_bytes) dbg(`  DIVERGÊNCIA após ${labels[counter]}: entregue ao .zip ${sent} bytes, o Python emitiu ${recovery.sql_bytes}`);
    }
    await put(recovery.finish());
    await out.close();
    await adding;
    if (addError) throw addError;
    dbg(`${db}.sql fechado: ${sent} bytes entregues ao .zip, o Python emitiu ${recovery.sql_bytes}${sent === recovery.sql_bytes ? " (iguais)" : " (DIFERENTES)"}; memória do Python ${mbytes(pyodide._module.HEAPU8.length)}`);
    reports[db] = JSON.parse(recovery.report());
    postMessage({ log: `${db}: ${reports[db].registros.toLocaleString("pt-BR")} registros em ${Object.keys(reports[db].tabelas).length} tabelas.` });
  }

  postMessage({ phase: "objects" });
  const report = combine(reports, (Date.now() - started) / 1000);
  for (const extra of load ? fastExtras(reports) : extras(reports)) await writer.add(extra.name, new zip.TextReader(extra.text));
  postMessage({ phase: "close" });
  await writer.close();
  pyodide.runPython("carver.close()");
  const blob = file ? await file.handle.getFile() : null;
  dbg(`.zip fechado${blob ? ` (${blob.size} bytes no disco do navegador)` : " (gravado direto no destino escolhido)"}`);
  return { report, blob };
}

const safeName = (text) => text.replace(/[^A-Za-z0-9_.-]/g, "_");
const PART_BYTES = 256 * 1048576;   // o mesmo de load_format.PART_BYTES: um arquivo de dados é cortado perto disto

// FORMATO RÁPIDO de UM banco: as linhas de cada tabela vão para arquivos dados/*.txt dentro do .zip (cortados em PART_BYTES) e a
// estrutura, os LOAD DATA, os índices, os objetos e a conferência saem como .sql, no fim (são pequenos e ficam na memória).
async function writeFast(zip, writer, plan, labels, counter) {
  const { db, recovery } = plan;
  const dir = safeName(db);
  for (let index = 0; index < plan.tables.length; index++, counter++) {
    postMessage({ tableStart: { index: counter, total: labels.length, name: labels[counter] } });
    const tableStarted = Date.now();
    recovery.begin_table(index);
    const parts = [];
    let out = null, adding = null, failure = null, written = 0;
    const closePart = async () => {
      if (!out) return;
      await out.close();
      await adding;
      if (failure) throw failure;
      out = null;
    };
    for (let chunk = recovery.next_chunk(); chunk !== undefined; chunk = recovery.next_chunk()) {
      const bytes = chunk.toJs();
      chunk.destroy();
      if (!bytes.length) continue;
      if (!out || written >= PART_BYTES) {
        await closePart();
        const name = `${dir}/dados/${String(index).padStart(4, "0")}_${safeName(plan.tables[index])}.${String(parts.length).padStart(3, "0")}.txt`;
        parts.push(name);
        const stream = new TransformStream();
        failure = null;
        adding = writer.add(name, stream.readable);
        adding.catch((e) => { failure = e; });
        out = stream.writable.getWriter();
        written = 0;
      }
      await out.write(bytes);
      written += bytes.length;
    }
    await closePart();
    recovery.table_load_sql(index, JSON.stringify(parts));
    if (Date.now() - tableStarted > 3000) postMessage({ log: `Tabela ${labels[counter]}: ${((Date.now() - tableStarted) / 1000).toFixed(1)} s.` });
    postMessage({ table: { ...JSON.parse(recovery.info(index)), name: labels[counter] } });
  }
  recovery.finish().destroy();
  const files = JSON.parse(recovery.load_files());
  for (const [name, text] of Object.entries(files)) await writer.add(`${dir}/${name}`, new zip.TextReader(text));
  return counter;
}

// LEIA-ME do .zip no formato rápido.
function fastExtras(reports) {
  const lines = Object.entries(reports).map(([db, r]) => `  ${db}: ${Object.keys(r.tabelas).length} tabelas, ${r.registros} registros`);
  const readme = [
    "RECUPERAÇÃO DOS BANCOS DA PASTA DATA (formato rápido)",
    "",
    "Este .zip não traz um .sql com INSERTs: traz as linhas em arquivos de texto (pasta dados de cada banco) que o MySQL carrega com LOAD DATA,",
    "o que é mais rápido, e os índices secundários são criados no fim, com os dados já dentro.",
    "",
    "COMO RESTAURAR (precisa do MySQL ou MariaDB):",
    "  1. Extraia o .zip numa pasta (não rode de dentro do .zip).",
    "  2. Para cada banco (cada pasta com o nome dele), execute estes arquivos NESTA ORDEM:",
    "       estrutura.sql  apaga (DROP TABLE) e recria as tabelas, sem os índices secundários.",
    "       carregar.sql   carrega as linhas de dados/*.txt (LOAD DATA LOCAL INFILE).",
    "       indices.sql    cria os índices secundários.",
    "       objetos.sql    chaves estrangeiras, views e triggers.",
    "       conferir.sql   conta as linhas de cada tabela e mostra OK ou DIVERGENTE.",
    "",
    "O carregar.sql lê os arquivos por caminhos relativos à pasta extraída (banco/dados/...). O cliente precisa estar com essa pasta como pasta atual",
    "e com o local_infile ligado. Num terminal, dentro da pasta extraída:",
    "  mysql -u root -p --local-infile=1 --default-character-set=utf8 < banco\\carregar.sql",
    "Se o seu programa não achar os arquivos de dados, use o formato comum (um .sql com INSERTs), que roda em qualquer cliente.",
    "",
    "ATENÇÃO: o estrutura.sql apaga as tabelas de mesmo nome que já existam nesses bancos. Faça isso num MySQL de teste ou sem dados que importem.",
    "",
    "SE DER ERRO \"The used command is not allowed with this MySQL version\" (1148): o servidor está com local_infile desligado.",
    "  Ligue com  SET GLOBAL local_infile=1;  (precisa de privilégio) e rode de novo.",
    "",
    "PARA FICAR AINDA MAIS RÁPIDO (opcional, no servidor que vai receber os dados): innodb_flush_log_at_trx_commit=2 durante a carga",
    "  e innodb_log_file_size maior (exige reiniciar o serviço). Volte aos valores normais depois.",
    "",
    "CONFERÊNCIA:",
    ...lines,
    "",
    "Gerado pelo Recuperador de Banco MySQL (versão navegador) em " + new Date().toLocaleString("pt-BR") + ".",
    "",
  ].join("\r\n");
  return [{ name: "LEIA-ME.txt", text: readme }];
}

// Junta os relatórios dos bancos num só (a tela lê este formato).
function combine(reports, seconds) {
  const only = Object.values(reports).length === 1 ? Object.values(reports)[0] : null;   // um banco só: o SHA-256 dele vale para o relatório
  const total = { banco: Object.keys(reports).join(", "), bancos: reports, segundos: Math.round(seconds * 10) / 10, registros: 0, sql_bytes: 0, sha256: only ? only.sha256 : "",
    paginas_refeitas_doublewrite: 0, paginas_indice_invalidas: 0, objetos: { views: 0, triggers: 0, chaves_estrangeiras: 0 }, avisos: [], tabelas: {} };
  for (const [db, r] of Object.entries(reports)) {
    total.registros += r.registros;
    total.sql_bytes += r.sql_bytes;
    total.paginas_refeitas_doublewrite = Math.max(total.paginas_refeitas_doublewrite, r.paginas_refeitas_doublewrite);   // o ibdata1 é um só
    total.paginas_indice_invalidas = Math.max(total.paginas_indice_invalidas, r.paginas_indice_invalidas);
    for (const key of Object.keys(total.objetos)) total.objetos[key] += (r.objetos || {})[key] || 0;
    for (const warning of r.avisos) total.avisos.push(`[${db}] ${warning}`);
    for (const [table, info] of Object.entries(r.tabelas)) total.tabelas[`${db}.${table}`] = info;
  }
  return total;
}

// LEIA-ME do .zip com vários bancos.
function extras(reports) {
  const lines = Object.entries(reports).map(([db, r]) => `  ${db}.sql: ${Object.keys(r.tabelas).length} tabelas, ${r.registros} registros, SHA-256 ${r.sha256}`);
  const readme = [
    "RECUPERAÇÃO DOS BANCOS DA PASTA DATA",
    "",
    "Este .zip tem um dump SQL por banco e este LEIA-ME.",
    "",
    "COMO RESTAURAR (precisa do MySQL ou MariaDB):",
    "  Abra cada .sql no Query Browser (ou no cliente MySQL que preferir) e execute o arquivo inteiro, um banco por vez.",
    "  Ou, num terminal:  mysql -u root -p < banco.sql",
    "",
    "CONFERÊNCIA:",
    ...lines,
    "",
    "Gerado pelo Recuperador de Banco MySQL (versão navegador) em " + new Date().toLocaleString("pt-BR") + ".",
    "",
  ].join("\r\n");
  return [{ name: "LEIA-ME.txt", text: readme }];
}
