// zip-worker.js - abre um .zip e grava no disco do navegador (OPFS) só o que a leitura direta precisa: o ibdata1 e os
// .frm, .ibd e .TRG. O .zip é lido aos pedaços (BlobReader) e cada arquivo vai para o disco em fluxo, então o tamanho
// do banco não é limitado pela memória. Sem OPFS não há como: a descompactação na memória não aguenta bancos grandes.
// Recebe { file, dir, reserveRatio }; avisa {log}, {progress}, {done: {files}} ou {error}.
import * as zip from './vendor/zip/zip.min.js';
import { selectData } from './static/select.js';

zip.configure({ useWebWorkers: false });   // este já é um worker

const say = log => postMessage({ log });
const size = bytes => bytes >= 1073741824 ? (bytes / 1073741824).toFixed(2).replace('.', ',') + ' GB'
  : (bytes / 1048576).toFixed(1).replace('.', ',') + ' MB';

// Um destino para os bytes de um arquivo: um WritableStream que grava no OPFS (createSyncAccessHandle, que existe em
//   todos os navegadores com OPFS) e devolve o File ao fechar.
async function opfsSink(root, relPath) {
  let folder = root;
  const parts = relPath.split('/');
  for (const part of parts.slice(0, -1)) folder = await folder.getDirectoryHandle(part, { create: true });
  const handle = await folder.getFileHandle(parts[parts.length - 1], { create: true });
  const access = await handle.createSyncAccessHandle();
  let position = 0;
  const writable = new WritableStream({
    write(chunk) { access.write(chunk, { at: position }); position += chunk.byteLength; },
    close() { access.flush(); access.close(); },
    abort() { access.close(); },
  });
  return { writable, file: () => handle.getFile() };
}

onmessage = async ({ data }) => {
  try {
    const { file, dir, reserveRatio } = data;
    if (!navigator.storage || !navigator.storage.getDirectory) {
      throw new Error('Este navegador não tem disco privado (OPFS) para descompactar o .zip. Extraia o .zip e escolha a pasta data.');
    }
    const reader = new zip.ZipReader(new zip.BlobReader(file));
    let all;
    try { all = await reader.getEntries(); }
    catch (error) { throw new Error('Não consegui abrir o .zip (arquivo corrompido ou incompleto): ' + error.message); }
    const found = all.filter(e => !e.directory)
      .map(e => ({ path: e.filename.replace(/\\/g, '/').replace(/^(\.\/|\/)+/, ''), size: e.uncompressedSize, entry: e }));
    if (found.some(e => e.entry.encrypted)) throw new Error('O .zip tem senha: extraia-o antes e escolha a pasta data.');
    const chosen = selectData(found, { emptyMyisam: true });
    if (!chosen) throw new Error('O .zip não tem o arquivo ibdata1: não parece uma pasta data do MySQL.');
    if (chosen.copies > 1) say('Havia mais de um ibdata1 no .zip: usei o maior.');
    say(`.zip: ${found.length} arquivos; vão ser gravados ${chosen.items.length} (${size(chosen.size)}), ibdata1 de ${size(chosen.ibdata.size)}.`);

    // Espaço no disco do navegador: o banco descompactado e, sem arquivo de destino, o dump que vem depois.
    const need = chosen.size * (1.02 + (reserveRatio || 0));
    const room = await navigator.storage.estimate();
    if (room.quota && room.quota - room.usage < need) {
      throw new Error(`Falta espaço no navegador: a recuperação precisa de cerca de ${size(need)} e há ${size(room.quota - room.usage)} livres. ` +
        'Libere espaço no disco ou extraia o .zip e escolha a pasta data.');
    }
    const root = await (await navigator.storage.getDirectory()).getDirectoryHandle(dir, { create: true });

    const files = [];
    let done = 0, lastPost = 0;
    for (const item of chosen.items) {
      if (item.empty) { files.push({ name: item.name, data: new File([], item.name.split('/').pop()) }); continue; }
      const report = current => {
        const now = Date.now();
        if (now - lastPost < 120) return;
        lastPost = now;
        postMessage({ progress: { done: done + current, total: chosen.size, name: item.name } });
      };
      const sink = await opfsSink(root, item.name);
      await item.entry.getData(sink.writable, { onprogress: report });
      files.push({ name: item.name, data: await sink.file() });
      done += item.size;
    }
    postMessage({ progress: { done: chosen.size, total: chosen.size, name: '' } });
    await reader.close();
    postMessage({ done: { files } });
  } catch (error) {
    const quota = error && error.name === 'QuotaExceededError';
    postMessage({ error: quota ? 'O disco do navegador encheu durante a descompactação. Libere espaço ou extraia o .zip e escolha a pasta data.'
      : String((error && error.message) || error) });
  }
};
