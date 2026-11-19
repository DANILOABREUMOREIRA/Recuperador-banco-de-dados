"""
web_driver.py - Recuperação por LEITURA DIRETA (sem o MySQL), para rodar no navegador (Pyodide) ou no Python comum.

É o "modo de leitura direta" do programa de desktop (engine_extract._extract_all_direct), sem servidor nenhum:
estrutura pelos .frm e pelo dicionário do InnoDB, dados pelas páginas, chaves estrangeiras pelo SYS_FOREIGN, views e
triggers pelos arquivos da pasta. O dump sai em PEDAÇOS (cabeçalho, uma tabela por vez, rodapé) para nunca ficar
inteiro na memória: quem chama grava cada pedaço e pede o próximo.

Conferência no Python comum (gera o mesmo dump que o navegador):
    python web_driver.py <pasta data> <saida.sql> [relatorio.json]
"""
import hashlib
import json
import os
import re
import sys
import time

# O Python portátil do desktop ignora a pasta do script e acha os módulos na pasta do programa: aqui vale a destes arquivos.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import dictionary_rebuild
import frm_parser
import kernel_bridge
import kernel_paths
import load_format
from innodb_carver import CarverError, InnodbCarver, ddl_from_dictionary, rows_to_sql
from lost_tables import recover_lost_tables
from stream_carver import stream_table

# Mesmos valores do programa de desktop (engine_common.py / engine_objects.py), para o dump sair igual.
HIDDEN_SCHEMAS = {"mysql", "performance_schema", "information_schema", "test"}
VARIANT_SUFFIX = re.compile(r"_\d$")   # cópias do chkdsk: tabela_0, tabela_1
# O dump segue o padrão do backup do sistema (MySQL Administrator dump 1.4): é o que o MySQL Administrator e o Query Browser reconhecem
#   ao restaurar. Cabeçalho, comentários de cada tabela, DISABLE/ENABLE KEYS e rodapé são os mesmos dele.
MA_HEADER = (
    "-- MySQL Administrator dump 1.4\n"
    "--\n"
    "-- ------------------------------------------------------\n"
    "-- Server version\t5.5.38\n"
    "\n\n"
    "/*!40101 SET @OLD_CHARACTER_SET_CLIENT=@@CHARACTER_SET_CLIENT */;\n"
    "/*!40101 SET @OLD_CHARACTER_SET_RESULTS=@@CHARACTER_SET_RESULTS */;\n"
    "/*!40101 SET @OLD_COLLATION_CONNECTION=@@COLLATION_CONNECTION */;\n"
    "/*!40101 SET NAMES utf8 */;\n"
    "\n"
    "/*!40014 SET @OLD_UNIQUE_CHECKS=@@UNIQUE_CHECKS, UNIQUE_CHECKS=0 */;\n"
    "/*!40014 SET @OLD_FOREIGN_KEY_CHECKS=@@FOREIGN_KEY_CHECKS, FOREIGN_KEY_CHECKS=0 */;\n"
    "/*!40101 SET @OLD_SQL_MODE=@@SQL_MODE, SQL_MODE='NO_AUTO_VALUE_ON_ZERO' */;\n"
    "\n\n"
    "--\n"
    "-- Create schema {db}\n"
    "--\n"
    "\n"
    "CREATE DATABASE IF NOT EXISTS {db};\n"
    "USE {db};\n"
    "\n"
)
SQL_FOOTER = (
    "\n\n"
    "/*!40101 SET SQL_MODE=@OLD_SQL_MODE */;\n"
    "/*!40014 SET FOREIGN_KEY_CHECKS=@OLD_FOREIGN_KEY_CHECKS */;\n"
    "/*!40014 SET UNIQUE_CHECKS=@OLD_UNIQUE_CHECKS */;\n"
    "/*!40101 SET CHARACTER_SET_CLIENT=@OLD_CHARACTER_SET_CLIENT */;\n"
    "/*!40101 SET CHARACTER_SET_RESULTS=@OLD_CHARACTER_SET_RESULTS */;\n"
    "/*!40101 SET COLLATION_CONNECTION=@OLD_COLLATION_CONNECTION */;\n"
    "/*!40101 SET CHARACTER_SET_CLIENT=@OLD_CHARACTER_SET_CLIENT */;\n"
)


# FAZ: o cabeçalho do dump. O nome do banco vai sem aspas, como o MySQL Administrator escreve; com aspas só se tiver caractere especial.
def sql_header(db):
    name = db if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", db) else "`" + db.replace("`", "``") + "`"
    return MA_HEADER.format(db=name)


# FAZ: os comentários que o MySQL Administrator escreve em volta de cada tabela.
def table_open(name):
    return f"--\n-- Definition of table `{name}`\n--\n\n"


def data_open(name):
    return f"\n--\n-- Dumping data for table `{name}`\n--\n\n/*!40000 ALTER TABLE `{name}` DISABLE KEYS */;\n"


def data_close(name):
    return f"/*!40000 ALTER TABLE `{name}` ENABLE KEYS */;\n\n\n"
OBJECT_HEADER = "-- Objetos do banco (views, triggers, rotinas e eventos)\n"
# 4096 folhas de 16 KB = 64 MB. O pico de memória do carve() é de 5 a 9 vezes o tamanho da tabela: até 64 MB cabe com
#   folga nos 2 GB do navegador; acima disso a tabela é lida em lotes.
STREAM_PAGES = int(os.environ.get("RECUPERADOR_STREAM_PAGES", 4096))


UNNAMED_DB = "_sem_banco"   # tabelas achadas pelo dicionário sem .frm: o banco de origem é desconhecido


def list_databases(data_dir):
    """{banco: quantidade de .frm} dos bancos de usuário da pasta data (sem mysql, performance_schema, information_schema)."""
    found = {}
    for entry in sorted(os.listdir(data_dir)):
        path = os.path.join(data_dir, entry)
        if entry.lower() in HIDDEN_SCHEMAS - {"test"} or not os.path.isdir(path):
            continue
        frms = sum(1 for f in os.listdir(path) if f.lower().endswith(".frm"))
        if frms:
            found[entry] = frms
    return found


def prepare_all(data_dir, log=print):
    """Para recuperar VÁRIOS bancos da mesma pasta: abre o ibdata1 uma vez (a varredura é a parte cara) e roda a busca de
    tabelas perdidas olhando todos os bancos juntos (uma tabela sem nome só pode ser casada com um .frm de qualquer banco).
    Devolve (leitor, achadas {"banco/tabela": como}, [bancos a recuperar]); as tabelas sem banco ficam em UNNAMED_DB."""
    if not os.path.isfile(os.path.join(data_dir, "ibdata1")):
        raise RuntimeError("A pasta escolhida não tem o arquivo ibdata1.")
    databases = sorted(list_databases(data_dir))
    carver = InnodbCarver(os.path.join(data_dir, "ibdata1"), log=log, data_dir=data_dir).load()
    frm_columns = {}
    for db in databases:
        known = {k.split("/", 1)[1] for k in carver.tables if k.startswith(db.lower() + "/")}
        names = {f[:-4].lower() for f in os.listdir(os.path.join(data_dir, db)) if f.lower().endswith(".frm")}
        wanted = {n for n in names if n not in known}
        if wanted:
            for name, table in dictionary_rebuild.frm_candidates(data_dir, db, wanted=wanted).items():
                frm_columns[f"{db}/{name}"] = [c.name for c in table.columns]
    try:
        found = recover_lost_tables(carver, frm_columns, log=log, unnamed_db=UNNAMED_DB)
    except Exception as e:   # redundância: nunca derruba a recuperação
        found = {}
        log(f"AVISO: busca de tabelas perdidas falhou ({e}).")
    if any(k.startswith(UNNAMED_DB + "/") for k in carver.tables):
        databases.append(UNNAMED_DB)
    return carver, found, databases


def recovery_for(data_dir, db, carver, found, log=print, stream_pages=None, mode="sql"):
    """Recovery do banco `db` usando o leitor e as tabelas achadas de prepare_all()."""
    restored = {full.split("/", 1)[1]: how for full, how in found.items() if full.startswith(db + "/")}
    return Recovery(data_dir, log=log, stream_pages=stream_pages, database=db, carver=carver, restored=restored, mode=mode)


class Recovery:
    """Uso: plan() -> cabeçalho; table(i) para cada tabela -> pedaço do dump; finish() -> rodapé; report() -> resumo."""

    def __init__(self, data_dir, log=print, stream_pages=None, database=None, carver=None, restored=None, mode="sql"):
        self.data_dir, self.log = data_dir, log
        # mode "load": o formato rápido (load_format.py). begin_table/next_chunk entregam só as LINHAS (o texto que o LOAD DATA lê); a
        #   estrutura, os LOAD DATA, os índices e os objetos ficam guardados aqui e saem em load_files().
        self.load = mode == "load"
        self.struct, self.loads, self.alters, self.tail = [], [], [], b""
        self.load_meta = {}     # tabela -> (colunas, tipo de cada coluna, coluna AUTO_INCREMENT)
        # carver: leitor já carregado (a varredura do ibdata1 é a parte cara; vários bancos da mesma pasta a reaproveitam).
        # restored: o que a busca de tabelas perdidas (lost_tables) já achou, quando ela rodou antes, para todos os bancos.
        self.shared_carver, self.lost_done = carver, restored is not None
        self.database = database          # banco a recuperar; None = o com mais .frm
        self.other_databases = []
        self.restored = dict(restored or {})   # tabela -> como foi achada de volta (dicionário danificado)
        # Tabelas com mais folhas que isto são lidas em lotes (veja _is_big). RECUPERADOR_STREAM_PAGES troca o limite nos
        #   testes (0 = toda tabela em lotes).
        self.stream_pages = STREAM_PAGES if stream_pages is None else stream_pages
        self._parts = iter(())
        self.started = time.time()
        self.carver, self.db, self.folder = None, None, None
        self.tables = []        # [(nome, só_no_dicionário)]
        self.status = {}        # nome -> {"rows", "status", "note"}
        self.no_fk = {}         # nome minúsculo -> nome: tabelas cujo CREATE TABLE não trouxe as chaves estrangeiras
        self.warnings = []
        self.objects = {"views": 0, "triggers": 0, "chaves_estrangeiras": 0}
        self.sha, self.sql_bytes = hashlib.sha256(), 0
        self.on_pages = None    # opcional: função (páginas lidas, total) chamada durante a leitura de cada tabela
        self.debug = None       # DEBUG-PROVISORIO: função (texto) que recebe o registro técnico para a aba Depuração
        self._cur_stats, self._cur_how = {}, "-"

    def warn(self, message):
        self.warnings.append(message)
        self.log("AVISO: " + message)

    # Todo pedaço do dump passa por aqui: soma o tamanho e o SHA-256 (o mesmo no navegador e no Python comum).
    def _emit(self, data):
        self.sha.update(data)
        self.sql_bytes += len(data)
        return data

    # FAZ: escolhe o banco a recuperar: o pedido em `database`, ou, sem pedido, o do usuário com mais .frm (igual ao
    #   detect_database do desktop). Os outros bancos da pasta ficam em self.other_databases: o dump leva um banco só, e o
    #   plano avisa. A contagem de .frm não é à prova de dano (um .frm apagado pode fazer um banco irmão passar na frente),
    #   por isso o aviso e o parâmetro existem.
    def _detect_database(self):
        if self.database == UNNAMED_DB:
            return UNNAMED_DB
        found = list_databases(self.data_dir)
        if not found:
            raise RuntimeError("Nenhum banco de usuário com arquivos .frm foi encontrado na pasta escolhida.")
        wanted = next((name for name in found if self.database and name.lower() == self.database.lower()), None)
        if self.database and not wanted:
            raise RuntimeError(f"O banco {self.database} não existe na pasta (há: {', '.join(found)}).")
        best = wanted or max(found, key=lambda name: found[name])
        # Com o leitor compartilhado todos os bancos da pasta são recuperados, cada um no seu Recovery: não há "outros".
        self.other_databases = [] if self.shared_carver else [(name, count) for name, count in found.items() if name != best]
        return best

    def _view_files(self):
        views = {}
        for entry in sorted(os.listdir(self.folder)):
            if entry.lower().endswith(".frm"):
                with open(os.path.join(self.folder, entry), "rb") as f:
                    head = f.read(9)
                    if head == b"TYPE=VIEW":
                        views[entry[:-4]] = (head + f.read()).decode("utf8", errors="replace")
        return views

    def _is_myisam(self, table):
        names = {f.lower() for f in os.listdir(self.folder)}
        return table.lower() + ".myd" in names or table.lower() + ".myi" in names

    def plan(self):
        """Abre o ibdata1, escolhe o banco e lista as tabelas. Devolve o cabeçalho do dump."""
        if not os.path.isfile(os.path.join(self.data_dir, "ibdata1")):
            raise RuntimeError("A pasta escolhida não tem o arquivo ibdata1.")
        self.db = self._detect_database()
        if self.other_databases:
            others = ", ".join(f"{name} ({count} tabelas)" for name, count in self.other_databases)
            self.warn(f"Há mais de um banco na pasta. Foi recuperado {self.db}; não entraram no dump: {others}.")
        unnamed = self.db == UNNAMED_DB   # sem pasta: só o que o dicionário trouxe
        self.folder = None if unnamed else os.path.join(self.data_dir, self.db)
        self.views = {} if unnamed else self._view_files()
        view_names = {v.lower() for v in self.views}
        listed = [] if unnamed else sorted(f[:-4] for f in os.listdir(self.folder)
                                           if f.lower().endswith(".frm") and not f.startswith("#sql") and f[:-4].lower() not in view_names)
        lowered = {t.lower() for t in listed}
        tables = [t for t in listed if not (VARIANT_SUFFIX.search(t) and VARIANT_SUFFIX.sub("", t).lower() in lowered)]
        skipped = sorted(set(listed) - set(tables))
        if skipped:
            self.log(f"Cópias de chkdsk ignoradas ({len(skipped)}): {', '.join(skipped)}")

        # Sem o dicionário do ibdata1 não há como achar os dados: o erro sobe com a mensagem do leitor de páginas.
        self.carver = self.shared_carver or InnodbCarver(os.path.join(self.data_dir, "ibdata1"), log=self.log, data_dir=self.data_dir).load()
        prefix = self.db.lower() + "/"
        known = {k[len(prefix):] for k in self.carver.tables if k.startswith(prefix)}
        self._reconstruct_missing({t.lower() for t in tables} - known)
        # Depois da reconstrução: as tabelas achadas de volta (inclusive as sem nome) já estão no dicionário do leitor.
        known = {k[len(prefix):] for k in self.carver.tables if k.startswith(prefix)}
        dict_only = sorted(known - lowered)
        if dict_only:
            self.log(f"Tabelas sem .frm mas com dados no ibdata1 (lidas direto): {', '.join(dict_only)}")
        self.tables = [(t, False) for t in tables] + [(t, True) for t in dict_only]
        self.log(f"Banco {self.db}: {len(self.tables)} tabelas a extrair.")
        if self.debug:   # DEBUG-PROVISORIO
            self.debug(f"PLANO banco={self.db} tabelas={len(self.tables)} so_no_dicionario={len(dict_only)} restauradas={len(self.restored)} "
                       f"views={len(self.views)} formato={'LOAD DATA' if self.load else 'INSERT'}")
        return self._emit(sql_header(self.db).encode("utf8"))

    # FAZ: tabelas com .frm mas sem linha no dicionário: casa o .frm com as páginas (dictionary_rebuild). É
    #   redundância: uma falha aqui só gera aviso.
    def _reconstruct_missing(self, wanted):
        candidates = dictionary_rebuild.frm_candidates(self.data_dir, self.db, wanted=wanted) if wanted else {}
        # 1º: tabelas cuja linha só em SYS_TABLES se perdeu (SYS_COLUMNS e SYS_INDEXES continuam): casa pelas colunas do .frm.
        #   Roda mesmo sem nenhum .frm legível: as que sobrarem saem sem nome, em vez de se perderem.
        found = {}
        if not self.lost_done:
            try:
                found = recover_lost_tables(self.carver, {f"{self.db}/{n}": [c.name for c in t.columns] for n, t in candidates.items()},
                                            log=self.log, unnamed_db=self.db)
            except Exception as e:     # redundância: nunca derruba a recuperação
                self.warn(f"Busca de tabelas pela SYS_COLUMNS falhou ({e}).")
        for full, how in sorted(found.items()):
            name = full.split("/", 1)[1]
            self.log(f"  {name}: {how}.")
            self.restored[name] = how
        candidates = {n: t for n, t in candidates.items() if n not in self.restored}
        if not candidates:
            return
        self.log(f"{len(candidates)} tabela(s) têm .frm mas não constam no dicionário do ibdata1; procurando os dados.")
        try:
            matched, missed = dictionary_rebuild.reconstruct(self.carver, candidates, self.db, log=self.log)
        except Exception as e:
            self.warn(f"Reconstrução pelas páginas falhou ({e}); essas tabelas ficam sem dados.")
            return
        for name, how in sorted(matched.items()):
            self.log(f"  {name}: dados encontrados pelas páginas ({how}).")
            self.restored[name] = f"dados encontrados pelas páginas ({how})"
        for name, why in sorted(missed.items()):
            self.log(f"  {name}: dados não localizados ({why}).")

    # A tabela sai em PEDAÇOS: quem chama pede um de cada vez (begin_table + next_chunk) e grava, então uma tabela de
    #   GBs nunca fica inteira na memória. table() junta os pedaços, para quem não precisa disso (testes, linha de comando).
    def begin_table(self, index):
        """Começa a tabela de número `index`; os pedaços do dump vêm de next_chunk()."""
        self._parts = self._traced(index)

    # DEBUG-PROVISORIO: o mesmo que _table_parts, e no fim da tabela manda o resumo dela para self.debug.
    def _traced(self, index):
        started, before = time.time(), self.sql_bytes
        self._cur_stats, self._cur_how = {}, "-"
        yield from self._table_parts(index)
        self._dbg_table(index, started, before)

    def _dbg_table(self, index, started, bytes_before):
        if not self.debug:
            return
        name = self.tables[index][0]
        info, s = self.status.get(name, {}), self._cur_stats
        line = (f"TABELA {index + 1}/{len(self.tables)} {self.db}.{name} | {info.get('status', '?')} | linhas={info.get('rows', 0)} | "
                f"bytes={self.sql_bytes - bytes_before} inserts={s.get('inserts', 0)} maior_insert={s.get('max_insert', 0)} | "
                f"{self._cur_how} | {time.time() - started:.1f}s")
        extra = [f"{k}={s[k]}" for k in ("pages", "leaf_pages", "read_pages", "bad_records", "deleted", "duplicates", "damaged_pages",
                                         "records_missing", "loose_pages", "missing_leaves", "oversized") if s.get(k)]
        if s.get("mode"):
            extra.append(f"modo={s['mode']}")
        self.debug(line + (" | " + " ".join(extra) if extra else ""))
        if info.get("note"):
            self.debug("    nota: " + info["note"])
        for key in ("bad_examples", "damaged_examples"):
            if s.get(key):
                self.debug(f"    {key}: " + "; ".join(s[key]))

    def next_chunk(self):
        """Próximo pedaço do dump da tabela em curso (bytes), ou None quando ela acabou."""
        return next(self._parts, None)

    def leaf_count(self, index):
        """Folhas (páginas de 16 KB) do índice da tabela `index`, 0 se não der para saber. Só serve para estimar o tempo."""
        try:
            tdef = self.carver.table(f"{self.db}/{self.tables[index][0]}")
            if not tdef:
                return 0
            ts = self.carver.table_space(tdef)
            return len(ts.leaf_pages.get(self.carver.clustered_id(tdef, ts), []))
        except Exception:   # estimativa: nunca derruba a recuperação
            return 0

    def table(self, index):
        """O dump inteiro da tabela de número `index` (DROP + CREATE + INSERTs), de uma vez."""
        return b"".join(self._table_parts(index))

    # Tabela com mais folhas que isto (64 MB de páginas por padrão) é lida em lotes (stream_carver); as menores continuam
    #   no carve() de sempre, que também recupera registros de páginas liberadas.
    def _is_big(self, tdef):
        try:
            ts = self.carver.table_space(tdef)
            return len(ts.leaf_pages.get(self.carver.clustered_id(tdef, ts), [])) > self.stream_pages
        except CarverError:
            return False   # o carve() levanta o mesmo erro e o relatório mostra

    def _table_parts(self, index):
        name, dict_only = self.tables[index]
        full = f"{self.db}/{name}"
        if not dict_only and self._is_myisam(name):
            self._cur_how = "myisam"
            self.status[name] = {"rows": 0, "status": "NAO_LIDA",
                                 "note": "MyISAM: só o servidor MySQL lê esta tabela; não entrou no dump."}
            self.warn(f"Tabela {name}: MyISAM, não lida (use o programa de desktop para ela).")
            return
        hint = None if dict_only else frm_parser.read_table_ddl(os.path.join(self.folder, name + ".frm"), name)
        tdef = self.carver.table(full)
        if not tdef:
            # Sem dados nas páginas: recria só a estrutura (se o .frm é legível), com aviso.
            if not hint:
                # Se o dicionário perdeu a linha dela, os dados podem ter saído numa tabela sem nome (lost_tables).
                spare = sorted(n for n in self.restored if n.startswith("_sem_nome_"))
                where = f" Os dados podem estar em {', '.join(spare)}." if spare else ""
                self.status[name] = {"rows": 0, "status": "NAO_LIDA",
                                     "note": "Sem estrutura legível (.frm inválido e fora do dicionário); não recriada." + where}
                self.warn(f"Tabela {name}: sem estrutura legível; ignorada.{where}")
                return
            self.no_fk[name.lower()] = name
            self.status[name] = {"rows": 0, "status": "AVISO",
                                 "note": "Estrutura recriada pelo .frm, mas os dados não foram localizados nas páginas."}
            self.warn(f"Tabela {name}: estrutura recriada, dados não localizados.")
            yield self._structure(name, f"DROP TABLE IF EXISTS `{name}`;\n{hint};\n\n\n" if self.load else
                                  table_open(name) + f"DROP TABLE IF EXISTS `{name}`;\n{hint};\n" + data_open(name) + data_close(name))
            return

        ddl = ddl_from_dictionary(tdef, hint)
        rows, problem, info, stats = 0, None, "", {}
        blobs_before = getattr(self.carver, "external_damaged", 0)
        big = self._is_big(tdef)
        self._cur_how = ("grande" if big else "pequena") + "/" + ("kernel" if kernel_bridge.available() and tdef.compact else "python") + \
            ("/so_dicionario" if dict_only else "")
        if big:
            structure = f"DROP TABLE IF EXISTS `{name}`;\n{ddl};\n"
            yield self._structure(name, structure if self.load else table_open(name) + structure + data_open(name), ddl)
            stats = {}
            try:
                if kernel_bridge.available():
                    # no navegador, o kernel WebAssembly converte as páginas e já entrega o texto SQL
                    for columns, count, pieces in kernel_paths.stream_sql(self.carver, full, ddl_hint=hint, progress=self.on_pages, stats=stats,
                                                                          lines=True):
                        rows += count
                        if self.load:
                            self._load_columns(name, tdef, columns, ddl)
                            yield self._emit(b"".join(text for _, text in pieces))
                        else:
                            yield self._emit(kernel_paths.inserts(name, columns, pieces, stats) + b"\n")
                else:
                    for columns, records in stream_table(self.carver, full, ddl_hint=hint, progress=self.on_pages, stats=stats):
                        rows += len(records)
                        if self.load:
                            self._load_columns(name, tdef, columns, ddl)
                            yield self._emit(load_format.rows_to_lines(records))
                        else:
                            yield self._emit(rows_to_sql(name, columns, records, stats=stats) + b"\n")
                if stats.get("bad_records"):
                    problem = f"{stats['bad_records']} registros ilegíveis descartados"
                if stats.get("damaged_pages"):
                    missing = f"; faltam ao menos {stats['records_missing']} registros" if stats["records_missing"] else ""
                    problem = "; ".join(x for x in (problem, f"{stats['damaged_pages']} página(s) danificadas ({stats['damaged_examples'][0]}){missing}") if x)
                lost = self.carver.lost_leaf_pages(full)
                if lost:
                    problem = "; ".join(x for x in (problem, f"ao menos {len(lost)} página(s) da tabela perdida(s)") if x)
                if stats.get("mode") == "cadeias":
                    problem = "; ".join(x for x in (problem, f"árvore danificada ({stats.get('reason')}): lida pelas cadeias de folhas, "
                                                              "pode incluir registros de páginas antigas") if x)
            except (CarverError, OSError) as e:
                problem = f"leitura direta falhou: {e}"
            info = f"tabela grande, lida em lotes pela cadeia de folhas ({stats.get('leaf_pages', 0):,} páginas)".replace(",", ".")
            if stats.get("loose_pages"):
                info += f"; {stats['loose_pages']} página(s) fora da árvore (liberadas) não lidas"
            if not self.load:
                yield self._emit(data_close(name).encode("utf8"))
        else:
            data = b""
            try:
                if kernel_bridge.available():
                    columns, count, pieces, stats = kernel_paths.carve_sql(self.carver, full, ddl_hint=hint, progress=self.on_pages)
                    data, rows = (b"".join(text + b"\n" for _, text in pieces) if self.load else kernel_paths.inserts(name, columns, pieces, stats)), count
                else:
                    columns, records, stats = self.carver.carve(full, ddl_hint=hint, progress=self.on_pages)
                    data, rows = (load_format.rows_to_lines(records) if self.load else rows_to_sql(name, columns, records, stats=stats)), len(records)
                if self.load:
                    self._load_columns(name, tdef, columns, ddl)
                if stats["bad_records"]:
                    problem = f"{stats['bad_records']} registros ilegíveis descartados"
                health = self.carver.tree_health(full)
                if health and health["ruins"]:
                    problem = "; ".join(x for x in (problem, f"{health['ruins']} folha(s) com a estrutura danificada") if x)
                lost = self.carver.lost_leaf_pages(full)
                if lost:
                    problem = "; ".join(x for x in (problem, f"ao menos {len(lost)} página(s) da tabela perdida(s)") if x)
            except (CarverError, OSError) as e:
                problem = f"leitura direta falhou: {e}"
            if self.load:
                self._structure(name, f"DROP TABLE IF EXISTS `{name}`;\n{ddl};\n", ddl)
                yield self._emit(data)
            else:
                yield self._emit((table_open(name) + f"DROP TABLE IF EXISTS `{name}`;\n{ddl};\n" + data_open(name)).encode("utf8")
                                 + (data + b"\n" if data else b"") + data_close(name).encode("utf8"))
        self._cur_stats = stats
        cut = getattr(self.carver, "external_damaged", 0) - blobs_before
        if cut:
            problem = "; ".join(x for x in (problem, f"{cut} valor(es) longo(s) (text/blob) incompleto(s): páginas de BLOB danificadas") if x)
        if stats.get("oversized"):
            problem = "; ".join(x for x in (problem, f"{stats['oversized']} linha(s) com mais de 1 MB: para importar, aumente o "
                                                     "max_allowed_packet do servidor MySQL (ex.: max_allowed_packet=64M no my.ini)") if x)
        if problem:
            self.warn(f"Tabela {name}: {problem}")
        self.no_fk[name.lower()] = name
        info = "; ".join(x for x in (self.restored.get(name), info) if x)
        self.status[name] = {"rows": rows, "status": "AVISO" if problem else "OK", "note": "; ".join(x for x in (problem, info) if x)}

    # FAZ: o texto de estrutura de uma tabela. No formato comum ele faz parte do dump (devolve os bytes a gravar). No rápido ele é guardado
    #   para o estrutura.sql, sem os índices secundários (que vão para o indices.sql), e devolve b"" (nada a gravar agora).
    def _structure(self, name, text, ddl=None):
        if not self.load:
            return self._emit(text.encode("utf8"))
        if ddl:
            plain, alter = load_format.split_indexes(name, ddl)
            text = text.replace(ddl, plain, 1)
            if alter:
                self.alters.append(alter)
        self.struct.append(text)
        return b""

    # FAZ: guarda, ao ver as colunas de uma tabela, o que o LOAD DATA precisa saber dela (tipo de cada coluna e AUTO_INCREMENT).
    def _load_columns(self, name, tdef, columns, ddl):
        if name not in self.load_meta:
            kinds = {c.name: load_format.load_kind(c) for c in tdef.columns}
            self.load_meta[name] = (list(columns), kinds, load_format.auto_increment(ddl))

    def table_load_sql(self, index, paths):
        """Formato rápido: o LOAD DATA da tabela `index`, um por arquivo de dados em `paths` (JSON: caminhos relativos ao zip)."""
        name = self.tables[index][0]
        if name in self.load_meta:
            columns, kinds, auto = self.load_meta[name]
            for path in json.loads(paths):
                self.loads.append(load_format.load_statement(name, path, columns, kinds, auto))

    def load_files(self):
        """Formato rápido: JSON {arquivo: texto} com estrutura.sql, carregar.sql, indices.sql, objetos.sql e conferir.sql do banco."""
        head = load_format.HEADER.format(db=self.db)
        checks = [f"SELECT '{name}' AS tabela, {info['rows']} AS esperado, COUNT(*) AS carregado, "
                  f"IF(COUNT(*)={info['rows']},'OK','DIVERGENTE') AS resultado FROM `{name}`;"
                  for name, info in self.status.items() if info["status"] != "NAO_LIDA"]
        return json.dumps({
            "estrutura.sql": head + "\n".join(self.struct),
            "carregar.sql": head + "SET autocommit=0;\n\n" + "\n".join(self.loads) + "\nCOMMIT;\n",
            "indices.sql": head + "\n".join(self.alters) + "\n",
            "objetos.sql": head + self.tail.decode("utf8"),
            "conferir.sql": head + "\n".join(checks) + "\n",
        }, ensure_ascii=False)

    def info(self, index):
        """JSON com o resultado da tabela de número `index` (a tela mostra cada tabela assim que ela termina)."""
        name = self.tables[index][0]
        status = self.status.get(name, {"rows": 0, "status": "NAO_LIDA", "note": ""})
        return json.dumps({"name": name, **status}, ensure_ascii=False)

    def _foreign_keys(self):
        known, statements = {t.lower() for t in self.status}, []
        for fk in self.carver.foreign_keys():
            child_db, _, child = fk["tabela"].partition("/")
            ref_db, _, parent = fk["ref_tabela"].partition("/")
            if child_db.lower() != self.db.lower() or child.lower() not in self.no_fk:
                continue
            if ref_db.lower() == self.db.lower() and parent.lower() not in known:
                continue
            ref = f"`{parent}`" if ref_db.lower() == self.db.lower() else f"`{ref_db}`.`{parent}`"
            cols = ", ".join(f"`{c}`" for c in fk["colunas"])
            ref_cols = ", ".join(f"`{c}`" for c in fk["ref_colunas"])
            text = (f"ALTER TABLE `{self.no_fk[child.lower()]}` ADD CONSTRAINT `{fk['nome']}` FOREIGN KEY ({cols}) "
                    f"REFERENCES {ref} ({ref_cols})")
            if fk["on_delete"]:
                text += f" ON DELETE {fk['on_delete']}"
            if fk["on_update"]:
                text += f" ON UPDATE {fk['on_update']}"
            statements.append(text + ";")
        self.objects["chaves_estrangeiras"] = len(statements)
        if not statements:
            return b""
        return ("\n-- Chaves estrangeiras lidas do dicionário do InnoDB\n" + "\n".join(statements) + "\n").encode("utf8")

    def _objects(self):
        views = {}
        for name, text in self.views.items():
            try:
                views[name] = frm_parser.strip_empty_definer(frm_parser.view_to_ddl(name, frm_parser.parse_view_frm(text)))
            except frm_parser.FrmError as e:
                self.warn(f"View {name}: arquivo ilegível ({e}); não foi recriada.")
        triggers = []
        for entry in sorted(os.listdir(self.folder)) if self.folder else []:
            if entry.upper().endswith(".TRG"):
                try:
                    with open(os.path.join(self.folder, entry), "r", encoding="utf8", errors="replace") as f:
                        triggers += [frm_parser.strip_empty_definer(s) for s in frm_parser.parse_trigger_file(f.read())]
                except (OSError, frm_parser.FrmError):
                    continue
        # Rotinas e eventos moram em mysql.proc / mysql.event (MyISAM): sem servidor não são lidos, só avisados.
        for system_table, what in (("proc", "rotinas"), ("event", "eventos")):
            path = os.path.join(self.data_dir, "mysql", system_table + ".MYD")
            if os.path.isfile(path) and os.path.getsize(path) > 0:
                self.warn(f"Sem o servidor MySQL não dá para ler {what} (ficam em mysql.{system_table}); não entraram no dump.")
        self.objects.update(views=len(views), triggers=len(triggers))
        if not views and not triggers:
            return b""
        lines = [OBJECT_HEADER, "DELIMITER ;;"]
        for name in self._order_views(views):
            lines += [f"DROP VIEW IF EXISTS `{name}`;;", views[name].rstrip(";") + ";;"]
        for statement in triggers:
            trigger = re.search(r"TRIGGER\s+`?([^`\s]+)`?", statement, re.I)
            if trigger:
                lines.append(f"DROP TRIGGER IF EXISTS `{trigger.group(1)}`;;")
            lines.append(statement.rstrip(";") + ";;")
        lines.append("DELIMITER ;\n")
        return "\n".join(lines).encode("utf8")

    # Views que usam outras views vêm depois delas (igual ao desktop); dependência circular cai na ordem alfabética.
    @staticmethod
    def _order_views(views):
        ordered, pending = [], sorted(views)
        while pending:
            progress = False
            for name in list(pending):
                body = views[name].lower()
                if not [o for o in pending if o != name and re.search(r"`?\b" + re.escape(o.lower()) + r"\b`?", body)]:
                    ordered.append(name)
                    pending.remove(name)
                    progress = True
            if not progress:
                ordered += pending
                break
        return ordered

    def finish(self):
        """Devolve o fim do dump: chaves estrangeiras, views e triggers. Fecha os arquivos."""
        tail = self._foreign_keys() + self._objects()
        self.tail = tail
        self.repaired = len(self.carver.repaired_pages)
        self.bad_pages = self.carver.space.bad_pages
        if not self.shared_carver:   # um carver emprestado (vários bancos na pasta) é fechado por quem o abriu
            self.carver.close()
        out = self._emit(tail if self.load else tail + SQL_FOOTER.encode("utf8"))
        if self.debug:   # DEBUG-PROVISORIO
            self.debug(f"FIM banco={self.db} bytes_emitidos={self.sql_bytes} sha256={self.sha.hexdigest()} fks={self.objects['chaves_estrangeiras']} "
                       f"views={self.objects['views']} triggers={self.objects['triggers']} paginas_doublewrite={self.repaired} avisos={len(self.warnings)}")
        return out

    def report(self):
        """Resumo em JSON (texto): o que foi lido, tabela por tabela, e o SHA-256 do dump."""
        return json.dumps({
            "banco": self.db, "segundos": round(time.time() - self.started, 1),
            "registros": sum(t["rows"] for t in self.status.values()),
            "sql_bytes": self.sql_bytes, "sha256": self.sha.hexdigest(),
            "paginas_refeitas_doublewrite": self.repaired, "paginas_indice_invalidas": self.bad_pages,
            "objetos": self.objects, "avisos": self.warnings, "tabelas": self.status,
        }, ensure_ascii=False)


if __name__ == "__main__":
    recovery = Recovery(sys.argv[1], database=sys.argv[4] if len(sys.argv) > 4 else None)
    with open(sys.argv[2], "wb") as out:
        out.write(recovery.plan())
        for number in range(len(recovery.tables)):
            recovery.begin_table(number)          # pedaço a pedaço, como o navegador: tabela grande nunca fica inteira na memória
            while (chunk := recovery.next_chunk()) is not None:
                out.write(chunk)
        out.write(recovery.finish())
    summary = json.loads(recovery.report())
    if len(sys.argv) > 3:
        with open(sys.argv[3], "w", encoding="utf8") as out:
            json.dump(summary, out, indent=1, ensure_ascii=False)
    print({k: v for k, v in summary.items() if k not in ("tabelas", "avisos")}, f"avisos={len(summary['avisos'])}")
