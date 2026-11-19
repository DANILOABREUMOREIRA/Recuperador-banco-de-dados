// worker.js - roda o motor de leitura direta (Python) dentro do navegador, com o Pyodide.
// Precisa ser um Web Worker: o WORKERFS lê os arquivos do disco aos pedaços (FileReaderSync), sem carregá-los na
// memória, e a tela não trava enquanto o Python trabalha.
importScripts("https://cdn.jsdelivr.net/pyodide/v0.26.4/full/pyodide.js");
importScripts("kernel.js");   // define loadKernel: o laço quente (ler registros e escrever o SQL) compilado para WebAssembly

const MODULES = ["innodb_carver", "page_health", "mysql_codec", "frm_parser", "dictionary_rebuild", "stream_carver", "bigfile", "lost_tables",
  "kernel_bridge", "kernel_paths", "load_format", "web_driver"];

// Recebe { files: [{name: "ibdata1" | "banco/tabela.frm", data: File}], handle: FileSystemFileHandle | null,
//   dumpDir } e avisa a tela do que acontece: {phase}, {log}, {planned}, {tableStart}, {pages}, {table}, {done} ou {error}.
// O dump vai para o arquivo que o usuário escolheu (handle: Chrome e Edge) ou, sem ele, para um arquivo do disco do
//   navegador (OPFS, na pasta dumpDir) que a tela baixa no fim. Nunca fica na memória: um dump de banco grande tem GBs.
// Uma pasta com UM banco gera um .sql. Com VÁRIOS bancos, todos são recuperados e saem num .zip com um .sql por banco
//   (montado em fluxo, durante a recuperação).
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
        pyodide.globals.set("js_kernel", await loadKernel(await response.arrayBuffer()));
        pyodide.runPython("kernel_bridge.set_kernel(js_kernel)");
        postMessage({ log: "Kernel WebAssembly ligado: as tabelas são lidas pelo código compilado." });
      } catch (error) {
        postMessage({ log: `Kernel WebAssembly indisponível (${error.message}): leitura só em Python, mais lenta.` });
      }
    }
    const databases = JSON.parse(pyodide.runPython('json.dumps(list(web_driver.list_databases("/data")))'));
    // Formato rápido (data.mode === "load"), mesmo com um banco só, e vários bancos saem num .zip; o formato comum de um banco só é um .sql.
    if (databases.length > 1 || data.mode === "load") {
      const result = await recoverMany(pyodide, data, databases, data.mode === "load");
      postMessage({ done: result.report, blob: result.blob, heapMB: Math.round(pyodide._module.HEAPU8.length / 1048576) });
      return;
    }
    pyodide.runPython('recovery = web_driver.Recovery("/data", log=js_log, stream_pages=stream_pages)');
    const recovery = pyodide.globals.get("recovery");
    recovery.on_pages = (done, total) => postMessage({ pages: { done, total } });

    let write;
    if (data.handle) {
      writable = await data.handle.createWritable();
      write = (bytes) => writable.write(bytes);
    } else {
      staged = await opfsFile(data.dumpDir, "dump.sql");
      write = (bytes) => staged.write(bytes);
    }
    const put = async (proxy) => {
      const bytes = proxy.toJs();
      proxy.destroy();
      if (bytes.length) await write(bytes);
    };

    postMessage({ phase: "dict" });
    await put(recovery.plan());
    const names = JSON.parse(pyodide.runPython("json.dumps([name for name, _ in recovery.tables])"));
    postMessage({ planned: names, weights: names.map((_, i) => recovery.leaf_count(i)) });
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
  let file = null, destination;
  if (data.handle) destination = await data.handle.createWritable();
  else {
    file = await opfsFile(data.dumpDir, "dump.zip");
    destination = new WritableStream({
      write(chunk) { file.write(chunk); },
      close() { file.access.flush(); file.access.close(); },
      abort() { file.access.close(); },
    });
  }
  const writer = new zip.ZipWriter(destination, { zip64: true });   // o tamanho de cada .sql só se sabe no fim

  postMessage({ phase: "dict" });
  postMessage({ log: databases.length > 1 ? `${databases.length} bancos na pasta: ${databases.join(", ")}. Todos serão recuperados, num .zip.`
    : `Formato rápido: o .zip leva as linhas em arquivos de texto, que o MySQL carrega com LOAD DATA.` });
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
    const header = recovery.plan();   // cada banco lista as tabelas dele; o cabeçalho do .sql espera a vez do banco
    const tables = JSON.parse(pyodide.runPython("json.dumps([name for name, _ in recoveries[db_name].tables])"));
    plans.push({ db, recovery, header, tables });
  }
  const labels = plans.flatMap(p => p.tables.map(t => `${p.db}.${t}`));
  postMessage({ planned: labels, weights: plans.flatMap(p => p.tables.map((_, i) => p.recovery.leaf_count(i))) });

  const reports = {};
  let counter = 0;
  const dirs = [];
  for (const plan of plans) {
    const { db, recovery } = plan;
    if (load) {
      counter = await writeFast(zip, writer, plan, labels, counter);
      dirs.push(safeName(db));
      reports[db] = JSON.parse(recovery.report());
      postMessage({ log: `${db}: ${reports[db].registros.toLocaleString("pt-BR")} registros em ${Object.keys(reports[db].tabelas).length} tabelas.` });
      continue;
    }
    const stream = new TransformStream();
    const adding = writer.add(`${db}.sql`, stream.readable);
    let addError = null;
    adding.catch(e => { addError = e; });
    const out = stream.writable.getWriter();
    const put = async (proxy) => {
      const bytes = proxy.toJs();
      proxy.destroy();
      if (bytes.length) await out.write(bytes);
    };
    await put(plan.header);
    for (let index = 0; index < plan.tables.length; index++, counter++) {
      postMessage({ tableStart: { index: counter, total: labels.length, name: labels[counter] } });
      recovery.begin_table(index);
      for (let chunk = recovery.next_chunk(); chunk !== undefined; chunk = recovery.next_chunk()) await put(chunk);
      postMessage({ table: { ...JSON.parse(recovery.info(index)), name: labels[counter] } });
    }
    await put(recovery.finish());
    await out.close();
    await adding;
    if (addError) throw addError;
    reports[db] = JSON.parse(recovery.report());
    postMessage({ log: `${db}: ${reports[db].registros.toLocaleString("pt-BR")} registros em ${Object.keys(reports[db].tabelas).length} tabelas.` });
  }

  postMessage({ phase: "objects" });
  const report = combine(reports, (Date.now() - started) / 1000);
  for (const extra of load ? fastExtras(reports, dirs) : extras(reports)) await writer.add(extra.name, new zip.TextReader(extra.text));
  postMessage({ phase: "close" });
  await writer.close();
  pyodide.runPython("carver.close()");
  return { report, blob: file ? await file.handle.getFile() : null };
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

// LEIA-ME, restaurar.sql e restaurar.bat do .zip no formato rápido.
function fastExtras(reports, dirs) {
  const lines = Object.entries(reports).map(([db, r]) => `  ${db}: ${Object.keys(r.tabelas).length} tabelas, ${r.registros} registros`);
  const readme = [
    "RECUPERAÇÃO DOS BANCOS DA PASTA DATA (formato rápido)",
    "",
    "Este .zip não traz um .sql com INSERTs: traz as linhas em arquivos de texto (pasta dados de cada banco) que o MySQL carrega com LOAD DATA,",
    "o que é mais rápido, e os índices secundários são criados no fim, com os dados já dentro.",
    "",
    "COMO RESTAURAR (precisa do MySQL ou MariaDB ligado e do comando mysql no PATH):",
    "  1. Extraia o .zip numa pasta (não rode de dentro do .zip).",
    "  2. Dê dois cliques em restaurar.bat, informe o usuário e a senha do MySQL quando ele pedir.",
    "     Ou, num terminal, dentro da pasta extraída:  mysql -u root -p --local-infile=1 < restaurar.sql",
    "",
    "O QUE ELE FAZ, banco por banco (os comandos estão em restaurar.sql):",
    "  estrutura.sql  apaga (DROP TABLE) e recria as tabelas, sem os índices secundários.",
    "  carregar.sql   carrega as linhas de dados/*.txt (LOAD DATA LOCAL INFILE).",
    "  indices.sql    cria os índices secundários.",
    "  objetos.sql    chaves estrangeiras, views e triggers.",
    "  conferir.sql   conta as linhas de cada tabela e mostra OK ou DIVERGENTE.",
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
  const sql = dirs.flatMap((dir) => ["estrutura", "carregar", "indices", "objetos", "conferir"].map((part) => `SOURCE ${dir}/${part}.sql;`)).join("\n") + "\n";
  const bat = [
    "@echo off", "chcp 65001 > nul", 'cd /d "%~dp0"',
    "echo Restaurando os bancos no MySQL. Precisa do MySQL ligado e do comando mysql no PATH.",
    "set /p MYSQL_USER=Usuario do MySQL [root]: ",
    'if "%MYSQL_USER%"=="" set MYSQL_USER=root',
    "echo Informe a senha do MySQL quando ele pedir. O que importar aparece abaixo; no fim vem a conferencia das linhas.",
    "mysql -u %MYSQL_USER% -p --local-infile=1 --default-character-set=utf8 --max_allowed_packet=64M -t < restaurar.sql",
    "echo.", "echo Fim. Se nao houve mensagem de erro acima, confira que nenhuma linha da conferencia diz DIVERGENTE.", "pause", "",
  ].join("\r\n");
  return [{ name: "LEIA-ME.txt", text: readme }, { name: "restaurar.sql", text: sql }, { name: "restaurar.bat", text: bat }];
}

// Junta os relatórios dos bancos num só (a tela lê este formato).
function combine(reports, seconds) {
  const total = { banco: Object.keys(reports).join(", "), bancos: reports, segundos: Math.round(seconds * 10) / 10, registros: 0, sql_bytes: 0, sha256: "",
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

// LEIA-ME e restaurar.bat do .zip com vários bancos.
function extras(reports) {
  const lines = Object.entries(reports).map(([db, r]) => `  ${db}.sql: ${Object.keys(r.tabelas).length} tabelas, ${r.registros} registros, SHA-256 ${r.sha256}`);
  const readme = [
    "RECUPERAÇÃO DOS BANCOS DA PASTA DATA",
    "",
    "Este .zip tem um dump SQL por banco, este LEIA-ME e o restaurar.bat.",
    "",
    "COMO RESTAURAR (precisa do MySQL ou MariaDB instalado):",
    "  1. Extraia o .zip numa pasta.",
    "  2. Dê dois cliques em restaurar.bat e informe a senha do MySQL quando ele pedir (importa todos os .sql da pasta).",
    "     Ou, num terminal, um banco por vez:  mysql -u root -p < banco.sql",
    "",
    "CONFERÊNCIA:",
    ...lines,
    "",
    "Gerado pelo Recuperador de Banco MySQL (versão navegador) em " + new Date().toLocaleString("pt-BR") + ".",
    "",
  ].join("\r\n");
  const bat = [
    "@echo off", "chcp 65001 > nul",
    "echo Restaurando os dumps no MySQL. Precisa do MySQL instalado e do comando mysql no PATH.",
    "set /p MYSQL_USER=Usuario do MySQL [root]: ",
    'if "%MYSQL_USER%"=="" set MYSQL_USER=root',
    'for %%f in ("%~dp0*.sql") do (', "  echo Importando %%~nxf ...", '  mysql -u %MYSQL_USER% -p < "%%f"', ")",
    "echo.", "echo Fim. Se nao houve mensagem de erro acima, os bancos foram restaurados.", "pause", "",
  ].join("\r\n");
  return [{ name: "LEIA-ME.txt", text: readme }, { name: "restaurar.bat", text: bat }];
}
