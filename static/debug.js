// debug.js - PROVISÓRIO: a aba "Depuração". Registra, com a hora de cada passo, tudo o que acontece numa recuperação (o que o worker e o
// motor Python informam, as mensagens de andamento, os erros) e, no fim, confere o arquivo gerado. O usuário copia o texto inteiro e
// manda para análise. Não roda nada por conta própria: é só um registro, a recuperação funciona igual sem ele.
// PARA TIRAR ANTES DE PUBLICAR: apague este arquivo, o <script> e a aba (botão goDebug e a seção view-debug) do index.html, o #dbgText do
// app.css e todas as linhas marcadas DEBUG-PROVISORIO (app.js, worker.js, py/web_driver.py, py/kernel_paths.py).

const DBG = { lines: [], t0: Date.now(), run: 0, steps: {}, engineInserts: {}, timer: null };

function dbgClock() {
  const ms = Date.now() - DBG.t0, s = Math.floor(ms / 1000);
  return `+${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}.${String(ms % 1000).padStart(3, '0')}`;
}

// Uma linha do registro (texto de várias linhas: só a primeira leva a hora, as outras ficam recuadas).
function dbg(text) {
  const parts = String(text).split('\n');
  DBG.lines.push(`[${dbgClock()}] ${parts[0]}`);
  for (let i = 1; i < parts.length; i++) DBG.lines.push('      ' + parts[i]);
  if (DBG.lines.length > 40000) DBG.lines.splice(0, DBG.lines.length - 40000);
  const table = /^TABELA \d+\/\d+ (.+?) \| .*?inserts=(\d+)/.exec(parts[0]);   // para conferir com o que o arquivo tem
  if (table) DBG.engineInserts[table[1]] = Number(table[2]);
  dbgSchedule();
}

// Registra só a cada 10% (varredura do ibdata1, descompactação, leitura do arquivo gerado).
function dbgStep(key, fraction) {
  const step = Math.min(10, Math.floor(fraction * 10));
  if (DBG.steps[key] === step) return;
  DBG.steps[key] = step;
  dbg(`${key}: ${step * 10}%`);
}

function dbgSchedule() {
  try {
    if (currentView !== 'debug' || DBG.timer) return;
  } catch (e) { return; }
  DBG.timer = setTimeout(() => { DBG.timer = null; dbgDraw(); }, 300);
}
function dbgDraw() {
  const box = document.getElementById('dbgText');
  if (!box) return;
  const stick = box.scrollTop + box.clientHeight >= box.scrollHeight - 40;
  box.value = DBG.lines.join('\n') || 'Sem registro ainda. Rode uma recuperação.';
  if (stick) box.scrollTop = box.scrollHeight;
  document.getElementById('dbgCount').textContent = DBG.lines.length + ' linhas';
}

// Começa um registro novo (chamado ao clicar em Recuperar): ambiente, entrada e destino.
function dbgStart(name, source, handle, mode) {
  DBG.run += 1; DBG.lines = []; DBG.t0 = Date.now(); DBG.steps = {}; DBG.engineInserts = {};
  dbg('=== REGISTRO DE DEPURAÇÃO (provisório) ===');
  dbg(`site ${document.querySelector('.rail-version').textContent} | ${new Date().toString()}`);
  dbg(`navegador: ${navigator.userAgent}`);
  dbg(`núcleos: ${navigator.hardwareConcurrency} | memória aprox.: ${navigator.deviceMemory || '?'} GB | endereço: ${location.href}`);
  dbg(`gravar direto em arquivo (showSaveFilePicker): ${CAN_STREAM ? 'sim' : 'não'} | disco privado (OPFS): ${navigator.storage && navigator.storage.getDirectory ? 'sim' : 'não'}`);
  try {
    navigator.storage.estimate().then(e => dbg(`armazenamento do navegador: usado ${fmtSize(e.usage)} de ${fmtSize(e.quota)}`));
  } catch (e) { /* sem estimativa */ }
  dbg(`recuperação "${name}": entrada ${source.archive ? '.zip ' + source.archive.name + ' (' + fmtSize(source.archive.size) + ')' : 'pasta data'} | ` +
      `destino ${handle ? 'arquivo escolhido ' + handle.name : 'disco do navegador (baixa no fim)'} | saída .zip | formato ${mode}`);
  if (source.files) {
    const groups = {};
    for (const f of source.files) {
      const dir = f.name.includes('/') ? f.name.split('/')[0] + '/' : '(raiz)';
      const g = groups[dir] || (groups[dir] = { n: 0, bytes: 0 });
      g.n += 1; g.bytes += f.data.size;
    }
    dbg('arquivos da pasta data: ' + Object.entries(groups).map(([d, g]) => `${d} ${g.n} arq. ${fmtSize(g.bytes)}`).join(' | '));
    dbg('arquivos da raiz: ' + source.files.filter(f => !f.name.includes('/')).map(f => `${f.name} (${f.data.size} bytes)`).join(', '));
  }
}

// As mensagens de andamento do worker. Linhas de log e {dbg} já entram por addLog e por dbg; as por página e por tabela não repetem.
function dbgMsg(d) {
  if (d.phase) dbg('ETAPA: ' + d.phase);
  else if (d.planned) {
    const pages = (d.weights || []).reduce((a, b) => a + b, 0);
    dbg(`PLANO: ${d.planned.length} tabelas; ${pages} folhas de 16 KB no total (${fmtSize(pages * 16384)})`);
  } else if (d.scan) dbgStep('varredura do ibdata1', d.scan.done / Math.max(1, d.scan.total));
  else if (d.error) dbg('ERRO (mensagem do worker): ' + d.error);
  else if (d.done) dbg(`CONCLUÍDO: ${d.done.registros} registros, ${Object.keys(d.done.tabelas).length} tabelas, memória do Python ${d.heapMB} MB, ${d.done.segundos} s`);
}

// Fim com sucesso: resumo do relatório e conferência do arquivo gerado.
async function dbgFinish(report) {
  const run = DBG.run;
  dbg('=== RESUMO ===');
  const names = Object.keys(report.tabelas), status = {};
  names.forEach(n => { status[report.tabelas[n].status] = (status[report.tabelas[n].status] || 0) + 1; });
  dbg(`banco(s): ${report.banco} | tabelas: ${names.length} (${Object.entries(status).map(([k, v]) => k + '=' + v).join(', ')}) | registros: ${report.registros} | ` +
      `bytes do .sql (motor): ${report.sql_bytes} | tempo: ${report.segundos} s`);
  dbg(`SHA-256 (motor): ${report.sha256 || 'um por banco, nas linhas FIM acima'} | avisos: ${report.avisos.length} | páginas refeitas do doublewrite: ${report.paginas_refeitas_doublewrite}`);
  names.filter(n => report.tabelas[n].status !== 'OK').forEach(n => dbg(`  ${report.tabelas[n].status} ${n}: ${report.tabelas[n].note}`));
  try { await dbgVerifyFile(report, run); } catch (e) { dbg('ERRO ao conferir o arquivo: ' + ((e && e.stack) || e)); }
  if (run === DBG.run) dbg('=== FIM DO LOG ===');
}
function dbgFail(message) {
  dbg('A RECUPERAÇÃO FALHOU: ' + message);
  dbg('=== FIM DO LOG ===');
}

// Lê um arquivo (ou parte de um .zip) por pedaços: conta linhas, acha a maior, e onde começa e quantos INSERT tem cada tabela.
function dbgScanner() {
  const decoder = new TextDecoder('utf-8');
  // Um INSERT ocupa várias linhas (um registro por linha) e termina na linha que acaba em ";": o tamanho do comando é a soma delas.
  const s = { bytes: 0, lines: 0, maxLine: 0, maxLineNo: 0, maxStmt: 0, maxStmtNo: 0, stmts: 0, over: 0, overPacket: 0, overExamples: [], inserts: {}, drops: {} };
  let lineLen = 0, head = '', lastByte = 0, inStmt = false, stmtLen = 0, stmtStart = 0, stmtHead = '';
  const endLine = () => {
    s.lines += 1;
    if (lineLen > s.maxLine) { s.maxLine = lineLen; s.maxLineNo = s.lines; }
    let m;
    if (head.startsWith('INSERT INTO `')) {
      inStmt = true; stmtLen = 0; stmtStart = s.lines; stmtHead = head;
      if ((m = /^INSERT INTO `([^`]+)`/.exec(head))) s.inserts[m[1]] = (s.inserts[m[1]] || 0) + 1;
    } else if (head.startsWith('DROP TABLE IF EXISTS `') && (m = /^DROP TABLE IF EXISTS `([^`]+)`/.exec(head))) s.drops[m[1]] = s.lines;
    if (inStmt) {
      stmtLen += lineLen + 1;
      if (lastByte === 59) {   // ";" fecha o comando
        inStmt = false; s.stmts += 1;
        if (stmtLen > s.maxStmt) { s.maxStmt = stmtLen; s.maxStmtNo = stmtStart; }
        if (stmtLen > 1000000) {
          s.over += 1;
          if (stmtLen > 1048576) s.overPacket += 1;
          if (s.overExamples.length < 5) s.overExamples.push(`comando na linha ${stmtStart} (${stmtLen} bytes) ${stmtHead.slice(0, 60)}`);
        }
      }
    }
    lineLen = 0; head = ''; lastByte = 0;
  };
  s.push = chunk => {
    let pos = 0;
    while (pos < chunk.length) {
      const nl = chunk.indexOf(10, pos), end = nl < 0 ? chunk.length : nl;
      if (lineLen < 96 && end > pos) head += decoder.decode(chunk.subarray(pos, Math.min(end, pos + 96 - lineLen)));
      if (end > pos) lastByte = chunk[end - 1];
      lineLen += end - pos;
      if (nl < 0) break;
      endLine();
      pos = nl + 1;
    }
    s.bytes += chunk.length;
  };
  s.finish = () => { if (lineLen > 0) endLine(); };
  return s;
}

const dbgPad = (value, width) => String(value).padEnd(width);

// O que a leitura do arquivo achou, contra o que o motor disse. tables = {tabela: {rows, status}} do banco `db`.
function dbgScanReport(sc, label, expectedBytes, tables, db) {
  dbg(`${label}: ${sc.bytes} bytes no arquivo; o motor emitiu ${expectedBytes} -> ${sc.bytes === expectedBytes ? 'IGUAL' : 'DIFERENTE !!!'}`);
  dbg(`${label}: ${sc.lines} linhas | maior linha ${sc.maxLine} bytes (linha ${sc.maxLineNo}) | ${sc.stmts} comandos INSERT, o maior com ${sc.maxStmt} bytes (linha ${sc.maxStmtNo}) | ` +
      `comandos acima de 1.000.000 bytes: ${sc.over} | acima de 1.048.576 (derruba o MySQL padrão): ${sc.overPacket}`);
  sc.overExamples.forEach(x => dbg('  ' + x));
  dbg(`${label}: MAPA DAS TABELAS (linha do DROP TABLE no arquivo | INSERTs no arquivo, e os que o motor contou | linhas esperadas | status)`);
  let totalInserts = 0, diverge = 0;
  for (const name of Object.keys(tables)) {
    const t = tables[name], inFile = sc.inserts[name] || 0, engine = DBG.engineInserts[`${db}.${name}`];
    totalInserts += inFile;
    let flag = '';
    if (engine !== undefined && engine !== inFile) { flag = '  <-- INSERTs DIFERENTES'; diverge += 1; }
    if (sc.drops[name] === undefined && t.status !== 'NAO_LIDA') { flag += '  <-- SEM DROP TABLE NO ARQUIVO'; diverge += 1; }
    dbg(`  ${dbgPad(name, 34)} linha ${dbgPad(sc.drops[name] === undefined ? '-' : sc.drops[name], 7)} | INSERTs ${dbgPad(inFile + ' (' + (engine === undefined ? '?' : engine) + ')', 12)} | ` +
        `linhas ${dbgPad(t.rows, 8)} | ${t.status}${flag}`);
  }
  Object.keys(sc.drops).filter(n => !(n in tables)).forEach(n => dbg(`  no arquivo e fora do relatório: ${n} (linha ${sc.drops[n]})`));
  dbg(`${label}: ${totalInserts} INSERTs no total; divergências: ${diverge}`);
}

async function dbgVerifyFile(report, run) {
  const file = last.blob || (last.handle ? await last.handle.getFile() : null);
  if (!file) { dbg('arquivo gerado: não achei o arquivo para conferir.'); return; }
  dbg('=== CONFERÊNCIA DO ARQUIVO GERADO (o arquivo é lido de novo, do disco) ===');
  dbg(`arquivo: ${file.name || '(sem nome)'} | ${file.size} bytes`);
  if (!last.multi) {
    const sc = dbgScanner(), reader = file.stream().getReader();
    let done = 0;
    for (;;) {
      const { value, done: end } = await reader.read();
      if (end) break;
      sc.push(value);
      done += value.length;
      dbgStep('leitura do arquivo gerado', done / Math.max(1, file.size));
      if (run !== DBG.run) return;
    }
    sc.finish();
    dbgScanReport(sc, 'dump.sql', report.sql_bytes, report.tabelas, report.banco);
    if (file.size <= 700 * 1048576) {
      const digest = new Uint8Array(await crypto.subtle.digest('SHA-256', await file.arrayBuffer()));
      const hex = [...digest].map(b => b.toString(16).padStart(2, '0')).join('');
      dbg(`SHA-256 do arquivo: ${hex} | do motor: ${report.sha256} -> ${hex === report.sha256 ? 'IGUAL' : 'DIFERENTE !!!'}`);
    } else dbg('SHA-256 do arquivo: não calculado (arquivo grande demais para o navegador).');
    return;
  }
  const zip = await import('../vendor/zip/zip.min.js');
  zip.configure({ useWebWorkers: false });
  const reader = new zip.ZipReader(new zip.BlobReader(file));
  const entries = await reader.getEntries();
  dbg('conteúdo do .zip: ' + entries.map(e => `${e.filename} (${e.uncompressedSize} bytes)`).join(', '));
  for (const entry of entries.filter(e => /\.sql$/i.test(e.filename))) {
    const db = entry.filename.replace(/\.sql$/i, ''), sc = dbgScanner(), info = report.bancos[db];
    await entry.getData(new WritableStream({ write(chunk) { sc.push(chunk); } }));
    sc.finish();
    dbgScanReport(sc, entry.filename, info ? info.sql_bytes : '?', info ? info.tabelas : {}, db);
    if (run !== DBG.run) break;
  }
  await reader.close();
}

// A consulta para rodar no Query Browser depois de restaurar: conta as linhas de cada tabela e compara com o esperado.
function dbgQuery() {
  const report = last && last.report;
  if (!report) return null;
  const groups = report.bancos ? Object.entries(report.bancos).map(([db, r]) => [db, r.tabelas]) : [[report.banco, report.tabelas]];
  const ident = s => '`' + String(s).replace(/`/g, '``') + '`';
  const text = s => "'" + String(s).replace(/\\/g, '\\\\').replace(/'/g, "\\'") + "'";
  const parts = [];
  for (const [db, tables] of groups) {
    for (const [name, t] of Object.entries(tables)) {
      if (t.status === 'NAO_LIDA') continue;
      const rows = Number(t.rows);
      parts.push(`SELECT ${text(db + '.' + name)} AS tabela, ${rows} AS esperado, COUNT(*) AS carregado, IF(COUNT(*) = ${rows}, 'OK', 'DIVERGENTE') AS resultado FROM ${ident(db)}.${ident(name)}`);
    }
  }
  return parts.join('\nUNION ALL\n') + ';\n';
}

async function dbgCopy(text, message) {
  const fb = document.getElementById('dbgFb');
  try { await navigator.clipboard.writeText(text); }
  catch (e) {
    const box = document.createElement('textarea');
    box.value = text; document.body.appendChild(box); box.select(); document.execCommand('copy'); box.remove();
  }
  fb.textContent = '✓ ' + message;
}
function copyDbg() { dbgCopy(DBG.lines.join('\n'), `Registro copiado (${DBG.lines.length} linhas)`); }
function copyDbgQuery() {
  const query = dbgQuery();
  if (!query) { document.getElementById('dbgFb').textContent = 'Rode uma recuperação até o fim primeiro.'; return; }
  dbgCopy(query, 'Consulta copiada: cole e execute no Query Browser depois de restaurar');
}
function saveDbg() {
  saveBlob(new Blob([DBG.lines.join('\n')], { type: 'text/plain;charset=utf-8' }), 'depuracao_' + new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-') + '.txt');
}

window.addEventListener('error', e => dbg(`ERRO NÃO TRATADO na página: ${e.message} (${e.filename}:${e.lineno})`));
window.addEventListener('unhandledrejection', e => dbg(`PROMESSA REJEITADA na página: ${(e.reason && e.reason.stack) || e.reason}`));
