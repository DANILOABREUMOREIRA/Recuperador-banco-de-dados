"""edge_load_test.py - Confere que o LOAD DATA do formato rápido (py/load_format.py) grava EXATAMENTE o mesmo que o INSERT, com valores difíceis.
Precisa de dois MySQL 5.5 de teste ligados (inst.py): INSERT na porta A e LOAD DATA na porta B. Uso: python edge_load_test.py <portaA> <portaB>"""
import os
import random
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "py"))
import load_format
from innodb_carver import SqlExpr, rows_to_sql

BIN = r"C:\Users\danil\Desktop\IAS\recuperacao de banco\programa\sistema\mysql\bin"
port_a, port_b = sys.argv[1], sys.argv[2]
out = tempfile.mkdtemp(prefix="edge_load_")
os.makedirs(os.path.join(out, "dados"))
random.seed(7)

DDL = """DROP TABLE IF EXISTS `edge`;
CREATE TABLE `edge` (
  `id` int NOT NULL AUTO_INCREMENT,
  `t` varchar(300) CHARACTER SET utf8,
  `tl` varchar(50) CHARACTER SET latin1,
  `bl` blob,
  `vb` varbinary(20),
  `dc` decimal(12,3),
  `ts` timestamp NULL DEFAULT NULL,
  `dt` datetime,
  `bt` bit(8),
  `f` float,
  `d` double,
  `en` enum('a','b','c'),
  `st` set('x','y','z'),
  `ty` tinyint unsigned,
  `bg` bigint unsigned,
  `lt` longtext CHARACTER SET utf8,
  `ch` char(10),
  `yr` year,
  `tm` time,
  `dte` date,
  PRIMARY KEY (`id`),
  KEY `ix_t` (`t`(20)),
  KEY `ix_dc` (`dc`)
) ENGINE=InnoDB;"""
cols = ["id", "t", "tl", "bl", "vb", "dc", "ts", "dt", "bt", "f", "d", "en", "st", "ty", "bg", "lt", "ch", "yr", "tm", "dte"]
T = ["", "plain", "O'Brien", "back\\slash", "tab\there", "new\nline", "cr\rlf\r\n", "nul\0byte", "ctrl\x1aZ",
     "emoji \U0001F600 \u00e7\u00e3o \u00f1 \u65e5\u672c\u8a9e", "  lead and trail  ", "\\N", "NULL", "\\0", "\\n lit", "a,b;c\"d`e", "x" * 255,
     "),(", "x')\n(1,2", "FROM_UNIXTIME(5)", "0x4142", None]
TL = ["caf\u00e9", "\u00ff\u20ac\u2030", "plain", None, "\\", "'"]
BL = [b"", b"\x00\x01\xff", bytes(random.randrange(256) for _ in range(100)), None, b"\\N", b"\n\t\r", b"')\n("]
DC = [SqlExpr("-1234.560"), SqlExpr("0.000"), SqlExpr("999999999.999"), None]
TS = [SqlExpr("FROM_UNIXTIME(1700000000)"), "0000-00-00 00:00:00", SqlExpr("FROM_UNIXTIME(86400)"), None]
DT = ["2024-02-29 23:59:59", "0000-00-00 00:00:00", "9999-12-31 23:59:59", None]
F = [1.5, 3.4028234663852886e+38, 1e-05, 0.1, None]
D = [0.1, 1.7976931348623157e+308, 5e-324, -2.5, None]
LT = ["".join(chr(random.choice([65, 39, 92, 10, 9, 13, 0x1f600, 233, 26, 41, 44])) for _ in range(60000)), None, ""]
rows = []
for i in range(80):
    pick = lambda L: L[i % len(L)] if i % 7 else L[(i * 3) % len(L)]
    rows.append([i, pick(T), pick(TL), pick(BL), pick(BL[:3] + [None]) if i % 2 else None, pick(DC), pick(TS), pick(DT),
                 [0, 255, 170, None][i % 4], pick(F), pick(D), [0, 1, 2, 3, None][i % 5], [0, 1, 5, 7, None][i % 5], [0, 255, 7, None][i % 4],
                 [0, 18446744073709551615, 5, None][i % 4], pick(LT), ["abc", "", None, "x y"][i % 4], [0, 1901, 2155, None][i % 4],
                 ["-838:59:59", "00:00:00", "838:59:59", None][i % 4], ["0000-00-00", "2024-01-01", None][i % 3]])
K = load_format
kinds = {"ts": K.STAMP, "bt": K.CAST, "en": K.CAST, "st": K.CAST, "yr": K.CAST, "bl": K.HEX, "vb": K.HEX}

head = "/*!40101 SET NAMES utf8 */;\nSET FOREIGN_KEY_CHECKS=0;\nSET UNIQUE_CHECKS=0;\nSET SQL_MODE='NO_AUTO_VALUE_ON_ZERO';\nCREATE DATABASE IF NOT EXISTS edge;\nUSE edge;\n"
open(os.path.join(out, "ins.sql"), "wb").write((head + DDL + "\n").encode() + rows_to_sql("edge", cols, rows, batch=1))
plain, alter = K.split_indexes("edge", DDL.split("\n", 1)[1])
open(os.path.join(out, "dados", "edge.000.txt"), "wb").write(K.rows_to_lines(rows))
load = head + "DROP TABLE IF EXISTS `edge`;\n" + plain + "\nSET autocommit=0;\n" + K.load_statement("edge", "dados/edge.000.txt", cols, kinds, K.auto_increment(DDL)) + alter + "\n"
open(os.path.join(out, "load.sql"), "w", encoding="utf8").write(load)


def run(port, name):
    with open(os.path.join(out, name), "rb") as fh:
        r = subprocess.run([BIN + r"\mysql.exe", "-h127.0.0.1", f"-P{port}", "-uroot", "--default-character-set=utf8", "--local-infile=1"], stdin=fh,
                           cwd=out, capture_output=True, text=True, encoding="utf8", errors="replace")
    print(name, port, "rc", r.returncode, r.stderr.strip()[:600])


def dump(port):
    r = subprocess.run([BIN + r"\mysqldump.exe", "-h127.0.0.1", f"-P{port}", "-uroot", "--no-create-info", "--skip-extended-insert", "--hex-blob",
                        "--default-character-set=utf8", "edge"], capture_output=True)
    return r.stdout


def create(port):
    r = subprocess.run([BIN + r"\mysqldump.exe", "-h127.0.0.1", f"-P{port}", "-uroot", "--no-data", "--skip-comments", "edge"], capture_output=True)
    return r.stdout


run(port_a, "ins.sql")
run(port_b, "load.sql")
a, b = dump(port_a), dump(port_b)
print("linhas INSERT:", a.count(b"INSERT INTO"), "linhas LOAD:", b.count(b"INSERT INTO"))
print("dados:", "IDENTICOS" if a == b else "DIFERENTES", "| estrutura:", "IGUAL" if create(port_a) == create(port_b) else "DIFERENTE")
if a != b:
    n = 0
    for x, y in zip(a.split(b"\n"), b.split(b"\n")):
        if x != y:
            n += 1
            if n <= 3:
                print("INS: ", x[:400])
                print("LOAD:", y[:400])
    print("linhas diferentes:", n)
