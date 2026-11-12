# Alteração local no libarchive.js 2.0.2

`worker-bundle.js` foi alterado em dois pontos (o resto do pacote está como foi publicado):

1. `entries(e, r)`: `r` agora também pode ser um `Set` de caminhos (antes só um caminho).
2. Novo método `extractSelected(paths)`: extrai vários arquivos numa **única passada** pelo arquivo. O
   `extractSingleFile` reabre o arquivo a cada chamada sem fechar o anterior: num `.rar` sólido com centenas de
   arquivos a memória do WebAssembly se esgota e a extração falha com "Error".

Uso (em `static/extract.js`): `archive.client.extractSelected([...caminhos])`.
