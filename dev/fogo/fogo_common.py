"""
fogo_common.py - Caminhos e ajudantes do "teste de fogo" (gerar bancos, corromper de forma aleatória, restaurar, comparar).

Usa o MySQL 5.5 portátil que acompanha o programa (programa/sistema/mysql). Nada aqui toca na pasta do MySQL instalado
no computador: tudo vive numa área de trabalho própria (FOGO_DIR), que pode ser trocada pela variável FOGO_DIR.
"""
import os
import shutil
import subprocess
import sys
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."))
MYSQL = os.path.join(ROOT, "programa", "sistema", "mysql")
TEMPLATE = os.path.join(ROOT, "programa", "sistema", "mysql_template")
PY_DIR = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "py"))
FOGO_DIR = os.environ.get("FOGO_DIR") or r"C:\fogo"
PORT = int(os.environ.get("FOGO_PORT", 3398))
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def bin_path(name):
    return os.path.join(MYSQL, "bin", name)


def new_datadir(datadir):
    """Cria uma pasta data vazia com as tabelas de sistema do MySQL (copiadas do modelo do programa)."""
    if os.path.exists(datadir):
        raise SystemExit(f"{datadir} já existe: apague antes se quiser recomeçar.")
    os.makedirs(os.path.join(datadir, "mysql"))
    for name in os.listdir(TEMPLATE):
        shutil.copy2(os.path.join(TEMPLATE, name), os.path.join(datadir, "mysql", name))


def start_server(datadir, buffer_mb=1024, log_mb=48, charset="latin1", extra=()):
    """Sobe o mysqld portátil numa pasta data (ibdata1 único, sem file_per_table). Devolve o processo."""
    args = [bin_path("mysqld.exe"), "--no-defaults", f"--basedir={MYSQL}", f"--datadir={datadir}", f"--port={PORT}",
            "--bind-address=127.0.0.1", "--skip-grant-tables", "--skip-name-resolve", "--console",
            "--innodb_file_per_table=0", f"--innodb_buffer_pool_size={buffer_mb}M", f"--innodb_log_file_size={log_mb}M",
            "--innodb_log_buffer_size=16M", "--innodb_flush_log_at_trx_commit=0", "--innodb_autoextend_increment=64",
            "--innodb_data_file_path=ibdata1:12M:autoextend", "--max_allowed_packet=64M", f"--character-set-server={charset}",
            "--sql-mode=", "--innodb_fast_shutdown=0", *extra]
    log = open(os.path.join(os.path.dirname(datadir) or ".", "mysqld.log"), "ab")
    process = subprocess.Popen(args, stdout=log, stderr=log, creationflags=NO_WINDOW)
    for _ in range(180):
        if process.poll() is not None:
            raise SystemExit("mysqld terminou ao subir: veja mysqld.log")
        if admin("ping").returncode == 0:
            return process
        time.sleep(1)
    raise SystemExit("mysqld não respondeu em 3 minutos")


def admin(*args):
    return subprocess.run([bin_path("mysqladmin.exe"), "--no-defaults", "-h127.0.0.1", f"-P{PORT}", "-uroot", *args],
                          capture_output=True, text=True, creationflags=NO_WINDOW)


def stop_server(process):
    """Desligamento limpo (innodb_fast_shutdown=0: junta o insert buffer e faz o purge, o ibdata1 fica completo)."""
    admin("shutdown")
    try:
        process.wait(timeout=600)
    except subprocess.TimeoutExpired:
        process.kill()


def run_sql(sql, database=None, charset="utf8", check=True):
    """Roda um script SQL pelo cliente mysql.exe (entrada padrão). Devolve a saída; levanta erro se falhar."""
    args = [bin_path("mysql.exe"), "--no-defaults", "-h127.0.0.1", f"-P{PORT}", "-uroot", f"--default-character-set={charset}",
            "--max_allowed_packet=64M"]
    if database:
        args.append(database)
    done = subprocess.run(args, input=sql.encode("utf8"), capture_output=True, creationflags=NO_WINDOW)
    if check and done.returncode != 0:
        raise RuntimeError(done.stderr.decode("utf8", "replace")[:1500])
    return done.stdout.decode("utf8", "replace")


def query(sql, database=None):
    """SELECT simples: devolve [[colunas]] (texto separado por tabulação, sem cabeçalho)."""
    args = [bin_path("mysql.exe"), "--no-defaults", "-h127.0.0.1", f"-P{PORT}", "-uroot", "-N", "-B", "--default-character-set=utf8"]
    if database:
        args.append(database)
    done = subprocess.run(args, input=sql.encode("utf8"), capture_output=True, creationflags=NO_WINDOW)
    if done.returncode != 0:
        raise RuntimeError(done.stderr.decode("utf8", "replace")[:800])
    return [line.split("\t") for line in done.stdout.decode("utf8", "replace").splitlines() if line]


def say(text):
    print(str(text).encode("ascii", "replace").decode("ascii"), flush=True)
