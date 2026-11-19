"""tsvdump.py <pasta data> <banco> <saida> - recupera um banco com o motor do site (Python nativo) e grava, do MESMO decodificado:
  saida/dump.sql        o .sql que o site gera hoje
  saida/estrutura.sql   o dump sem as linhas INSERT
  saida/carregar.sql    LOAD DATA LOCAL INFILE por tabela (partes de 256 MB)
  saida/dados/*.tsv     as linhas
  saida/contagens.json  linhas por tabela
"""
import json, os, sys, time

WEB = r"C:\Users\danil\Desktop\IAS\recuperacao de banco\web\py"
sys.path.insert(0, WEB)
import web_driver
from innodb_carver import SqlExpr, MYSQL_BIT, MYSQL_TIMESTAMP, MYSQL_ENUM, MYSQL_SET, MYSQL_STRING, MYSQL_YEAR, DATA_INT

PART_BYTES = 256 << 20
ESC = {0x5C: "\\\\", 9: "\\t", 10: "\\n", 13: "\\r", 0: "\\0", 26: "\\Z"}


def cell(v, state):
    if v is None:
        return "\\N"
    if isinstance(v, SqlExpr):
        s = str(v)
        return s[len("FROM_UNIXTIME("):-1] if s.startswith("FROM_UNIXTIME(") else s
    if isinstance(v, bool):
        return str(int(v))
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, (bytes, bytearray)):
        state["hex"] = True
        return bytes(v).hex()
    return v.translate(ESC)


class TableTsv:
    def __init__(self, outdir, name):
        self.outdir, self.name = outdir, name
        self.parts, self.rows, self.size = [], 0, 0
        self.cols, self.hexcols, self.f = None, set(), None

    def _open(self):
        if self.f:
            self.f.close()
        rel = f"dados/{self.name}.{len(self.parts):03d}.tsv"
        self.parts.append(rel)
        self.f = open(os.path.join(self.outdir, rel), "wb")
        self.size = 0

    def write(self, columns, rows):
        if not rows:
            return
        self.cols = columns
        if self.f is None:
            self._open()
        lines = []
        for row in rows:
            cells = []
            for i, v in enumerate(row):
                st = {}
                cells.append(cell(v, st))
                if st:
                    self.hexcols.add(columns[i])
            lines.append("\t".join(cells))
        data = ("\n".join(lines) + "\n").encode("utf8")
        self.f.write(data)
        self.rows += len(rows)
        self.size += len(data)
        if self.size >= PART_BYTES:      # fecha a parte: a próxima abre sozinha
            self.f.close(); self.f = None

    def close(self):
        if self.f:
            self.f.close(); self.f = None


def load_sql(name, t, kinds, auto=None):
    sets, listing = [], []
    for n, col in enumerate(t.cols):
        q = f"`{col}`"
        var = f"@c{n}"
        mtype, kind = kinds.get(col, (None, None))
        if col == (auto or (None,))[0]:
            listing.append(var); sets.append(f"{q}=CAST({var} AS {auto[1]})")
        elif kind == MYSQL_YEAR or kind == MYSQL_BIT or (mtype == DATA_INT and kind in (MYSQL_STRING, MYSQL_ENUM, MYSQL_SET)):
            listing.append(var); sets.append(f"{q}=CAST({var} AS UNSIGNED)")
        elif kind == MYSQL_TIMESTAMP:
            listing.append(var); sets.append(f"{q}=IF({var} LIKE '0000-%',{var},FROM_UNIXTIME(CAST({var} AS UNSIGNED)))")
        elif col in t.hexcols:
            listing.append(var); sets.append(f"{q}=UNHEX({var})")
        else:
            listing.append(q)
    tail = f" SET {', '.join(sets)}" if sets else ""
    out = ""
    for part in t.parts:
        out += (f"LOAD DATA LOCAL INFILE '{part}' INTO TABLE `{name}` CHARACTER SET utf8 FIELDS TERMINATED BY '\\t' "
                f"ESCAPED BY '\\\\' LINES TERMINATED BY '\\n' ({', '.join(listing)}){tail};\nCOMMIT;\n")
    return out


def auto_columns(path):
    """tabela(minúscula) -> (coluna AUTO_INCREMENT, SIGNED|UNSIGNED), lida do CREATE TABLE do estrutura.sql."""
    import re
    found, table = {}, None
    for line in open(path, encoding="utf8", errors="replace"):
        m = re.match(r"CREATE TABLE `([^`]+)`", line)
        if m:
            table = m.group(1).lower()
        m = re.match(r"\s*`([^`]+)` .*AUTO_INCREMENT", line)
        if m and table:
            found[table] = (m.group(1), "UNSIGNED" if " unsigned" in line.lower() else "SIGNED")
    return found


def main(data, db, outdir):
    os.makedirs(os.path.join(outdir, "dados"), exist_ok=True)
    tables = {}
    original = web_driver.rows_to_sql
    current = {}

    def wrapped(table_name, columns, rows, batch=500):
        t = tables.setdefault(table_name, TableTsv(outdir, table_name))
        t.write(columns, rows)
        return original(table_name, columns, rows, batch)

    web_driver.rows_to_sql = wrapped
    t0 = time.time()
    rec = web_driver.Recovery(data, log=lambda m: None, database=db)
    out = open(os.path.join(outdir, "dump.sql"), "wb")
    out.write(rec.plan())
    for i in range(len(rec.tables)):
        rec.begin_table(i)
        while True:
            c = rec.next_chunk()
            if c is None:
                break
            out.write(c)
    out.write(rec.finish())
    out.close()
    meta = {}
    for name, t in tables.items():
        t.close()
        tdef = rec.carver.table(f"{rec.db}/{name}")
        kinds = {c.name: (c.mtype, c.mysql_type) for c in tdef.columns} if tdef else {}
        meta[name] = (t, kinds)
    gen = time.time() - t0

    with open(os.path.join(outdir, "dump.sql"), "rb") as src, open(os.path.join(outdir, "estrutura.sql"), "wb") as dst:
        for line in src:
            if not line.startswith(b"INSERT INTO `"):
                dst.write(line)

    head = ("/*!40101 SET NAMES utf8 */;\nSET FOREIGN_KEY_CHECKS=0;\nSET UNIQUE_CHECKS=0;\nSET SQL_MODE='NO_AUTO_VALUE_ON_ZERO';\n"
            f"USE `{rec.db}`;\nSET autocommit=0;\n\n")
    counts = {}
    autos = auto_columns(os.path.join(outdir, 'estrutura.sql'))
    with open(os.path.join(outdir, "carregar.sql"), "w", encoding="utf8") as f:
        f.write(head)
        for name, (t, kinds) in sorted(meta.items()):
            if not t.parts:
                continue
            f.write(load_sql(name, t, kinds, autos.get(name.lower())))
            counts[name] = t.rows
        f.write("\nCOMMIT;\n")
    json.dump(counts, open(os.path.join(outdir, "contagens.json"), "w"))
    print(f"gerado em {gen:.0f}s: {len(counts)} tabelas com dados, {sum(counts.values())} linhas;"
          f" dump.sql {os.path.getsize(os.path.join(outdir,'dump.sql'))/1048576:.1f} MB")


if __name__ == "__main__":
    main(*sys.argv[1:4])
