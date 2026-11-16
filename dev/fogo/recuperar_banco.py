"""
recuperar_banco.py - Recupera um ou mais bancos de uma pasta data com o motor web (py/web_driver.py) e guarda, por tabela, o
relatório e a lista ORDENADA do hash de cada linha (arquivos .u64), para a comparação exata com o original.

Como o navegador, mas sem gravar o dump: cada pedaço é só contado e transformado em hashes. Vários bancos da MESMA pasta
dividem uma varredura do ibdata1 (é a parte cara) e, antes de recuperar qualquer um, rodam a busca de tabelas perdidas
(lost_tables) olhando TODOS os bancos juntos: uma tabela sem nome só pode ser casada com um .frm de qualquer banco da pasta.
As que sobram ficam em `_sem_banco` (o banco de origem é desconhecido); só o processo chamado com --sem-banco as lê.

Uso: python recuperar_banco.py <pasta data> <pasta de saída> <banco1,banco2,...> [--sem-banco]
Saída: <banco>.json (relatório do motor) e <banco>.<tabela>.u64 (hashes das linhas, ordenados)
"""
import array
import json
import os
import sys
import time
import zlib

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "py"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dictionary_rebuild
import web_driver
from innodb_carver import InnodbCarver, sql_literal
from lost_tables import recover_lost_tables
from stream_carver import stream_table

HIDDEN = {"mysql", "performance_schema", "information_schema", "test"}


def row_hash(values):
    """Hash de 64 bits do texto SQL de uma linha: o mesmo que o motor grava no dump, então vale para comparar com o original."""
    raw = ("(" + ",".join(sql_literal(v) for v in values) + ")").encode("utf8", "replace")
    return (zlib.crc32(raw) << 32) | zlib.adler32(raw)


HASHES = {}   # tabela -> array('Q') das linhas já vistas


def rows_to_sql_hashing(table_name, columns, rows, batch=500):
    """Igual a innodb_carver.rows_to_sql (mesmos bytes), mas guarda também o hash de cada linha."""
    if not rows:
        return b""
    bucket = HASHES.setdefault(table_name, array.array("Q"))
    cols = ", ".join(f"`{c}`" for c in columns)
    out = []
    for start in range(0, len(rows), batch):
        tuples = []
        for row in rows[start:start + batch]:
            text = "(" + ",".join(sql_literal(v) for v in row) + ")"
            raw = text.encode("utf8", "replace")
            bucket.append((zlib.crc32(raw) << 32) | zlib.adler32(raw))
            tuples.append(text)
        out.append(f"INSERT INTO `{table_name}` ({cols}) VALUES {','.join(tuples)};")
    return "\n".join(out).encode("utf8")


web_driver.rows_to_sql = rows_to_sql_hashing


def save_hashes(out_dir, db, table):
    bucket = HASHES.pop(table, None) or array.array("Q")
    ordered = array.array("Q", sorted(bucket))
    with open(os.path.join(out_dir, f"{db}.{table}.u64"), "wb") as f:
        ordered.tofile(f)
    return len(ordered)


def user_databases(data):
    return sorted(e for e in os.listdir(data) if e.lower() not in HIDDEN and os.path.isdir(os.path.join(data, e))
                  and any(f.lower().endswith(".frm") for f in os.listdir(os.path.join(data, e))))


def global_lost_pass(carver, data, log):
    """Busca de tabelas perdidas olhando todos os bancos da pasta. Devolve {"banco/tabela": como} (nomes completos)."""
    prefixes = {k.split("/", 1)[0].lower() for k in carver.tables if "/" in k}
    frm_columns = {}
    for db in user_databases(data):
        known = {k.split("/", 1)[1] for k in carver.tables if k.startswith(db.lower() + "/")}
        names = {f[:-4].lower() for f in os.listdir(os.path.join(data, db)) if f.lower().endswith(".frm")}
        wanted = {n for n in names if n not in known}
        if wanted:
            for name, table in dictionary_rebuild.frm_candidates(data, db, wanted=wanted).items():
                frm_columns[f"{db}/{name}"] = [c.name for c in table.columns]
    return recover_lost_tables(carver, frm_columns, log=log, unnamed_db="_sem_banco")


def recover_unnamed(carver, out_dir, log):
    """Lê as tabelas _sem_banco/_sem_nome_*: não há banco nem .frm, só o dicionário. Devolve {tabela: {rows, status, note}}."""
    result = {}
    for name in sorted(k for k in carver.tables if k.startswith("_sem_banco/")):
        short = name.split("/", 1)[1]
        rows, note, status = 0, "", "OK"
        try:
            stats, bucket = {}, array.array("Q")
            table = carver.table(name)
            ts = carver.table_space(table)
            leaves = len(ts.leaf_pages.get(carver.clustered_id(table, ts), []))
            if leaves > 4096:
                for _columns, batch in stream_table(carver, name, stats=stats):
                    bucket.extend(row_hash(r) for r in batch)
            else:
                _columns, batch, _stats = carver.carve(name)
                bucket.extend(row_hash(r) for r in batch)
            HASHES[short] = bucket
            rows = save_hashes(out_dir, "_sem_banco", short)
            note = "sem nome (dicionário perdido)"
        except Exception as e:                       # noqa: BLE001 - o teste tem de seguir
            status, note = "ERRO", f"{type(e).__name__}: {e}"
        result[short] = {"rows": rows, "status": status, "note": note}
        log(f"  _sem_banco/{short}: {rows} linhas {status}")
    return result


def main():
    data, out_dir, dbs = sys.argv[1], sys.argv[2], [d for d in sys.argv[3].split(",") if d]
    with_unnamed = "--sem-banco" in sys.argv
    os.makedirs(out_dir, exist_ok=True)
    log = lambda message: print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)
    started = time.time()
    try:
        carver = InnodbCarver(os.path.join(data, "ibdata1"), log=lambda m: None, data_dir=data).load()
    except Exception as e:                           # noqa: BLE001
        log(f"FALHA AO CARREGAR O IBDATA1: {type(e).__name__}: {e}")
        json.dump({"fatal": f"{type(e).__name__}: {e}"}, open(os.path.join(out_dir, "fatal.json"), "w"))
        sys.exit(3)
    log(f"ibdata1 lido em {time.time() - started:.0f} s: {carver.space.n_pages} páginas, {len(carver.tables)} tabelas no dicionário")
    try:
        found = global_lost_pass(carver, data, log)
    except Exception as e:                           # noqa: BLE001
        found = {}
        log(f"busca de tabelas perdidas falhou: {type(e).__name__}: {e}")
    for db in dbs:
        db_started = time.time()
        restored = {full.split("/", 1)[1]: how for full, how in found.items() if full.startswith(db + "/")}
        try:
            recovery = web_driver.Recovery(data, log=lambda m: None, database=db, carver=carver, restored=restored)
            recovery.plan()
            for number in range(len(recovery.tables)):
                name = recovery.tables[number][0]
                recovery.begin_table(number)
                while recovery.next_chunk() is not None:
                    pass
                save_hashes(out_dir, db, name)
            recovery.finish()
            report = json.loads(recovery.report())
        except Exception as e:                       # noqa: BLE001 - uma falha do motor é um achado, não o fim do teste
            import traceback
            report = {"banco": db, "fatal": f"{type(e).__name__}: {e}", "traceback": traceback.format_exc()[-1500:]}
            log(f"{db}: FALHA DO MOTOR: {type(e).__name__}: {e}")
        report["segundos"] = round(time.time() - db_started, 1)
        json.dump(report, open(os.path.join(out_dir, f"{db}.json"), "w", encoding="utf8"), ensure_ascii=False)
        log(f"{db}: {report.get('registros', '-')} linhas, {len(report.get('tabelas', {}))} tabelas, {report['segundos']} s")
    if with_unnamed:
        unnamed = recover_unnamed(carver, out_dir, log)
        json.dump({"tabelas": unnamed}, open(os.path.join(out_dir, "_sem_banco.json"), "w", encoding="utf8"), ensure_ascii=False)
    log(f"FIM em {time.time() - started:.0f} s")


if __name__ == "__main__":
    main()
