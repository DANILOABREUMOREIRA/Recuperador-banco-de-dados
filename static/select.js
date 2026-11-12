// select.js - decide quais arquivos de uma pasta ou de um .zip/.rar entram na recuperação.
// Módulo compartilhado: a tela (app.js) usa para pastas e o descompactador (extract.js, zip-worker.js) para arquivos.

// Não servem para a leitura direta: os logs do InnoDB, .err e .pid.
export const SKIPPED = /^(ib_logfile\d+|.*\.(err|pid))$/i;
// MyISAM: o motor só precisa saber que a tabela existe (o nome do arquivo); o conteúdo é lido só pelo servidor MySQL.
const MYISAM_DATA = /\.(MYD|MYI)$/i;

// entries = [{path, size, ...}] com caminhos separados por "/" e sem diretórios. A pasta escolhida pode ser a própria
//   data ou uma pasta acima dela: acha o ibdata1 (o maior, se houver vários) e usa a pasta dele como raiz. Só entram os
//   arquivos da raiz e das pastas dos bancos (profundidade até 2).
// emptyMyisam = true (descompactação): .MYD e .MYI fora de mysql/ viram arquivos vazios, para não gastar disco à toa.
// Devolve {items: [{...entry, name, empty}], ibdata, copies, size} ou null se não há ibdata1.
export function selectData(entries, { emptyMyisam = false } = {}) {
  const roots = entries.filter(e => e.path.split('/').pop() === 'ibdata1').sort((a, b) => b.size - a.size);
  if (!roots.length) return null;
  const prefix = roots[0].path.slice(0, -'ibdata1'.length);
  const items = entries.filter(e => e.path.startsWith(prefix))
    .map(e => ({ ...e, name: e.path.slice(prefix.length) }))
    .filter(e => e.name.split('/').length <= 2 && !SKIPPED.test(e.name.split('/').pop()))
    // Caminho suspeito (zip slip): ".." ou segmento vazio nunca entra.
    .filter(e => e.name.split('/').every(part => part !== '' && part !== '.' && part !== '..'))
    .map(e => ({ ...e, empty: emptyMyisam && MYISAM_DATA.test(e.name) && !/^mysql\//i.test(e.name) }));
  return { items, ibdata: roots[0], copies: roots.length, size: items.reduce((sum, e) => sum + (e.empty ? 0 : e.size), 0) };
}
