// zip-worker.js - abre um .zip e grava no disco do navegador (OPFS) só o que a leitura direta precisa: o ibdata1 e os
// .frm, .ibd e .TRG. O .zip é lido aos pedaços (BlobReader) e cada arquivo vai para o disco em fluxo, então o tamanho
// do banco não é limitado pela memória. Sem OPFS (navegador antigo) cai para a memória, que serve só para bancos pequenos.
// Recebe { file, dir }; avisa {log}, {progress}, {done: {files, mode}} ou {error}.
import * as zip from './vendor/zip/zip.min.js';
import { selectData } from './static/select.js';

zip.configure({ useWebWorkers: false });   // este já é um worker

const say = log => postMessage({ log });
const mb = bytes => (bytes / 1048576).toFixed(1).replace('.', ',') + ' MB';

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
    const { file, dir } = data;
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
    say(`.zip: ${found.length} arquivos; vão ser gravados ${chosen.items.length} (${mb(chosen.size)}), ibdata1 de ${mb(chosen.ibdata.size)}.`);

    let root = null;
    try {
      root = await (await navigator.storage.getDirectory()).getDirectoryHandle(dir, { create: true });
      const room = await navigator.storage.estimate();
      if (room.quota && room.quota - room.usage < chosen.size * 1.02) {
        throw new Error(`Falta espaço no navegador: o banco descompactado tem ${mb(chosen.size)} e há ${mb(room.quota - room.usage)} livres. ` +
          'Libere espaço no disco ou extraia o .zip e escolha a pasta data.');
      }
    } catch (error) {
      if (/Falta espaço/.test(error.message)) throw error;
      root = null;   // sem OPFS: usa a memória
      say('Este navegador não tem disco privado (OPFS): descompactando na memória, só serve para bancos pequenos.');
    }

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
      if (root) {
        const sink = await opfsSink(root, item.name);
        await item.entry.getData(sink.writable, { onprogress: report });
        files.push({ name: item.name, data: await sink.file() });
      } else {
        files.push({ name: item.name, data: await item.entry.getData(new zip.BlobWriter(), { onprogress: report }) });
      }
      done += item.size;
    }
    postMessage({ progress: { done: chosen.size, total: chosen.size, name: '' } });
    await reader.close();
    postMessage({ done: { files, mode: root ? 'opfs' : 'memory' } });
  } catch (error) {
    postMessage({ error: String((error && error.message) || error) });
  }
};
