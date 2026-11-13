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

### Tabelas grandes: leitura em lotes

O leitor de páginas do desktop (`carve()`) junta todas as linhas de uma tabela antes de gerar o SQL. Medido: o pico de
memória é de **5 a 9 vezes** o tamanho da tabela em páginas, e o Python do navegador tem cerca de 2 GB. Por isso, a
tabela com **mais de 64 MB de páginas** (4096 folhas de 16 KB) é lida por `py/stream_carver.py`:

- parte da página **raiz** e desce pelos nós internos, como o MySQL faz, lendo só as folhas da árvore viva, na ordem da
  chave, em lotes de até 4000 linhas ou 8 MB; cada lote vai para o disco antes do próximo;
- folha que nenhum nó vivo cita é página liberada e não é lida. Isso difere do `carve()`, que também devolve linhas de
  páginas liberadas ("registros apagados que reaparecem"). Conferido: em `cclasstrib` o MySQL confirma 155 linhas
  íntegras e a leitura pela árvore devolve 155; a leitura por cadeia de folhas devolvia 530, por seguir cadeias antigas;
- se a árvore está danificada (raiz ou nó interno ilegível), cai para as cadeias de folhas ligadas nos dois sentidos, que
  recuperam mais mas podem trazer páginas antigas; o relatório diz qual modo foi usado.

As tabelas menores seguem no `carve()`, com o comportamento de sempre (o dump de `dataoriginal` continua com o mesmo
SHA-256).

**Medido** (navegador, banco de 578 MB, `nfcexml` com 177 MB de páginas): memória do Python 781 MB com o método antigo e
**258 MB** em lotes, mesmas 949.532 linhas. Em todas as tabelas de dois bancos reais, a leitura em lotes devolveu o mesmo
conteúdo do `carve()`, exceto linhas que só existiam em páginas liberadas.

### Medido com um banco real de 13,4 GB (Python do desktop, não o navegador)

`ibdata1` de 13,4 GB, 248 tabelas, **22,6 milhões de linhas**, 4,4 GB de SQL: **pico de memória de 360 MB** (8 tabelas lidas em lotes; a maior,
`estoque`, tem 6,7 milhões de linhas) e cerca de 36 minutos. Depois da varredura do `ibdata1` o processo usava só 53 MB.

### O que ainda precisa de medida para 20 GB

- **O navegador com bancos grandes.** O experimento de 13 GB foi nativo. No navegador a memória do Python foi medida até 578 MB (pasta) e 210 MB
  (`.zip`); WORKERFS com arquivo de dezenas de GB e OPFS com cota grande não foram testados (o painel de teste tem cota de 2 GB).
- **Espaço em disco.** O `.zip` precisa do banco descompactado no disco do navegador, mais o dump (cerca de 0,55 vez o
  tamanho) fora do Chrome e do Edge.
- **Tempo no navegador.** Cerca de 2 vezes o do desktop nos bancos pequenos; para 20 GB seria perto de 1h30 a 2h. Não medido.

### Validação com corrupção de propósito

Numa **cópia** do banco de 13 GB foram aplicados 11 danos (folhas zeradas, páginas rasgadas, um nó interno da árvore, uma página do dicionário
e quatro `.frm`), com a perda de cada um prevista antes de zerar. Resultado final: 241 tabelas idênticas ao original, 4 com a perda exata prevista,
e a perda total de **9.703 de 22,6 milhões** de linhas (0,043%), todas explicadas (38 + 66 + 116 + 9.384 + 99). O experimento achou três defeitos,
já corrigidos:

1. **Uma página zerada da `SYS_TABLES` fazia 157 tabelas voltarem vazias.** `py/lost_tables.py` as acha de volta pelas colunas do `.frm`;
   as que perderam o `.frm` também saem como `_sem_nome_<id>` (conferido por impressão digital: o conteúdo é idêntico ao original).
2. **Dano silencioso:** uma tabela perdeu 99 linhas e o relatório dizia OK. Agora cada página é conferida (registros do cabeçalho × lidos,
   estrutura da página) e o relatório avisa.
3. **Escolha do banco por contagem de `.frm`:** um `.frm` apagado fez o banco irmão ser escolhido. Agora há aviso de "mais de um banco" e o
   parâmetro `database`.

## O que faz

- Lê o dicionário interno do InnoDB (`SYS_TABLES`, `SYS_COLUMNS`, `SYS_INDEXES`, `SYS_FIELDS`) e os registros das
  páginas do `ibdata1` ou dos `.ibd` (formatos COMPACT, REDUNDANT e DYNAMIC).
- Aplica o doublewrite buffer: página rasgada é lida pela cópia íntegra.
- Estrutura (`CREATE TABLE`) pelos `.frm` e pelo dicionário; tabelas com `.frm` mas sem linha no dicionário são
  procuradas pelas páginas (e pelo que sobrou de `SYS_COLUMNS` e `SYS_INDEXES`; ver *Validação com corrupção de propósito*).
- Avisa de páginas danificadas que não derrubam nada mas somem com registros, e quando a pasta tem mais de um banco.
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

- Veja *O que ainda precisa de medida para 20 GB*, acima.
- No navegador, testado com `ibdata1` de 210 MB (pasta e `.zip`) e de 578 MB (pasta); 13 GB só no Python do desktop.
- A recuperação leva **um banco** por vez; os outros da pasta não entram no dump (há aviso).
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
py/stream_carver.py   lê tabelas grandes em lotes, pela árvore viva (novo; o resto de py/ é cópia do desktop)
py/lost_tables.py     acha tabelas cuja linha em SYS_TABLES se perdeu (novo)
py/innodb_carver.py   leitura das páginas do InnoDB          (cópia do desktop)
py/page_health.py     checksum, página rasgada, doublewrite  (cópia do desktop)
py/frm_parser.py      .frm, views e triggers                 (cópia do desktop)
py/dictionary_rebuild.py  tabelas sem linha no dicionário    (cópia do desktop)
py/mysql_codec.py     DECIMAL/DATE/TIME/DATETIME             (cópia do desktop)
dev/dev_server.py     servidor local de teste
dev/digest_run.py     roda a recuperação e guarda contagem e impressão digital do conteúdo de cada tabela
dev/corromper.py      corrompe de propósito uma CÓPIA de uma pasta data e anota o resultado esperado
dev/comparar.py       compara original e cópia corrompida, tabela por tabela, contra o plano de danos
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
