"""
lost_tables.py - Acha de volta as tabelas cuja linha em SYS_TABLES se perdeu.

PROBLEMA (visto ao corromper uma cópia de um banco de 13 GB): zerar UMA página da SYS_TABLES fez 157 tabelas sumirem do
dicionário e voltarem vazias. O motor já sabia reconstruir tabelas cujos ÍNDICES não têm dono em SYS_INDEXES
(dictionary_rebuild), mas aqui só a SYS_TABLES perdeu linhas: SYS_COLUMNS, SYS_INDEXES e SYS_FIELDS seguem intactos e
citam a tabela pelo número. Sem a linha da SYS_TABLES a tabela não é montada, e ninguém a procurava.

COMO: as tabelas que têm índice clusterizado em SYS_INDEXES e colunas em SYS_COLUMNS, mas nenhuma linha em SYS_TABLES,
são as perdidas. O NOME só existia na SYS_TABLES, então vem do .frm: a tabela cujo .frm tem exatamente as mesmas colunas, na
mesma ordem. Casamento único = tabela montada com o que o dicionário ainda guarda (tipos, índice, página raiz); ambíguo
(duas tabelas com as mesmas colunas) = não adivinha o nome. Tabela perdida sem .frm que case, ou ambígua, ainda tem os
dados: sai com o nome `_sem_nome_<id>` (o nome não existe mais), para que o dado não se perca.
"""
from innodb_carver import (DICT_CLUSTERED, DICT_HDR_FIRST_ID, DICT_N_COLS_COMPACT, PAGE_N_HEAP, Column, TableDef, u16)


def recover_lost_tables(carver, db, frm_columns, log=lambda message: None):
    """frm_columns = {nome da tabela: [nomes das colunas do .frm]} das tabelas com .frm e sem linha no dicionário.
    Registra no carver as que dá para casar e devolve {nome: como foi casada}."""
    carver.load()
    known = {table.id for table in carver.tables.values()}
    columns_of, fields_of, lost = {}, {}, {}
    for row in carver._read_sys_table("SYS_COLUMNS"):
        columns_of.setdefault(row["TABLE_ID"], []).append(row)
    for row in carver._read_sys_table("SYS_FIELDS"):
        pos = row["POS"] >> 16 if row["POS"] > 0xFFFF else row["POS"]
        fields_of.setdefault(row["INDEX_ID"], []).append((pos, row["COL_NAME"]))
    for row in carver._read_sys_table("SYS_INDEXES"):
        table_id = row["TABLE_ID"]
        if (table_id is None or table_id in known or table_id < DICT_HDR_FIRST_ID or row["ID"] is None
                or not (row["TYPE"] or 0) & DICT_CLUSTERED or table_id not in columns_of):
            continue
        lost[table_id] = row
    if not lost:
        return {}

    def signature(names):
        return tuple(name.lower() for name in names)

    orphans, wanted = {}, {}
    for table_id, index in lost.items():
        ordered = sorted(columns_of[table_id], key=lambda c: c["POS"])
        orphans.setdefault(signature(c["NAME"] for c in ordered), []).append((table_id, index, ordered))
    for name, names in frm_columns.items():
        wanted.setdefault(signature(names), []).append(name)

    def register(table_id, index, ordered, name):
        """Monta a tabela com o que o dicionário ainda guarda e a registra no leitor. False se não dá para usá-la."""
        compact = True
        if not index["SPACE"] and 0 < (index["PAGE_NO"] or 0) < carver.space.n_pages:
            compact = bool(u16(carver.space.page(index["PAGE_NO"]), PAGE_N_HEAP) & 0x8000)
        table = TableDef(f"{db}/{name}", table_id, len(ordered) | (DICT_N_COLS_COMPACT if compact else 0), space_id=index["SPACE"])
        table.columns = [Column(c["NAME"], c["MTYPE"], c["PRTYPE"], c["LEN"], c["POS"]) for c in ordered]
        table.clustered_index_id, table.root_page = index["ID"], index["PAGE_NO"]
        table.generated_pk = index["NAME"] == "GEN_CLUST_INDEX"
        table.pk_columns = [col for _pos, col in sorted(fields_of.get(index["ID"], []))]
        if not table.generated_pk and not table.pk_columns:
            return False
        carver.register_table(f"{db}/{name}", table)
        carver.index_ids.add(index["ID"])
        return True

    matched, used, ambiguous = {}, set(), 0
    for sig, names in wanted.items():
        candidates = orphans.get(sig, [])
        if len(candidates) != 1 or len(names) != 1:
            ambiguous += 1 if candidates else 0
            continue
        table_id, index, ordered = candidates[0]
        if register(table_id, index, ordered, names[0]):
            used.add(table_id)
            matched[names[0]] = "linha da SYS_TABLES perdida; colunas e índice vieram de SYS_COLUMNS e SYS_INDEXES"
    # O que sobrou (sem .frm que case, ou ambíguo) e ainda tem páginas de dados vira tabela SEM NOME: o dado é recuperado,
    #   o nome não existe mais. Não dá para saber de qual banco da pasta ela era.
    unnamed = 0
    for table_id, index in lost.items():
        if table_id in used or not carver.space.leaf_pages.get(index["ID"]):
            continue
        ordered = sorted(columns_of[table_id], key=lambda c: c["POS"])
        name = f"_sem_nome_{table_id}"
        if register(table_id, index, ordered, name):
            unnamed += 1
            matched[name] = ("sem nome: a linha da SYS_TABLES e o .frm se perderam; o banco de origem é desconhecido; "
                             "colunas e índice vieram de SYS_COLUMNS e SYS_INDEXES")
    log(f"{len(lost)} tabela(s) perderam a linha em SYS_TABLES: {len(matched) - unnamed} casadas com um .frm pelas colunas, "
        f"{ambiguous} ambíguas, {unnamed} recuperadas sem nome.")
    return matched
