"""
corruptor_aleatorio.py - Estraga de forma ALEATÓRIA uma pasta data MySQL 5.5 (InnoDB) e anota o que foi destruído, para que a
recuperação possa ser conferida linha a linha.

Trabalha numa CÓPIA (recusa qualquer caminho dentro de ProgramData). A semente define tudo: com a mesma semente o estrago é o
mesmo; com sementes diferentes não há padrão. Cada execução sorteia, entre treze tipos de dano, quais usar, quantos e com que
intensidade (e, uma vez em sete, faz um estrago "catastrófico", dez vezes maior):

   zerar páginas soltas, zerar blocos contíguos, rasgar páginas (rodapé trocado), trocar bytes ao acaso, despejar lixo em
   faixas de bytes, copiar uma página sobre outra, estragar o cabeçalho de páginas, truncar o fim do ibdata1, estragar o
   dicionário interno, estragar as páginas de sistema (0 a 7), estragar o doublewrite, estragar `.frm` (zerar, truncar, trocar
   bytes, apagar, trocar entre tabelas), estragar o redo log.

GABARITO (pasta de saída): antes de cada dano o script decodifica as linhas das páginas que vai atingir (com o motor de leitura
do próprio projeto, na cópia ainda íntegra) e guarda o hash de cada uma:
   danos.json                         o que foi feito, com páginas e posições
   esperado.json                      por tabela: quantas linhas podem sumir e quantas podem aparecer alteradas
   permitido.<banco>.<tabela>.u64     hashes das linhas que podem sumir (páginas destruídas) ou mudar (bytes trocados)
Perda dentro do permitido é esperada; perda fora dele é falha da recuperação.

Uso: python corruptor_aleatorio.py <pasta data ORIGINAL (só leitura)> <CÓPIA dela, que será estragada> <pasta do gabarito> <semente>
"""
import array
import json
import os
import random
import struct
import sys
import zlib

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "py"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import frm_parser
from innodb_carver import (FIL_PAGE_NEXT, FIL_PAGE_PREV, PAGE_INDEX_ID, PAGE_LEVEL, CarverError, InnodbCarver, decode_value,
                           iter_records, sql_literal, u16, u32)
from page_health import FIL_PAGE_OFFSET, FIL_PAGE_TYPE, PAGE_SIZE

HIDDEN = {"mysql", "performance_schema", "information_schema", "test"}


def row_hash(values):
    raw = ("(" + ",".join(sql_literal(v) for v in values) + ")").encode("utf8", "replace")
    return (zlib.crc32(raw) << 32) | zlib.adler32(raw)


class Oraculo:
    """Sabe a que tabela pertence cada página de folha da cópia íntegra e decodifica as linhas de uma página."""

    def __init__(self, data, only_db=None):
        self.data = data
        self.carver = InnodbCarver(os.path.join(data, "ibdata1"), log=lambda m: None, data_dir=data).load()
        self.owner, self.tables = {}, {}
        for name, table in sorted(self.carver.tables.items()):
            if "/" not in name or name.split("/", 1)[0].lower() in HIDDEN or name.split("/", 1)[0].lower().startswith("sys"):
                continue
            if only_db and name.split("/", 1)[0].lower() != only_db.lower():
                continue   # bancos que não são o do teste: não têm o que cobrar, e ler todos custa horas
            ts = self.carver.table_space(table)
            if ts is not self.carver.space:
                continue
            index_id = self.carver.clustered_id(table, ts)
            leaves = ts.leaf_pages.get(index_id, [])
            if not leaves:
                continue
            db, short = name.split("/", 1)
            frm = os.path.join(data, db, short + ".frm")
            hint = frm_parser.read_table_ddl(frm, short) if os.path.exists(frm) else None
            self.tables[name] = (table, hint, index_id)
            for page in leaves:
                self.owner[page] = name
        self.dictionary = {kind: set(self.carver.space.leaf_pages.get(number, [])) for kind, number in
                           (("SYS_TABLES", 1), ("SYS_COLUMNS", 2), ("SYS_INDEXES", 3), ("SYS_FIELDS", 4))}
        self.n_pages = self.carver.space.n_pages
        self.blob_map = None

    def page_hashes(self, page_no):
        """Hashes das linhas vivas de UMA folha (a mesma conta do restaurador)."""
        name = self.owner.get(page_no)
        if not name:
            return None, []
        table, hint, _index = self.tables[name]
        fields, _n_key, parser, out_index = self.carver._fields_and_parser(table, hint)
        page, rows = self.carver.space.page(page_no), []
        for origin in iter_records(page, table.compact):
            try:
                parsed = parser(page, origin, fields, lambda data: self.carver.read_external(data, self.carver.space))
                if parsed is None or parsed[0]:
                    continue
                rows.append([None if parsed[1][i] is None else decode_value(fields[i], parsed[1][i]) for i in out_index])
            except (CarverError, IndexError, ValueError, struct.error):
                continue
        _names, rows = self.carver._in_column_order(table, fields, out_index, rows)
        return name, [row_hash(r) for r in rows]

    def blob_rows(self, page_no):
        """Linhas que têm valor longo (BLOB/TEXT fora da linha) gravado nesta página: se ela for estragada, o valor sai
        incompleto e a linha aparece alterada. Quem não é página de BLOB devolve []. O mapa é montado na primeira vez."""
        if u16(self.carver.space.page(page_no), FIL_PAGE_TYPE) != 10:
            return []
        if self.blob_map is None:
            self.blob_map = {}
            for name, (table, hint, index_id) in self.tables.items():
                fields, _n_key, parser, out_index = self.carver._fields_and_parser(table, hint)
                for leaf in self.carver.space.leaf_pages.get(index_id, []):
                    page, chains, rows = self.carver.space.page(leaf), [], []
                    for origin in iter_records(page, table.compact):
                        seen = []

                        def external(data, seen=seen):
                            seen.extend(self.chain(data))
                            return self.carver.read_external(data, self.carver.space)
                        try:
                            parsed = parser(page, origin, fields, external)
                            if parsed is None or parsed[0]:
                                continue
                            rows.append([None if parsed[1][i] is None else decode_value(fields[i], parsed[1][i]) for i in out_index])
                            chains.append(seen)
                        except (CarverError, IndexError, ValueError, struct.error):
                            continue
                    if any(chains):
                        _names, rows = self.carver._in_column_order(table, fields, out_index, rows)
                        for row, seen in zip(rows, chains):
                            for blob in seen:
                                self.blob_map.setdefault(blob, []).append((name, row_hash(row)))
        return self.blob_map.get(page_no, [])

    def chain(self, data):
        """Números das páginas de BLOB de uma referência externa (os 20 bytes do fim do campo)."""
        ref, pages, page_no, offset = data[-20:], [], None, 0
        page_no, offset = u32(ref, 4), u32(ref, 8)
        for _ in range(100000):
            if page_no in (0, 0xFFFFFFFF) or page_no >= self.n_pages:
                break
            pages.append(page_no)
            page_no, offset = u32(self.carver.space.page(page_no), offset + 4), 38
        return pages


class Estrago:
    def __init__(self, original, data, gabarito, seed):
        if "programdata" in os.path.abspath(data).lower():   # o original só é lido; o que recebe o estrago nunca fica no MySQL
            sys.exit("RECUSADO: este script não escreve na pasta do MySQL em ProgramData.")
        if os.path.abspath(original) == os.path.abspath(data):
            sys.exit("RECUSADO: o estrago vai numa CÓPIA, diferente do original.")
        self.data, self.gabarito, self.seed = data, gabarito, seed
        self.rng = random.Random(seed)
        self.ibdata = os.path.join(data, "ibdata1")
        # FOGO_SO_BANCO=<banco>: o ibdata1 é compartilhado por vários bancos; o estrago cai só nas páginas do banco escolhido
        #   (e no dicionário), como num teste de um banco só.
        self.only_db = os.environ.get("FOGO_SO_BANCO") or None
        self.oraculo = Oraculo(original, self.only_db)   # lê o original íntegro: o que as páginas tinham antes de qualquer dano
        self.n_pages = self.oraculo.n_pages
        self.universe = None
        if self.only_db:
            dictionary = set().union(*self.oraculo.dictionary.values())
            self.universe = sorted(p for p in set(self.oraculo.owner) | dictionary if p >= 8)
        self.log = []
        self.allowed = {}         # tabela -> [hashes que podem sumir]
        self.changed = {}         # tabela -> quantas linhas podem aparecer alteradas
        self.dict_hit = []        # páginas do dicionário atingidas
        self.noloss = []          # danos que não deveriam custar nenhuma linha
        scale = self.rng.choice([0.3, 0.6, 1, 1, 1.5, 2.5])
        self.catastrophic = self.rng.random() < 1 / 7
        self.scale = scale * (10 if self.catastrophic else 1)

    # ---------------------------------------------------------------- utilidades
    def count(self, low, high):
        return max(1, int(self.rng.randint(low, high) * self.scale))

    def random_pages(self, k, low=8):
        if self.universe:
            return sorted({self.rng.choice(self.universe) for _ in range(k)})
        return sorted({self.rng.randrange(low, self.n_pages) for _ in range(k)})

    def random_page(self):
        return self.rng.choice(self.universe) if self.universe else self.rng.randrange(8, self.n_pages)

    def write_at(self, page, data, offset=0):
        with open(self.ibdata, "r+b") as f:
            f.seek(page * PAGE_SIZE + offset)
            f.write(data)

    def read_page(self, page):
        with open(self.ibdata, "rb") as f:
            f.seek(page * PAGE_SIZE)
            return bytearray(f.read(PAGE_SIZE))

    def destroy(self, pages, kind):
        """Anota que as linhas destas páginas podem sumir (antes de estragar: a cópia ainda está íntegra)."""
        for page in pages:
            name, hashes = self.oraculo.page_hashes(page)
            if name:
                self.allowed.setdefault(name, []).extend(hashes)
            self.blob_hit(page)
            for table, leaves in self.oraculo.dictionary.items():
                if page in leaves:
                    self.dict_hit.append((table, page, kind))

    def blob_hit(self, page):
        """Página de BLOB atingida: as linhas dona dela podem sumir ou voltar com o valor longo incompleto."""
        for name, row in self.oraculo.blob_rows(page):
            self.allowed.setdefault(name, []).append(row)
            self.changed[name] = self.changed.get(name, 0) + 1

    # ---------------------------------------------------------------- danos no ibdata1
    def zerar_paginas(self):
        pages = self.random_pages(self.count(5, 150))
        self.destroy(pages, "zero")
        for p in pages:
            self.write_at(p, b"\0" * PAGE_SIZE)
        self.log.append({"dano": "zerar_paginas", "paginas": pages})

    def zerar_bloco(self):
        runs = []
        for _ in range(self.rng.randint(1, 3)):
            size = self.count(1, 200)
            start = self.random_page() if self.universe else self.rng.randrange(8, max(9, self.n_pages - size))
            pages = list(range(start, min(self.n_pages, start + size)))
            self.destroy(pages, "bloco")
            for p in pages:
                self.write_at(p, b"\0" * PAGE_SIZE)
            runs.append([start, len(pages)])
        self.log.append({"dano": "zerar_bloco", "blocos": runs})

    def rasgar(self):
        pages = self.random_pages(self.count(3, 60))
        for p in pages:
            self.write_at(p, bytes(self.rng.randrange(256) for _ in range(4)), PAGE_SIZE - 4)
        self.log.append({"dano": "rasgar", "paginas": pages})
        self.noloss.append(("rasgar", pages))

    def bytes_trocados(self):
        pages = self.random_pages(self.count(3, 80))
        self.flipped_pages(pages)
        for p in pages:
            for _ in range(self.rng.randint(1, 30)):
                where = self.rng.randrange(0, 200) if self.rng.random() < 0.4 else self.rng.randrange(0, PAGE_SIZE)
                self.write_at(p, bytes([self.rng.randrange(256)]), where)
        self.log.append({"dano": "bytes_trocados", "paginas": pages})

    def flipped_pages(self, pages):
        for page in pages:
            name, hashes = self.oraculo.page_hashes(page)
            if name:
                self.allowed.setdefault(name, []).extend(hashes)
                self.changed[name] = self.changed.get(name, 0) + len(hashes)
            self.blob_hit(page)
            for table, leaves in self.oraculo.dictionary.items():
                if page in leaves:
                    self.dict_hit.append((table, page, "bytes"))

    def lixo(self):
        ranges = []
        for _ in range(self.rng.randint(1, 4)):
            size = int(self.rng.choice([512, 4096, 65536, 1 << 20, 3 << 20]) * min(self.scale, 3))
            start = self.rng.randrange(8 * PAGE_SIZE, max(8 * PAGE_SIZE + 1, self.n_pages * PAGE_SIZE - size))
            if self.universe:
                start = self.random_page() * PAGE_SIZE
            first, last = start // PAGE_SIZE, (start + size) // PAGE_SIZE
            self.flipped_pages(range(first, min(self.n_pages, last + 1)))
            fill = b"\xff" * size if self.rng.random() < 0.3 else bytes(self.rng.randrange(256) for _ in range(min(size, 1 << 16))) * (size // (1 << 16) + 1)
            with open(self.ibdata, "r+b") as f:
                f.seek(start)
                f.write(fill[:size])
            ranges.append([start, size])
        self.log.append({"dano": "lixo", "faixas": ranges})

    def trocar_paginas(self):
        pairs = []
        for _ in range(self.count(2, 40)):
            a, b = self.random_page(), self.random_page()
            if a == b:
                continue
            self.destroy([b], "copia")
            self.write_at(b, bytes(self.read_page(a)))
            pairs.append([a, b])
        self.log.append({"dano": "trocar_paginas", "pares": pairs})

    def cabecalho(self):
        pages = self.random_pages(self.count(3, 40))
        self.destroy(pages, "cabecalho")
        for p in pages:
            offset, size = self.rng.choice([(FIL_PAGE_OFFSET, 4), (FIL_PAGE_TYPE, 2), (34, 4), (PAGE_INDEX_ID, 8), (PAGE_LEVEL, 2), (FIL_PAGE_PREV, 4), (FIL_PAGE_NEXT, 4)])
            self.write_at(p, bytes(self.rng.randrange(256) for _ in range(size)), offset)
        self.log.append({"dano": "cabecalho", "paginas": pages})

    def truncar(self):
        fraction = self.rng.uniform(0.0005, 0.25 if self.catastrophic else 0.06)
        cut = int(os.path.getsize(self.ibdata) * (1 - fraction))
        if self.rng.random() < 0.6:
            cut -= cut % PAGE_SIZE
        first = cut // PAGE_SIZE
        self.destroy(range(first, self.n_pages), "truncado")
        with open(self.ibdata, "r+b") as f:
            f.truncate(cut)
        self.log.append({"dano": "truncar", "novo_tamanho": cut, "paginas_perdidas": self.n_pages - first})

    def dicionario(self):
        pool = []
        for table, weight in (("SYS_TABLES", 5), ("SYS_COLUMNS", 3), ("SYS_INDEXES", 2), ("SYS_FIELDS", 2)):
            pool += [(table, p) for p in sorted(self.oraculo.dictionary[table])] * weight
        picked = self.rng.sample(pool, min(len(pool), self.count(1, 6)))
        for table, page in picked:
            self.dict_hit.append((table, page, "dicionario"))
            if self.rng.random() < 0.6:
                self.write_at(page, b"\0" * PAGE_SIZE)
            else:
                for _ in range(self.rng.randint(1, 40)):
                    self.write_at(page, bytes([self.rng.randrange(256)]), self.rng.randrange(0, PAGE_SIZE))
        self.log.append({"dano": "dicionario", "paginas": [[t, p] for t, p in picked]})

    def sistema(self):
        pages = sorted(self.rng.sample(range(0, 8), self.rng.randint(1, 3)))
        for p in pages:
            for _ in range(self.rng.randint(1, 60)):
                self.write_at(p, bytes([self.rng.randrange(256)]), self.rng.randrange(0, PAGE_SIZE))
        self.log.append({"dano": "paginas_de_sistema", "paginas": pages})

    def doublewrite(self):
        pages = sorted({self.rng.randrange(64, 192) for _ in range(self.count(2, 60))})
        for p in pages:
            self.write_at(p, b"\0" * PAGE_SIZE)
        self.log.append({"dano": "doublewrite", "paginas": pages})
        self.noloss.append(("doublewrite", pages))

    def redo(self):
        path = os.path.join(self.data, "ib_logfile0")
        if os.path.exists(path):
            with open(path, "r+b") as f:
                for _ in range(self.rng.randint(1, 20)):
                    f.seek(self.rng.randrange(0, os.path.getsize(path)))
                    f.write(bytes(self.rng.randrange(256) for _ in range(self.rng.randint(1, 4096))))
        self.log.append({"dano": "redo_log"})

    # ---------------------------------------------------------------- danos nos .frm
    def frm(self):
        files = sorted(os.path.join(self.data, db, f) for db in os.listdir(self.data) if db.lower() not in HIDDEN
                       and os.path.isdir(os.path.join(self.data, db)) for f in os.listdir(os.path.join(self.data, db)) if f.lower().endswith(".frm"))
        if not files:
            return
        done = []
        for path in self.rng.sample(files, min(len(files), self.rng.randint(1, 5))):
            op = self.rng.choice(["zerar", "truncar", "bytes", "apagar", "trocar", "lixo"])
            size = os.path.getsize(path)
            if op == "zerar":
                with open(path, "r+b") as f:
                    f.write(b"\0" * min(size, 64))
            elif op == "truncar":
                with open(path, "r+b") as f:
                    f.truncate(self.rng.randrange(0, max(1, size)))
            elif op == "bytes":
                with open(path, "r+b") as f:
                    for _ in range(self.rng.randint(1, 20)):
                        f.seek(self.rng.randrange(0, max(1, size)))
                        f.write(bytes([self.rng.randrange(256)]))
            elif op == "apagar":
                os.remove(path)
            elif op == "trocar":
                other = self.rng.choice(files)
                if other != path and os.path.exists(other):
                    with open(other, "rb") as f:
                        content = f.read()
                    with open(path, "wb") as f:
                        f.write(content)
            else:
                with open(path, "wb") as f:
                    f.write(bytes(self.rng.randrange(256) for _ in range(self.rng.randint(1, size + 200))))
            done.append([os.path.relpath(path, self.data), op])
        self.log.append({"dano": "frm", "arquivos": done})

    # ---------------------------------------------------------------- execução
    def run(self):
        operations = [("zerar_paginas", 0.8), ("zerar_bloco", 0.5), ("rasgar", 0.5), ("bytes_trocados", 0.6), ("lixo", 0.45),
                      ("trocar_paginas", 0.35), ("cabecalho", 0.4), ("truncar", 0.3), ("dicionario", 0.5), ("sistema", 0.25),
                      ("doublewrite", 0.3), ("frm", 0.6), ("redo", 0.25)]
        chosen = [name for name, prob in operations if self.rng.random() < prob]
        while len(chosen) < 3:
            chosen.append(self.rng.choice(operations)[0])
        self.rng.shuffle(chosen)
        for name in dict.fromkeys(chosen):
            getattr(self, name)()
        os.makedirs(self.gabarito, exist_ok=True)
        for table, hashes in self.allowed.items():
            db, short = table.split("/", 1)
            with open(os.path.join(self.gabarito, f"permitido.{db}.{short}.u64"), "wb") as f:
                array.array("Q", sorted(hashes)).tofile(f)
        summary = {"semente": self.seed, "catastrofico": self.catastrophic, "intensidade": self.scale,
                   "operacoes": list(dict.fromkeys(chosen)),
                   "tabelas": {t: {"linhas_que_podem_sumir": len(h), "linhas_que_podem_mudar": self.changed.get(t, 0)} for t, h in self.allowed.items()},
                   "dicionario_atingido": [list(x) for x in self.dict_hit]}
        json.dump(summary, open(os.path.join(self.gabarito, "esperado.json"), "w", encoding="utf8"), ensure_ascii=False, indent=1)
        json.dump(self.log, open(os.path.join(self.gabarito, "danos.json"), "w", encoding="utf8"), ensure_ascii=False, indent=1)
        return summary


if __name__ == "__main__":
    result = Estrago(sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])).run()
    print(f"semente {result['semente']}: {', '.join(result['operacoes'])} | catastrófico={result['catastrofico']} intensidade={result['intensidade']:.1f}")
    print(f"tabelas com linhas que podem sumir: {len(result['tabelas'])}, dicionário atingido em {len(result['dicionario_atingido'])} páginas")
