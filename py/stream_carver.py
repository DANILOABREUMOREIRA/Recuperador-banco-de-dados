"""
stream_carver.py - Lê tabelas GRANDES do InnoDB em lotes, sem juntar a tabela inteira na memória.

POR QUÊ: innodb_carver.carve() guarda todas as linhas da tabela (e a melhor versão de cada chave) antes de devolver:
o pico de memória é de 5 a 9 vezes o tamanho da tabela em páginas. O Python do navegador tem uns 2 GB, então uma tabela
com mais de uns 300 MB de páginas não cabe. Num banco de 20 GB isso é o que mais pesa.

COMO: em vez de varrer as folhas em ordem de arquivo e escolher a versão mais nova de cada chave, lê só as folhas da
ÁRVORE VIVA: parte da página raiz e desce pelos ponteiros dos nós internos, como o MySQL (e o mysqldump) faz. Na árvore
viva cada chave aparece uma vez, então não há o que desduplicar e os registros saem em lotes, na ordem da chave primária.
- Folha que nenhum nó vivo cita é página liberada (de uma árvore antiga ou de um pedaço removido): não é lida. É o que
  faz `cclasstrib` voltar com as 155 linhas que o MySQL confirma, e não com as de versões antigas da tabela.
- Se a árvore está danificada (raiz ilegível ou um nó interno perdido), não dá para saber quais folhas são vivas: cai
  para as CADEIAS de folhas (ponteiros anterior/próxima, só os trechos de 2 ou mais páginas ligadas nos dois sentidos),
  que recuperam mais, mas podem trazer registros de páginas antigas. O relatório avisa.
Só as tabelas grandes usam este caminho; as pequenas continuam no carve() de sempre, que recupera também registros de
páginas liberadas.
"""
import struct

from innodb_carver import (FIL_NULL, FIL_PAGE_NEXT, FIL_PAGE_PREV, PAGE_INDEX_ID, PAGE_LEVEL, CarverError,
                           decode_value, iter_records, u16, u32, u64)
from page_health import FIL_PAGE_INDEX, FIL_PAGE_OFFSET, FIL_PAGE_TYPE, PAGE_SIZE

BATCH_ROWS = 4000            # linhas por lote, no máximo
BATCH_BYTES = 8 * 1048576    # bytes por lote (estimativa): tabelas com colunas grandes enchem o lote com poucas linhas
FIL_HEADER = 38
REC_STATUS_NODE_PTR = 1
FSP_IBD_ROOT_PAGE = 3        # raiz do índice clusterizado num .ibd


def _node_child(page, origin, key_fields, compact):
    """Número da página filha citada por um registro de nó interno (ponteiro de nó): os campos da chave e, no fim, 4 bytes."""
    if compact:
        if page[origin - 3] & 0x07 != REC_STATUS_NODE_PTR:
            return None
        nullable = sum(1 for f in key_fields if f.nullable)
        null_ptr, len_ptr = origin - 6, origin - 6 - (nullable + 7) // 8
        pos, null_i = origin, 0
        for f in key_fields:
            if f.nullable:
                is_null = page[null_ptr - null_i // 8] & (1 << (null_i % 8))
                null_i += 1
                if is_null:
                    continue
            size = f.fixed_size(True)
            if not size:
                size = page[len_ptr]
                len_ptr -= 1
                if f.two_byte_len_possible() and size & 0x80:
                    size = ((size & 0x3F) << 8) | page[len_ptr]
                    len_ptr -= 1
            pos += size
        if pos + 4 > PAGE_SIZE - 8:
            raise CarverError("ponteiro de nó ultrapassa a página")
        return u32(page, pos)
    # REDUNDANT: o filho é sempre os 4 últimos bytes do registro; o fim do último campo está nos offsets antes do cabeçalho.
    n_fields = (u16(page, origin - 4) >> 1) & 0x3FF
    if not n_fields:
        raise CarverError("registro de nó sem campos")
    one_byte = page[origin - 3] & 0x01
    last = n_fields - 1
    end = (page[origin - 7 - last] & 0x7F) if one_byte else (u16(page, origin - 8 - 2 * last) & 0x3FFF)
    if end < 4 or origin + end > PAGE_SIZE - 8:
        raise CarverError("offset de nó inválido")
    return u32(page, origin + end - 4)


def live_leaves(carver, ts, table, index_id, key_fields, leaf_set):
    """(folhas vivas na ordem da chave, informações) descendo da raiz. Devolve (None, motivo) se a árvore está danificada
    e não dá para saber quais folhas são vivas."""
    roots = [table.root_page] + ([FSP_IBD_ROOT_PAGE] if ts is not carver.space else [])
    root = next((r for r in roots if r not in (None, FIL_NULL, 0) and 0 <= r < ts.n_pages and _is_index_page(ts, r, index_id)), None)
    if root is None:
        return None, "raiz da árvore ilegível"
    order, seen, problems = [], set(), []

    def walk(page_no, level, depth):
        if depth > 12 or page_no in seen:
            return
        seen.add(page_no)
        if level == 0:                        # folha: só confere se é uma folha que a varredura reconheceu
            if page_no in leaf_set:
                order.append(page_no)
            else:
                problems.append(f"folha {page_no} ausente")
            return
        page = ts.page(page_no)
        if not _is_index_page(ts, page_no, index_id, level):
            problems.append(f"nó {page_no} inválido")
            return
        for origin in iter_records(page, table.compact):
            try:
                child = _node_child(page, origin, key_fields, table.compact)
            except (CarverError, IndexError, struct.error):
                problems.append(f"registro ilegível no nó {page_no}")
                continue
            if child is None or not 0 < child < ts.n_pages:
                problems.append(f"ponteiro inválido no nó {page_no}")
                continue
            walk(child, level - 1, depth + 1)

    root_page = ts.page(root)
    walk(root, u16(root_page, PAGE_LEVEL), 0)
    # Folha ausente = página que existiu e se perdeu (as linhas dela não existem mais): não invalida a árvore. Nó interno
    #   ilegível ou perdido, sim: a parte da árvore abaixo dele ficaria de fora sem ninguém saber.
    internal = [p for p in problems if not p.startswith("folha ")]
    if internal:
        return None, "árvore danificada (" + internal[0] + ")"
    return order, {"folhas_ausentes": len(problems)}


def _is_index_page(ts, page_no, index_id, level=None):
    page = ts.page(page_no)
    return (u16(page, FIL_PAGE_TYPE) == FIL_PAGE_INDEX and u32(page, FIL_PAGE_OFFSET) == page_no
            and u64(page, PAGE_INDEX_ID) == index_id and (level is None or u16(page, PAGE_LEVEL) == level))


def _links(ts, page_no):
    """(anterior, próxima) de uma página lendo só o cabeçalho de 38 bytes (ou a cópia do doublewrite, se houver)."""
    page = ts.overlay.get(page_no)
    if page is None:
        ts.f.seek(page_no * PAGE_SIZE)
        page = ts.f.read(FIL_HEADER)
    return u32(page, FIL_PAGE_PREV), u32(page, FIL_PAGE_NEXT)


def leaf_runs(ts, leaves):
    """Plano B (árvore danificada): trechos de folhas ligadas nos dois sentidos, só os de 2 ou mais páginas."""
    links = {page_no: _links(ts, page_no) for page_no in leaves}
    follow, has_prev = {}, set()
    for page_no, (_prev, nxt) in links.items():
        if nxt != FIL_NULL and nxt in links and links[nxt][0] == page_no:
            follow[page_no] = nxt
            has_prev.add(nxt)
    runs, seen = [], set()
    for page_no in leaves:                      # na ordem do arquivo
        if page_no in has_prev or page_no in seen:
            continue
        run, here = [], page_no
        while here is not None and here not in seen:   # 'seen' também protege de laço
            seen.add(here)
            run.append(here)
            here = follow.get(here)
        runs.append(run)
    kept = [run for run in runs if len(run) >= 2] or runs
    return [page_no for run in kept for page_no in run]


def stream_table(carver, name, ddl_hint=None, progress=None, stats=None):
    """Gera (colunas, linhas) em lotes, lendo as folhas da árvore viva. Registros apagados saem; registro ilegível é
    contado em stats["bad_records"] e não derruba a leitura. Preenche stats (opcional): mode ("árvore" ou "cadeias"),
    leaf_pages, read_pages, loose_pages (folhas fora da árvore viva), missing_leaves, pages, bad_records, deleted."""
    table = carver.table(name)
    if not table:
        raise CarverError(f"tabela {name} não encontrada no dicionário interno")
    carver.require_readable(table)
    ts = carver.table_space(table)
    index_id = carver.clustered_id(table, ts)
    fields, n_key, parser, out_index = carver._fields_and_parser(table, ddl_hint)
    leaves = ts.leaf_pages.get(index_id, [])
    order, info = live_leaves(carver, ts, table, index_id, fields[:n_key], set(leaves))
    stats = stats if stats is not None else {}
    stats.update(pages=0, bad_records=0, deleted=0, leaf_pages=len(leaves), missing_leaves=0)
    if order is None:
        stats.update(mode="cadeias", reason=info)
        order = leaf_runs(ts, leaves)
    else:
        stats.update(mode="árvore", missing_leaves=info["folhas_ausentes"])
    stats.update(read_pages=len(order), loose_pages=len(leaves) - len(order))
    total = len(order)

    def external(data):
        return carver.read_external(data, ts)

    batch, size = [], 0
    for page_no in order:
        if progress and stats["pages"] % 64 == 0:
            progress(stats["pages"], total)
        page = ts.page(page_no)
        stats["pages"] += 1
        for origin in iter_records(page, table.compact):
            try:
                parsed = parser(page, origin, fields, external)
                if parsed is None:
                    continue
                deleted, raw = parsed
                if deleted:
                    stats["deleted"] += 1
                    continue
                values = [None if raw[i] is None else decode_value(fields[i], raw[i]) for i in out_index]
            except (CarverError, IndexError, struct.error, ValueError):
                stats["bad_records"] += 1
                continue
            batch.append(values)
            size += 16 * len(values) + sum(len(v) for v in values if isinstance(v, (bytes, str)))
        if len(batch) >= BATCH_ROWS or size >= BATCH_BYTES:
            names, rows = carver._in_column_order(table, fields, out_index, batch)
            batch, size = [], 0
            yield names, rows
    if progress:
        progress(total, total)
    if batch:
        yield carver._in_column_order(table, fields, out_index, batch)
