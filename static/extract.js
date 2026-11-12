// extract.js - descompacta um .zip ou .rar no navegador e devolve os arquivos que a leitura direta precisa.
// .zip: zip-worker.js lê aos pedaços e grava no disco privado do navegador (OPFS): sem limite de memória.
// .rar: libarchive.js (WebAssembly) lê o arquivo inteiro na memória e extrai um por um; serve para bancos de até uns
//   2 GB (o arquivo e o maior item precisam caber na memória do WebAssembly).
// Em ambos, o que voltou deve ser apagado com cleanup(dir) depois da recuperação.
import { selectData } from './select.js';

const DIR_PREFIX = 'rm-';
export const newDir = () => DIR_PREFIX + Date.now();

// Apaga a pasta temporária do OPFS (tudo o que foi descompactado). Sem OPFS ou sem a pasta, não faz nada.
export async function cleanup(dir) {
  try { await (await navigator.storage.getDirectory()).removeEntry(dir, { recursive: true }); } catch (e) { /* já foi */ }
}
// Ao abrir a página: apaga o que uma execução anterior deixou (fechada no meio). Só as pastas com mais de 1 hora, para
//   não mexer no que outra aba ainda usa.
export async function cleanupLeftovers() {
  try {
    const root = await navigator.storage.getDirectory();
    for await (const [name] of root.entries()) {
      const stamp = Number(name.slice(DIR_PREFIX.length));
      if (name.startsWith(DIR_PREFIX) && Date.now() - stamp > 3600000) await root.removeEntry(name, { recursive: true });
    }
  } catch (e) { /* sem OPFS */ }
}

// hooks: {log(texto), progress({done, total, name}), phase(texto)}. Devolve {files: [{name, data: File}], mode, dir}.
export async function extractArchive(file, dir, hooks) {
  if (/\.zip$/i.test(file.name)) return extractZip(file, dir, hooks);
  if (/\.rar$/i.test(file.name)) return extractRar(file, hooks);
  throw new Error('Formato não suportado. Use .rar, .zip ou a pasta data.');
}

function extractZip(file, dir, hooks) {
  return new Promise((resolve, reject) => {
    const worker = new Worker(new URL('../zip-worker.js', import.meta.url), { type: 'module' });
    worker.onerror = event => { worker.terminate(); reject(new Error(event.message || 'Não consegui abrir o .zip.')); };
    worker.onmessage = ({ data }) => {
      if (data.log) hooks.log(data.log);
      else if (data.progress) hooks.progress(data.progress);
      else if (data.error) { worker.terminate(); reject(new Error(data.error)); }
      else if (data.done) { worker.terminate(); resolve({ ...data.done, dir }); }
    };
    worker.postMessage({ file, dir });
    hooks.cancel = () => worker.terminate();
  });
}

async function extractRar(file, hooks) {
  const { Archive } = await import('../vendor/libarchive/libarchive.js');
  hooks.phase('Lendo o .rar (o arquivo inteiro vai para a memória)...');
  let archive;
  try { archive = await Archive.open(file); }
  catch (error) { throw new Error('Não consegui abrir o .rar: ' + ((error && error.message) || error)); }
  hooks.cancel = () => archive.close();
  try {
    if (await archive.hasEncryptedData()) throw new Error('O .rar tem senha: extraia-o antes e escolha a pasta data.');
    const listed = await archive.getFilesArray();
    const found = listed.map(({ file: item, path }) => ({ path: (path + item.name).replace(/\\/g, '/'), raw: path + item.name, size: item.size }));
    const chosen = selectData(found, { emptyMyisam: true });
    if (!chosen) throw new Error('O .rar não tem o arquivo ibdata1: não parece uma pasta data do MySQL.');
    if (chosen.copies > 1) hooks.log('Havia mais de um ibdata1 no .rar: usei o maior.');
    hooks.log(`.rar: ${found.length} arquivos; vão ser lidos ${chosen.items.length} (${(chosen.size / 1048576).toFixed(1).replace('.', ',')} MB).`);
    // Uma passada só pelo arquivo, pegando todos os itens de uma vez (num .rar sólido, pedir um por um esgota a memória).
    // O .rar não informa o andamento dentro da passada: a barra corre sem medida.
    hooks.progress({ done: 0, total: chosen.size, name: '' });
    const wanted = chosen.items.filter(item => !item.empty);
    const extracted = await archive.client.extractSelected(wanted.map(item => item.raw));
    const byPath = new Map(extracted.map(entry => [entry.path, entry]));
    const files = [];
    for (const item of chosen.items) {
      if (item.empty) { files.push({ name: item.name, data: new File([], item.name.split('/').pop()) }); continue; }
      const entry = byPath.get(item.raw);
      if (!entry) throw new Error('O .rar não entregou o arquivo ' + item.name + '.');
      files.push({ name: item.name, data: new File([entry.fileData], item.name.split('/').pop()) });
      byPath.delete(item.raw);   // solta a cópia da lista assim que o File existe
    }
    hooks.progress({ done: chosen.size, total: chosen.size, name: '' });
    return { files, mode: 'memory', dir: null };
  } finally {
    archive.close();
  }
}
