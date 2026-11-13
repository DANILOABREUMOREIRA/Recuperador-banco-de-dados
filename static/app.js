// Interface do Recuperador de Banco MySQL (versão navegador). É o "cérebro" da tela: guarda a pasta escolhida, liga o
// Web Worker que roda o Python (worker.js), recebe dele o andamento e redesenha a tela. Mesma interface do programa de
// desktop, sem o servidor local: nada é enviado para fora do computador.
// Sequência típica: o usuário solta a pasta -> useFolder -> askName -> confirmName -> startRecovery -> runRecovery ->
// mensagens do worker (phase, planned, tableStart, pages, table, done) até COMPLETED.

// ESTADO DA TELA. picked = a pasta data escolhida {name, files, size, ibdata, copies}; last = o que a tela mostra do
//   andamento; logs = todas as linhas do log; worker = o Web Worker em curso.
let picked = null;
let projectName = '';
let nameOpen = false;
let worker = null;
let startedAt = 0;
let timer = null;
let shownError = false;
let logs = [];
let last = freshState();

function freshState() {
  return { state: 'IDLE', progress: 0, step_description: 'Aguardando banco.', task: null, item: null, tables: {}, elapsed_s: null,
    report: null, blob: null, mode: null, savedName: '', projectName: '' };
}

const $ = id => document.getElementById(id);
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const running = () => last.state === 'RUNNING';
// Chrome e Edge gravam o dump direto no disco (showSaveFilePicker); os outros guardam na memória e baixam no fim.
const CAN_STREAM = typeof window.showSaveFilePicker === 'function';

function note(text, isErr) { const n = $('mainNote'); n.textContent = text || ''; n.className = 'main-note' + (isErr ? ' err' : ''); }

// ---------- tema claro e escuro ----------
// A escolha fica gravada no navegador; sem escolha vale a do sistema (theme-init.js já pôs o atributo ao abrir).
function drawTheme() {
  const dark = document.documentElement.getAttribute('data-theme') === 'dark';
  const button = $('themeBtn');
  button.setAttribute('aria-pressed', String(dark));
  button.title = dark ? 'Modo claro' : 'Modo escuro';
  button.setAttribute('aria-label', button.title);
}
function toggleTheme() {
  const next = document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', next);
  try { localStorage.setItem('theme', next); } catch (e) { /* sem armazenamento */ }
  drawTheme();
}
drawTheme();

// ---------- painel de listas e telas ----------
function setSide(open) {
  document.body.classList.toggle('side-open', open);
  $('toggle').setAttribute('aria-expanded', String(open));
  try { localStorage.setItem('side', open ? '1' : '0'); } catch (e) { /* sem armazenamento */ }
}
function toggleSide() { setSide(!document.body.classList.contains('side-open')); }
const VIEWS = ['recover', 'tables', 'downloads', 'log', 'how'];
let currentView = 'recover';
function showView(name) {
  currentView = name;
  VIEWS.forEach(view => $('view-' + view).classList.toggle('show', view === name));
  document.querySelectorAll('.rail-btn[data-view]').forEach(button => button.classList.toggle('active', button.dataset.view === name));
  $('stage').scrollTop = 0;
  drawTables();
  if (window.innerWidth <= 900) setSide(false);
}
(function initSide() {
  let saved = null;
  try { saved = localStorage.getItem('side'); } catch (e) { /* sem armazenamento */ }
  setSide(saved === null ? window.innerWidth > 900 : saved === '1');
})();

// ---------- pop-up central (o nome da recuperação) ----------
let focusBeforeModal = null;
function openModal() {
  if (!$('modalBg').classList.contains('open')) focusBeforeModal = document.activeElement;
  $('modalBg').classList.add('open');
  setTimeout(() => { if ($('modalBg').classList.contains('open') && !$('modal').contains(document.activeElement)) $('modal').focus(); }, 0);
}
function closeModal() {
  if (nameOpen) { cancelName(); return; }  // fechar o pedido de nome = desistir da escolha
  $('modalBg').classList.remove('open');
  if (focusBeforeModal && document.contains(focusBeforeModal)) focusBeforeModal.focus();
  focusBeforeModal = null;
}
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') { closeModal(); return; }
  if (e.key !== 'Tab' || !$('modalBg').classList.contains('open')) return;
  const focusable = [...$('modal').querySelectorAll('button:not([disabled]), input, textarea, [tabindex]:not([tabindex="-1"])')]
    .filter(el => el.offsetParent !== null);
  if (!focusable.length) return;
  const firstEl = focusable[0], lastEl = focusable[focusable.length - 1];
  if (e.shiftKey && (document.activeElement === firstEl || document.activeElement === $('modal'))) { e.preventDefault(); lastEl.focus(); }
  else if (!e.shiftKey && document.activeElement === lastEl) { e.preventDefault(); firstEl.focus(); }
});

// ---------- escolher o banco: .zip ou a pasta data ----------
const EMPTY_TITLE = 'Solte aqui o banco';
const EMPTY_SUB = 'arquivo .zip ou a pasta data';
const fmtSize = bytes => bytes >= 1073741824 ? (bytes / 1073741824).toFixed(2).replace('.', ',') + ' GB'
  : (bytes / 1048576).toFixed(1).replace('.', ',') + ' MB';

function showSource(title, sub, filled) {
  $('drop').classList.toggle('has-file', filled);
  $('dropTitle').textContent = title;
  $('dropSub').textContent = sub;
}
function drawDest() {
  $('destLine').textContent = CAN_STREAM
    ? 'Ao recuperar, você escolhe onde salvar o .sql: ele é gravado direto no disco, tabela por tabela.'
    : 'Este navegador guarda o dump no disco do navegador e baixa no fim: precisa de espaço livre extra. Para bancos grandes use o Chrome ou o Edge.';
}
drawDest();

// A pasta escolhida pode ser a própria data ou uma pasta acima dela: select.js acha o ibdata1 (o maior, se houver
//   vários) e usa a pasta dele como raiz. picked = {kind: 'folder', name, files, ibdata, size} ou
//   {kind: 'archive', name, file, size}.
async function useFolder(name, list) {
  const { selectData } = await import('./select.js');
  const chosen = selectData(list.map(f => ({ path: f.path, size: f.file.size, file: f.file })));
  if (!chosen) { note('A pasta não tem o arquivo ibdata1: não parece uma pasta data do MySQL.', true); return; }
  picked = { kind: 'folder', name, files: chosen.items.map(i => ({ name: i.name, data: i.file })), ibdata: chosen.ibdata.file, size: chosen.size };
  projectName = '';
  note(chosen.copies > 1 ? 'Havia mais de um ibdata1 na pasta: usei o maior.' : '');
  askName(name);
}
// Um .zip, por clique ou arrastado. O .rar não entra: o formato obriga a ler o arquivo inteiro na memória.
const ARCHIVE = /\.zip$/i;
function pickFile(files) {
  if (!files || !files.length || running()) return;
  const file = files[0];   // a lista do campo é viva: copia antes de limpar
  $('fileInput').value = '';
  if (/\.rar$/i.test(file.name)) { note('O .rar não é lido aqui: extraia-o e escolha a pasta data, ou use o programa de desktop.', true); return; }
  if (!ARCHIVE.test(file.name)) { note('Formato não suportado. Use .zip ou a pasta data.', true); return; }
  picked = { kind: 'archive', name: file.name.replace(ARCHIVE, ''), file, size: file.size };
  projectName = '';
  note('');
  askName(picked.name);
}
// Pasta ARRASTADA: a API de arquivos do navegador entrega os itens em lotes; lê recursivamente.
function readDirectory(entry, prefix) {
  return new Promise(resolve => {
    const reader = entry.createReader();
    const found = [];
    const next = () => reader.readEntries(async batch => {
      if (!batch.length) { resolve(found); return; }
      for (const child of batch) {
        if (child.isFile) found.push(await new Promise(done => child.file(file => done({ file, path: prefix + child.name }))));
        else found.push(...await readDirectory(child, prefix + child.name + '/'));
      }
      next();
    }, () => resolve(found));
    next();
  });
}
async function pickFolderEntry(entry) {
  note('Lendo a pasta...');
  useFolder(entry.name, await readDirectory(entry, entry.name + '/'));
}
// Pasta escolhida pelo clique (campo de arquivo com webkitdirectory).
function pickFolderInput(files) {
  if (!files || !files.length || running()) return;
  const list = [...files].map(file => ({ file, path: file.webkitRelativePath || file.name }));
  const name = (files[0].webkitRelativePath || '').split('/')[0] || 'data';
  $('folderInput').value = '';
  useFolder(name, list);
}
(function initDrop() {
  const drop = $('drop');
  ['dragenter', 'dragover'].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.add('over'); }));
  ['dragleave', 'drop'].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.remove('over'); }));
  drop.addEventListener('drop', e => {
    if (running()) return;
    // As entradas precisam ser lidas já no evento; depois dele o navegador as invalida.
    const entries = [...(e.dataTransfer.items || [])].map(i => i.webkitGetAsEntry && i.webkitGetAsEntry()).filter(Boolean);
    if (entries.length === 1 && entries[0].isDirectory) pickFolderEntry(entries[0]); else pickFile(e.dataTransfer.files);
  });
  ['dragover', 'drop'].forEach(ev => window.addEventListener(ev, e => e.preventDefault()));
})();

// ---------- nome da recuperação ----------
function askName(suggestion) {
  nameOpen = true;
  $('nameInput').value = projectName || suggestion;
  $('nameError').textContent = '';
  $('nameInfo').textContent = picked.kind === 'archive' ? `Arquivo ${picked.file.name}: ${fmtSize(picked.size)}.`
    : `Pasta ${picked.name}: ${picked.files.length} arquivos, ibdata1 de ${fmtSize(picked.ibdata.size)}.`;
  openModal();
  setTimeout(() => { $('nameInput').focus(); $('nameInput').select(); }, 50);
}
function confirmName() {
  const value = $('nameInput').value.trim();
  if (!value) { $('nameInput').focus(); return; }
  projectName = value;
  nameOpen = false;
  showSource(projectName, (picked.kind === 'archive' ? 'arquivo ' + picked.file.name + ' (' + fmtSize(picked.size) + ')'
    : 'pasta ' + picked.name + ' (' + picked.files.length + ' arquivos, ' + fmtSize(picked.size) + ')') + ' — pronto para recuperar', true);
  closeModal();
  // Escolher outra pasta começa do zero: some o resultado anterior e o Baixar volta a ficar bloqueado.
  if (last.state !== 'IDLE') { last = freshState(); logs = []; draw(); }
  refreshButtons();
}
function cancelName() {
  nameOpen = false;
  picked = null; projectName = '';
  showSource(EMPTY_TITLE, EMPTY_SUB, false);
  closeModal();
  refreshButtons();
}

// ---------- botões ----------
function refreshButtons() {
  $('btnRun').disabled = running() || !picked || !projectName;
  $('drop').classList.toggle('locked', running());
  const done = last.state === 'COMPLETED';
  // Baixar só vale quando o navegador guardou o dump na memória; no outro caso o .sql já está no disco.
  $('btnGet').disabled = !(done && last.blob);
  $('btnGet').textContent = done && last.mode === 'disk' ? 'Salvo no disco' : 'Baixar .sql';
  $('dlSql').disabled = !(done && last.blob);
  $('dlSqlInfo').textContent = done && last.mode === 'disk' ? 'Gravado direto no disco: ' + last.savedName : 'Todas as tabelas e dados, em script';
  $('dlReport').disabled = !(done && last.report);
}
function setRunLabel() {
  $('runLabel').innerHTML = running() ? '<span class="spinner"></span>Recuperando ' + last.progress + '%' : 'Recuperar';
}

// ---------- recuperação ----------
const fileSafe = name => name.replace(/[\\/:*?"<>|]+/g, ' ').replace(/\s+/g, ' ').trim().replace(/^\.+/, '') || 'banco_recuperado';

// FAZ: o clique em Recuperar. No Chrome e no Edge pergunta primeiro onde salvar o .sql (precisa ser logo no clique).
async function startRecovery() {
  if (running() || !picked || !projectName) return;
  let handle = null;
  if (CAN_STREAM) {
    try {
      handle = await window.showSaveFilePicker({ suggestedName: fileSafe(projectName) + '.sql',
        types: [{ description: 'Dump SQL', accept: { 'application/sql': ['.sql'] } }] });
    } catch (error) {
      if (error.name !== 'AbortError') note('Não foi possível escolher o local do arquivo: ' + error.message, true);
      return;
    }
  }
  note('');
  runRecovery(picked.kind === 'archive' ? { archive: picked.file } : { files: picked.files }, handle, projectName);
}

// AS ETAPAS (a estante e a dica): nome, descrição e a porcentagem em que cada uma começa.
const STAGES = [
  { name: 'Preparação', start: 0, text: 'Liga o Python no navegador (Pyodide) e monta a lista de arquivos do banco.' },
  { name: 'Descompactação', start: 3, text: 'Abre o .zip e grava no disco do navegador só o que a leitura precisa (ibdata1, .frm, .ibd e .TRG). Com a pasta data, esta etapa não existe.' },
  { name: 'Dicionário do InnoDB', start: 15, text: 'Refaz as páginas rasgadas pelo doublewrite, varre o ibdata1 e lê as tabelas internas do InnoDB.' },
  { name: 'Leitura das tabelas', start: 24, text: 'Lê cada tabela direto das páginas, monta o CREATE TABLE e os INSERTs e grava no dump.' },
  { name: 'Chaves, views e triggers', start: 92, text: 'Recria as chaves estrangeiras do dicionário e as views e triggers dos arquivos da pasta.' },
  { name: 'Fechamento', start: 97, text: 'Fecha o arquivo .sql e monta o resumo e o relatório.' },
];
const EXTRACT_FROM = 3, EXTRACT_SPAN = 12, DICT_FROM = 15;   // a descompactação vai de 3% a 15%
const TABLES_FROM = 24, TABLES_SPAN = 68;                    // a etapa das tabelas vai de 24% a 92%

const IS_LOCAL = ['localhost', '127.0.0.1'].includes(location.hostname);   // só o servidor de teste aceita ?auto e ?stream
// A cada recuperação um número: se o usuário cancelar, o que ainda chegar do trabalho antigo é ignorado.
let runSeq = 0;
let extractDir = null;     // pasta temporária do OPFS onde o .zip foi descompactado (apagada ao fim)
let extractHooks = null;   // para cancelar a descompactação

// FAZ: começa uma recuperação. source = {files: [...]} (pasta data) ou {archive: File} (.zip, que é
//   descompactado antes). onDone(relatório, blob) ou onDone({error}) só é usado pelo teste automático de desenvolvimento.
function runRecovery(source, handle, name, onDone) {
  runSeq += 1;
  // Sem o arquivo de destino escolhido (Firefox, Safari), o dump é guardado numa pasta do disco do navegador e baixado no
  //   fim ('staged'). Nunca na memória: um dump de bancos grandes tem GBs.
  cleanupDump();
  dumpDir = handle ? null : tempName('dump');
  last = { ...freshState(), state: 'RUNNING', mode: handle ? 'disk' : 'staged', savedName: handle ? handle.name : '', projectName: name,
    step_description: 'Preparando...', task: { label: 'Preparação', percent: null, detail: '' } };
  logs = [];
  shownError = false;
  startedAt = Date.now();
  clearInterval(timer);
  timer = setInterval(() => { last.elapsed_s = Math.round((Date.now() - startedAt) / 1000); drawElapsed(); }, 1000);
  addLog(`Recuperação "${name}" iniciada.`);
  draw();
  if (!source.archive) { startWorker(source.files, handle, name, onDone); return; }
  const myRun = runSeq;
  extractFirst(source.archive, myRun)
    .then(files => { if (myRun === runSeq) startWorker(files, handle, name, onDone); })
    .catch(error => { if (myRun === runSeq) { fail(error.message || String(error)); if (onDone) onDone({ error: error.message }); } });
}

// FAZ: descompacta o .zip e devolve a lista de arquivos [{name, data: File}] (veja extract.js).
async function extractFirst(file, myRun) {
  const { extractArchive } = await import('./extract.js');
  extractDir = tempName();
  setPhase(EXTRACT_FROM, 'Descompactando ' + file.name + '...', 'Descompactação', null);
  addLog(`Descompactando ${file.name} (${fmtSize(file.size)})...`);
  extractHooks = {
    log: text => { if (myRun === runSeq) addLog(text); },
    phase: text => { if (myRun === runSeq) setPhase(EXTRACT_FROM, text, 'Descompactação', null); },
    progress: p => {
      if (myRun !== runSeq) return;
      const part = p.done / Math.max(1, p.total);
      last.progress = Math.round(EXTRACT_FROM + EXTRACT_SPAN * part);
      last.step_description = 'Descompactando ' + file.name + (p.name ? ': ' + p.name : '');
      last.task = { label: 'Descompactação', percent: Math.round(100 * part), detail: `${fmtSize(p.done)} de ${fmtSize(p.total)}` };
      last.item = { label: p.name, percent: null, detail: '' };
      scheduleDraw();
    },
    cancel: null,
  };
  // Sem destino escolhido o dump também vai para o disco do navegador: já reserva esse espaço na conferência.
  const result = await extractArchive(file, extractDir, extractHooks, dumpDir ? 0.6 : 0);
  addLog(`Descompactado: ${result.files.length} arquivos no disco do navegador.`);
  return result.files;
}
// Nome de uma pasta temporária no disco do navegador (OPFS); extract.js apaga as esquecidas depois de 1 hora.
const tempName = suffix => 'rm-' + Date.now() + (suffix ? '-' + suffix : '');
let dumpDir = null;        // pasta do OPFS onde o dump fica guardado quando não há arquivo de destino
// Apaga o que a descompactação deixou no disco do navegador (o dump já foi gravado ou está guardado).
async function cleanupExtracted() {
  const dir = extractDir;
  extractDir = null;
  if (!dir) return;
  const { cleanup } = await import('./extract.js');
  await cleanup(dir);
}
// Apaga o dump guardado de uma recuperação anterior (ao começar outra, ou se esta falhar ou for cancelada).
async function cleanupDump() {
  const dir = dumpDir;
  dumpDir = null;
  if (!dir) return;
  const { cleanup } = await import('./extract.js');
  await cleanup(dir);
}

// FAZ: liga o Web Worker do Python e traduz as mensagens dele em andamento na tela.
function startWorker(list, handle, name, onDone) {
  const myRun = runSeq;
  let names = [], tableNo = 0;
  addLog(`${list.length} arquivos, ibdata1 de ${fmtSize(list.find(f => f.name === 'ibdata1').data.size)}.`);
  worker = new Worker('worker.js');
  worker.onerror = event => { if (myRun === runSeq) fail(event.message || 'Erro ao iniciar o Python no navegador.'); };
  worker.onmessage = ({ data }) => {
    if (myRun !== runSeq) return;
    if (data.log) { addLog(data.log); return; }
    if (data.phase === 'prep') { setPhase(Math.max(last.progress, 2), 'Ligando o Python no navegador...', 'Preparação', null); }
    else if (data.phase === 'dict') { setPhase(DICT_FROM, 'Lendo o dicionário do ibdata1...', 'Dicionário do InnoDB', null); }
    else if (data.phase === 'objects') { setPhase(93, 'Chaves estrangeiras, views e triggers...', 'Chaves, views e triggers', null); }
    else if (data.phase === 'close') { setPhase(98, 'Fechando o arquivo...', 'Fechamento', null); }
    else if (data.planned) {
      names = data.planned;
      names.forEach(n => { last.tables[n] = { rows: 0, status: 'PENDING', note: '' }; });
      setPhase(TABLES_FROM, 'Lendo as tabelas...', 'Lendo tabelas', 0, `0 de ${names.length} tabelas`);
    }
    else if (data.tableStart) {
      tableNo = data.tableStart.index;
      const done = tableNo / Math.max(1, names.length);
      last.progress = Math.round(TABLES_FROM + TABLES_SPAN * done);
      last.step_description = `Tabela ${tableNo + 1} de ${names.length}: ${data.tableStart.name}`;
      last.task = { label: 'Lendo tabelas', percent: Math.round(100 * done), detail: `${tableNo} de ${names.length} tabelas` };
      last.item = { label: data.tableStart.name, percent: null, detail: '' };
      scheduleDraw();
    }
    else if (data.pages) {
      const part = data.pages.done / Math.max(1, data.pages.total);
      last.progress = Math.round(TABLES_FROM + TABLES_SPAN * (tableNo + part) / Math.max(1, names.length));
      last.item = { label: last.item ? last.item.label : '', percent: Math.round(100 * part),
        detail: `${data.pages.done.toLocaleString('pt-BR')} de ${data.pages.total.toLocaleString('pt-BR')} páginas` };
      scheduleDraw();
    }
    else if (data.table) { last.tables[data.table.name] = data.table; last.item = null; scheduleDraw(true); }
    else if (data.error) { fail(data.error); if (onDone) onDone({ error: data.error }); }
    else if (data.done) {
      data.done.memoria_python_mb = data.heapMB;
      addLog(`Memória do Python no navegador: ${data.heapMB} MB (pico).`);
      finish(data.done, data.blob, name);
      if (onDone) onDone(data.done, data.blob);
    }
  };
  // ?stream=N (só em localhost, para teste): lê em lotes as tabelas com mais de N folhas de 16 KB.
  const stream = IS_LOCAL ? new URLSearchParams(location.search).get('stream') : null;
  worker.postMessage({ files: list, handle, dumpDir, streamPages: stream === null ? undefined : Number(stream) });
  draw();
}
function setPhase(progress, text, label, percent, detail) {
  last.progress = progress;
  last.step_description = text;
  last.task = { label, percent, detail: detail || '' };
  last.item = null;
  scheduleDraw();
}
function fail(message) {
  clearInterval(timer);
  if (worker) worker.terminate();
  worker = null;
  cleanupExtracted();
  cleanupDump();
  addLog(message);
  last.state = 'ERROR';
  last.step_description = 'A recuperação falhou.';
  last.task = null; last.item = null;
  draw();
}
function finish(report, blob, name) {
  clearInterval(timer);
  if (worker) worker.terminate();   // solta os arquivos que o Python mantinha abertos
  worker = null;
  cleanupExtracted();
  Object.keys(report.tabelas).forEach(n => { last.tables[n] = report.tabelas[n]; });
  last.state = 'COMPLETED';
  last.progress = 100;
  last.elapsed_s = Math.round(report.segundos);
  last.report = report;
  last.blob = blob;
  last.task = null; last.item = null;
  last.step_description = 'Recuperação concluída.';
  addLog(`Concluída em ${report.segundos} s: ${Object.keys(report.tabelas).length} tabelas, ${report.registros.toLocaleString('pt-BR')} registros, ` +
    `${fmtSize(report.sql_bytes)}. SHA-256 ${report.sha256}`);
  saveHistory(name, report);
  draw();
}
function cancelRecovery() {
  if (!confirm('Cancelar a recuperação em andamento?\n\nO progresso desta execução será descartado. Os arquivos originais não são alterados.')) return;
  runSeq += 1;   // o que ainda chegar do trabalho antigo é ignorado
  if (extractHooks && extractHooks.cancel) extractHooks.cancel();
  if (worker) worker.terminate();
  worker = null;
  cleanupExtracted();
  cleanupDump();
  clearInterval(timer);
  last.state = 'CANCELLED';
  last.task = null; last.item = null;
  last.step_description = 'Recuperação cancelada.';
  addLog('Recuperação cancelada pelo usuário.');
  draw();
}
// Fechar ou recarregar a página no meio da recuperação a perderia: o navegador pergunta antes.
window.addEventListener('beforeunload', e => { if (running()) { e.preventDefault(); e.returnValue = ''; } });

function addLog(text) {
  logs.push(text);
  if (logs.length > 5000) logs.splice(0, logs.length - 5000);
  scheduleDraw();
}

// ---------- baixar e relatório ----------
function onGet() { download('sql'); }
function saveBlob(blob, name) {
  const link = document.createElement('a');
  link.href = URL.createObjectURL(blob);
  link.download = name;
  link.click();
  setTimeout(() => URL.revokeObjectURL(link.href), 60000);
}
function download(type) {
  if (type === 'sql' && last.blob) saveBlob(last.blob, fileSafe(last.projectName) + '.sql');
  else if (type === 'report' && last.report) openReportOf(last.report, last.projectName);
}
function openReport() { download('report'); }
// O relatório é uma página HTML completa, montada aqui e aberta numa aba nova (o navegador deixa salvá-la).
function openReportOf(report, title) {
  const html = reportHtml(report, title);
  const url = URL.createObjectURL(new Blob([html], { type: 'text/html;charset=utf-8' }));
  window.open(url, '_blank');
  setTimeout(() => URL.revokeObjectURL(url), 120000);
}
const STATUS_TEXT = { OK: 'Recuperada', AVISO: 'Aviso', NAO_LIDA: 'Não lida' };
function reportHtml(report, title) {
  const names = Object.keys(report.tabelas).sort((a, b) => (report.tabelas[a].status === 'OK') - (report.tabelas[b].status === 'OK') || a.localeCompare(b));
  const count = status => names.filter(n => report.tabelas[n].status === status).length;
  const rows = names.map(n => {
    const t = report.tabelas[n];
    return `<tr><td>${esc(n)}</td><td class="n">${Number(t.rows).toLocaleString('pt-BR')}</td><td class="${esc(t.status)}">${esc(STATUS_TEXT[t.status] || t.status)}</td><td>${esc(t.note)}</td></tr>`;
  }).join('');
  const avisos = report.avisos.length ? `<h2>Avisos (${report.avisos.length})</h2><ul>${report.avisos.map(a => `<li>${esc(a)}</li>`).join('')}</ul>` : '';
  const facts = [['Banco', report.banco], ['Tabelas', names.length], ['Registros', report.registros.toLocaleString('pt-BR')],
    ['Tamanho do .sql', fmtSize(report.sql_bytes)], ['Com aviso', count('AVISO')], ['Não lidas', count('NAO_LIDA')],
    ['Chaves estrangeiras / views / triggers', `${report.objetos.chaves_estrangeiras} / ${report.objetos.views} / ${report.objetos.triggers}`],
    ['Páginas refeitas pelo doublewrite', report.paginas_refeitas_doublewrite], ['Duração', report.segundos + ' s']]
    .map(([k, v]) => `<tr><th>${esc(k)}</th><td>${esc(v)}</td></tr>`).join('');
  return `<!doctype html><html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">` +
    `<title>Relatório ${esc(title || report.banco)}</title><style>
    :root{color-scheme:light dark}body{font:15px/1.5 system-ui,"Segoe UI",sans-serif;max-width:960px;margin:24px auto;padding:0 16px}
    h1{font-size:1.4rem}h2{font-size:1.1rem;margin-top:1.6em}table{border-collapse:collapse;width:100%;margin:8px 0}
    th,td{border-bottom:1px solid #8884;padding:5px 8px;text-align:left;vertical-align:top;overflow-wrap:anywhere}th{width:40%}
    .data th{width:auto}td.n{text-align:right;font-variant-numeric:tabular-nums}.OK{color:#1a7a3e}.AVISO{color:#a06000}.NAO_LIDA{color:#b3261e}
    code{font-family:Consolas,monospace;overflow-wrap:anywhere}</style></head><body>
    <h1>Relatório da recuperação: ${esc(title || report.banco)}</h1>
    <p>Gerado em ${esc(new Date().toLocaleString('pt-BR'))} pelo Recuperador de Banco MySQL (versão navegador).</p>
    <table>${facts}</table><p>SHA-256 do .sql: <code>${esc(report.sha256)}</code></p>${avisos}
    <h2>Tabelas</h2><table class="data"><tr><th>Tabela</th><th>Registros</th><th>Status</th><th>Observação</th></tr>${rows}</table>
    <p>Esta versão lê só tabelas InnoDB direto das páginas. Registros apagados há pouco tempo podem reaparecer.</p></body></html>`;
}

// ---------- histórico (gravado neste navegador) ----------
const HISTORY_KEY = 'history', HISTORY_MAX = 6;
function readHistory() {
  try { const list = JSON.parse(localStorage.getItem(HISTORY_KEY) || '[]'); return Array.isArray(list) ? list : []; } catch (e) { return []; }
}
function writeHistory(list) {
  // Se o armazenamento do navegador encher, tira o relatório das mais antigas até caber.
  for (let drop = 0; drop <= list.length; drop++) {
    const copy = list.map((item, i) => i >= list.length - drop ? { ...item, report: null } : item);
    try { localStorage.setItem(HISTORY_KEY, JSON.stringify(copy)); return; } catch (e) { /* tenta de novo, com menos */ }
  }
}
function saveHistory(name, report) {
  const tables = Object.keys(report.tabelas).length;
  const entry = { id: Date.now(), name, date: new Date().toLocaleString('pt-BR'),
    summary: `${tables} tabelas, ${report.registros.toLocaleString('pt-BR')} registros, ${fmtSize(report.sql_bytes)}`, report };
  writeHistory([entry, ...readHistory()].slice(0, HISTORY_MAX));
  drawHistory();
}
function clearHistory() {
  if (!confirm('Apagar o histórico de recuperações deste navegador?')) return;
  try { localStorage.removeItem(HISTORY_KEY); } catch (e) { /* sem armazenamento */ }
  drawHistory();
}
function smallButton(text, action) {
  const button = document.createElement('button');
  button.type = 'button'; button.className = 'small-btn'; button.textContent = text;
  button.addEventListener('click', action);
  return button;
}
// Os textos entram por textContent: um nome de recuperação nunca vira código da página.
function drawHistory() {
  const list = readHistory();
  $('histCount').textContent = list.length;
  $('histClearRow').hidden = !list.length;
  const box = $('histList');
  box.replaceChildren();
  if (!list.length) {
    const empty = document.createElement('p');
    empty.className = 'hint'; empty.textContent = 'Nenhuma recuperação concluída ainda.';
    box.append(empty);
    return;
  }
  list.forEach(item => {
    const row = document.createElement('div'); row.className = 'dl-item';
    const text = document.createElement('div');
    const name = document.createElement('b'); name.textContent = item.name;
    const info = document.createElement('small'); info.textContent = [item.date, item.summary].filter(Boolean).join(' · ');
    text.append(name, info);
    const buttons = document.createElement('span'); buttons.className = 'hist-btns';
    if (item.report) buttons.append(smallButton('Relatório', () => openReportOf(item.report, item.name)));
    row.append(text, buttons);
    box.append(row);
  });
}
drawHistory();

function fmtElapsed(seconds) {
  const two = n => String(n).padStart(2, '0');
  if (seconds >= 3600) return Math.floor(seconds / 3600) + ' h ' + two(Math.floor(seconds % 3600 / 60)) + ' min';
  return seconds >= 60 ? Math.floor(seconds / 60) + ' min ' + two(seconds % 60) + ' s' : seconds + ' s';
}

// ---------- a estante de livros (andamento geral) ----------
const SHELF_HEIGHTS = [86, 100, 72, 94, 80, 100, 68, 90, 98, 76, 100, 84, 70, 96, 88, 100, 74, 92, 82, 98, 78, 94];
(function buildShelf() {
  SHELF_HEIGHTS.forEach(h => {
    const book = document.createElement('i');
    book.style.setProperty('--h', h + '%');
    book.appendChild(document.createElement('b'));
    $('shelf').appendChild(book);
  });
})();
let shelfNow = { percent: 0, state: 'IDLE' };
function stageOf(percent) {
  let found = 0;
  STAGES.forEach((stage, i) => { if (percent >= stage.start) found = i; });
  return found;
}
function drawShelf(percent, state) {
  shelfNow = { percent, state };
  const books = [...$('shelf').children];
  const filled = percent / 100 * books.length;
  books.forEach((book, i) => {
    const part = Math.min(1, Math.max(0, filled - i));
    book.firstChild.style.height = Math.round(100 * part) + '%';
    book.classList.toggle('on', part > 0);
    book.dataset.stage = String(stageOf((i + 0.5) * 100 / books.length));
  });
  $('shelf').className = 'shelf' + (state === 'ERROR' ? ' err' : state === 'CANCELLED' ? ' stop' : '');
  $('shelfPct').textContent = percent + '%';
  const current = STAGES[stageOf(percent)];
  $('shelf').setAttribute('aria-label', 'Andamento geral da recuperação: ' + percent + '%' + (current && percent > 0 ? ' — ' + current.name : ''));
}
function showShelfTip(index) {
  const stage = STAGES[index];
  if (!stage) return;
  const end = index + 1 < STAGES.length ? STAGES[index + 1].start : 100;
  const { percent, state } = shelfNow;
  const label = state === 'IDLE' ? 'Aguardando'
    : (state === 'COMPLETED' || percent >= end) ? 'Concluída'
    : percent < stage.start ? 'Aguardando'
    : state === 'RUNNING' ? 'Em andamento' : 'Interrompida';
  const tip = $('shelfTip');
  tip.replaceChildren();
  const head = document.createElement('header');
  const dot = document.createElement('i'); dot.className = 'tip-dot'; dot.dataset.stage = String(index);
  const name = document.createElement('b'); name.textContent = stage.name;
  const stateText = document.createElement('span'); stateText.className = 'tip-state'; stateText.textContent = label;
  head.append(dot, name, stateText);
  const text = document.createElement('p'); text.textContent = stage.text;
  tip.append(head, text);
  tip.classList.add('show');
  [...$('shelf').children].forEach(book => book.classList.toggle('hot', book.dataset.stage === String(index)));
}
function hideShelfTip() {
  $('shelfTip').classList.remove('show');
  [...$('shelf').children].forEach(book => book.classList.remove('hot'));
}
$('shelf').addEventListener('mouseover', e => { const book = e.target.closest('i'); if (book) showShelfTip(Number(book.dataset.stage)); });
$('shelf').addEventListener('mouseleave', hideShelfTip);

// ---------- as duas barras ----------
// percent null = anda mas não dá para medir: a barra mostra uma faixa correndo.
function setBar(barId, fillId, percent) {
  const indeterminate = percent === null || percent === undefined;
  $(barId).classList.toggle('indet', indeterminate);
  $(fillId).style.width = indeterminate ? '' : Math.max(0, Math.min(100, percent)) + '%';
}
const barText = (percent, detail) => [percent === null || percent === undefined ? '' : percent + '%', detail || ''].filter(Boolean).join(' · ');
function drawBars(data) {
  const live = data.state === 'RUNNING';
  const task = live ? data.task : null, item = live ? data.item : null;
  const prog = data.progress;
  $('taskLabel').textContent = task ? task.label : 'Andamento geral';
  $('taskDetail').textContent = task ? barText(task.percent, task.detail) : prog + '%';
  setBar('bar', 'barFill', task ? task.percent : prog);
  $('bar').setAttribute('aria-valuenow', String(task && task.percent !== null ? task.percent : prog));
  $('itemLabel').textContent = item ? item.label : ' ';
  $('itemDetail').textContent = item ? barText(item.percent, item.detail) : '';
  setBar('barItem', 'barItemFill', item ? item.percent : 0);
}

// ---------- tabelas e filtros ----------
let rowsSort = null;      // null | 'desc' (maiores primeiro) | 'asc' (menores primeiro)
let statusFilter = null;  // null (todas) | 'RECOVERED' | 'PENDING' | 'WARNING' | 'SKIPPED'
const STATUS_LABELS = { RECOVERED: 'Recuperadas', PENDING: 'Pendentes', WARNING: 'Com perda ou aviso', SKIPPED: 'Não lidas' };
const statusGroup = t => t.status === 'OK' ? 'RECOVERED' : t.status === 'PENDING' ? 'PENDING' : t.status === 'NAO_LIDA' ? 'SKIPPED' : 'WARNING';
function sortByRows() { rowsSort = rowsSort === null ? 'desc' : rowsSort === 'desc' ? 'asc' : null; drawTables(); }
function setFilter(group) { statusFilter = group; showView('tables'); }
function drawCounts() {
  const counts = { RECOVERED: 0, PENDING: 0, WARNING: 0, SKIPPED: 0 };
  Object.values(last.tables).forEach(t => { counts[statusGroup(t)] += 1; });
  $('cnt-all').textContent = Object.keys(last.tables).length;
  Object.keys(counts).forEach(group => { $('cnt-' + group).textContent = counts[group]; });
  document.querySelectorAll('.list .row[id^="flt-"]').forEach(row => row.classList.toggle('sel', currentView === 'tables' && row.id === 'flt-' + (statusFilter || 'all')));
}
function drawTables() {
  drawCounts();
  if (currentView !== 'tables') return;
  const all = Object.keys(last.tables);
  if (!all.length) return;
  const names = statusFilter ? all.filter(n => statusGroup(last.tables[n]) === statusFilter) : all.slice();
  const dir = rowsSort === 'desc' ? -1 : 1;
  if (rowsSort) names.sort((a, b) => dir * (Number(last.tables[a].rows) - Number(last.tables[b].rows)));
  $('arrRows').textContent = rowsSort === 'desc' ? '▼' : rowsSort === 'asc' ? '▲' : '⇅';
  $('thRows').classList.toggle('active', !!rowsSort);
  $('thRows').setAttribute('aria-sort', rowsSort === 'desc' ? 'descending' : rowsSort === 'asc' ? 'ascending' : 'none');
  $('tblCount').textContent = statusFilter ? names.length + ' de ' + all.length + ' · ' + STATUS_LABELS[statusFilter] : all.length + ' tabelas';
  if (!names.length) {
    $('tblBody').innerHTML = '<tr><td colspan="3" class="empty">Nenhuma tabela neste grupo.</td></tr>';
    return;
  }
  $('tblBody').innerHTML = names.map(name => {
    const t = last.tables[name];
    const tag = t.status === 'OK' ? '<span class="tag info">RECUPERADA</span>'
      : t.status === 'PENDING' ? '<span class="tag warn" style="opacity:.7">PENDENTE</span>'
      : t.status === 'NAO_LIDA' ? '<span class="tag warn">NÃO LIDA</span>'
      : '<span class="tag warn">AVISO</span>';
    const why = t.note ? '<small' + (t.status === 'OK' ? '' : ' class="warn"') + '>' + esc(t.note) + '</small>' : '';
    return '<tr><td><b>' + esc(name) + '</b>' + why + '</td><td>' + Number(t.rows).toLocaleString('pt-BR') + '</td><td>' + tag + '</td></tr>';
  }).join('');
}

// ---------- desenho ----------
// As mensagens do worker chegam aos montes (a cada 64 páginas); desenhar no máximo uma vez por quadro mantém a tela
//   leve. 'tables' = refaz também a lista de tabelas (só ao terminar cada tabela).
let drawQueued = false, tablesDirty = false;
function scheduleDraw(tables) {
  if (tables) tablesDirty = true;
  if (drawQueued) return;
  drawQueued = true;
  requestAnimationFrame(() => { drawQueued = false; draw(); });
}
function drawElapsed() {
  const timed = last.state !== 'IDLE' && last.elapsed_s !== null && last.elapsed_s !== undefined;
  $('elapsed').textContent = timed ? '· ' + fmtElapsed(last.elapsed_s) : '';
}
function draw() {
  const data = last;
  document.body.classList.toggle('running', data.state === 'RUNNING');
  drawShelf(data.state === 'COMPLETED' ? 100 : data.progress, data.state);
  drawBars(data);
  $('runFill').style.width = data.state === 'RUNNING' ? data.progress + '%' : '0';
  setRunLabel();
  $('btnCancel').style.display = data.state === 'RUNNING' ? 'inline-block' : 'none';
  const icon = { COMPLETED: '✓ ', ERROR: '✗ ', CANCELLED: '■ ' }[data.state] || '';
  $('stepText').textContent = icon + (data.step_description || '');
  if (logs.length) {
    const box = $('consoleBox');
    box.textContent = logs.slice(-80).join('\n');
    box.scrollTop = box.scrollHeight;
  } else { $('consoleBox').textContent = 'Sem atividade.'; }
  if (currentView === 'log') { $('logText').value = logs.join('\n') || 'Sem atividade.'; }
  drawElapsed();
  drawCounts();
  if (tablesDirty || data.state !== 'RUNNING') { tablesDirty = false; drawTables(); }
  $('doneActions').hidden = !(data.state === 'COMPLETED' && data.report);

  if (data.state === 'COMPLETED') {
    const lossy = Object.values(data.tables).some(t => t.status !== 'OK');
    const where = data.mode === 'disk' ? ' Arquivo gravado: ' + data.savedName + '.' : ' Clique em Baixar .sql.';
    note((lossy ? 'Concluída em ' + fmtElapsed(data.elapsed_s) + ' com avisos em algumas tabelas — veja a lista em Tabelas recuperadas.'
      : 'Recuperação concluída em ' + fmtElapsed(data.elapsed_s) + '.') + where);
  } else if (data.state === 'ERROR') {
    note('A recuperação falhou. Veja o log completo na barra lateral.', true);
    if (!shownError) { shownError = true; openLog('A recuperação falhou. Copie o log abaixo e envie ao suporte.'); }
  } else if (data.state === 'CANCELLED') {
    note('Recuperação cancelada. O arquivo .sql escolhido pode ter ficado vazio: apague-o.');
  }
  refreshButtons();
}

// ---------- log ----------
function openLog(hint) {
  showView('log');
  $('logHint').textContent = typeof hint === 'string' ? hint : 'Tudo o que o motor registrou nesta recuperação. Copie e envie ao suporte em caso de erro.';
  $('logFb').textContent = '';
  const box = $('logText');
  box.value = logs.join('\n') || 'Sem atividade.';
  box.scrollTop = box.scrollHeight;
}
function copyLog() {
  const box = $('logText');
  const done = () => { $('logFb').textContent = '✓ Log copiado'; };
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(box.value).then(done).catch(() => { box.select(); document.execCommand('copy'); done(); });
  } else { box.select(); document.execCommand('copy'); done(); }
}
function saveLog() {
  const name = 'log_recuperacao_' + new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-') + '.txt';
  saveBlob(new Blob([$('logText').value], { type: 'text/plain;charset=utf-8' }), name);
}

// Sem Web Worker ou WebAssembly não há como rodar o Python: avisa logo, em vez de falhar no meio.
if (typeof Worker === 'undefined' || typeof WebAssembly === 'undefined') {
  note('Este navegador não roda o Python (precisa de Web Worker e WebAssembly). Use o Chrome, o Edge ou o Firefox atualizados.', true);
}
draw();

// Ao abrir: apaga o que uma execução anterior deixou no disco do navegador (fechada no meio da descompactação).
import('./extract.js').then(m => m.cleanupLeftovers()).catch(() => {});

// Teste automático de desenvolvimento (dev/dev_server.py, só em localhost): ?auto=1 pega a pasta data do servidor local;
// ?auto=archive pega o .zip que ele serve em /archive. Confere se o arquivo montado tem o mesmo SHA-256 que o
// Python calculou e devolve o resultado ao servidor.
const AUTO = IS_LOCAL ? new URLSearchParams(location.search).get('auto') : null;
if (AUTO) {
  (async () => {
    const finishTest = async (report, blob) => {
      if (blob) {
        const digest = new Uint8Array(await crypto.subtle.digest('SHA-256', await blob.arrayBuffer()));
        report.arquivo_bytes = blob.size;
        report.arquivo_sha256 = [...digest].map(b => b.toString(16).padStart(2, '0')).join('');
      }
      fetch('/result', { method: 'POST', body: JSON.stringify(report) });
    };
    if (AUTO === 'archive') {
      const response = await fetch('/archive', { cache: 'no-store' });
      const file = new File([await response.blob()], decodeURIComponent(response.headers.get('X-Archive-Name') || 'banco.zip'));
      runRecovery({ archive: file }, null, 'auto', finishTest);
      return;
    }
    const names = await (await fetch('/list', { cache: 'no-store' })).json();
    const list = [];
    // no-store: senão o navegador reaproveita arquivos de outra pasta data testada antes no mesmo endereço.
    for (const name of names) list.push({ name, data: await (await fetch('/data/' + name, { cache: 'no-store' })).blob() });
    runRecovery({ files: list }, null, 'auto', finishTest);
  })();
}
