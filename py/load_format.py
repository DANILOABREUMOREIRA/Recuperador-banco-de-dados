"""
load_format.py - O formato "rápido" de entrega: as linhas vão em arquivos de texto e entram no MySQL com LOAD DATA, e os índices
secundários são criados só no fim.
"""

# VISÃO GERAL: o .zip do formato rápido leva, por banco: estrutura.sql (DROP + CREATE TABLE sem os índices secundários),
#   dados/*.txt (uma linha por registro, escrita como os valores de um INSERT: (1,'abc',NULL)), carregar.sql (um LOAD DATA por
#   arquivo), indices.sql (os índices, criados com os dados já dentro), objetos.sql (chaves estrangeiras, views e triggers) e
#   conferir.sql (conta as linhas de cada tabela). Este módulo só monta esses textos; quem lê as tabelas é web_driver.py.
#   O LOAD DATA lê o mesmo texto de um INSERT: aspas simples ao redor do texto, \ como escape e NULL sem aspas. O que o LOAD DATA
#   não entende sozinho (binário em 0xHEX, TIMESTAMP em FROM_UNIXTIME, coluna AUTO_INCREMENT, BIT, YEAR, ENUM e SET) é lido para
#   uma variável e convertido no SET do comando.

import re

from innodb_carver import (CHARSET_BINARY, DATA_BINARY, DATA_BLOB, DATA_DOUBLE, DATA_FIXBINARY, DATA_FLOAT, DATA_INT, MYSQL_BIT,
                           MYSQL_ENUM, MYSQL_NEWDECIMAL, MYSQL_SET, MYSQL_STRING, MYSQL_TIMESTAMP, MYSQL_YEAR, sql_literal)

PLAIN, CAST, STAMP, HEX = "plain", "cast", "stamp", "hex"

# Cada arquivo de dados é cortado perto deste tamanho: um LOAD DATA é uma transação só, e uma enorme incharia o ibdata1 do cliente.
PART_BYTES = 256 << 20

HEADER = ("/*!40101 SET NAMES utf8 */;\n"
          "SET FOREIGN_KEY_CHECKS=0;\n"
          "SET UNIQUE_CHECKS=0;\n"
          "SET SQL_MODE='NO_AUTO_VALUE_ON_ZERO';\n"
          "CREATE DATABASE IF NOT EXISTS `{db}`;\n"
          "USE `{db}`;\n\n")


# FAZ: diz como o valor de uma coluna chega ao LOAD DATA, na mesma ordem em que decode_value (innodb_carver) o escreve.
def load_kind(col):
    mt, ty = col.mtype, col.mysql_type
    if mt == DATA_INT:
        if ty == MYSQL_TIMESTAMP:
            return STAMP
        if ty == MYSQL_YEAR or ty in (MYSQL_STRING, MYSQL_ENUM, MYSQL_SET):
            return CAST          # o texto "0" viraria 2000 (YEAR); ENUM e SET chegam como número de posição
        return PLAIN
    if mt in (DATA_FLOAT, DATA_DOUBLE):
        return PLAIN
    if ty == MYSQL_NEWDECIMAL:
        return PLAIN if col.decimal else HEX
    if ty == MYSQL_BIT:
        return CAST
    if col.coll_id == CHARSET_BINARY or mt in (DATA_FIXBINARY, DATA_BINARY) or (mt == DATA_BLOB and col.coll_id in (0, CHARSET_BINARY)):
        return HEX
    return PLAIN


# FAZ: as linhas (listas de valores Python) como texto do LOAD DATA: uma linha por registro, "(v1,v2,...)" e quebra de linha.
def rows_to_lines(rows):
    return "".join("(" + ",".join(sql_literal(v) for v in row) + ")\n" for row in rows).encode("utf8")


_KEY = re.compile(r"^\s*(UNIQUE |FULLTEXT |SPATIAL )?KEY\s")


# FAZ: separa o CREATE TABLE em (CREATE sem os índices secundários, ALTER TABLE que os cria). A chave primária fica.
def split_indexes(name, ddl):
    lines = ddl.split("\n")
    close = next((i for i, line in enumerate(lines) if i and line.startswith(")")), None)
    if close is None:
        return ddl, ""
    body = [line.rstrip().rstrip(",") for line in lines[1:close]]
    keys = [line.strip() for line in body if _KEY.match(line)]
    if not keys:
        return ddl, ""
    kept = [line for line in body if not _KEY.match(line)]
    kept = [line + ("," if i < len(kept) - 1 else "") for i, line in enumerate(kept)]
    alter = f"ALTER TABLE `{name}` " + ", ".join("ADD " + key for key in keys) + ";"
    return "\n".join([lines[0]] + kept + lines[close:]), alter


# FAZ: a coluna AUTO_INCREMENT do CREATE TABLE e se ela tem sinal: (nome, "SIGNED"|"UNSIGNED"), ou None.
def auto_increment(ddl):
    for line in ddl.split("\n"):
        m = re.match(r"\s*`([^`]+)` .*AUTO_INCREMENT", line)
        if m:
            return m.group(1), "UNSIGNED" if " unsigned" in line.lower() else "SIGNED"
    return None


# FAZ: o LOAD DATA de UM arquivo de dados. 'path' é relativo à pasta onde o mysql é executado. Uma coluna AUTO_INCREMENT passa
#   pelo SET: sem isso, com qualquer outro SET na tabela, o valor 0 viraria um número novo.
def load_statement(table, path, columns, kinds, auto):
    listing, sets = [], []
    for n, col in enumerate(columns):
        q, var = f"`{col}`", f"@c{n}"
        kind = kinds.get(col, PLAIN)
        if auto and col == auto[0]:
            listing.append(var)
            sets.append(f"{q}=CAST({var} AS {auto[1]})")
        elif kind == CAST:
            listing.append(var)
            sets.append(f"{q}=CAST({var} AS UNSIGNED)")
        elif kind == STAMP:
            listing.append(var)
            sets.append(f"{q}=IF({var} LIKE 'FROM_UNIXTIME(%',FROM_UNIXTIME(CAST(SUBSTRING_INDEX(SUBSTRING({var},15),')',1) AS UNSIGNED)),{var})")
        elif kind == HEX:
            listing.append(var)
            sets.append(f"{q}=UNHEX(SUBSTRING({var},3))")
        else:
            listing.append(q)
    tail = f" SET {', '.join(sets)}" if sets else ""
    return (f"LOAD DATA LOCAL INFILE '{path}' INTO TABLE `{table}` CHARACTER SET utf8 FIELDS TERMINATED BY ',' "
            r"OPTIONALLY ENCLOSED BY '\'' ESCAPED BY '\\' LINES STARTING BY '(' TERMINATED BY ')\n' "
            f"({', '.join(listing)}){tail};\nCOMMIT;\n")
