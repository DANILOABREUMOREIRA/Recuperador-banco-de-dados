"""inst.py start <nome> <porta> [args mysqld...] | stop <porta> -- instância MySQL 5.5 portátil de teste (nunca a 3306)."""
import os, shutil, subprocess, sys, time
SIS = r"C:\Users\danil\Desktop\IAS\recuperacao de banco\programa\sistema"
BIN = os.path.join(SIS, "mysql", "bin")
ROOT = os.path.join(os.environ["TEMP"], "lt", "inst")

def start(name, port, extra):
    port = int(port)
    assert port >= 3390, "porta reservada"
    d = os.path.join(ROOT, name); shutil.rmtree(d, ignore_errors=True); os.makedirs(os.path.join(d, "data"))
    shutil.copytree(os.path.join(SIS, "mysql_template"), os.path.join(d, "data", "mysql"))
    cmd = [os.path.join(BIN, "mysqld.exe"), "--no-defaults", f"--basedir={os.path.dirname(BIN)}", f"--datadir={os.path.join(d,'data')}",
           f"--port={port}", "--bind-address=127.0.0.1", "--skip-grant-tables", "--innodb_log_file_size=5242880", "--console"] + extra
    log = open(os.path.join(d, "mysqld.log"), "ab")
    p = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, creationflags=0x08000000 | 0x00000200)
    for _ in range(120):
        r = subprocess.run([os.path.join(BIN, "mysql.exe"), "-h127.0.0.1", f"-P{port}", "-uroot", "-N", "-e", "select version()"], capture_output=True, text=True)
        if r.returncode == 0: print("ok", port, r.stdout.strip(), "pid", p.pid); return
        if p.poll() is not None: sys.exit("mysqld caiu, veja " + log.name)
        time.sleep(1)
    sys.exit("timeout")

def stop(port):
    subprocess.run([os.path.join(BIN, "mysqladmin.exe"), "-h127.0.0.1", f"-P{port}", "-uroot", "shutdown"], capture_output=True)
    print("stop", port)

if sys.argv[1] == "start": start(sys.argv[2], sys.argv[3], sys.argv[4:])
else: stop(sys.argv[2])
