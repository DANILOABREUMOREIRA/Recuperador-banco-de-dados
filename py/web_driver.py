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

import dictionary_rebuild
import frm_parser
from innodb_carver import CarverError, InnodbCarver, ddl_from_dictionary, rows_to_sql

# Mesmos valores do programa de desktop (engine_common.py / engine_objects.py), para o dump sair igual.
HIDDEN_SCHEMAS = {"mysql", "performance_schema", "information_schema", "test"}
VARIANT_SUFFIX = re.compile(r"_\d$")   # cópias do chkdsk: tabela_0, tabela_1
SQL_HEADER = (
    "/*!40101 SET NAMES utf8 */;\n"
    "SET FOREIGN_KEY_CHECKS=0;\n"
    "SET UNIQUE_CHECKS=0;\n"
    "SET SQL_MODE='NO_AUTO_VALUE_ON_ZERO';\n"
    "CREATE DATABASE IF NOT EXISTS `{db}`;\n"
    "USE `{db}`;\n\n"
)
OBJECT_HEADER = "-- Objetos do banco (views, triggers, rotinas e eventos)\n"


class Recovery:
    """Uso: plan() -> cabeçalho; table(i) para cada tabela -> pedaço do dump; finish() -> rodapé; report() -> resumo."""

    def __init__(self, data_dir, log=print):
        self.data_dir, self.log = data_dir, log
        self.started = time.time()
        self.carver, self.db, self.folder = None, None, None
        self.tables = []        # [(nome, só_no_dicionário)]
        self.status = {}        # nome -> {"rows", "status", "note"}
        self.no_fk = {}         # nome minúsculo -> nome: tabelas cujo CREATE TABLE não trouxe as chaves estrangeiras
        self.warnings = []
        self.objects = {"views": 0, "triggers": 0, "chaves_estrangeiras": 0}
        self.sha, self.sql_bytes = hashlib.sha256(), 0
        self.on_pages = None    # opcional: função (páginas lidas, total) chamada durante a leitura de cada tabela

    def warn(self, message):
        self.warnings.append(message)
        self.log("AVISO: " + message)

    # Todo pedaço do dump passa por aqui: soma o tamanho e o SHA-256 (o mesmo no navegador e no Python comum).
    def _emit(self, data):
        self.sha.update(data)
        self.sql_bytes += len(data)
        return data

    # FAZ: escolhe o banco do usuário com mais .frm (igual ao detect_database do desktop).
    def _detect_database(self):
        best, count = None, 0
        for entry in sorted(os.listdir(self.data_dir)):
            path = os.path.join(self.data_dir, entry)
            if entry.lower() in HIDDEN_SCHEMAS - {"test"} or not os.path.isdir(path):
                continue
            frms = sum(1 for f in os.listdir(path) if f.lower().endswith(".frm"))
            if frms > count:
                best, count = entry, frms
        if not best:
            raise RuntimeError("Nenhum banco de usuário com arquivos .frm foi encontrado na pasta escolhida.")
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
        self.folder = os.path.join(self.data_dir, self.db)
        self.views = self._view_files()
        view_names = {v.lower() for v in self.views}
        listed = sorted(f[:-4] for f in os.listdir(self.folder)
                        if f.lower().endswith(".frm") and not f.startswith("#sql") and f[:-4].lower() not in view_names)
        lowered = {t.lower() for t in listed}
        tables = [t for t in listed if not (VARIANT_SUFFIX.search(t) and VARIANT_SUFFIX.sub("", t).lower() in lowered)]
        skipped = sorted(set(listed) - set(tables))
        if skipped:
            self.log(f"Cópias de chkdsk ignoradas ({len(skipped)}): {', '.join(skipped)}")

        # Sem o dicionário do ibdata1 não há como achar os dados: o erro sobe com a mensagem do leitor de páginas.
        self.carver = InnodbCarver(os.path.join(self.data_dir, "ibdata1"), log=self.log, data_dir=self.data_dir).load()
        prefix = self.db.lower() + "/"
        known = {k[len(prefix):] for k in self.carver.tables if k.startswith(prefix)}
        dict_only = sorted(known - lowered)
        if dict_only:
            self.log(f"Tabelas sem .frm mas com dados no ibdata1 (lidas direto): {', '.join(dict_only)}")
        self._reconstruct_missing({t.lower() for t in tables} - known)
        self.tables = [(t, False) for t in tables] + [(t, True) for t in dict_only]
        self.log(f"Banco {self.db}: {len(self.tables)} tabelas a extrair.")
        return self._emit(SQL_HEADER.format(db=self.db).encode("utf8"))

    # FAZ: tabelas com .frm mas sem linha no dicionário: casa o .frm com as páginas (dictionary_rebuild). É
    #   redundância: uma falha aqui só gera aviso.
    def _reconstruct_missing(self, wanted):
        if not wanted:
            return
        candidates = dictionary_rebuild.frm_candidates(self.data_dir, self.db, wanted=wanted)
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
        for name, why in sorted(missed.items()):
            self.log(f"  {name}: dados não localizados ({why}).")

    def table(self, index):
        """Extrai a tabela de número `index` e devolve o pedaço dela no dump (DROP + CREATE + INSERTs)."""
        name, dict_only = self.tables[index]
        full = f"{self.db}/{name}"
        if not dict_only and self._is_myisam(name):
            self.status[name] = {"rows": 0, "status": "NAO_LIDA",
                                 "note": "MyISAM: só o servidor MySQL lê esta tabela; não entrou no dump."}
            self.warn(f"Tabela {name}: MyISAM, não lida (use o programa de desktop para ela).")
            return b""
        hint = None if dict_only else frm_parser.read_table_ddl(os.path.join(self.folder, name + ".frm"), name)
        tdef = self.carver.table(full)
        if not tdef:
            # Sem dados nas páginas: recria só a estrutura (se o .frm é legível), com aviso.
            if not hint:
                self.status[name] = {"rows": 0, "status": "NAO_LIDA",
                                     "note": "Sem estrutura legível (.frm inválido e fora do dicionário); não recriada."}
                self.warn(f"Tabela {name}: sem estrutura legível; ignorada.")
                return b""
            self.no_fk[name.lower()] = name
            self.status[name] = {"rows": 0, "status": "AVISO",
                                 "note": "Estrutura recriada pelo .frm, mas os dados não foram localizados nas páginas."}
            self.warn(f"Tabela {name}: estrutura recriada, dados não localizados.")
            return self._emit(f"DROP TABLE IF EXISTS `{name}`;\n{hint};\n\n\n".encode("utf8"))

        ddl = ddl_from_dictionary(tdef, hint)
        data, rows, problem = b"", 0, None
        try:
            columns, records, stats = self.carver.carve(full, ddl_hint=hint, progress=self.on_pages)
            data, rows = rows_to_sql(name, columns, records), len(records)
            if stats["bad_records"]:
                problem = f"{stats['bad_records']} registros ilegíveis descartados"
            lost = self.carver.lost_leaf_pages(full)
            if lost:
                problem = "; ".join(x for x in (problem, f"ao menos {len(lost)} página(s) da tabela perdida(s)") if x)
        except (CarverError, OSError) as e:
            problem = f"leitura direta falhou: {e}"
        if problem:
            self.warn(f"Tabela {name}: {problem}")
        self.no_fk[name.lower()] = name
        self.status[name] = {"rows": rows, "status": "AVISO" if problem else "OK", "note": problem or ""}
        return self._emit(f"DROP TABLE IF EXISTS `{name}`;\n{ddl};\n".encode("utf8") + data + b"\n\n")

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
        for entry in sorted(os.listdir(self.folder)):
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
        self.repaired = len(self.carver.repaired_pages)
        self.bad_pages = self.carver.space.bad_pages
        self.carver.close()
        return self._emit(tail)

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
    recovery = Recovery(sys.argv[1])
    with open(sys.argv[2], "wb") as out:
        out.write(recovery.plan())
        for number in range(len(recovery.tables)):
            out.write(recovery.table(number))
        out.write(recovery.finish())
    summary = json.loads(recovery.report())
    if len(sys.argv) > 3:
        with open(sys.argv[3], "w", encoding="utf8") as out:
            json.dump(summary, out, indent=1, ensure_ascii=False)
    print({k: v for k, v in summary.items() if k not in ("tabelas", "avisos")}, f"avisos={len(summary['avisos'])}")
