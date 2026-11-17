"""
kernel_bridge.py - Ponte entre o Python (Pyodide) e o kernel WebAssembly (kernel.wasm, veja kernel.js).
"""

# VISÃO GERAL: no navegador, o trecho mais lento da recuperação (ler cada registro da página, decodificar e escrever o texto SQL)
#   roda no kernel, compilado para WebAssembly. Este arquivo (1) descreve as colunas da tabela para o kernel, nos mesmos termos
#   que innodb_carver.decode_value usa; (2) entrega uma página ao kernel e (3) devolve o resultado já separado. O que o kernel não
#   sabe fazer (valor fora da página, FLOAT/DOUBLE, texto UTF-8 inválido) volta como "registro especial" e o Python o refaz.
# FORA DO NAVEGADOR (Python comum, testes): KERNEL fica None e tudo roda no caminho antigo, só em Python.

import re
import struct

from innodb_carver import (CHARSET_BINARY, DATA_BINARY, DATA_BLOB, DATA_DOUBLE, DATA_FIXBINARY, DATA_FLOAT, DATA_INT, DATA_MYSQL,
                           DATA_SYS, MYSQL_BIT, MYSQL_DATE, MYSQL_DATETIME, MYSQL_NEWDATE, MYSQL_NEWDECIMAL, MYSQL_TIME,
                           MYSQL_TIMESTAMP, MYSQL_YEAR, UTF8_IDS, UTF8MB4_IDS)

# kinds e flags: iguais aos de dev/wasm/assembly/kernel.ts
K_SKIP, K_INT, K_DATETIME, K_DATE, K_TIME, K_TIMESTAMP, K_YEAR, K_FLOAT = 0, 1, 2, 3, 4, 5, 6, 7
K_DECIMAL, K_BIT, K_BINARY, K_UTF8, K_CP1252 = 9, 10, 11, 12, 13
F_UNSIGNED, F_RSTRIP = 1, 2
SYS_NAMES = ("DB_ROW_ID", "DB_TRX_ID", "DB_ROLL_PTR")
MODE_BATCH, MODE_FULL = 0, 1
_INF = float("inf")

KERNEL = None        # o objeto JS (kernel.js), registrado pelo worker.js; None = sem kernel
ENABLED = True       # os testes desligam para medir o caminho antigo


def set_kernel(js_kernel):
    global KERNEL
    KERNEL = js_kernel


def available():
    return KERNEL is not None and ENABLED


# FAZ: o 'kind' de uma coluna: espelha, na mesma ordem, as decisões de innodb_carver.decode_value.
def field_kind(col):
    mt, ty = col.mtype, col.mysql_type
    if mt == DATA_SYS and col.name in SYS_NAMES:
        return K_SKIP, 0, 0, 0
    if mt == DATA_INT:
        kind = {MYSQL_DATETIME: K_DATETIME, MYSQL_DATE: K_DATE, MYSQL_NEWDATE: K_DATE, MYSQL_TIME: K_TIME,
                MYSQL_TIMESTAMP: K_TIMESTAMP, MYSQL_YEAR: K_YEAR}.get(ty, K_INT)
        return kind, (F_UNSIGNED if col.unsigned else 0), 0, 0
    if mt in (DATA_FLOAT, DATA_DOUBLE):
        return K_FLOAT, 0, 4 if mt == DATA_FLOAT else 8, 0     # p = tamanho esperado em bytes
    if ty == MYSQL_NEWDECIMAL:
        if col.decimal:
            return K_DECIMAL, 0, col.decimal[0], col.decimal[1]
        return K_BINARY, 0, 0, 0
    if ty == MYSQL_BIT:
        return K_BIT, 0, 0, 0
    if col.coll_id == CHARSET_BINARY or mt in (DATA_FIXBINARY, DATA_BINARY) or (mt == DATA_BLOB and col.coll_id in (0, CHARSET_BINARY)):
        return K_BINARY, 0, 0, 0
    flags = F_RSTRIP if (col.fixed_size(True) or mt == DATA_MYSQL) else 0
    return (K_UTF8 if (col.coll_id in UTF8_IDS or col.coll_id in UTF8MB4_IDS) else K_CP1252), flags, 0, 0


_current = None      # a tabela cuja descrição está hoje no kernel (ele guarda uma só)


class Prepared:
    """Uma tabela descrita para o kernel: a ordem das colunas de saída e as funções que convertem páginas."""

    def __init__(self, kernel, fields, out_index, perm, n_key):
        self.kernel, self.fields, self.out_index, self.perm, self.n_key = kernel, fields, out_index, perm, n_key
        self.order = [out_index[i] for i in perm]          # campo de cada coluna de saída, na ordem do CREATE TABLE
        nullable = sum(1 for f in fields if f.nullable)
        ints = [len(fields), nullable, n_key, len(self.order), 0, 0, 0, 0]
        for f in fields:
            kind, flags, p, s = field_kind(f)
            ints += [kind, f.fixed_size(True), 1 if f.nullable else 0, 1 if f.two_byte_len_possible() else 0, flags, p, s, 0]
        ints += self.order
        self.schema = struct.pack(f"<{len(ints)}i", *ints)
        self.loaded = False

    def load(self):
        global _current
        self.kernel.setSchema(self.schema)
        _current = self

    # FAZ: converte uma página. Devolve Page (veja abaixo) ou None se o kernel falhar (quem chama refaz a página em Python).
    def convert(self, page, mode):
        if _current is not self:
            self.load()          # outra tabela usou o kernel no meio: devolve a descrição desta
        try:
            return Page(self.kernel.convert(page, mode).to_bytes(), mode)
        except Exception:        # noqa: BLE001 - qualquer falha do kernel cai no Python, que é o caminho de referência
            return None


class Page:
    """O resultado de uma página: contagens, registros especiais, metadados (modo 1) e o texto."""

    __slots__ = ("seen", "deleted", "bad", "rows", "overflow", "specials", "meta", "blob")

    def __init__(self, pack, mode):
        seen, deleted, bad, rows, n_special, n_meta, out_len, overflow = struct.unpack_from("<8i", pack, 0)
        self.seen, self.deleted, self.bad, self.rows, self.overflow = seen, deleted, bad, rows, bool(overflow)
        at = 32
        self.specials = struct.unpack_from(f"<{n_special}i", pack, at) if n_special else ()
        at += 4 * n_special
        self.meta = memoryview(pack)[at:at + 36 * n_meta].cast("i") if n_meta else ()
        at += 36 * n_meta
        self.blob = pack[at:at + out_len]

    # modo 0: o texto das linhas vivas juntas por vírgula (o kernel deixa uma vírgula depois de cada linha)
    def text(self):
        return fix_floats(self.blob[:-1]) if self.blob else b""


_FLOAT = re.compile(rb"\xff(?:\x00(.{4})|\x01(.{8}))", re.S)


def _float_text(m):
    value = struct.unpack("<f", m[1])[0] if m[1] is not None else struct.unpack("<d", m[2])[0]
    return b"NULL" if value != value or value in (_INF, -_INF) else repr(value).encode()


# FAZ: troca os marcadores de FLOAT/DOUBLE que o kernel deixa no texto pelo número escrito como o Python escreve (repr);
#   NaN e infinito viram NULL, como em innodb_carver.float_or_none.
def fix_floats(text):
    return _FLOAT.sub(_float_text, text) if b"\xff" in text else text


# FAZ: prepara a tabela para o kernel, ou devolve None quando não dá (formato REDUNDANT, sem kernel, desligado, colunas demais).
def prepare(table, fields, out_index, perm, n_key):
    if not available() or not table.compact or len(fields) > 4000:
        return None
    prepared = Prepared(KERNEL, fields, out_index, perm, n_key)
    prepared.load()
    return prepared
