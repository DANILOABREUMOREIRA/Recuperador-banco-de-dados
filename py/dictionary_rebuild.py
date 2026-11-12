"""
dictionary_rebuild.py - Acha os dados de uma tabela quando o dicionário interno do InnoDB (SYS_TABLES/SYS_INDEXES...)
perdeu a linha dela ou foi destruído por inteiro, usando o .frm como definição.

O que o TwinDB faz na mão (create_defs.pl + procurar o index_id por tentativa) e o que a pesquisa de "template matching"
de registros descreve: cada índice clusterizado do ibdata1 tem folhas cheias de registros que só fazem sentido com UMA
definição de colunas. Para cada índice sem dono (nenhuma linha do dicionário o cita) tentamos decodificar uma amostra
de registros com a definição de cada tabela sem dono, e ficamos com a que decodifica quase tudo, com DB_TRX_ID plausível,
chaves primárias distintas e valores sensatos (datas válidas, UTF-8 válido, tamanhos dentro do declarado).

Tabelas com arquivo .ibd próprio (innodb_file_per_table) não precisam de adivinhação: o arquivo tem o nome da tabela e a
raiz do índice clusterizado é a página 3.
"""

# VISÃO GERAL: o 'catálogo' do InnoDB (o dicionário interno) diz a que tabela pertence cada conjunto de páginas. Se
#   ele perdeu a linha de uma tabela, ou foi destruído por inteiro, as páginas com os dados continuam no arquivo, mas
#   ficam 'sem dono'. Este arquivo ACHA o dono usando o arquivo .frm da tabela como gabarito: - tabelas com arquivo
#   .ibd próprio (innodb_file_per_table): o NOME do arquivo já diz a tabela e a raiz do índice é a página 3. Fácil. -
#   tabelas dentro do ibdata1: para cada índice 'sem dono', tenta decodificar uma amostra de registros com a definição
#   de cada .frm candidato e fica com o que decodifica quase tudo, com valores plausíveis (datas válidas, texto UTF-8
#   válido, tamanhos dentro do declarado) e com a GEOMETRIA certa (o fim de cada registro é o começo do seguinte). Se
#   um índice serve a duas tabelas parecidas, é AMBÍGUO e não é registrado: melhor não adivinhar. É o que o TwinDB faz
#   'na mão' (create_defs.pl + procurar o index_id por tentativa). Chamado por engine_objects._reconstruct_missing.

# Importações: 'os' = arquivos; 'struct' = ler números de bytes; frm_parser (as fichas .frm), innodb_carver (leitura
#   de páginas) e page_health são deste projeto.
import os
import struct

import frm_parser as fp
import innodb_carver as ic
from page_health import TRX_SYS_PAGE_NO

# TRX_SYS_TRX_ID_STORE: posição, na página 5 (TRX_SYS), do maior id de transação gravado. Usado para descartar ids de
#   transação absurdos.
TRX_SYS_TRX_ID_STORE = 38
# LIMIARES DA PONTUAÇÃO: MIN_SCORE (90%) = fração mínima de registros plausíveis; MIN_GEOMETRY (70%) = adjacência
#   mínima dos registros; AMBIGUITY_MARGIN = se dois candidatos estão a menos de 0,15 de diferença, o casamento é
#   ambíguo.
MIN_SCORE = 0.9
MIN_GEOMETRY = 0.7
AMBIGUITY_MARGIN = 0.15
# TAMANHO DA AMOSTRA examinada para pontuar um índice: 3 páginas e até 40 registros (suficiente para decidir e
#   rápido).
SAMPLE_PAGES = 3
SAMPLE_RECORDS = 40
# MAX_TRX_SLACK: folga acima do maior id de transação conhecido (1 milhão) antes de considerar um DB_TRX_ID absurdo.
MAX_TRX_SLACK = 1 << 20
# TEXT_TYPES: tipos internos de coluna que guardam texto.
TEXT_TYPES = (ic.DATA_VARCHAR, ic.DATA_CHAR, ic.DATA_MYSQL, ic.DATA_VARMYSQL)


# ---------------------------------------------------------------------- .frm -> definição do InnoDB
# FAZ: devolve uma função que diz se um VALOR decodificado é plausível para a coluna (ou None se não há o que checar).
#   Texto: tamanho dentro do declarado, UTF-8 válido e sem caracteres de controle; DATE/DATETIME: mês e dia dentro do
#   limite; float: finito. Se decodificar com a definição ERRADA, os valores saem absurdos e essas funções os
#   reprovam.
def _validator(col):
    """Função (valor decodificado, bytes) -> bool que diz se o valor é plausível para a coluna (ou None)."""
    # 'm' = tipo interno da coluna; 'ty' = tipo MySQL (byte baixo do 'prtype').
    m, ty = col.mtype, col.mysql_type
    # Colunas de TEXTO: o valor não pode ser maior que o declarado, deve ser UTF-8 válido (se o charset for UTF-8) e
    #   não pode ter bytes de controle.
    if m in TEXT_TYPES and col.coll_id != ic.CHARSET_BINARY:
        strict = col.coll_id in ic.UTF8_IDS or col.coll_id in ic.UTF8MB4_IDS or col.coll_id == 11

        # A função de verificação de texto.
        def check_text(value, raw):
            if col.len and len(raw) > col.len:
                return False
            data = bytes(raw)
            if strict:
                try:
                    data.decode("utf8")
                except UnicodeDecodeError:
                    return False
            return not any(b < 9 or 13 < b < 32 for b in data)

        return check_text
    if m in (ic.DATA_BINARY, ic.DATA_VARCHAR) and col.len:
        return lambda value, raw: len(raw) <= col.len
    # Colunas DATE: mês de 0 a 12, dia de 0 a 31 e ano < 10000.
    if m == ic.DATA_INT and ty in (ic.MYSQL_DATE, ic.MYSQL_NEWDATE):
        def check_date(value, raw):
            y, mo, d = (int(x) for x in str(value).split("-"))
            return 0 <= mo <= 12 and 0 <= d <= 31 and y < 10000
        return check_date
    # Colunas DATETIME: além da data, hora < 24, minuto e segundo < 60.
    if m == ic.DATA_INT and ty == ic.MYSQL_DATETIME:
        def check_datetime(value, raw):
            date, clock = str(value).split(" ")
            y, mo, d = (int(x) for x in date.split("-"))
            h, mi, s = (int(x) for x in clock.split(":"))
            return 0 <= mo <= 12 and 0 <= d <= 31 and h < 24 and mi < 60 and s < 60
        return check_datetime
    # Colunas FLOAT/DOUBLE: precisam ser números finitos e razoáveis.
    if m in (ic.DATA_FLOAT, ic.DATA_DOUBLE):
        return lambda value, raw: value is None or abs(value) < 1e300
    return None


# FAZ: converte uma coluna do .frm (FrmColumn) numa coluna no formato do InnoDB (ic.Column), com os mesmos
#   mtype/prtype/tamanho que o ha_innodb gravaria no dicionário. É a 'tradução' de uma linguagem para a outra, coluna
#   por coluna, tipo por tipo.
def innodb_column(col, position):
    """FrmColumn -> ic.Column com os mesmos mtype/prtype/len que o ha_innodb grava em SYS_COLUMNS."""
    # 'kind' = a categoria da coluna no .frm; 'notnull' = flag de NOT NULL no formato do InnoDB.
    kind, notnull = col.kind, (0 if col.nullable else ic.DATA_NOT_NULL)
    cs = col.charset_id
    # Cada ramo abaixo traduz UM tipo do .frm: define o tipo interno (mtype), o tipo MySQL, o tamanho em bytes e as
    #   flags (sinal, binário).
    if kind == "int":
        mtype, mysql, length = ic.DATA_INT, col.ftype, fp.pack_length(col)
        flags = ic.DATA_BINARY_TYPE | (ic.DATA_UNSIGNED if col.unsigned else 0)
    elif kind == "year":
        mtype, mysql, length, flags = ic.DATA_INT, ic.MYSQL_YEAR, 1, ic.DATA_BINARY_TYPE | ic.DATA_UNSIGNED
    elif kind == "timestamp":
        mtype, mysql, length, flags = ic.DATA_INT, ic.MYSQL_TIMESTAMP, 4, ic.DATA_BINARY_TYPE | ic.DATA_UNSIGNED
    elif kind in ("date", "time", "datetime"):
        mysql = {"date": ic.MYSQL_DATE, "time": ic.MYSQL_TIME, "datetime": ic.MYSQL_DATETIME}[kind]
        mtype, length, flags = ic.DATA_INT, fp.pack_length(col), ic.DATA_BINARY_TYPE
    elif kind == "float":
        mtype = ic.DATA_FLOAT if col.ftype == fp.T_FLOAT else ic.DATA_DOUBLE
        mysql, length = (ic.MYSQL_FLOAT, 4) if col.ftype == fp.T_FLOAT else (ic.MYSQL_DOUBLE, 8)
        flags = ic.DATA_BINARY_TYPE | (ic.DATA_UNSIGNED if col.unsigned else 0)
    elif kind == "decimal":
        mtype, mysql, length = ic.DATA_FIXBINARY, ic.MYSQL_NEWDECIMAL, fp.pack_length(col)
        flags = ic.DATA_BINARY_TYPE | (ic.DATA_UNSIGNED if col.unsigned else 0)
    elif kind == "bit":
        mtype, mysql, length = ic.DATA_FIXBINARY, ic.MYSQL_BIT, fp.pack_length(col)
        flags, cs = ic.DATA_BINARY_TYPE | ic.DATA_UNSIGNED, 63
    # ENUM e SET são guardados como INTEIROS (o índice do valor na lista).
    elif kind in ("enum", "set"):
        mtype, mysql, length = ic.DATA_INT, ic.MYSQL_STRING, fp.pack_length(col)
        flags, cs = ic.DATA_UNSIGNED, 0
    elif kind == "char":
        mysql, length = ic.MYSQL_STRING, col.length
        if col.is_binary_charset:
            mtype, flags = ic.DATA_FIXBINARY, ic.DATA_BINARY_TYPE
        else:
            mtype, flags = (ic.DATA_CHAR if cs == 8 else ic.DATA_MYSQL), 0
    elif kind == "varchar":
        mysql, length = ic.MYSQL_VARCHAR, col.length
        if col.is_binary_charset:
            mtype, flags = ic.DATA_BINARY, ic.DATA_BINARY_TYPE
        else:
            mtype, flags = (ic.DATA_VARCHAR if cs == 8 else ic.DATA_VARMYSQL), 0
    elif kind == "blob":
        mtype, mysql, length = ic.DATA_BLOB, ic.MYSQL_BLOB, fp.BLOB_KIND[col.ftype][1] + 8
        flags = ic.DATA_BINARY_TYPE if col.is_binary_charset else 0
    else:
        # Tipo sem tradução (ex.: geometria): recusa em vez de adivinhar.
        raise ic.CarverError(f"coluna {col.name}: tipo {kind} não suportado na reconstrução")
    # Monta a coluna. O 'prtype' junta, num só número: tipo MySQL (byte baixo), flags (NOT NULL, sem sinal, binário) e
    #   o charset (a partir do bit 16).
    column = ic.Column(col.name, mtype, mysql | flags | notnull | (cs << 16), length, position)
    if kind == "decimal":
        column.decimal = fp.decimal_precision(col)
    # Anexa a função de plausibilidade e o valor máximo (para ENUM/SET), usados na pontuação.
    column.check = _validator(column)
    column.max_value = (1 << (8 * length)) - 1 if kind in ("enum", "set") else None
    return column


# FAZ: monta o equivalente ao que o dicionário teria para esta tabela (TableDef): colunas, chave clusterizada (a
#   PRIMARY KEY; sem PK, o InnoDB usa uma chave oculta), e formato (COMPACT/REDUNDANT/DYNAMIC). Ainda sem ids: quem
#   preenche é o casamento.
def tabledef_from_frm(full_name, frm_table, compact=True):
    """TableDef equivalente ao que o dicionário teria para esta tabela (sem ids): colunas, chave clusterizada, formato."""
    # Flags da tabela: bit 0 = COMPACT; ROW_FORMAT=DYNAMIC acrescenta o bit Barracuda.
    flags = 1 if compact else 0
    if frm_table.row_type == 2:
        flags |= ic.DICT_TF_FORMAT_BARRACUDA
    td = ic.TableDef(full_name, None, ic.DICT_N_COLS_COMPACT if compact else 0, flags=flags or None)
    td.columns = [innodb_column(c, i) for i, c in enumerate(frm_table.columns)]
    # Chave clusterizada: a PRIMARY KEY do .frm; sem PK, o InnoDB cria uma chave oculta (DB_ROW_ID).
    key = frm_table.primary_key()
    if key:
        td.pk_columns = [col.name for col, _ in key.parts]
    else:
        td.generated_pk = True
    td.reconstructed = True
    return td


# ---------------------------------------------------------------------- pontuação
# FAZ: lê da página TRX_SYS o maior id de transação já gravado (mais uma folga) para descartar registros com DB_TRX_ID
#   absurdo, sinal de decodificação errada.
def max_trx_id(carver):
    """Maior id de transação gravado (página TRX_SYS), para descartar DB_TRX_ID absurdo."""
    # Lê o maior id de transação; se a página estiver ilegível, usa um teto enorme (não descarta nada).
    try:
        page = carver.space.page(TRX_SYS_PAGE_NO)
        value = struct.unpack_from(">Q", page, TRX_SYS_TRX_ID_STORE)[0]
        if 0 < value < 1 << 48:
            return value + MAX_TRX_SLACK
    except (ic.CarverError, struct.error):
        pass
    return 1 << 48


# FAZ: mede a GEOMETRIA: numa página sem buracos, o fim de um registro é exatamente o começo do seguinte, o primeiro
#   registro começa logo depois do supremum e o último termina no topo do heap. Devolve a fração dessas verificações
#   que a definição satisfaz: perto de 100% com a definição certa, perto de 0% com a errada.
def adjacency(page, table, fields):
    """Fração das verificações de geometria da página que a definição satisfaz: o fim de cada registro é o começo do
    seguinte no heap, o primeiro começa logo depois do supremum e o último termina no topo do heap. Páginas sem buracos
    dão 100% com a definição certa e quase 0% com uma errada."""
    compact = table.compact
    # Passo 1: para cada registro da página, calcula onde ele COMEÇA e onde TERMINA, segundo a definição testada.
    extents = []
    for origin in ic.iter_records(page, compact):
        try:
            extents.append((origin,) + ic.record_extent(page, origin, fields, compact))
        except (IndexError, ic.CarverError, struct.error):
            extents.append((origin, -1, -1))
    # Sem registros: não há como medir.
    if not extents:
        return None
    # Passo 2: ordena pela posição na página e compara cada registro com o seguinte.
    extents.sort()
    checks = matches = 0
    for (_, _, end), (_, start, _) in zip(extents, extents[1:]):
        checks += 1
        matches += end == start
    # Passo 3: o primeiro registro começa em 120 (COMPACT) e o último deve terminar no topo do heap.
    first_start = 120 if compact else None
    if first_start:
        checks += 1
        matches += extents[0][1] == first_start
    checks += 1
    matches += extents[-1][2] == ic.u16(page, ic.PAGE_HEADER + 2)  # PAGE_HEAP_TOP
    return matches / checks


# FAZ: dá nota a um índice para uma definição de tabela: 'plausivel' (fração de registros que decodificam com valores
#   sensatos), 'geometria' (adjacência) e 'vistos' (quantos registros examinou). Examina só uma amostra (algumas
#   páginas e registros).
def score_index(ts, leaf_pages, table, trx_limit, sample_pages=SAMPLE_PAGES, sample_records=SAMPLE_RECORDS):
    """{"plausivel": fração de registros que decodificam com valores sensatos, "geometria": adjacência dos registros,
    "vistos": registros examinados}. A página diz se é COMPACT ou REDUNDANT; formato errado já sai sem pontos."""
    # Prepara: a lista de campos do registro (PK, DB_TRX_ID, DB_ROLL_PTR, colunas), o número de campos da chave e o
    #   decodificador (COMPACT ou REDUNDANT).
    fields = table.record_fields()
    n_key = 1 if table.generated_pk else len(table.pk_columns)
    parser = ic.parse_compact if table.compact else ic.parse_redundant
    # Contadores: 'total' = registros examinados; 'good' = os que passaram em todas as verificações; 'keys' = chaves
    #   já vistas; 'geometry' = notas de geometria por página.
    good = total = 0
    keys, geometry = set(), []
    # Percorre as primeiras páginas de folha do índice (a amostra).
    for page_no in leaf_pages[:sample_pages]:
        page = ts.page(page_no)
        # Página com estrutura destruída: não serve para pontuar.
        if ic.page_structure_error(page, table.compact):
            continue
        # Mede a geometria da página com esta definição.
        shape = adjacency(page, table, fields)
        if shape is not None:
            geometry.append(shape)
        # Percorre os registros da página, até o limite da amostra.
        for origin in ic.iter_records(page, table.compact):
            if total >= sample_records:
                break
            total += 1
            try:
                # Decodifica o registro com a definição testada; erro de decodificação = registro não plausível.
                parsed = parser(page, origin, fields, lambda data: data)
                # Ignora registros de outro tipo e os marcados como apagados.
                if parsed is None or parsed[0]:
                    continue
                raw = parsed[1]
                # DB_TRX_ID absurdo (maior que o conhecido): decodificação errada.
                if ic.be_uint(raw[n_key] or b"") > trx_limit:
                    continue
                key = tuple(bytes(v) if v is not None else None for v in raw[:n_key])
                # Chave repetida na amostra: não pode (chave primária é única) -> definição errada.
                if key in keys:
                    continue
                keys.add(key)
                # Confere cada coluna: valor plausível e, para ENUM/SET, índice dentro da lista.
                valid = True
                for field, data in zip(fields, raw):
                    if data is None or field.mtype == ic.DATA_SYS:
                        continue
                    value = ic.decode_value(field, data)
                    check = getattr(field, "check", None)
                    if check and not check(value, data):
                        valid = False
                        break
                    limit = getattr(field, "max_value", None)
                    if limit is not None and ic.be_uint(data) > limit:
                        valid = False
                        break
                # Conta o registro como bom se passou em tudo.
                good += valid
            except (ic.CarverError, IndexError, struct.error, ValueError, TypeError):
                pass
    # Devolve as notas: fração plausível, quantos registros viu e a média da geometria.
    return {"plausivel": good / total if total else 0.0, "vistos": total,
            "geometria": sum(geometry) / len(geometry) if geometry else 0.0}


# FAZ: diz se a página está no formato COMPACT (bit 0x8000 no número de registros do heap) ou no antigo REDUNDANT.
def page_compact(ts, page_no):
    return bool(ic.u16(ts.page(page_no), ic.PAGE_N_HEAP) & 0x8000)


# ---------------------------------------------------------------------- casamento
# FAZ: lê os arquivos .frm do banco e devolve {nome: tabela} só das tabelas InnoDB que o parser consegue descrever;
#   ignora .frm ilegíveis ou de outros motores.
def frm_candidates(data_dir, db_name, wanted=None, charsets=None):
    """{nome: FrmTable} das tabelas InnoDB do banco que o .frm consegue descrever."""
    # Lê cada .frm (se pedido, só os de tabelas 'wanted'); o que não for legível é ignorado.
    found = {}
    folder = os.path.join(data_dir, db_name)
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return found
    for entry in names:
        if not entry.lower().endswith(".frm"):
            continue
        name = entry[:-4]
        if wanted is not None and name.lower() not in wanted:
            continue
        try:
            with open(os.path.join(folder, entry), "rb") as f:
                table = fp.parse_frm(f.read(), charsets)
        except (OSError, fp.FrmError, KeyError, IndexError, struct.error, ValueError):
            continue
        # Só interessam tabelas InnoDB (ou sem engine informado).
        if table.engine.lower() in ("innodb", ""):
            table.name = name
            found[name] = table
    return found


# FAZ: a função principal. Recebe os candidatos (.frm) e tenta ligar cada um às suas páginas, REGISTRANDO no carver as
#   tabelas achadas. Devolve ({nome: como foi casado}, {nome: por que não foi}). Parte 1: arquivos .ibd (nome do
#   arquivo = tabela). Parte 2: índices sem dono no ibdata1 (por pontuação).
def reconstruct(carver, candidates, db_name, log=None):
    """Registra no carver as tabelas de `candidates` ({nome: FrmTable}) que dá para casar com páginas.
    Retorna ({nome: descrição do casamento}, {nome: motivo de não ter casado})."""
    log = log or (lambda msg: None)
    carver.load()
    # 'matched' = tabelas achadas; 'missed' = não achadas, com o motivo; 'pending' = ainda sem dono.
    matched, missed = {}, {}
    pending = dict(candidates)

    # PARTE 1: arquivos .ibd. O nome do arquivo diz qual é a tabela; confere se o conteúdo combina com o .frm (>= 90%
    #   plausível).
    # 1) Um .ibd por tabela: o nome do arquivo diz qual é a tabela.
    for name, frm in sorted(pending.items()):
        path = os.path.join(carver.data_dir, db_name, name + ".ibd")
        if not os.path.isfile(path):
            continue
        try:
            ts = ic.Tablespace(path)
            ts.scan()
            index_id = ts.root_index_id()
            if index_id is None or not ts.leaf_pages.get(index_id):
                missed[name] = "o .ibd não tem raiz de índice na página 3 ou não tem folhas"
                ts.close()
                continue
            compact = page_compact(ts, ts.leaf_pages[index_id][0])
            table = tabledef_from_frm(f"{db_name}/{name}", frm, compact)
            result = score_index(ts, ts.leaf_pages[index_id], table, 1 << 48)
        except (ic.CarverError, OSError) as e:
            missed[name] = f"erro lendo o .ibd: {e}"
            continue
        if result["vistos"] and result["plausivel"] < MIN_SCORE:
            missed[name] = f"o .ibd não combina com o .frm (só {result['plausivel']:.0%} dos registros plausíveis)"
            ts.close()
            continue
        table.clustered_index_id, table.space_id = index_id, ts.space_id or 1
        carver.table_spaces[table.name] = ts
        carver.register_table(table.name, table)
        matched[name] = f"arquivo {name}.ibd ({len(ts.leaf_pages[index_id])} folhas)"
        del pending[name]

    # PARTE 2: índices sem dono no ibdata1 -> casar com as definições que sobraram.
    # 2) ibdata1: casar índices sem dono com as definições que sobraram.
    # Lista os índices 'sem dono' que têm folhas de verdade.
    orphans = [o for o in carver.orphan_indexes() if carver.space.leaf_pages.get(o["index_id"])]
    if not pending or not orphans:
        for name in pending:
            missed.setdefault(name, "nenhum índice sem dono no ibdata1")
        return matched, missed
    # Limite de DB_TRX_ID plausível, para a pontuação.
    trx_limit = max_trx_id(carver)
    log(f"Casando {len(orphans)} índice(s) sem dono do ibdata1 com {len(pending)} tabela(s) sem linha no dicionário...")
    # Para cada índice sem dono, testa cada definição pendente: primeiro um TESTE RÁPIDO (1 página, 3 registros,
    #   precisa dar 100%); só os que passam recebem a pontuação completa.
    best_for_orphan = {}
    for orphan in orphans:
        index_id = orphan["index_id"]
        leaves = carver.space.leaf_pages[index_id]
        compact = page_compact(carver.space, leaves[0])
        scored = []
        for name, frm in pending.items():
            try:
                table = tabledef_from_frm(f"{db_name}/{name}", frm, compact)
            except (ic.CarverError, KeyError):
                continue
            quick = score_index(carver.space, leaves, table, trx_limit, sample_pages=1, sample_records=3)
            if quick["plausivel"] < 1.0:
                continue
            result = score_index(carver.space, leaves, table, trx_limit)
            if result["vistos"] and result["plausivel"] >= MIN_SCORE and result["geometria"] >= MIN_GEOMETRY:
                scored.append((result["geometria"] + result["plausivel"], name, table, result))
        if scored:
            scored.sort(key=lambda item: -item[0])
            best_for_orphan[index_id] = (scored, orphan)

    # RESOLUÇÃO: se um índice serve a duas tabelas parecidas (notas muito próximas), é AMBÍGUO e ninguém o recebe; se
    #   uma tabela tem vários índices candidatos, fica com o mais recente (maior LSN).
    # um índice casado com duas tabelas de mesma estrutura é ambíguo; uma tabela com vários índices fica com o mais novo
    by_table = {}
    for index_id, (scored, orphan) in best_for_orphan.items():
        top = [item for item in scored if item[0] >= scored[0][0] - AMBIGUITY_MARGIN]
        if len(top) > 1:
            for _, name, _, _ in top:
                missed[name] = (f"o índice {index_id} serviria a {len(top)} tabelas de estrutura parecida "
                                f"({', '.join(n for _, n, _, _ in top)}); ambíguo")
            continue
        score, name, table, result = top[0]
        lsn = max(ic.u64(carver.space.page(n), ic.FIL_PAGE_LSN) for n in carver.space.leaf_pages[index_id][:50])
        by_table.setdefault(name, []).append((lsn, index_id, score, table, orphan))
    # Registra no carver cada tabela casada e anota como foi (índice, folhas, registros, afinidade).
    for name, options in by_table.items():
        if name in missed:
            continue
        options.sort(reverse=True)
        lsn, index_id, score, table, orphan = options[0]
        table.clustered_index_id, table.space_id = index_id, 0
        carver.register_table(table.name, table)
        matched[name] = (f"índice {index_id} do ibdata1 ({orphan['folhas']} folhas, {orphan['registros']} registros; "
                         f"{score / 2:.0%} de afinidade com o .frm)")   # score = plausível + geometria (0 a 2)
        if len(options) > 1:
            log(f"  {name}: {len(options) - 1} índice(s) mais antigo(s) com a mesma estrutura ignorados "
                f"(tabela apagada e recriada).")
        pending.pop(name, None)
    for name in pending:
        missed.setdefault(name, "nenhum índice sem dono combina com o .frm")
    return matched, missed

