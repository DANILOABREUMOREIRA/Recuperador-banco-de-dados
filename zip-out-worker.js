// zip-out-worker.js - compacta o dump (.sql) num .zip em fluxo: o .sql é lido aos pedaços e o .zip é gravado aos pedaços,
// então o tamanho do banco não é limitado pela memória (um dump de 20 GB não passa pela memória do navegador).
// Recebe { file: File (o .sql), sqlName, extras: [{name, text}], handle: FileSystemFileHandle | null, dir, prefix, raw }.
//   prefix: texto gravado ANTES do .zip (o .bat que se extrai sozinho: o arquivo é .bat e .zip ao mesmo tempo; o tar do Windows
//   acha o .zip depois do texto). raw: o `file` já é um .zip pronto (vários bancos): é só copiado depois do prefixo.
//   Com handle (Chrome e Edge) o .zip é gravado direto no arquivo que o usuário escolheu. Sem handle vai para o disco do
//   navegador (OPFS, pasta dir) e volta como File para a tela baixar.
// Avisa {progress: {done, total}}, {done: {file}} ou {error}.
import * as zip from './vendor/zip/zip.min.js';

zip.configure({ useWebWorkers: false });   // este já é um worker

// Um destino OPFS (createSyncAccessHandle existe em todos os navegadores com OPFS); devolve o File ao fechar.
async function opfsSink(dir) {
  const folder = await (await navigator.storage.getDirectory()).getDirectoryHandle(dir, { create: true });
  const handle = await folder.getFileHandle('dump.zip', { create: true });
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
    const { file, sqlName, extras, handle, dir, prefix, raw } = data;
    let sink = null, writable;
    if (handle) writable = await handle.createWritable();
    else {
      if (!navigator.storage || !navigator.storage.getDirectory) {
        throw new Error('Este navegador não tem disco privado (OPFS) para montar o .zip. Use o Chrome ou o Edge, ou baixe o .sql.');
      }
      sink = await opfsSink(dir);
      writable = sink.writable;
    }
    if (prefix) {
      const head = writable.getWriter();
      await head.write(new TextEncoder().encode(prefix));
      head.releaseLock();
    }
    if (raw) {                                  // o .zip de vários bancos já está pronto: copia aos pedaços
      const out = writable.getWriter();
      const reader = file.stream().getReader();
      let done = 0, lastRaw = 0;
      for (;;) {
        const { value, done: end } = await reader.read();
        if (end) break;
        await out.write(value);
        done += value.byteLength;
        const now = Date.now();
        if (now - lastRaw > 250) { lastRaw = now; postMessage({ progress: { done, total: file.size } }); }
      }
      await out.close();
      postMessage({ done: { file: sink ? await sink.file() : null } });
      return;
    }
    const writer = new zip.ZipWriter(writable, { zip64: file.size >= 2 ** 31 });
    // Os textos pequenos primeiro: se for cancelado no meio, o que já entrou continua legível.
    for (const extra of extras) await writer.add(extra.name, new zip.TextReader(extra.text));
    let lastPost = 0;
    await writer.add(sqlName, new zip.BlobReader(file), {
      onprogress: (done, total) => {
        const now = Date.now();
        if (now - lastPost > 250 || done === total) { lastPost = now; postMessage({ progress: { done, total } }); }
      },
    });
    await writer.close();
    postMessage({ done: { file: sink ? await sink.file() : null } });
  } catch (error) {
    const quota = error && error.name === 'QuotaExceededError';
    postMessage({ error: quota ? 'O disco do navegador encheu ao montar o .zip. Libere espaço ou use o Chrome ou o Edge, que gravam direto no arquivo escolhido.'
      : 'Não consegui montar o .zip: ' + (error && error.message || error) });
  }
};
