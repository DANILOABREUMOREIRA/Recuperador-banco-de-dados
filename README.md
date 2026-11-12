# Recuperador MySQL InnoDB - versão navegador

Recupera tabelas **InnoDB** de uma pasta `data` do MySQL 5.x direto no navegador, sem instalar nada e sem servidor.
Os arquivos são lidos no computador do usuário: nada é enviado para a internet.

É o "modo de leitura direta" do programa de desktop (método TwinDB undrop-for-innodb), rodando em Python dentro do
navegador pelo [Pyodide](https://pyodide.org).

## Como usar

1. Abra o site.
2. Clique em **Escolher pasta data** e aponte para a pasta `data` do MySQL (a que tem o `ibdata1`).
   Banco em `.rar` ou `.zip`: extraia antes.
3. Clique em **Recuperar**. No Chrome e no Edge você escolhe onde salvar o `.sql` e ele é gravado direto no disco.
   No Firefox e no Safari o dump fica na memória e é baixado no fim (só para bancos pequenos).
4. Importe o `.sql` num MySQL: `mysql -uroot -p < banco_recuperado.sql`

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

- gerar uma pasta `data` nova (aqui sai só o `.sql`);
- tabelas MyISAM, rotinas e eventos (só o servidor MySQL os lê);
- `CHECK TABLE` e conferência de linhas órfãs no banco novo;
- tabelas `ROW_FORMAT=COMPRESSED`, páginas diferentes de 16 KB, MySQL 8.0;
- entrada `.sql`, `.rar` ou `.zip`.

Limitação do método: registros apagados há pouco tempo podem reaparecer (páginas antigas já liberadas).

## Limites conhecidos

- Cada tabela é montada inteira na memória antes de ir para o disco. O navegador dá de 2 a 4 GB ao Python: uma
  tabela sozinha com vários GB não cabe.
- Testado com `ibdata1` de 210 MB e 578 MB. Acima disso ainda não foi testado.
- Cerca de 2 vezes mais lento que o Python do desktop.
- O Pyodide vem de `cdn.jsdelivr.net`: precisa de internet para abrir o site.

## Publicar

Site estático, sem build. Na Vercel: **Add New > Project**, importe este repositório, *Framework Preset* **Other**,
sem comando de build e sem pasta de saída. No GitHub Pages: publique a raiz do repositório.

## Estrutura

```
index.html            tela (escolher pasta, andamento, resultado, relatório)
worker.js             liga o Pyodide, monta os arquivos (WORKERFS) e grava o dump
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
python dev/dev_server.py <pasta data>
```

e abra `http://127.0.0.1:8765/?auto=1`. O resultado fica em `dev/resultado_navegador.json`; o campo `sha256` tem de
ser igual ao do relatório gerado pelo Python comum.

## Licença

GPL-2.0 (arquivo `LICENSE`). `py/innodb_carver.py` porta a lógica de formato do
[TwinDB undrop-for-innodb](https://github.com/twindb/undrop-for-innodb), que é GPL-2.0.
