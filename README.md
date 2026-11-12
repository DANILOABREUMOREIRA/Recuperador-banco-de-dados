# Recuperador MySQL InnoDB - versão navegador

Recupera tabelas **InnoDB** de um banco MySQL 5.x (pasta `data` ou `.zip`) direto no navegador, sem instalar
nada e sem servidor. Os arquivos são lidos no computador do usuário: nada é enviado para a internet.

É o "modo de leitura direta" do programa de desktop (método TwinDB undrop-for-innodb), rodando em Python dentro do
navegador pelo [Pyodide](https://pyodide.org).

## Como usar

1. Abra o site.
2. Solte na tela, ou clique para escolher, um **`.zip`** ou a pasta **`data`** do MySQL (a que tem o `ibdata1`). A
   pasta também pode ser escolhida pelo link *ou escolher a pasta data*. Banco em `.rar`: extraia antes.
3. Dê um nome e clique em **Recuperar**. No Chrome e no Edge você escolhe onde salvar o `.sql` e ele é gravado direto no
   disco. Nos outros navegadores o dump é guardado no disco do navegador e baixado no fim (pede espaço livre extra).
4. Importe o `.sql` num MySQL: `mysql -uroot -p < banco_recuperado.sql`

## Interface

É a do programa de desktop: barra escura de ícones (Recuperar, Downloads, Log completo, Como funciona?), painel de
tabelas por status, estante de livros com o andamento geral e duas barras (a etapa e o item em curso). O botão de lua
e sol, no pé da barra, alterna entre o modo claro e o escuro. Ficou de fora o que depende do Windows ou de um servidor
MySQL: Query Browser, *Abrir pasta*, *Salvar em* e a pasta `data` nova na saída. As recuperações anteriores ficam
gravadas no próprio navegador.

## Meta: bancos de até 20 GB

Regra do projeto: só entra o que escala até 20 GB. O que depende de ler tudo na memória do navegador (2 a 4 GB) não
entra.

| Entrada | Como é lida | Escala? |
|---|---|---|
| Pasta `data` | Os arquivos são lidos direto do disco, aos pedaços, sem copiar | Sim |
| `.zip` | [zip.js](https://gildas-lormeau.github.io/zip.js/) lê o arquivo aos pedaços e grava no disco privado do navegador (OPFS) só o que a leitura precisa; tudo é apagado no fim | Sim, se houver espaço livre (a conferência avisa antes) |
| `.rar` | O formato obriga a ler o arquivo inteiro na memória | **Não: removido.** Extraia antes e escolha a pasta |

| Saída | Como é gravada | Escala? |
|---|---|---|
| `.sql` no Chrome e no Edge | Direto no arquivo escolhido, tabela por tabela | Sim |
| `.sql` nos outros navegadores | Guardado no disco do navegador (OPFS) e baixado no fim; pede o espaço do dump a mais | Sim, com espaço livre |
| Dump na memória | — | **Não: removido** |

Do `.zip` só entram o `ibdata1`, os `.frm`, `.ibd` e `.TRG`. Os `.MYD`/`.MYI` fora de `mysql/` viram arquivos vazios
(só o nome importa). Arquivo com senha é recusado com uma mensagem.

### O que ainda impede 20 GB

- **Memória por tabela.** O motor lê todas as linhas de uma tabela antes de gerar o SQL. Medido: o pico é de **5 a 9
  vezes** o tamanho da tabela em páginas (`nfcexml`: 38 MB de páginas, pico de 196 MB; `produto`: 12,6 MB, pico de
  118 MB). Com cerca de 2 GB úteis no Python do navegador, uma tabela com mais de uns 300 MB de páginas não cabe. Num
  banco de 20 GB isso é provável. A saída é ler a tabela em lotes, sem juntar tudo; ainda não foi feita.
- **Espaço em disco.** O `.zip` precisa do banco descompactado no disco do navegador, mais o dump (cerca de 0,55 vez o
  tamanho) fora do Chrome e do Edge.
- **Tempo.** Cerca de 2 vezes o do desktop; por extrapolação, perto de 1h40 para 20 GB. Não medido.

## O que faz

- Lê o dicionário interno do InnoDB (`SYS_TABLES`, `SYS_COLUMNS`, `SYS_INDEXES`, `SYS_FIELDS`) e os registros das
  páginas do `ibdata1` ou dos `.ibd` (formatos COMPACT, REDUNDANT e DYNAMIC).
- Aplica o doublewrite buffer: página rasgada é lida pela cópia íntegra.
- Estrutura (`CREATE TABLE`) pelos `.frm` e pelo dicionário; tabelas com `.frm` mas sem linha no dicionário são
  procuradas pelas páginas.
- Chaves estrangeiras (de `SYS_FOREIGN`), views e triggers (dos arquivos da pasta).
- Relatório por tabela: registros, avisos de registros ilegíveis e de páginas perdidas, SHA-256 do dump.

## O que não faz

Use o programa de desktop para estes casos:

- gerar uma pasta `data` nova: isso exige um servidor MySQL para importar o dump e gravar os arquivos do InnoDB, e o
  navegador não tem um (aqui sai só o `.sql`);
- tabelas MyISAM, rotinas e eventos (só o servidor MySQL os lê);
- `CHECK TABLE` e conferência de linhas órfãs no banco novo;
- tabelas `ROW_FORMAT=COMPRESSED`, páginas diferentes de 16 KB, MySQL 8.0;
- entrada `.sql`.

Limitação do método: registros apagados há pouco tempo podem reaparecer (páginas antigas já liberadas).

## Limites conhecidos

- Veja *O que ainda impede 20 GB*, acima: a memória por tabela é o limite de hoje.
- Testado com `ibdata1` de 210 MB (pasta e `.zip`) e de 578 MB (pasta). Acima disso ainda não foi testado.
- Cerca de 2 vezes mais lento que o Python do desktop.
- O Pyodide vem de `cdn.jsdelivr.net`: precisa de internet para abrir o site.

## Publicar

Site estático, sem build. Na Vercel: **Add New > Project**, importe este repositório, *Framework Preset* **Other**,
sem comando de build e sem pasta de saída. No GitHub Pages: publique a raiz do repositório.

## Estrutura

```
index.html            tela: a mesma interface do programa de desktop (3 colunas, estante de livros, duas barras)
static/app.css        visual, com modo claro e modo escuro (variáveis de cor em :root e :root[data-theme="dark"])
static/app.js         lógica da tela: banco, nome, andamento, tabelas, histórico (neste navegador), relatório
static/select.js      escolhe quais arquivos entram (acha o ibdata1, filtra logs e caminhos suspeitos)
static/extract.js     descompacta o .zip e limpa o disco do navegador depois
static/theme-init.js  escolhe o tema antes de a página aparecer (o gravado, ou o do sistema)
static/*.png, *.svg   ícone dos óculos (aba do navegador) e logo da barra; favicon.ico na raiz
zip-worker.js         lê o .zip aos pedaços e grava no OPFS
worker.js             liga o Pyodide, monta os arquivos (WORKERFS) e grava o dump
vendor/zip            zip.js 2.24.0 (BSD-3-Clause)
py/web_driver.py      orquestra a leitura direta e monta o dump em pedaços
py/innodb_carver.py   leitura das páginas do InnoDB          (cópia do desktop)
py/page_health.py     checksum, página rasgada, doublewrite  (cópia do desktop)
py/frm_parser.py      .frm, views e triggers                 (cópia do desktop)
py/dictionary_rebuild.py  tabelas sem linha no dicionário    (cópia do desktop)
py/mysql_codec.py     DECIMAL/DATE/TIME/DATETIME             (cópia do desktop)
dev/dev_server.py     servidor local de teste
```

Os cinco módulos marcados como cópia vêm de `programa/sistema/` do programa de desktop, sem alteração. Ao mudar o
motor lá, copie-os de novo para `py/`.

## Testar

O mesmo `web_driver.py` roda no Python comum e gera o mesmo dump que o navegador:

```
python py/web_driver.py <pasta data> saida.sql relatorio.json
```

Teste automático no navegador (compara o SHA-256 do arquivo com o que o Python calculou):

```
python dev/dev_server.py <pasta data> [arquivo .zip]
```

Abra `http://127.0.0.1:8765/?auto=1` para a pasta ou `?auto=archive` para o .zip (use `-` no lugar da pasta se só
quiser o arquivo). O resultado fica em `dev/resultado_navegador.json`; o campo `sha256` tem de ser igual ao do relatório
gerado pelo Python comum.

## Licença

GPL-2.0 (arquivo `LICENSE`). `py/innodb_carver.py` porta a lógica de formato do
[TwinDB undrop-for-innodb](https://github.com/twindb/undrop-for-innodb), que é GPL-2.0. As bibliotecas em `vendor/`
mantêm as licenças delas (arquivos `LICENSE` em cada pasta).
