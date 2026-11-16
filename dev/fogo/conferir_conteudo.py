"""
conferir_conteudo.py - Prova de FIDELIDADE do conteúdo contra o MySQL de verdade, num banco pequeno.

1. Copia a pasta data original, sobe o mysqld nela e tira o CHECKSUM TABLE de cada tabela do banco.
2. Recupera o banco com o motor web (dump .sql), importa o dump num MySQL novo e tira o CHECKSUM TABLE de novo.
3. Compara. Igual = o dump devolve exatamente os mesmos valores (tipos, NULLs, textos longos, BLOBs, datas, DECIMAL...).

Uso: python conferir_conteudo.py <pasta data original> <banco> <pasta de trabalho (nova)>
"""
import json
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fogo_common import PY_DIR, new_datadir, query, run_sql, say, start_server, stop_server

data, db, work = sys.argv[1], sys.argv[2], sys.argv[3]
os.makedirs(work)


def checksums(datadir, charset, log_mb=48):
    server = start_server(datadir, buffer_mb=256, log_mb=log_mb, charset=charset)
    try:
        tables = [row[0] for row in query(f"SELECT table_name FROM information_schema.tables WHERE table_schema='{db}' AND table_type='BASE TABLE'")]
        return {t: query(f"CHECKSUM TABLE `{t}`", db)[0][-1] for t in tables}, tables
    finally:
        stop_server(server)


copy = os.path.join(work, "original")
shutil.copytree(data, copy)
original, tables = checksums(copy, "latin1")
say(f"original: {len(original)} tabelas com CHECKSUM TABLE")

dump = os.path.join(work, "dump.sql")
python = sys.executable
subprocess.run([python, os.path.join(PY_DIR, "web_driver.py"), data, dump, os.path.join(work, "rec.json"), db], check=True, capture_output=True)
say(f"dump: {os.path.getsize(dump) / 1048576:.1f} MB")

imported_dir = os.path.join(work, "importado")
new_datadir(imported_dir)
server = start_server(imported_dir, buffer_mb=256, log_mb=48)
try:
    done = subprocess.run([os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "..", "programa", "sistema", "mysql", "bin", "mysql.exe"),
                           "--no-defaults", "-h127.0.0.1", f"-P{os.environ.get('FOGO_PORT', 3398)}", "-uroot", "--default-character-set=utf8",
                           "--max_allowed_packet=64M", "--force"], stdin=open(dump, "rb"), capture_output=True)
    errors = done.stderr.decode("utf8", "replace").strip().splitlines()
    say(f"importação: {len(errors)} mensagens de erro" + (f", primeira: {errors[0][:200]}" if errors else ""))
    recovered = {t: query(f"CHECKSUM TABLE `{t}`", db)[0][-1] for t in [r[0] for r in query(f"SELECT table_name FROM information_schema.tables WHERE table_schema='{db}' AND table_type='BASE TABLE'")]}
finally:
    stop_server(server)

diff = [t for t in tables if original.get(t) != recovered.get(t)]
say(f"IGUAIS {len(tables) - len(diff)} de {len(tables)} tabelas")
for t in diff:
    say(f"  DIFERE {t}: original {original.get(t)} importado {recovered.get(t)}")
json.dump({"original": original, "importado": recovered, "diferentes": diff}, open(os.path.join(work, "checksums.json"), "w"), indent=1)
