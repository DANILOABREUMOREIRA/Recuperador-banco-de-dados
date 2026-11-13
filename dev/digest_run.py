"""
digest_run.py - Roda a recuperação (como o navegador) e guarda, por tabela, a contagem e uma impressão digital do
CONTEÚDO, sem gravar o dump: serve para comparar o original com uma cópia corrompida e recuperada.

A impressão é a soma (módulo 2^64) de um hash de cada linha: não depende da ordem das linhas nem de como o dump foi
cortado em INSERTs. O dump gerado é byte a byte o de sempre (a função de saída é a mesma, só que também soma o hash).

Uso: python digest_run.py <pasta data> <saida.json> [limite de folhas para ler em lotes] [banco]
"""
import ctypes
import ctypes.wintypes as wt
import json
import os
import sys
import time
import zlib

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "py"))
import web_driver
from innodb_carver import sql_literal

DIGESTS = {}   # tabela -> [linhas, soma dos hashes]


def rows_to_sql_digest(table_name, columns, rows, batch=500):
    """Igual a innodb_carver.rows_to_sql (mesmos bytes), mas soma também o hash de cada linha."""
    if not rows:
        return b""
    entry = DIGESTS.setdefault(table_name, [0, 0])
    cols = ", ".join(f"`{c}`" for c in columns)
    out = []
    for start in range(0, len(rows), batch):
        tuples = []
        for row in rows[start:start + batch]:
            text = "(" + ",".join(sql_literal(v) for v in row) + ")"
            raw = text.encode("utf8", "replace")
            entry[1] = (entry[1] + ((zlib.crc32(raw) << 32) | zlib.adler32(raw))) & 0xFFFFFFFFFFFFFFFF
            tuples.append(text)
        entry[0] += len(tuples)
        out.append(f"INSERT INTO `{table_name}` ({cols}) VALUES {','.join(tuples)};")
    return "\n".join(out).encode("utf8")


web_driver.rows_to_sql = rows_to_sql_digest


class _Mem(ctypes.Structure):
    _fields_ = [("cb", wt.DWORD), ("PageFaultCount", wt.DWORD), ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t), ("a", ctypes.c_size_t), ("b", ctypes.c_size_t), ("c", ctypes.c_size_t),
                ("d", ctypes.c_size_t), ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]


def memory_mb():
    info = _Mem()
    info.cb = ctypes.sizeof(info)
    call = ctypes.windll.psapi.GetProcessMemoryInfo
    call.argtypes = [wt.HANDLE, ctypes.POINTER(_Mem), wt.DWORD]
    ctypes.windll.kernel32.GetCurrentProcess.restype = wt.HANDLE
    call(ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(info), info.cb)
    return info.WorkingSetSize / 1048576, info.PeakWorkingSetSize / 1048576


if __name__ == "__main__":
    folder, target = sys.argv[1], sys.argv[2]
    limit = int(sys.argv[3]) if len(sys.argv) > 3 and sys.argv[3] else None
    database = sys.argv[4] if len(sys.argv) > 4 and sys.argv[4] else None
    recovery = web_driver.Recovery(folder, log=lambda message: print("  log:", message[:200], flush=True), stream_pages=limit, database=database)
    started = time.time()
    recovery.plan()
    print(f"plano: {len(recovery.tables)} tabelas do banco {recovery.db} em {time.time() - started:.0f} s, memória {memory_mb()[0]:.0f} MB", flush=True)
    last_report = time.time()
    with open(os.devnull, "wb") as sink:
        for number in range(len(recovery.tables)):
            began = time.time()
            recovery.begin_table(number)
            while (chunk := recovery.next_chunk()) is not None:
                sink.write(chunk)
            took = time.time() - began
            if took > 60 or time.time() - last_report > 300:
                last_report = time.time()
                print(f"  [{number + 1}/{len(recovery.tables)}] {recovery.tables[number][0]}: {took:.0f} s | pico {memory_mb()[1]:.0f} MB | {time.time() - started:.0f} s no total", flush=True)
    recovery.finish()
    report = json.loads(recovery.report())
    for name, (rows, digest) in DIGESTS.items():
        if name in report["tabelas"]:
            report["tabelas"][name]["digest"] = f"{digest:016x}"
    report["pico_memoria_mb"] = round(memory_mb()[1])
    with open(target, "w", encoding="utf8") as out:
        json.dump(report, out, ensure_ascii=False)
    batch_tables = [n for n, i in report["tabelas"].items() if "lotes" in i["note"]]
    print(f"FIM: {report['segundos']} s | {report['registros']} registros | {report['sql_bytes'] / 1048576:.0f} MB de SQL | "
          f"pico de memória {report['pico_memoria_mb']} MB | avisos {len(report['avisos'])} | tabelas em lotes {len(batch_tables)}", flush=True)
    for name in batch_tables[:10]:
        print("  em lotes:", name, report["tabelas"][name]["rows"], "linhas")
