"""
gerar_bancos.py - Gera N bancos MySQL 5.5 (InnoDB) FICTÍCIOS de ~500 MB cada, com tabelas e colunas aleatórias, todos na
mesma pasta data (um ibdata1 só), para o teste de fogo.

Cada banco (fogo01, fogo02, ...) tem de 8 a 14 tabelas de tamanhos muito diferentes (algumas vazias ou minúsculas, outras
com centenas de MB), com tipos variados (inteiros, DECIMAL, FLOAT, CHAR, VARCHAR curto e longo, TEXT e BLOB que vão para
fora da página, datas, ENUM, SET, BIT), colunas anuláveis, chaves primárias de vários jeitos (inteira automática, composta,
texto, sem chave), índices secundários, ROW_FORMAT=COMPACT e REDUNDANT, e mais: um par pai/filho com chave estrangeira,
uma view e um trigger. Metade dos bancos é latin1 e metade utf8. Tudo sai de uma semente: o mesmo número gera o mesmo banco.

Uso: python gerar_bancos.py <pasta data a criar> [bancos=10] [MB por banco=500] [semente=1] [prefixo=fogo] [índice inicial=1]
Para um banco por pasta data: bancos=1 e índice inicial = o número do banco (o número também define o charset: par = utf8).
"""
import json
import math
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # o Python portátil do programa não põe a pasta do script no caminho
from fogo_common import new_datadir, query, run_sql, say, start_server, stop_server

PREFIXES = ["cliente", "produto", "venda", "estoque", "fornecedor", "pedido", "nota", "caixa", "usuario", "log", "config",
            "pagamento", "entrega", "contrato", "tarifa", "evento", "item", "cadastro", "historico", "movimento", "lote"]
COLUMN_NAMES = ["codigo", "nome", "descricao", "valor", "quantidade", "data_cad", "status", "obs", "preco", "total", "cnpj",
                "telefone", "email", "cidade", "uf", "peso", "altura", "ativo", "tipo", "ref", "serie", "chave", "xml", "hash",
                "payload", "flags", "nivel", "versao", "saldo", "desconto", "numero", "turno", "origem", "destino", "marca"]
ROW_LIMIT = 60000        # soma dos tamanhos máximos das colunas (o MySQL recusa linhas maiores que 65535)
CHUNK = 100000           # linhas por INSERT ... SELECT (um bloco da tabela auxiliar `nums`)


def accented(rng):
    return rng.random() < 0.3


def make_column(rng, name, utf8):
    """(tipo SQL, expressão que gera o valor, bytes médios, bytes máximos, indexável, anulável)."""
    kind = rng.choice(["int"] * 5 + ["decimal"] * 2 + ["float", "double", "char", "varchar", "varchar", "varchar", "text", "blob",
                                                          "date", "datetime", "timestamp", "time", "year", "enum", "set", "bit"])
    mult = 3 if utf8 else 1
    if kind == "int":
        sql_type = rng.choice(["tinyint", "smallint", "mediumint", "int", "bigint"])
        bits = {"tinyint": 8, "smallint": 16, "mediumint": 24, "int": 32, "bigint": 40}[sql_type]
        size = {"tinyint": 1, "smallint": 2, "mediumint": 3, "int": 4, "bigint": 8}[sql_type]
        if rng.random() < 0.4:
            return f"{sql_type} unsigned", f"FLOOR(RAND()*{2 ** bits - 1})", size, size, True
        return sql_type, f"FLOOR(RAND()*{2 ** bits - 1})-{2 ** (bits - 1)}", size, size, True
    if kind == "decimal":
        p = rng.randint(4, 15)
        s = rng.randint(0, min(6, p - 1))
        return f"decimal({p},{s})", f"ROUND(RAND()*{10 ** (p - s) - 1},{s})", (p + 1) // 2 + 1, (p + 1) // 2 + 1, False
    if kind == "float":
        return "float", "ROUND(RAND()*100000,3)", 4, 4, False
    if kind == "double":
        return "double", "RAND()*1000000", 8, 8, False
    if kind == "char":
        n = rng.choice([1, 2, 5, 10, 20, 32, 50])
        expr = f"LEFT(CONCAT(MD5(RAND()),MD5(RAND())),{n})"
        if n >= 4 and accented(rng):
            expr = f"CONCAT(LEFT(MD5(RAND()),{n - 2}),'çã')"
        return f"char({n})", expr, n, n * mult, n <= 32
    if kind == "varchar":
        n = rng.choice([8, 20, 45, 100, 255, 256, 400, 1000, 4000])
        expr = f"LEFT(REPEAT(MD5(RAND()),{math.ceil(n / 32)}),1+FLOOR(RAND()*{n}))"
        if accented(rng) and n >= 20:
            expr = f"CONCAT('Ação ',{expr})"
            expr = f"LEFT({expr},{n})"
        return f"varchar({n})", expr, n // 2 + 2, n * mult + 2, n <= 100
    if kind == "text":
        sql_type = rng.choice(["tinytext", "text", "text", "mediumtext", "longtext"])
        if sql_type == "tinytext":
            return sql_type, "LEFT(REPEAT(MD5(RAND()),8),1+FLOOR(RAND()*255))", 100, 12, False
        big = rng.choice([0, 0.05, 0.2])
        return (sql_type, f"IF(RAND()<{big},REPEAT(MD5(RAND()),400),REPEAT(MD5(RAND()),1+FLOOR(RAND()*40)))",
                int(700 + big * 12800), 12, False)
    if kind == "blob":
        sql_type = rng.choice(["tinyblob", "blob", "blob", "mediumblob", "longblob"])
        if sql_type == "tinyblob":
            return sql_type, "UNHEX(LEFT(CONCAT(MD5(RAND()),MD5(RAND())),2*(1+FLOOR(RAND()*30))))", 16, 12, False
        big = rng.choice([0, 0.05, 0.2])
        return (sql_type, f"IF(RAND()<{big},UNHEX(REPEAT(MD5(RAND()),1000)),UNHEX(REPEAT(MD5(RAND()),1+FLOOR(RAND()*60))))",
                int(480 + big * 16000), 12, False)
    if kind == "date":
        return "date", "DATE_ADD('1990-01-01',INTERVAL FLOOR(RAND()*12000) DAY)", 3, 3, True
    if kind == "datetime":
        return "datetime", "DATE_ADD('1990-01-01 00:00:00',INTERVAL FLOOR(RAND()*1000000000) SECOND)", 8, 8, True
    if kind == "timestamp":
        return "timestamp NOT NULL DEFAULT '2000-01-01 00:00:00'", "FROM_UNIXTIME(631152000+FLOOR(RAND()*900000000))", 4, 4, False
    if kind == "time":
        return "time", "SEC_TO_TIME(FLOOR(RAND()*86399))", 3, 3, False
    if kind == "year":
        return "year(4)", "1970+FLOOR(RAND()*99)", 1, 1, False
    if kind == "enum":
        values = rng.sample(["a", "b", "c", "d", "e", "f", "ativo", "inativo", "pendente", "ok"], rng.randint(2, 6))
        picks = ",".join(f"'{v}'" for v in values)
        return f"enum({picks})", f"ELT(1+FLOOR(RAND()*{len(values)}),{picks})", 1, 1, False
    if kind == "set":
        return ("set('s1','s2','s3','s4')", "ELT(1+FLOOR(RAND()*5),'s1','s1,s2','s2,s3','s1,s2,s3,s4','s4')", 1, 1, False)
    bits = rng.randint(1, 16)
    return f"bit({bits})", f"FLOOR(RAND()*{2 ** bits})", (bits + 7) // 8, (bits + 7) // 8, False


def make_table(rng, name, utf8, is_big=False):
    """Esquema aleatório de uma tabela. Devolve um dicionário com o CREATE TABLE e como gerar as linhas."""
    columns, used, max_bytes = [], set(), 0
    pk_kind = rng.choice(["auto_int", "auto_int", "auto_bigint", "composite", "text_pk", "none", "none"])
    key_cols, key_sql, avg = [], [], 12
    n = lambda i: "{N}"   # número absoluto da linha (alias preenchido na hora do INSERT)
    if pk_kind == "auto_int":
        columns.append(("id", "int unsigned NOT NULL AUTO_INCREMENT", None))
        key_sql.append("PRIMARY KEY (`id`)")
        avg += 4
    elif pk_kind == "auto_bigint":
        columns.append(("id", "bigint NOT NULL AUTO_INCREMENT", None))
        key_sql.append("PRIMARY KEY (`id`)")
        avg += 8
    elif pk_kind == "composite":
        columns.append(("grupo", "int NOT NULL", "FLOOR(({N})/50)"))
        columns.append(("seq", "smallint NOT NULL", "(({N}) MOD 50)"))
        key_sql.append("PRIMARY KEY (`grupo`,`seq`)")
        avg += 6
    elif pk_kind == "text_pk":
        columns.append(("chave", "char(12) NOT NULL", "CONCAT('K',LPAD(({N}),10,'0'))"))
        key_sql.append("PRIMARY KEY (`chave`)")
        avg += 12
    used.update(c[0] for c in columns)
    index_candidates = []
    for _ in range(rng.randint(3, 22)):
        base = rng.choice(COLUMN_NAMES)
        col = base if base not in used else f"{base}{rng.randint(2, 99)}"
        if col in used:
            continue
        sql_type, expr, average, maximum, indexable = make_column(rng, col, utf8)
        if max_bytes + maximum > ROW_LIMIT:
            continue
        nullable = rng.random() < 0.35 and "timestamp" not in sql_type
        if nullable:
            expr = f"IF(RAND()<{rng.choice([0.05, 0.2, 0.5, 0.9])},NULL,{expr})"
            sql_type += " DEFAULT NULL"
        elif "NOT NULL" not in sql_type:
            sql_type += " NOT NULL"
            if rng.random() < 0.3 and any(t in sql_type for t in ("int", "decimal", "float", "double")):
                sql_type += " DEFAULT 0"
        columns.append((col, sql_type, expr))
        used.add(col)
        max_bytes += maximum
        avg += average + (1 if nullable else 0)
        if indexable and not nullable:
            index_candidates.append(col)
    if not any(expr for _c, _t, expr in columns):
        columns.append(("valor", "int NOT NULL", "FLOOR(RAND()*1000000)"))
        avg += 4
    for col in rng.sample(index_candidates, min(len(index_candidates), rng.randint(0, 3))):
        key_sql.append(f"KEY `ix_{col}` (`{col}`)")
        avg += 6
    row_format = " ROW_FORMAT=REDUNDANT" if rng.random() < 0.25 else ""
    charset = "utf8" if utf8 else "latin1"
    lines = [f"`{c}` {t}" for c, t, _e in columns] + key_sql
    ddl = f"CREATE TABLE `{name}` (\n  " + ",\n  ".join(lines) + f"\n) ENGINE=InnoDB{row_format} DEFAULT CHARSET={charset}"
    return {"name": name, "ddl": ddl, "columns": columns, "avg_bytes": int(avg * 1.5 + 8), "rows": 0, "pk": pk_kind}


def insert_sql(table, offset, count):
    """INSERT ... SELECT de `count` linhas (1..CHUNK) a partir do número absoluto `offset`+1, lendo a tabela auxiliar nums."""
    cols = [(c, e) for c, _t, e in table["columns"] if e]
    names = ",".join(f"`{c}`" for c, _e in cols)
    exprs = ",".join(e.replace("{N}", f"nums.n+{offset}") for _c, e in cols)
    return f"INSERT INTO `{table['name']}` ({names}) SELECT {exprs} FROM nums WHERE nums.n<={count};"


def populate(table, rows, start=0):
    """Gera todos os INSERTs que levam a tabela de `start` a `rows` linhas."""
    statements, done = [], start
    while done < rows:
        count = min(CHUNK, rows - done)
        statements.append(insert_sql(table, done, count))
        done += count
    return statements


def database_size(db):
    out = query(f"SELECT table_name, COALESCE(data_length,0)+COALESCE(index_length,0), table_rows FROM information_schema.tables "
                f"WHERE table_schema='{db}' AND engine='InnoDB'")
    return {name: (int(size), int(rows or 0)) for name, size, rows in out}


def build_database(index, db, mb, seed):
    rng = random.Random(seed * 1000 + index)
    utf8 = index % 2 == 0
    charset = "utf8" if utf8 else "latin1"
    budget = mb * 1048576
    run_sql(f"DROP DATABASE IF EXISTS `{db}`; CREATE DATABASE `{db}` DEFAULT CHARACTER SET {charset};")
    run_sql("CREATE TABLE nums (n INT NOT NULL PRIMARY KEY) ENGINE=MEMORY; CREATE TABLE dg (d INT) ENGINE=MEMORY;"
            "INSERT INTO dg VALUES (0),(1),(2),(3),(4),(5),(6),(7),(8),(9);"
            "INSERT INTO nums SELECT a.d+10*b.d+100*c.d+1000*e.d+10000*f.d+1 FROM dg a,dg b,dg c,dg e,dg f;", db)
    count = rng.randint(8, 14)
    tables, used = [], set()
    while len(tables) < count:
        name = f"{rng.choice(PREFIXES)}_{rng.choice(['a', 'b', 'x', 'k', 'z'])}{rng.randint(1, 99)}"
        if name not in used:
            used.add(name)
            tables.append(make_table(rng, name, utf8))
    # Pares pai/filho com chave estrangeira.
    dim = {"name": "dim_" + rng.choice(PREFIXES), "avg_bytes": 80, "rows": rng.randint(200, 3000), "pk": "auto_int",
           "ddl": f"CREATE TABLE `DIMNAME` (\n  `id` int unsigned NOT NULL AUTO_INCREMENT,\n  `nome` varchar(40) NOT NULL,\n  `ativo` tinyint NOT NULL DEFAULT 1,\n"
                  f"  PRIMARY KEY (`id`)\n) ENGINE=InnoDB DEFAULT CHARSET={charset}",
           "columns": [("nome", "varchar(40)", "CONCAT('Registro ',{N})"), ("ativo", "tinyint", "FLOOR(RAND()*2)")]}
    dim["ddl"] = dim["ddl"].replace("DIMNAME", dim["name"])
    fact = {"name": "fato_" + rng.choice(PREFIXES), "avg_bytes": 60, "pk": "auto_int", "rows": 0,
            "ddl": f"CREATE TABLE `FACTNAME` (\n  `id` int NOT NULL AUTO_INCREMENT,\n  `id_dim` int unsigned NOT NULL,\n  `valor` decimal(12,2) NOT NULL,\n"
                   f"  `dia` date NOT NULL,\n  PRIMARY KEY (`id`),\n  KEY `ix_dim` (`id_dim`),\n  CONSTRAINT `fk_{index}` FOREIGN KEY (`id_dim`) "
                   f"REFERENCES `DIMNAME` (`id`)\n) ENGINE=InnoDB DEFAULT CHARSET={charset}",
            "columns": [("id_dim", "int unsigned", f"1+FLOOR(RAND()*{dim['rows']})"), ("valor", "decimal(12,2)", "ROUND(RAND()*100000,2)"),
                        ("dia", "date", "DATE_ADD('2005-01-01',INTERVAL FLOOR(RAND()*6000) DAY)")]}
    fact["ddl"] = fact["ddl"].replace("FACTNAME", fact["name"]).replace("DIMNAME", dim["name"])
    # Tamanhos: pesos muito desiguais; duas tabelas minúsculas e uma vazia.
    weights = [rng.random() ** 2.5 + 0.003 for _ in tables]
    for tiny in rng.sample(range(len(tables)), 2):
        weights[tiny] = 0.0001
    total_weight = sum(weights)
    fact_share = 0.12
    for table, weight in zip(tables, weights):
        target = budget * (1 - fact_share) * weight / total_weight
        table["rows"] = int(min(target, 260 * 1048576) / table["avg_bytes"])
    tables[rng.randrange(len(tables))]["rows"] = 0
    fact["rows"] = int(budget * fact_share / 90)
    ordered = [dim, fact] + tables
    t0 = time.time()
    run_sql("SET FOREIGN_KEY_CHECKS=0;" + ";\n".join(t["ddl"] for t in ordered) + ";", db)
    for table in ordered:
        if table["rows"]:
            statements = populate(table, table["rows"])
            for batch in range(0, len(statements), 8):
                run_sql("SET unique_checks=0,foreign_key_checks=0;" + "\n".join(statements[batch:batch + 8]), db)
    # Ajuste fino: acrescenta linhas aos maiores até chegar perto do tamanho pedido (até 3 rodadas).
    for _ in range(3):
        sizes = database_size(db)
        current = sum(size for name, (size, _r) in sizes.items() if name not in ("nums", "dg"))
        if current >= budget * 0.95:
            break
        grown = sorted((t for t in ordered if t["rows"] > 1000), key=lambda t: -sizes.get(t["name"], (0, 0))[0])[:4]
        if not grown:
            break
        missing = budget - current
        share = missing / len(grown)
        for table in grown:
            size = max(1, sizes.get(table["name"], (1, 1))[0])
            extra = int(table["rows"] * share / size)
            statements = populate(table, table["rows"] + extra, start=table["rows"])
            table["rows"] += extra
            for batch in range(0, len(statements), 8):
                run_sql("SET unique_checks=0,foreign_key_checks=0;" + "\n".join(statements[batch:batch + 8]), db)
    # View e trigger.
    small = next((t for t in tables if 0 < t["rows"] <= 5000), None) or dim
    int_cols = [c for c, t, e in small["columns"] if e and ("int" in t) and "unsigned" not in t and "DEFAULT NULL" not in t]
    first = small["columns"][0][0]
    run_sql(f"CREATE VIEW v_{small['name']} AS SELECT `{first}` AS primeira FROM `{small['name']}`;", db)
    if int_cols:
        run_sql(f"CREATE TRIGGER trg_{small['name']} BEFORE INSERT ON `{small['name']}` FOR EACH ROW SET NEW.`{int_cols[0]}`=IFNULL(NEW.`{int_cols[0]}`,0);", db)
    run_sql("DROP TABLE nums; DROP TABLE dg;", db)
    sizes = database_size(db)
    manifest = {"banco": db, "charset": charset, "segundos": round(time.time() - t0), "tabelas": {
        t["name"]: {"linhas_pedidas": t["rows"], "pk": t["pk"], "colunas": len(t["columns"]), "mb": round(sizes.get(t["name"], (0, 0))[0] / 1048576, 1)}
        for t in ordered}}
    manifest["mb_total"] = round(sum(v["mb"] for v in manifest["tabelas"].values()), 1)
    return manifest


def main():
    datadir = sys.argv[1]
    databases = int(sys.argv[2]) if len(sys.argv) > 2 else 10
    mb = int(sys.argv[3]) if len(sys.argv) > 3 else 500
    seed = int(sys.argv[4]) if len(sys.argv) > 4 else 1
    prefix = sys.argv[5] if len(sys.argv) > 5 else "fogo"
    first = int(sys.argv[6]) if len(sys.argv) > 6 else 1
    new_datadir(datadir)
    server = start_server(datadir)
    manifests = []
    try:
        for index in range(first, first + databases):
            db = f"{prefix}{index:02d}"
            say(f"[{time.strftime('%H:%M:%S')}] gerando {db} ({mb} MB)...")
            manifest = build_database(index, db, mb, seed)
            manifests.append(manifest)
            say(f"   {db}: {len(manifest['tabelas'])} tabelas, {manifest['mb_total']} MB em {manifest['segundos']} s, {manifest['charset']}")
            with open(os.path.join(os.path.dirname(datadir), "manifesto.json"), "w", encoding="utf8") as out:
                json.dump(manifests, out, ensure_ascii=False, indent=1)
        # Contagem de linhas de cada tabela, direto do MySQL: é a referência independente.
        truth = {}
        for manifest in manifests:
            for table in manifest["tabelas"]:
                truth[f"{manifest['banco']}.{table}"] = int(query(f"SELECT COUNT(*) FROM `{table}`", manifest["banco"])[0][0])
        with open(os.path.join(os.path.dirname(datadir), "contagens_mysql.json"), "w") as out:
            json.dump(truth, out, indent=1)
        say(f"contagens do MySQL gravadas: {len(truth)} tabelas, {sum(truth.values())} linhas")
        run_sql("SET GLOBAL innodb_fast_shutdown=0;")
    finally:
        stop_server(server)
    size = os.path.getsize(os.path.join(datadir, "ibdata1")) / 1048576
    say(f"pronto: ibdata1 de {size:.0f} MB")


if __name__ == "__main__":
    main()
