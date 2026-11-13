"""
corromper.py - Corrompe de propósito uma CÓPIA de uma pasta data MySQL e anota o que esperar da recuperação.

NUNCA aponte para o original: o script recusa qualquer caminho dentro de ProgramData e só grava no ibdata1 e nos .frm
da cópia. Antes de estragar cada página ele calcula quantas linhas ela tem e a impressão digital delas (a mesma de
digest_run.py), então o resultado esperado de cada tabela atingida é: original menos as linhas das páginas zeradas.

Danos (sementes fixas, repetíveis):
  A. tabela grande: páginas de folha zeradas (2 blocos de 5 seguidas + 10 espalhadas)       -> perda exata, sem mais nada
  B. tabela grande: 10 páginas "rasgadas" (rodapé diferente do cabeçalho) + bytes trocados no meio de 3 registros
  C. tabela grande: UM NÓ INTERNO da árvore zerado                                           -> árvore danificada
  D. 2 tabelas médias: 3 páginas de folha zeradas cada                                       -> perda exata
  E. 1 tabela pequena: todas as folhas zeradas                                               -> tabela vazia
  F. dicionário: a página de SYS_TABLES com mais registros zerada                            -> tabelas somem do dicionário
  G. 3 .frm com o cabeçalho zerado e 1 .frm apagado                                          -> estrutura vem do dicionário

Uso: python corromper.py <copia da pasta data> <plano.json>
"""
import json
import os
import random
import sys
import zlib

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "py"))
import frm_parser
import web_driver
from innodb_carver import (PAGE_LEVEL, PAGE_N_RECS, CarverError, decode_value, iter_records, sql_literal, u16, u32)
from page_health import PAGE_SIZE, doublewrite_copies
from stream_carver import _node_child, live_leaves

COPY, PLAN = sys.argv[1], sys.argv[2]
if "programdata" in os.path.abspath(COPY).lower():
    sys.exit("RECUSADO: este script só corrompe cópias, nunca a pasta do MySQL em ProgramData.")
IBDATA = os.path.join(COPY, "ibdata1")
rng = random.Random(20261112)
plan = {"acoes": [], "esperado": {}}


def say(text):
    print(text, flush=True)


def page_digest(carver, table, ts, hint, page_no):
    """(linhas, impressão) das linhas vivas de UMA página de folha, com a conta de digest_run.py."""
    fields, _n_key, parser, out_index = carver._fields_and_parser(table, hint)
    page, rows = ts.page(page_no), []
    for origin in iter_records(page, table.compact):
        try:
            parsed = parser(page, origin, fields, lambda data: carver.read_external(data, ts))
            if parsed is None or parsed[0]:
                continue
            rows.append([None if parsed[1][i] is None else decode_value(fields[i], parsed[1][i]) for i in out_index])
        except (CarverError, IndexError, ValueError):
            continue
    _names, rows = carver._in_column_order(table, fields, out_index, rows)
    total = 0
    for row in rows:
        raw = ("(" + ",".join(sql_literal(v) for v in row) + ")").encode("utf8", "replace")
        total = (total + ((zlib.crc32(raw) << 32) | zlib.adler32(raw))) & 0xFFFFFFFFFFFFFFFF
    return len(rows), total


def write_at(page_no, data, offset=0):
    with open(IBDATA, "r+b") as f:
        f.seek(page_no * PAGE_SIZE + offset)
        f.write(data)


def zero_pages(table_name, pages, carver, table, ts, hint, why):
    lost_rows, lost_digest = 0, 0
    for page_no in pages:
        rows, digest = page_digest(carver, table, ts, hint, page_no)
        lost_rows += rows
        lost_digest = (lost_digest + digest) & 0xFFFFFFFFFFFFFFFF
        write_at(page_no, b"\0" * PAGE_SIZE)
    entry = plan["esperado"].setdefault(table_name, {"linhas_perdidas": 0, "digest_perdido": 0})
    entry["linhas_perdidas"] += lost_rows
    entry["digest_perdido"] = (entry["digest_perdido"] + lost_digest) & 0xFFFFFFFFFFFFFFFF
    plan["acoes"].append({"tabela": table_name, "dano": why, "paginas": pages, "linhas_nessas_paginas": lost_rows})
    say(f"  {table_name}: {why}: {len(pages)} página(s), {lost_rows} linhas nelas")


say(f"Abrindo a cópia {COPY} (varre o ibdata1 inteiro)...")
recovery = web_driver.Recovery(COPY, log=lambda message: None)
recovery.plan()
carver, db = recovery.carver, recovery.db
protected = set()
for pages in doublewrite_copies(IBDATA).keys():
    protected.add(pages[1])   # páginas que o doublewrite refaria sozinho: ficam de fora (esconderiam o dano)
say(f"Banco {db}: {len(recovery.tables)} tabelas; {len(protected)} páginas protegidas pelo doublewrite ficam de fora.")

# Tabelas que moram no ibdata1, com o número de folhas.
info = []
for name, _dict_only in recovery.tables:
    table = carver.table(f"{db}/{name}")
    if not table:
        continue
    ts = carver.table_space(table)
    if ts is not carver.space:
        continue
    index_id = carver.clustered_id(table, ts)
    leaves = ts.leaf_pages.get(index_id, [])
    if leaves:
        info.append((len(leaves), name, table, ts, index_id))
info.sort(reverse=True)
say("Maiores: " + ", ".join(f"{n} ({p} folhas)" for p, n, *_ in info[:5]))


def hint_of(name):
    return frm_parser.read_table_ddl(os.path.join(COPY, db, name + ".frm"), name)


def live_pages(entry):
    leaves, name, table, ts, index_id = entry
    fields, n_key, _p, _o = carver._fields_and_parser(table, hint_of(name))
    order, _ = live_leaves(carver, ts, table, index_id, fields[:n_key], set(leaves))
    return [p for p in (order or leaves) if p not in protected]


big = [e for e in info if e[0] > recovery.stream_pages]
mid = [e for e in info if 200 <= e[0] <= recovery.stream_pages]
small = [e for e in info if 2 <= e[0] < 40]
say(f"Tabelas grandes (em lotes): {len(big)}; médias: {len(mid)}; pequenas: {len(small)}")
used = set()

# A. tabela grande: folhas zeradas
if big:
    entry = big[0]
    live = live_pages(entry)
    start1, start2 = rng.randrange(0, len(live) // 2 - 5), rng.randrange(len(live) // 2, len(live) - 5)
    pages = live[start1:start1 + 5] + live[start2:start2 + 5] + rng.sample(live, 10)
    pages = sorted(set(pages))
    zero_pages(entry[1], pages, carver, entry[2], entry[3], hint_of(entry[1]), "A: folhas zeradas")
    used.add(entry[1])

# B. tabela grande: páginas rasgadas e bytes trocados
if len(big) > 1:
    entry = big[1]
    live = live_pages(entry)
    torn = rng.sample(live, 10)
    for page_no in torn:
        write_at(page_no, b"\xDE\xAD\xBE\xEF", PAGE_SIZE - 4)
    flipped = []
    for page_no in rng.sample([p for p in live if p not in torn], 3):
        for _ in range(3):
            offset = rng.randrange(200, 9000)
            write_at(page_no, bytes(rng.randrange(256) for _ in range(6)), offset)
        flipped.append(page_no)
    plan["acoes"].append({"tabela": entry[1], "dano": "B: 10 paginas rasgadas + bytes trocados em 3 paginas", "paginas_rasgadas": torn, "paginas_com_bytes_trocados": flipped})
    say(f"  {entry[1]}: B: 10 rasgadas, bytes trocados em {flipped}")
    used.add(entry[1])

# C. tabela grande: um nó interno zerado (a árvore perde um pedaço do caminho)
if len(big) > 2:
    entry = big[2]
    leaves, name, table, ts, index_id = entry
    fields, n_key, _p, _o = carver._fields_and_parser(table, hint_of(name))
    root = table.root_page
    page_no, level = root, u16(ts.page(root), PAGE_LEVEL)
    while level > 1:                       # desce até o nível 1 por um caminho sorteado
        page = ts.page(page_no)
        children = [_node_child(page, o, fields[:n_key], table.compact) for o in iter_records(page, table.compact)]
        page_no, level = rng.choice(children), level - 1
    below = len([1 for o in iter_records(ts.page(page_no), table.compact)])
    write_at(page_no, b"\0" * PAGE_SIZE)
    plan["acoes"].append({"tabela": name, "dano": "C: no interno (nivel 1) zerado", "pagina": page_no, "folhas_abaixo": below})
    plan["esperado"].setdefault(name, {})["arvore_danificada"] = True
    say(f"  {name}: C: nó interno {page_no} zerado ({below} folhas penduradas nele)")
    used.add(name)

# D. duas tabelas médias: 3 folhas zeradas cada
for entry in [e for e in mid if e[1] not in used][:2]:
    live = live_pages(entry)
    zero_pages(entry[1], sorted(rng.sample(live, 3)), carver, entry[2], entry[3], hint_of(entry[1]), "D: 3 folhas zeradas")
    used.add(entry[1])

# E. uma tabela pequena: todas as folhas zeradas
for entry in [e for e in small if e[1] not in used][:1]:
    live = live_pages(entry)
    zero_pages(entry[1], sorted(live), carver, entry[2], entry[3], hint_of(entry[1]), "E: todas as folhas zeradas")
    used.add(entry[1])

# F. dicionário: a página de SYS_TABLES com mais registros
sys_tables_pages = carver.space.leaf_pages.get(1, [])
if sys_tables_pages:
    best = max(sys_tables_pages, key=lambda p: u16(carver.space.page(p), PAGE_N_RECS))
    count = u16(carver.space.page(best), PAGE_N_RECS)
    write_at(best, b"\0" * PAGE_SIZE)
    plan["acoes"].append({"tabela": "SYS_TABLES", "dano": "F: pagina do dicionario zerada", "pagina": best, "registros": count})
    say(f"  dicionário: F: página {best} de SYS_TABLES zerada ({count} registros)")

# G. .frm: cabeçalho zerado em 3 tabelas e um arquivo apagado
frm_targets = [e for e in info if e[1] not in used and 2 <= e[0] <= 4000][:4]
for number, entry in enumerate(frm_targets):
    path = os.path.join(COPY, db, entry[1] + ".frm")
    if not os.path.isfile(path):
        continue
    if number < 3:
        with open(path, "r+b") as f:
            f.write(b"\0" * 64)
        what = "G: .frm com cabecalho zerado"
    else:
        os.remove(path)
        what = "G: .frm apagado"
    plan["acoes"].append({"tabela": entry[1], "dano": what})
    say(f"  {entry[1]}: {what}")

with open(PLAN, "w", encoding="utf8") as out:
    json.dump(plan, out, ensure_ascii=False, indent=1)
say(f"Plano gravado em {PLAN}: {len(plan['acoes'])} danos.")
