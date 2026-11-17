"""
kernel_paths.py - Os dois caminhos de leitura de tabela que usam o kernel WebAssembly (kernel_bridge).
"""

# VISÃO GERAL: web_driver.py lê cada tabela por um de dois caminhos, e os dois têm uma versão aqui que entrega o texto SQL
#   já pronto (em vez de listas de valores Python):
#   - stream_sql: tabelas grandes, em lotes, pela árvore viva (espelha stream_carver.stream_table);
#   - carve_sql: tabelas pequenas, todas as folhas, com a escolha da versão mais nova de cada chave (espelha InnodbCarver.carve_def).
#   O kernel converte a página; o que ele devolve como "registro especial" (valor fora da página, FLOAT/DOUBLE, texto
#   inválido) e as páginas em que ele falha são refeitos aqui, registro por registro, do jeito antigo. O texto de cada linha
#   é idêntico ao do caminho antigo: os testes comparam o hash de todas as linhas.

import struct

import kernel_bridge
from innodb_carver import (FIL_PAGE_LSN, PAGE_N_RECS, CarverError, decode_value, iter_records, page_structure_error, sql_literal, u16,
                           u64)
from stream_carver import BATCH_ROWS, leaf_runs, live_leaves

INSERT_ROWS = 500          # linhas por INSERT, como em innodb_carver.rows_to_sql
INSERT_BYTES = 1 << 20     # e no máximo ~1 MB de texto por INSERT (o max_allowed_packet padrão do MySQL 5.5 é de 1 MB)
RECORD_ERRORS = (CarverError, IndexError, struct.error, ValueError)


# FAZ: nomes das colunas na ordem do CREATE TABLE e a permutação que leva os campos do registro a essa ordem.
def column_layout(table, fields, out_index):
    names = [fields[i].name for i in out_index]
    order = {c.name: i for i, c in enumerate(table.columns)}
    perm = sorted(range(len(names)), key=lambda i: order.get(names[i], i))
    return [names[i] for i in perm], perm


# FAZ: o texto SQL de uma linha, do jeito antigo: valores decodificados e escritos por sql_literal.
def row_text(fields, out_index, perm, raw):
    values = [None if raw[i] is None else decode_value(fields[i], raw[i]) for i in out_index]
    return "(" + ",".join(sql_literal(values[p]) for p in perm) + ")"


# FAZ: a chave do registro em bytes, no mesmo formato que o kernel usa (NULL = 255; senão 0, tamanho em 2 bytes e os bytes).
def key_bytes(raw, n_key):
    out = bytearray()
    for v in raw[:n_key]:
        if v is None:
            out.append(255)
        else:
            v = bytes(v)
            out += b"\x00" + (len(v) & 0xFFFF).to_bytes(2, "little") + v
    return bytes(out)


# FAZ: junta pedaços (quantidade, bytes) em comandos INSERT de até INSERT_ROWS linhas / INSERT_BYTES bytes.
def inserts(table_name, columns, pieces):
    head = f"INSERT INTO `{table_name}` ({', '.join(f'`{c}`' for c in columns)}) VALUES ".encode("utf8")
    out, group, rows, size = [], [], 0, 0
    for count, text in pieces:
        group.append(text)
        rows += count
        size += len(text)
        if rows >= INSERT_ROWS or size >= INSERT_BYTES:
            out.append(head + b",".join(group) + b";")
            group, rows, size = [], 0, 0
    if group:
        out.append(head + b",".join(group) + b";")
    return b"\n".join(out)


# ------------------------------------------------------------------------------------------ tabelas grandes
def stream_sql(carver, name, ddl_hint=None, progress=None, stats=None):
    """Como stream_carver.stream_table, mas gera (colunas, linhas, [(quantidade, bytes), ...]) por lote, com o texto SQL pronto."""
    table = carver.table(name)
    if not table:
        raise CarverError(f"tabela {name} não encontrada no dicionário interno")
    carver.require_readable(table)
    ts = carver.table_space(table)
    index_id = carver.clustered_id(table, ts)
    fields, n_key, parser, out_index = carver._fields_and_parser(table, ddl_hint)
    names, perm = column_layout(table, fields, out_index)
    prepared = kernel_bridge.prepare(table, fields, out_index, perm, n_key)
    leaves = ts.leaf_pages.get(index_id, [])
    order, info = live_leaves(carver, ts, table, index_id, fields[:n_key], set(leaves))
    stats = stats if stats is not None else {}
    stats.update(pages=0, bad_records=0, deleted=0, leaf_pages=len(leaves), missing_leaves=0,
                 damaged_pages=0, records_missing=0, damaged_examples=[])
    if order is None:
        stats.update(mode="cadeias", reason=info)
        order = leaf_runs(ts, leaves)
    else:
        stats.update(mode="árvore", missing_leaves=info["folhas_ausentes"])
    stats.update(read_pages=len(order), loose_pages=len(leaves) - len(order))
    total = len(order)

    def external(data):
        return carver.read_external(data, ts)

    def python_record(page, origin, pieces_):
        try:
            parsed = parser(page, origin, fields, external)
            if parsed is None:
                return
            deleted, raw = parsed
            if deleted:
                stats["deleted"] += 1
                return
            pieces_.append((1, row_text(fields, out_index, perm, raw).encode("utf8")))
        except RECORD_ERRORS:
            stats["bad_records"] += 1

    pieces, rows, size = [], 0, 0
    for page_no in order:
        if progress and stats["pages"] % 64 == 0:
            progress(stats["pages"], total)
        page = ts.page(page_no)
        stats["pages"] += 1
        result = prepared.convert(page, kernel_bridge.MODE_BATCH) if prepared else None
        got = []
        if result is None or result.overflow:
            seen = 0
            for origin in iter_records(page, table.compact):
                seen += 1
                python_record(page, origin, got)
        else:
            seen = result.seen
            stats["deleted"] += result.deleted
            stats["bad_records"] += result.bad
            if result.rows:
                got.append((result.rows, result.text()))
            for origin in result.specials:
                python_record(page, origin, got)
        for count, text in got:
            pieces.append((count, text))
            rows += count
            size += len(text)
        declared, error = u16(page, PAGE_N_RECS), page_structure_error(page, table.compact)
        if error or declared != seen:
            stats["damaged_pages"] += 1
            stats["records_missing"] += max(0, declared - seen)
            if len(stats["damaged_examples"]) < 3:
                stats["damaged_examples"].append(f"página {page_no}: " + (error or f"o cabeçalho diz {declared} registros e só {seen} foram lidos"))
        if rows >= BATCH_ROWS or size >= 8 * 1048576:
            yield names, rows, pieces
            pieces, rows, size = [], 0, 0
    if progress:
        progress(total, total)
    if pieces:
        yield names, rows, pieces


# ------------------------------------------------------------------------------------------ tabelas pequenas
def carve_sql(carver, name, ddl_hint=None, progress=None):
    """Como InnodbCarver.carve, mas devolve (colunas, quantidade, [(quantidade, bytes), ...], estatísticas) com o texto SQL pronto."""
    table = carver.table(name)
    if not table:
        raise CarverError(f"tabela {name} não encontrada no dicionário interno")
    carver.require_readable(table)
    ts = carver.table_space(table)
    index_id = carver.clustered_id(table, ts)
    fields, n_key, parser, out_index = carver._fields_and_parser(table, ddl_hint)
    names, perm = column_layout(table, fields, out_index)
    prepared = kernel_bridge.prepare(table, fields, out_index, perm, n_key)
    stats = {"pages": 0, "bad_records": 0, "deleted": 0, "duplicates": 0}
    best = {}                     # chave (bytes) -> ((lsn, trx), apagado, texto em bytes)

    def external(data):
        return carver.read_external(data, ts)

    def python_record(page, origin, lsn):
        try:
            parsed = parser(page, origin, fields, external)
            if parsed is None:
                return
            deleted, raw = parsed
            key = key_bytes(raw, n_key)
            version = (lsn, int.from_bytes(raw[n_key] or b"", "big"))
            if key in best:
                stats["duplicates"] += 1
                if version <= best[key][0]:
                    return
            best[key] = (version, deleted, row_text(fields, out_index, perm, raw).encode("utf8"))
        except RECORD_ERRORS:
            stats["bad_records"] += 1

    leaves = ts.leaf_pages.get(index_id, [])
    for position, page_no in enumerate(leaves):
        if progress and position % 64 == 0:
            progress(position, len(leaves))
        page = ts.page(page_no)
        stats["pages"] += 1
        lsn = u64(page, FIL_PAGE_LSN)
        result = prepared.convert(page, kernel_bridge.MODE_FULL) if prepared else None
        if result is None or result.overflow:
            for origin in iter_records(page, table.compact):
                python_record(page, origin, lsn)
            continue
        stats["bad_records"] += result.bad
        meta, blob = result.meta, result.blob
        for i in range(0, len(meta), 9):
            key = blob[meta[i + 2]:meta[i + 2] + meta[i + 3]]
            version = (lsn, ((meta[i + 4] & 0xFFFFFFFF) << 32) | (meta[i + 5] & 0xFFFFFFFF))
            deleted = bool(meta[i + 1])
            if key in best:
                stats["duplicates"] += 1
                if version <= best[key][0]:
                    continue
            best[key] = (version, deleted, b"" if deleted else kernel_bridge.fix_floats(blob[meta[i + 6]:meta[i + 6] + meta[i + 7]]))
        for origin in result.specials:
            python_record(page, origin, lsn)
    if progress:
        progress(len(leaves), len(leaves))
    pieces = []
    for _version, deleted, text in best.values():
        if deleted:
            stats["deleted"] += 1
        else:
            pieces.append((1, text))
    return names, len(pieces), pieces, stats
