import os, subprocess, sys, random
sys.path.insert(0, r"C:\Users\danil\Desktop\IAS\recuperacao de banco\web\py"); sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tsvdump
from tsvdump import TableTsv, load_sql
from innodb_carver import SqlExpr, rows_to_sql, DATA_INT, DATA_FIXBINARY
B = r"C:\Users\danil\Desktop\IAS\recuperacao de banco\programa\sistema\mysql\bin"
OUT = os.path.join(os.environ["TEMP"], "lt", "out_edge"); os.makedirs(os.path.join(OUT, "dados"), exist_ok=True)
random.seed(7)
DDL = """DROP TABLE IF EXISTS `edge`; CREATE TABLE `edge` (id int NOT NULL AUTO_INCREMENT, t varchar(300) CHARACTER SET utf8, tl varchar(50) CHARACTER SET latin1,
 bl blob, vb varbinary(20), dc decimal(12,3), ts timestamp NULL DEFAULT NULL, dt datetime, bt bit(8), f float, d double, en enum('a','b','c'),
 st set('x','y','z'), ty tinyint unsigned, bg bigint unsigned, lt longtext CHARACTER SET utf8, ch char(10), yr year, tm time, dte date, PRIMARY KEY (id)) ENGINE=InnoDB;"""
cols = ["id","t","tl","bl","vb","dc","ts","dt","bt","f","d","en","st","ty","bg","lt","ch","yr","tm","dte"]
T = ["", "plain", "O'Brien", "back\\slash", "tab\there", "new\nline", "cr\rlf\r\n", "nul\0byte", "ctrl\x1aZ",
     "emoji \U0001F600 \u00e7\u00e3o \u00f1 \u65e5\u672c\u8a9e", "  lead and trail  ", "\\N", "NULL", "\\0", "\\n lit", "a,b;c\"d`e", "x" * 255, None]
TL = ["caf\u00e9", "\u00ff\u20ac\u2030", "plain", None, "\\", "'"]
BL = [b"", b"\x00\x01\xff", bytes(random.randrange(256) for _ in range(100)), None, b"\\N", b"\n\t\r"]
DC = [SqlExpr("-1234.560"), SqlExpr("0.000"), SqlExpr("999999999.999"), None]
TS = [SqlExpr("FROM_UNIXTIME(1700000000)"), "0000-00-00 00:00:00", SqlExpr("FROM_UNIXTIME(86400)"), None]
DT = ["2024-02-29 23:59:59", "0000-00-00 00:00:00", "9999-12-31 23:59:59", None]
F = [1.5, -0.0, 3.4028234663852886e+38, 1e-05, 0.1, None]
D = [0.1, 1.7976931348623157e+308, 5e-324, -2.5, None]
LT = ["".join(chr(random.choice([65, 39, 92, 10, 9, 13, 0x1f600, 233, 26])) for _ in range(60000)), None, ""]
rows = []
for i in range(60):
    pick = lambda L: L[i % len(L)] if i % 7 else L[(i * 3) % len(L)]
    rows.append([i, pick(T), pick(TL), pick(BL), pick(BL[:3] + [None]) if i % 2 else None, pick(DC), pick(TS), pick(DT),
                 [0, 255, 170, None][i % 4], pick(F), pick(D), [0, 1, 2, 3, None][i % 5], [0, 1, 5, 7, None][i % 5], [0, 255, 7, None][i % 4],
                 [0, 18446744073709551615, 5, None][i % 4], pick(LT), ["abc", "", None, "x y"][i % 4], [0, 1901, 2155, None][i % 4],
                 ["-838:59:59", "00:00:00", "838:59:59", None][i % 4], ["0000-00-00", "2024-01-01", None][i % 3]])
kinds = {"id": (DATA_INT, 3), "ts": (DATA_INT, 7), "bt": (DATA_FIXBINARY, 16), "en": (DATA_INT, 247), "st": (DATA_INT, 248), "dc": (None, 246), "yr": (DATA_INT, 13)}
HEAD = "/*!40101 SET NAMES utf8 */;\nSET FOREIGN_KEY_CHECKS=0;\nSET UNIQUE_CHECKS=0;\nSET SQL_MODE='NO_AUTO_VALUE_ON_ZERO';\nCREATE DATABASE IF NOT EXISTS edge;\nUSE edge;\n"
open(os.path.join(OUT, "ins.sql"), "wb").write((HEAD + DDL + "\n").encode() + rows_to_sql("edge", cols, rows, batch=1))
for f in os.listdir(os.path.join(OUT, "dados")): os.remove(os.path.join(OUT, "dados", f))
t = TableTsv(OUT, "edge"); t.write(cols, rows); t.close()
open(os.path.join(OUT, "load.sql"), "w", encoding="utf8").write(HEAD + DDL + "\nSET autocommit=0;\n" + load_sql("edge", t, kinds, ("id", "SIGNED")) + "COMMIT;\n")
def run(port, f, extra=()):
    with open(os.path.join(OUT, f), "rb") as fh:
        r = subprocess.run([B + r"\mysql.exe", "-h127.0.0.1", f"-P{port}", "-uroot", "--default-character-set=utf8", "--max_allowed_packet=64M", *extra], stdin=fh, cwd=OUT, capture_output=True, text=True, encoding="utf8", errors="replace")
    print(f, port, "rc", r.returncode, r.stderr.strip()[:500])
def dump(port):
    r = subprocess.run([B + r"\mysqldump.exe", "-h127.0.0.1", f"-P{port}", "-uroot", "--no-create-info", "--skip-extended-insert", "--hex-blob", "--default-character-set=utf8", "edge"], capture_output=True)
    return r.stdout
run(3391, "ins.sql"); run(3393, "load.sql", ["--local-infile=1"])
a, c = dump(3391), dump(3393)
print("linhas INSERT:", a.count(b"INSERT INTO"), "linhas LOAD:", c.count(b"INSERT INTO"))
print("IDENTICO" if a == c else "DIFERENTE")
if a != c:
    al, cl = a.split(b"\n"), c.split(b"\n")
    n = 0
    for x, y in zip(al, cl):
        if x != y:
            n += 1
            if n <= 4: print("INS: ", x[:500]); print("LOAD:", y[:500]); print()
    print("linhas diferentes:", n)
