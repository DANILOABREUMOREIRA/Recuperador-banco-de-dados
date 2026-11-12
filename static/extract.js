// extract.js - descompacta um .zip no navegador e devolve os arquivos que a leitura direta precisa.
// zip-worker.js lê o .zip aos pedaços e grava no disco privado do navegador (OPFS): o tamanho do banco não é limitado
// pela memória. O que voltou deve ser apagado com cleanup(dir) depois da recuperação.
// .rar não entra: o formato obriga a ler o arquivo inteiro na memória, que não aguenta bancos grandes.
export const DIR_PREFIX = 'rm-';

// Apaga uma pasta temporária do OPFS (o que foi descompactado ou o dump guardado). Sem OPFS ou sem a pasta, não faz nada.
export async function cleanup(dir) {
  try { await (await navigator.storage.getDirectory()).removeEntry(dir, { recursive: true }); } catch (e) { /* já foi */ }
}
// Ao abrir a página: apaga o que uma execução anterior deixou (fechada no meio). Só as pastas com mais de 1 hora, para
//   não mexer no que outra aba ainda usa.
export async function cleanupLeftovers() {
  try {
    const root = await navigator.storage.getDirectory();
    for await (const [name] of root.entries()) {
      const stamp = parseInt(name.slice(DIR_PREFIX.length), 10);
      if (name.startsWith(DIR_PREFIX) && Date.now() - stamp > 3600000) await root.removeEntry(name, { recursive: true });
    }
  } catch (e) { /* sem OPFS */ }
}

// hooks: {log(texto), progress({done, total, name}), cancel}. reserveRatio = quanto, em fração do tamanho descompactado,
//   a recuperação ainda vai gravar no disco do navegador depois (o dump, quando não há arquivo de destino), para conferir
//   o espaço de uma vez.
// Devolve {files: [{name, data: File}], dir}.
export function extractArchive(file, dir, hooks, reserveRatio = 0) {
  if (!/\.zip$/i.test(file.name)) {
    return Promise.reject(new Error(/\.rar$/i.test(file.name)
      ? 'O .rar não é lido aqui: extraia-o e escolha a pasta data, ou use o programa de desktop.'
      : 'Formato não suportado. Use .zip ou a pasta data.'));
  }
  return new Promise((resolve, reject) => {
    const worker = new Worker(new URL('../zip-worker.js', import.meta.url), { type: 'module' });
    worker.onerror = event => { worker.terminate(); reject(new Error(event.message || 'Não consegui abrir o .zip.')); };
    worker.onmessage = ({ data }) => {
      if (data.log) hooks.log(data.log);
      else if (data.progress) hooks.progress(data.progress);
      else if (data.error) { worker.terminate(); reject(new Error(data.error)); }
      else if (data.done) { worker.terminate(); resolve({ ...data.done, dir }); }
    };
    worker.postMessage({ file, dir, reserveRatio });
    hooks.cancel = () => worker.terminate();
  });
}
