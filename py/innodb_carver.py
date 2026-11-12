"""
innodb_carver.py - Leitura direta das páginas do ibdata1, sem o mysqld (camada 2 da recuperação).

É o método padrão da indústria para tabelas que derrubam o servidor mesmo com innodb_force_recovery:
o do TwinDB undrop-for-innodb (stream_parser + sys_parser + c_parser), sucessor do Percona Data
Recovery Tool. Em vez de seguir a B-tree (cujos ponteiros podres derrubam o mysqld), varre o
arquivo página a página, separa as páginas por index_id e decodifica os registros usando o
dicionário interno do próprio ibdata1 (SYS_TABLES/SYS_COLUMNS/SYS_INDEXES/SYS_FIELDS), que guarda a
estrutura com que os registros do cliente foram gravados.

Lógica portada de: TwinDB undrop-for-innodb (GPL-2.0, https://github.com/twindb/undrop-for-innodb),
com o formato de registro conferido no código-fonte do MySQL 5.5 (rem0rec.c, data0type.h, field.cc).
Este arquivo é distribuído sob a GPL-2.0, a mesma licença do undrop-for-innodb e do MySQL incluído em mysql/.
Diferença em relação ao original: TIME antigo decodificado como HHMMSS (Field_time do MySQL 5.5), não segundos.

Escopo: MySQL 5.1/5.5 (formatos REDUNDANT, COMPACT e DYNAMIC; tipos temporais antigos), páginas de 16 KB, tabelas no
tablespace compartilhado (ibdata1) ou em arquivos próprios (innodb_file_per_table=1, um .ibd por tabela). Tabelas
ROW_FORMAT=COMPRESSED (páginas compactadas) não são lidas. As páginas rasgadas do ibdata1 são refeitas com as cópias
íntegras do doublewrite buffer (o mesmo que o InnoDB faz ao subir), sem tocar no arquivo original.
"""

# VISÃO GERAL: o 'leitor de páginas'. Quando o servidor MySQL não consegue ler uma tabela, este arquivo LÊ OS BYTES do
#   ibdata1 (ou de um .ibd) sozinho, sem o MySQL, no método do TwinDB undrop-for-innodb. Em vez de seguir a árvore de
#   índices (cujos ponteiros podres derrubam o mysqld), varre o arquivo página por página, AGRUPA as páginas de folha
#   pelo número do índice (index_id), lê o DICIONÁRIO interno (SYS_TABLES, SYS_COLUMNS, SYS_INDEXES, SYS_FIELDS) para
#   saber a estrutura de cada tabela e DECODIFICA os registros byte a byte. Também gera o CREATE TABLE e os INSERTs,
#   detecta páginas destruídas, lê chaves estrangeiras e guarda tabelas reconstruídas pelo .frm. MAPA DAS PEÇAS (de
#   baixo para cima): 1) leitura de bytes e decodificação de valores (u16, decode_value...); 2) Tablespace = um
#   arquivo de páginas; 3) iter_records/parse_compact/parse_redundant = ler os registros DENTRO de uma página; 4)
#   TableDef/Column = a estrutura de uma tabela; 5) InnodbCarver = o orquestrador (carregar dicionário, achar páginas
#   de uma tabela, decodificar linhas); 6) sql_literal/rows_to_sql/ddl_from_dictionary = escrever SQL; 7)
#   record_extent = ferramenta para dictionary_rebuild.
# COMO ESTUDAR: leia primeiro a figura 'anatomia da página' do GUIA_DO_CODIGO.html (seção 7), depois Tablespace.scan,
#   parse_compact e InnodbCarver.carve_def.

# Importações: 'os' = caminhos; 're' = padrões de texto; 'struct' = ler números de bytes; page_health (exame de
#   páginas), mysql_codec (datas/DECIMAL) e as constantes de posição de page_health.
import os
import re
import struct

import page_health
from mysql_codec import decode_newdecimal, format_date, format_datetime, format_time
from page_health import FIL_PAGE_INDEX, FIL_PAGE_LSN, FIL_PAGE_OFFSET, FIL_PAGE_SPACE_ID, FIL_PAGE_TYPE, PAGE_SIZE

# FIL_NULL: valor 'nulo' dos números de página (0xFFFFFFFF): 'não há página anterior/próxima'.
FIL_NULL = 0xFFFFFFFF

# POSIÇÕES DENTRO DA PÁGINA (em bytes desde o começo dela): PREV/NEXT = números das páginas vizinha anterior/próxima
#   na lista de folhas; PAGE_HEADER = onde começa o cabeçalho próprio da página (byte 38); N_DIR_SLOTS, N_HEAP,
#   N_RECS, LEVEL e INDEX_ID são campos dele (nº de slots, de registros, nível da árvore e o número do índice).
#   INFIMUM/SUPREMUM = posição dos dois registros-sentinela (início e fim da lista de registros), que muda entre
#   COMPACT e REDUNDANT. MAX_RECS_PER_PAGE = limite de segurança para nunca entrar em laço infinito numa página podre.
#   PAGE_DIR_END = onde acaba o diretório de slots.
# Cabeçalhos (fil0fil.h / page0page.h)
FIL_PAGE_PREV = 8
FIL_PAGE_NEXT = 12
PAGE_HEADER = 38
PAGE_N_DIR_SLOTS = PAGE_HEADER
PAGE_N_HEAP = PAGE_HEADER + 4
PAGE_N_RECS = PAGE_HEADER + 16
PAGE_LEVEL = PAGE_HEADER + 26
PAGE_INDEX_ID = PAGE_HEADER + 28
PAGE_NEW_INFIMUM = 99
PAGE_NEW_SUPREMUM = 112
PAGE_OLD_INFIMUM = 101
PAGE_OLD_SUPREMUM = 116
MAX_RECS_PER_PAGE = PAGE_SIZE // 5
PAGE_DIR_END = PAGE_SIZE - 8  # o diretório de slots cresce para trás a partir daqui (FIL_PAGE_DATA_END = 8)

# REC_STATUS_ORDINARY: 'tipo' de registro comum (os outros tipos são nós internos e sentinelas).
#   REC_INFO_DELETED_FLAG: bit que marca um registro como APAGADO.
REC_STATUS_ORDINARY = 0
REC_INFO_DELETED_FLAG = 0x20

# DICIONÁRIO: as 4 tabelas internas do InnoDB têm index_id FIXOS (1 a 4); por isso dá para achá-las mesmo sem nenhum
#   outro dado. DICT_HDR_FIRST_ID = menores que 10 são do dicionário. DICT_N_COLS_COMPACT = bit que, em
#   SYS_TABLES.N_COLS, indica formato COMPACT.
# Dicionário: index_id fixo dos índices clusterizados das tabelas SYS_* (dict0boot.h / recover_dictionary.sh)
DICT_INDEX_IDS = {"SYS_TABLES": 1, "SYS_COLUMNS": 2, "SYS_INDEXES": 3, "SYS_FIELDS": 4}
DICT_CLUSTERED = 1
DICT_HDR_FIRST_ID = 10     # index_id abaixo disso é reservado às tabelas SYS_* e seus índices (dict0boot.h)
DICT_N_COLS_COMPACT = 0x80000000
# FLAGS DO FORMATO (SYS_TABLES.TYPE = table->flags): ZSSIZE_MASK = bits do tamanho de página compactada
#   (ROW_FORMAT=COMPRESSED); BARRACUDA = formato novo (DYNAMIC).
# SYS_TABLES.TYPE no MySQL 5.5 é o table->flags: bit 0 (compacto) sempre ligado, depois
DICT_TF_ZSSIZE_MASK = 0x1E    # tamanho da página compactada (ROW_FORMAT=COMPRESSED)
DICT_TF_FORMAT_BARRACUDA = 0x20
# FSP_IBD_ROOT_PAGE: num arquivo .ibd do MySQL 5.5, a página 3 é a raiz do índice clusterizado (onde estão as linhas
#   da tabela).
FSP_IBD_ROOT_PAGE = 3         # num .ibd do MySQL 5.5 a página 3 é a raiz do índice clusterizado

# TIPOS INTERNOS do InnoDB (data0type.h), 'mtype': 1=varchar, 2=char, 6=INT (todos os números e datas), 5=BLOB, 8=SYS
#   (colunas de sistema), 13=MYSQL (texto do MySQL) etc. DATA_NOT_NULL / DATA_UNSIGNED / DATA_BINARY_TYPE são FLAGS
#   somados ao 'prtype' da coluna.
# Tipos internos (data0type.h)
DATA_VARCHAR, DATA_CHAR, DATA_FIXBINARY, DATA_BINARY, DATA_BLOB, DATA_INT = 1, 2, 3, 4, 5, 6
DATA_SYS, DATA_FLOAT, DATA_DOUBLE, DATA_DECIMAL, DATA_VARMYSQL, DATA_MYSQL = 8, 9, 10, 11, 12, 13
DATA_NOT_NULL, DATA_UNSIGNED, DATA_BINARY_TYPE = 256, 512, 1024

# TIPOS DO MYSQL (mysql_com.h): o byte BAIXO do 'prtype' guarda o tipo que o usuário declarou (1=tinyint, 3=int,
#   246=decimal, 252=blob, 254=char...).
# Tipos MySQL (mysql_com.h), guardados no byte baixo do PRTYPE
MYSQL_TINY, MYSQL_SHORT, MYSQL_LONG, MYSQL_FLOAT, MYSQL_DOUBLE = 1, 2, 3, 4, 5
MYSQL_TIMESTAMP, MYSQL_LONGLONG, MYSQL_INT24, MYSQL_DATE, MYSQL_TIME = 7, 8, 9, 10, 11
MYSQL_DATETIME, MYSQL_YEAR, MYSQL_NEWDATE, MYSQL_VARCHAR, MYSQL_BIT = 12, 13, 14, 15, 16
MYSQL_NEWDECIMAL, MYSQL_ENUM, MYSQL_SET, MYSQL_BLOB, MYSQL_STRING = 246, 247, 248, 252, 254

# CHARSETS: 63 = binário; UTF8_IDS/UTF8MB4_IDS = os números de collation do utf8 e do utf8mb4 (o 'prtype' guarda o
#   número do charset a partir do bit 16).
CHARSET_BINARY = 63
UTF8_IDS = {33, 83} | set(range(192, 216))
UTF8MB4_IDS = {45, 46} | set(range(224, 248))
# DECIMAL_HINT: padrão que acha 'coluna decimal(p,s)' num CREATE TABLE de apoio. O dicionário do InnoDB NÃO guarda a
#   precisão do DECIMAL; ela só vem do DDL.
DECIMAL_HINT = re.compile(r"`(?P<col>[^`]+)`\s+decimal\s*\(\s*(?P<p>\d+)\s*(?:,\s*(?P<s>\d+))?\s*\)", re.I)


# FAZ: o erro deste módulo: 'não consegui ler isto'. Quem chama captura e usa outro caminho.
class CarverError(Exception):
    pass


# ---------------------------------------------------------------------- leitura de bytes
# FAZ: lê 2 bytes como número. 'Big-endian' (o byte mais significativo primeiro): o formato das páginas do InnoDB.
def u16(b, o):
    return (b[o] << 8) | b[o + 1]


# FAZ: lê 4 bytes como número (big-endian).
def u32(b, o):
    return struct.unpack_from(">I", b, o)[0]


# FAZ: lê 8 bytes como número (big-endian).
def u64(b, o):
    return struct.unpack_from(">Q", b, o)[0]


# FAZ: lê um trecho de bytes de qualquer tamanho como um número inteiro sem sinal (big-endian).
def be_uint(data):
    return int.from_bytes(data, "big")


# FAZ: máximo de bytes por caractere do charset: utf8mb4 = 4, utf8 = 3, os demais (latin1...) = 1.
def charset_mbmaxlen(coll_id):
    if coll_id in UTF8MB4_IDS:
        return 4
    if coll_id in UTF8_IDS:
        return 3
    return 1


# FAZ: uma COLUNA como o InnoDB a descreve no dicionário (SYS_COLUMNS) ou um campo interno do índice: nome, tipo
#   interno (mtype), 'prtype' (tipo MySQL + flags + charset) e tamanho.
class Column:
    """Coluna física como está no dicionário (SYS_COLUMNS) ou campo interno do índice."""

    # FAZ: guarda os dados e DESEMPACOTA o 'prtype': tipo MySQL (byte baixo), charset (a partir do bit 16), aceita
    #   NULL? tem sinal?
    def __init__(self, name, mtype, prtype, length, pos=0):
        self.name, self.mtype, self.prtype, self.len, self.pos = name, mtype, prtype, length, pos
        self.mysql_type = prtype & 0xFF
        self.coll_id = (prtype >> 16) & 0xFFFF
        self.nullable = not (prtype & DATA_NOT_NULL)
        self.unsigned = bool(prtype & DATA_UNSIGNED)
        self.decimal = None  # (precisão, escala), vem do DDL quando existir

    # FAZ: se a coluna tem tamanho FIXO, devolve esse tamanho; se é de tamanho variável, devolve 0 (então o tamanho
    #   vem gravado no registro). Reflete a regra do dtype_get_fixed_size_low do MySQL.
    def fixed_size(self, compact):
        """dtype_get_fixed_size_low(): 0 = coluna de tamanho variável."""
        if self.mtype in (DATA_SYS, DATA_CHAR, DATA_FIXBINARY, DATA_INT, DATA_FLOAT, DATA_DOUBLE):
            return self.len
        if self.mtype == DATA_MYSQL:
            if self.prtype & DATA_BINARY_TYPE or not compact:
                return self.len
            return self.len if charset_mbmaxlen(self.coll_id) == 1 else 0
        return 0

    # FAZ: o tamanho desta coluna pode precisar de 2 bytes (colunas maiores que 255 bytes ou BLOB)?
    def two_byte_len_possible(self):
        return self.len > 255 or self.mtype == DATA_BLOB

    # FAZ: texto de depuração da coluna (aparece ao imprimir o objeto).
    def __repr__(self):
        return f"Column({self.name}, mtype={self.mtype}, type={self.mysql_type}, len={self.len})"


# FAZ: cria uma coluna 'de sistema' (DB_ROW_ID, DB_TRX_ID, DB_ROLL_PTR) que o InnoDB acrescenta a todo registro; não
#   existe no CREATE TABLE.
def sys_field(name, length):
    return Column(name, DATA_SYS, DATA_NOT_NULL, length)


# ---------------------------------------------------------------------- decodificação de valores
# FAZ: converte os BYTES de um campo no valor Python certo: inteiro, float, texto, bytes, data (texto), DECIMAL exato
#   (SqlExpr) etc. O InnoDB guarda inteiros com o BIT DE SINAL INVERTIDO (para ordenar bem), então o código desfaz
#   isso; datas e horas são números 'empacotados' (veja mysql_codec).
def decode_value(col, data):
    """Bytes de um campo -> valor Python (int, float, str, bytes, SqlExpr ou None)."""
    # 'mt' = tipo interno (mtype); 'ty' = tipo MySQL. As datas, horas e números do tipo INT chegam aqui como inteiros
    #   'empacotados'.
    mt, ty = col.mtype, col.mysql_type
    # Passo 1: tipos de 'número empacotado': DATETIME, DATE, TIME, TIMESTAMP, YEAR e os inteiros comuns.
    if mt == DATA_INT:
        if ty == MYSQL_DATETIME and len(data) == 8:
            return format_datetime(be_uint(data) - (1 << 63))   # bit de sinal invertido pelo InnoDB
        if ty in (MYSQL_DATE, MYSQL_NEWDATE) and len(data) == 3:
            return format_date(be_uint(data) ^ 0x800000)
        if ty == MYSQL_TIME and len(data) == 3:
            return format_time(be_uint(data) ^ 0x800000)
        if ty == MYSQL_TIMESTAMP and len(data) == 4:
            seconds = be_uint(data)
            return SqlExpr(f"FROM_UNIXTIME({seconds})") if seconds else "0000-00-00 00:00:00"
        if ty == MYSQL_YEAR and len(data) == 1:
            return 1900 + data[0] if data[0] else 0
        # Inteiro comum: sem sinal -> o valor; com sinal -> desfaz a inversão do bit de sinal.
        value = be_uint(data)
        if col.unsigned:
            return value
        return value - (1 << (8 * len(data) - 1))  # bit de sinal invertido pelo InnoDB
    # Passo 2: float e double (formato IEEE, little-endian).
    if mt == DATA_FLOAT:
        return float_or_none(struct.unpack("<f", data)[0])
    if mt == DATA_DOUBLE:
        return float_or_none(struct.unpack("<d", data)[0])
    # Passo 3: DECIMAL. Só dá para decodificar se a precisão for conhecida (vem do DDL de apoio); senão, devolve os
    #   bytes crus.
    if ty == MYSQL_NEWDECIMAL:
        if col.decimal:
            return SqlExpr(decode_newdecimal(data, *col.decimal))
        return bytes(data)
    if ty == MYSQL_BIT:
        return be_uint(data)
    # Passo 4: binários viram bytes.
    if col.coll_id == CHARSET_BINARY or mt in (DATA_FIXBINARY, DATA_BINARY) or (
            mt == DATA_BLOB and col.coll_id in (0, CHARSET_BINARY)):
        return bytes(data)
    # Passo 5: o resto é texto. CHAR: o MySQL tira os espaços do fim.
    text = decode_text(data, col.coll_id)
    if col.fixed_size(True) or mt == DATA_MYSQL:  # CHAR: o MySQL remove os espaços de preenchimento
        text = text.rstrip(" ")
    return text


# FAZ: devolve o número, ou None se for NaN/infinito (não existem em SQL).
def float_or_none(value):
    return None if value != value or value in (float("inf"), float("-inf")) else value


# FAZ: bytes -> texto conforme o charset: utf8 para utf8/utf8mb4; o 'latin1' do MySQL é na prática cp1252.
def decode_text(data, coll_id):
    if coll_id in UTF8_IDS or coll_id in UTF8MB4_IDS:
        return bytes(data).decode("utf8", errors="replace")
    try:
        return bytes(data).decode("cp1252")  # o "latin1" do MySQL é o cp1252
    except UnicodeDecodeError:
        return bytes(data).decode("latin1")


# FAZ: um texto que deve ir CRU para o SQL, sem aspas (ex.: um DECIMAL como 1234.56 ou FROM_UNIXTIME(123)). É uma
#   subclasse de str só para ser reconhecida no sql_literal.
class SqlExpr(str):
    """Valor que vai cru no SQL (número decimal exato, FROM_UNIXTIME)."""


# ---------------------------------------------------------------------- arquivo e páginas
# FAZ: UM arquivo de páginas (o ibdata1 ou um .ibd). Sabe ler uma página, varrer o arquivo agrupando as folhas por
#   índice e aplicar cópias do doublewrite.
class Tablespace:
    """Um arquivo de tablespace (ibdata1 ou um .ibd). As folhas de índice são agrupadas por index_id."""

    # FAZ: abre o arquivo e prepara as caixas: leaf_pages (index_id -> páginas de folha), page_total, bad_pages e
    #   overlay (páginas refeitas em memória).
    def __init__(self, path, space_id=None):
        self.path = path
        self.f = open(path, "rb")
        self.n_pages = os.path.getsize(path) // PAGE_SIZE
        self.space_id = space_id if space_id is not None else self._read_space_id()
        self.leaf_pages = {}    # index_id -> [page_no] (folhas, nível 0)
        self.page_total = {}    # index_id -> páginas do índice (folhas e níveis internos)
        self.bad_pages = 0      # páginas INDEX com cabeçalho inconsistente
        self.overlay = {}       # página -> bytes: cópia íntegra do doublewrite no lugar de uma página rasgada

    # FAZ: lê, do cabeçalho da página 0, o 'id do espaço' (a identidade do arquivo: 0 = ibdata1).
    def _read_space_id(self):
        if self.n_pages == 0:
            return None
        self.f.seek(0)
        head = self.f.read(FIL_PAGE_SPACE_ID + 4)
        return u32(head, FIL_PAGE_SPACE_ID) if len(head) >= FIL_PAGE_SPACE_ID + 4 else None

    # FAZ: fecha o arquivo (no Windows, um arquivo aberto não pode ser apagado).
    def close(self):
        self.f.close()

    # FAZ: devolve os 16 KB de uma página. Se a página foi refeita pelo doublewrite, devolve a CÓPIA BOA (sobreposição
    #   em memória); o arquivo no disco nunca é alterado.
    def page(self, page_no):
        # Se há cópia boa do doublewrite para esta página, devolve ela.
        if page_no in self.overlay:
            return self.overlay[page_no]
        self.f.seek(page_no * PAGE_SIZE)
        data = self.f.read(PAGE_SIZE)
        if len(data) != PAGE_SIZE:
            raise CarverError(f"página {page_no} além do fim do arquivo")
        return data

    # FAZ: registra as cópias íntegras do doublewrite para as páginas rasgadas deste arquivo. Devolve os números das
    #   páginas refeitas.
    def apply_doublewrite(self, copies):
        """Passa a ler, no lugar das páginas rasgadas deste arquivo, as cópias íntegras do doublewrite buffer.
        copies = page_health.doublewrite_copies(ibdata1). Retorna os números das páginas refeitas."""
        repairs = page_health.torn_page_repairs(self.path, self.space_id or 0, copies)
        self.overlay.update(repairs)
        return sorted(repairs)

    # FAZ: o 'stream_parser' do TwinDB: varre o arquivo INTEIRO, uma página por vez e SEM seguir ponteiros, e agrupa
    #   as páginas de FOLHA (nível 0) pelo index_id. Pula páginas que não são de índice, as do doublewrite e as
    #   'tortas' (número gravado diferente da posição).
    def scan(self):
        """stream_parser: agrupa as folhas por index_id varrendo o arquivo inteiro (sem seguir ponteiros)."""
        self.f.seek(0)
        # As cópias do doublewrite têm o número da página de ORIGEM, não o da posição: não podem ser tratadas como
        #   páginas deste lugar.
        dblwr = page_health.doublewrite_page_numbers(self.f, self.n_pages)  # cópias de outras páginas: não são páginas daqui
        self.f.seek(0)
        # Lê as páginas uma a uma, do começo ao fim do arquivo.
        for page_no in range(self.n_pages):
            head = self.f.read(PAGE_SIZE)
            if len(head) < PAGE_SIZE:
                break
            if page_no in self.overlay:
                head = self.overlay[page_no]
            # Pula o que não interessa: páginas do doublewrite e as que não são de índice.
            if page_no in dblwr or u16(head, FIL_PAGE_TYPE) != FIL_PAGE_INDEX:
                continue
            # Página 'torta': o número gravado não bate com a posição (ou o nível é absurdo). É contada e ignorada.
            if u32(head, FIL_PAGE_OFFSET) != page_no or u16(head, PAGE_LEVEL) > 64:
                self.bad_pages += 1  # página torta/podre: o número gravado não bate com a posição
                continue
            # Página boa: anota o índice a que pertence; se for folha (nível 0), guarda o número dela na lista desse
            #   índice.
            index_id = u64(head, PAGE_INDEX_ID)
            self.page_total[index_id] = self.page_total.get(index_id, 0) + 1
            if u16(head, PAGE_LEVEL) == 0:
                self.leaf_pages.setdefault(index_id, []).append(page_no)

    # FAZ: devolve o index_id da página raiz (a 3, num .ibd) ou None se ela não for uma página de índice válida.
    def root_index_id(self, root_page=FSP_IBD_ROOT_PAGE):
        """index_id da página raiz indicada (num .ibd, a 3), ou None se ela não é uma página de índice."""
        if root_page >= self.n_pages:
            return None
        page = self.page(root_page)
        if u16(page, FIL_PAGE_TYPE) != FIL_PAGE_INDEX or u32(page, FIL_PAGE_OFFSET) != root_page:
            return None
        return u64(page, PAGE_INDEX_ID)


# FAZ: diz POR QUE uma página de índice derrubaria o mysqld (ou None se está sã). São as mesmas verificações que o
#   MySQL faz com 'assert': formato COMPACT/REDUNDANT coerente, registros-sentinela infimum/supremum íntegros,
#   diretório de slots coerente e rodapé igual ao cabeçalho (gravação não interrompida).
def page_structure_error(page, compact):
    """Motivo pelo qual uma página INDEX derrubaria o mysqld ao ser lida (o que ele confere com assert), ou None.
    São as verificações de rem0rec.c/page0page.c: formato, registros infimum/supremum, diretório de slots e fim da página."""
    # 1) O formato (COMPACT/REDUNDANT) da página tem de ser o da tabela.
    if bool(u16(page, PAGE_N_HEAP) & 0x8000) != compact:
        return "formato COMPACT/REDUNDANT diferente do da tabela"
    # 2) Os registros-sentinela 'infimum' e 'supremum' têm de estar íntegros.
    infimum, supremum = (PAGE_NEW_INFIMUM, PAGE_NEW_SUPREMUM) if compact else (PAGE_OLD_INFIMUM, PAGE_OLD_SUPREMUM)
    if page[infimum:infimum + 8] != b"infimum\0" or page[supremum:supremum + 8] != b"supremum":
        return "registros infimum/supremum destruídos"
    # 3) O diretório de slots (atalhos para achar registros) tem de ser coerente e apontar para os sentinelas.
    n_slots = u16(page, PAGE_N_DIR_SLOTS)
    if not 2 <= n_slots <= PAGE_SIZE // 4:
        return "diretório de slots inválido"
    if u16(page, PAGE_DIR_END - 2) != infimum or u16(page, PAGE_DIR_END - 2 * n_slots) != supremum:
        return "diretório de slots não aponta para infimum/supremum"
    # 4) O fim da página tem de coincidir com o cabeçalho: se não, a gravação foi interrompida.
    if page[PAGE_SIZE - 4:] != page[FIL_PAGE_LSN + 4:FIL_PAGE_LSN + 8]:
        return "fim da página não confere com o cabeçalho (gravação interrompida)"
    return None


# ---------------------------------------------------------------------- registros
# FAZ: percorre os registros de UMA página, do infimum ao supremum, seguindo o ponteiro 'próximo' de cada registro
#   (lista encadeada). Entrega a posição (origem) de cada um. Protege contra laços e ponteiros fora da página (página
#   podre).
def iter_records(page, compact):
    """Percorre a lista encadeada de registros dentro da página (de infimum até supremum)."""
    infimum, supremum = (PAGE_NEW_INFIMUM, PAGE_NEW_SUPREMUM) if compact else (PAGE_OLD_INFIMUM, PAGE_OLD_SUPREMUM)
    # 'origin' = posição do registro atual; 'seen' = posições já visitadas (para detectar laço).
    origin, seen = infimum, set()
    for _ in range(MAX_RECS_PER_PAGE):
        # COMPACT: o ponteiro 'próximo' é um deslocamento RELATIVO (pode ser negativo); REDUNDANT: é uma posição
        #   absoluta.
        if compact:
            rel = u16(page, origin - 2)
            rel = rel - 0x10000 if rel >= 0x8000 else rel
            origin = (origin + rel) & 0xFFFF
        else:
            origin = u16(page, origin - 2)
        # Chegou ao fim (supremum), ou o ponteiro é inválido/repetido (página podre): para.
        if origin == supremum or origin in seen or not (supremum < origin < PAGE_SIZE - 8):
            return
        seen.add(origin)
        yield origin


# FAZ: decodifica UM registro no formato COMPACT (o padrão do MySQL 5.5). Devolve (apagado?, [bytes de cada campo ou
#   None]). Como é: antes da 'origem' ficam, lidos PARA TRÁS, o cabeçalho de 5 bytes, o bitmap de NULL e os tamanhos
#   das colunas variáveis; depois da origem vêm os dados dos campos, na ordem.
def parse_compact(page, origin, fields, read_external):
    """rec_init_offsets_comp(): devolve (apagado, [bytes|None por campo]) de um registro COMPACT."""
    # Só registros 'ordinários' interessam (não nós internos da árvore).
    if page[origin - 3] & 0x07 != REC_STATUS_ORDINARY:
        return None
    # O bit 0x20 do cabeçalho marca registro apagado.
    deleted = bool(page[origin - 5] & REC_INFO_DELETED_FLAG)
    # Antes da origem, para trás: primeiro o bitmap de NULL (1 bit por coluna que aceita NULL), depois os tamanhos das
    #   colunas variáveis.
    nullable = [f for f in fields if f.nullable]
    null_bytes = (len(nullable) + 7) // 8
    null_ptr = origin - 6                       # 1º byte do bitmap de NULL (lido para trás)
    len_ptr = origin - 6 - null_bytes           # 1º byte dos tamanhos variáveis (lido para trás)
    # 'pos' = onde começa o próximo campo (depois da origem); 'null_i' = qual bit de NULL é o próximo.
    pos, null_i, values = origin, 0, []
    # Para cada campo, na ordem: NULL? tamanho fixo ou variável? pega os bytes.
    for f in fields:
        if f.nullable:
            is_null = page[null_ptr - null_i // 8] & (1 << (null_i % 8))
            null_i += 1
            if is_null:
                values.append(None)
                continue
        # Tamanho fixo da coluna ou, se variável (0), lê 1 byte do tamanho; se o bit 0x80 estiver ligado, o tamanho
        #   ocupa 2 bytes e o bit 0x40 diz que o valor está FORA da página.
        size, external = f.fixed_size(True), False
        if not size:
            size = page[len_ptr]
            len_ptr -= 1
            if f.two_byte_len_possible() and size & 0x80:
                external = bool(size & 0x40)
                size = ((size & 0x3F) << 8) | page[len_ptr]
                len_ptr -= 1
        # Defesa: um campo que ultrapassa a página indica registro corrompido.
        if pos + size > PAGE_SIZE - 8 or len_ptr < 0:
            raise CarverError("registro ultrapassa a página")
        data = page[pos:pos + size]
        pos += size
        # Campo grande fora da linha: segue a referência; senão usa os bytes que estão aqui.
        values.append(read_external(data) if external else data)
    return deleted, values


# FAZ: o mesmo para o formato REDUNDANT (antigo do MySQL 5.0): antes da origem ficam os 'offsets de fim' de cada
#   campo; é só calcular onde cada campo começa e termina.
def parse_redundant(page, origin, fields, read_external):
    """rec_init_offsets() formato antigo: offsets de fim de campo antes do cabeçalho de 6 bytes."""
    deleted = bool(page[origin - 6] & REC_INFO_DELETED_FLAG)
    # No formato antigo, o cabeçalho diz quantos campos há e se os offsets ocupam 1 ou 2 bytes.
    n_fields = (u16(page, origin - 4) >> 1) & 0x3FF
    one_byte = page[origin - 3] & 0x01
    # O número de campos do registro tem de ser o da definição; senão a definição não serve.
    if n_fields != len(fields):
        raise CarverError("número de campos não bate")
    values, start = [], 0
    # Cada campo tem um 'offset de fim' (com bits extras de NULL e 'externo'): o campo vai do fim do anterior até o
    #   seu fim.
    for i in range(n_fields):
        if one_byte:
            raw = page[origin - 7 - i]
            end, is_null, external = raw & 0x7F, raw & 0x80, False
        else:
            raw = u16(page, origin - 8 - 2 * i)
            end, is_null, external = raw & 0x3FFF, raw & 0x8000, raw & 0x4000
        if end < start or origin + end > PAGE_SIZE - 8:
            raise CarverError("offset de campo inválido")
        data = page[origin + start:origin + end]
        start = end
        values.append(None if is_null else (read_external(data) if external else data))
    return deleted, values


# ---------------------------------------------------------------------- dicionário
# FAZ: a definição de uma TABELA como o InnoDB a conhece: nome, colunas (na ordem do CREATE TABLE), índice
#   clusterizado, página raiz, chave primária e formato.
class TableDef:
    # FAZ: guarda os dados da linha do SYS_TABLES (nome, id, formato, espaço) e deixa as colunas e a chave para serem
    #   preenchidas por InnodbCarver._build_tables.
    def __init__(self, name, table_id, n_cols_raw, flags=None, space_id=0):
        self.name, self.id = name, table_id
        self.compact = bool(n_cols_raw & DICT_N_COLS_COMPACT)
        self.flags = flags         # SYS_TABLES.TYPE (table->flags no MySQL 5.5)
        self.space_id = space_id or 0   # 0 = ibdata1; outro valor = arquivo .ibd próprio
        self.columns = []          # na ordem POS (ordem do CREATE TABLE)
        self.clustered_index_id = None
        self.root_page = None      # página raiz do índice clusterizado (SYS_INDEXES.PAGE_NO)
        self.pk_columns = []       # nomes, na ordem do índice
        self.generated_pk = False  # sem PK: InnoDB usa DB_ROW_ID

    # FAZ: o ROW_FORMAT da tabela em palavras (REDUNDANT, COMPACT, DYNAMIC ou COMPRESSED), calculado dos flags.
    @property
    def row_format(self):
        # Sem o bit COMPACT: é o formato antigo REDUNDANT.
        if not self.compact:
            return "REDUNDANT"
        # Com tamanho de página compactada: COMPRESSED; com o bit Barracuda: DYNAMIC; senão COMPACT.
        flags = self.flags or 0
        if flags & DICT_TF_ZSSIZE_MASK:
            return "COMPRESSED"
        return "DYNAMIC" if flags & DICT_TF_FORMAT_BARRACUDA else "COMPACT"

    # FAZ: a lista de campos de um REGISTRO do índice clusterizado, na ordem em que estão gravados: chave primária,
    #   DB_TRX_ID, DB_ROLL_PTR e as demais colunas. Sem PK, o InnoDB cria uma chave oculta (DB_ROW_ID). É por esta
    #   lista que parse_compact sabe o que ler.
    def record_fields(self):
        """Campos do registro no índice clusterizado: PK, DB_TRX_ID, DB_ROLL_PTR, demais colunas."""
        # A chave primária (ou a chave oculta DB_ROW_ID) vem primeiro.
        by_name = {c.name.lower(): c for c in self.columns}
        if self.generated_pk:
            head = [sys_field("DB_ROW_ID", 6)]
        else:
            head = [by_name[n.lower()] for n in self.pk_columns]
        pk = {n.lower() for n in self.pk_columns}
        # Depois DB_TRX_ID (6 bytes) e DB_ROLL_PTR (7 bytes) e, por último, as colunas que não são da chave.
        rest = [c for c in self.columns if c.name.lower() not in pk]
        return head + [sys_field("DB_TRX_ID", 6), sys_field("DB_ROLL_PTR", 7)] + rest


# SYS_LAYOUT: a 'planta' das 4 tabelas do dicionário: lista de (nome da coluna, tipo, tamanho). É assim que se sabe
#   decodificar os registros do dicionário SEM ter um dicionário. SYS_PK_LEN = quantas colunas formam a chave primária
#   de cada uma.
SYS_LAYOUT = {
    "SYS_TABLES": [("NAME", DATA_VARCHAR, 0), ("DB_TRX_ID", DATA_SYS, 6), ("DB_ROLL_PTR", DATA_SYS, 7),
                   ("ID", DATA_SYS, 8), ("N_COLS", DATA_INT, 4), ("TYPE", DATA_INT, 4), ("MIX_ID", DATA_SYS, 8),
                   ("MIX_LEN", DATA_INT, 4), ("CLUSTER_NAME", DATA_VARCHAR, 0), ("SPACE", DATA_INT, 4)],
    "SYS_COLUMNS": [("TABLE_ID", DATA_SYS, 8), ("POS", DATA_INT, 4), ("DB_TRX_ID", DATA_SYS, 6),
                    ("DB_ROLL_PTR", DATA_SYS, 7), ("NAME", DATA_VARCHAR, 0), ("MTYPE", DATA_INT, 4),
                    ("PRTYPE", DATA_INT, 4), ("LEN", DATA_INT, 4), ("PREC", DATA_INT, 4)],
    "SYS_INDEXES": [("TABLE_ID", DATA_SYS, 8), ("ID", DATA_SYS, 8), ("DB_TRX_ID", DATA_SYS, 6),
                    ("DB_ROLL_PTR", DATA_SYS, 7), ("NAME", DATA_VARCHAR, 0), ("N_FIELDS", DATA_INT, 4),
                    ("TYPE", DATA_INT, 4), ("SPACE", DATA_INT, 4), ("PAGE_NO", DATA_INT, 4)],
    "SYS_FIELDS": [("INDEX_ID", DATA_SYS, 8), ("POS", DATA_INT, 4), ("DB_TRX_ID", DATA_SYS, 6),
                   ("DB_ROLL_PTR", DATA_SYS, 7), ("COL_NAME", DATA_VARCHAR, 0)],
}
SYS_PK_LEN = {"SYS_TABLES": 1, "SYS_COLUMNS": 2, "SYS_INDEXES": 2, "SYS_FIELDS": 2}
# AÇÕES DE CHAVE ESTRANGEIRA: bits que dizem o que acontece com as linhas filhas ao apagar (ON DELETE) ou alterar (ON
#   UPDATE) o pai.
# SYS_FOREIGN.N_COLS >> 24: bits de DICT_FOREIGN_ON_DELETE_* / ON_UPDATE_* (dict0mem.h)
FK_DELETE_ACTIONS = {1: "CASCADE", 2: "SET NULL", 16: "NO ACTION"}
FK_UPDATE_ACTIONS = {4: "CASCADE", 8: "SET NULL", 32: "NO ACTION"}


# FAZ: o orquestrador da leitura direta: carrega o dicionário do ibdata1, descobre onde estão as páginas de cada
#   tabela e as decodifica em linhas.
class InnodbCarver:
    # FAZ: abre o ibdata1 e prepara as caixas: tables (nome -> TableDef), table_spaces (arquivos .ibd abertos sob
    #   demanda), index_ids (todos os índices citados) e repaired_pages.
    def __init__(self, ibdata_path, log=None, data_dir=None):
        self.space = Tablespace(ibdata_path, space_id=0)
        self.data_dir = data_dir or os.path.dirname(ibdata_path)
        self.log = log or (lambda msg: None)
        self.tables = {}          # nome minúsculo sem schema (ou "schema/tabela") -> TableDef
        self.table_spaces = {}    # nome "schema/tabela" -> Tablespace do .ibd
        self.index_ids = set()    # todo index_id citado em SYS_INDEXES (clusterizado ou secundário)
        self.repaired_pages = []  # páginas do ibdata1 refeitas pelo doublewrite
        self.loaded = False

    # FAZ: fecha o ibdata1 e os .ibd abertos.
    def close(self):
        self.space.close()
        for ts in self.table_spaces.values():
            ts.close()

    # ------------------------------------------------------------ dicionário
    # FAZ: carrega o dicionário: (1) refaz páginas rasgadas com o doublewrite; (2) varre o ibdata1 agrupando as folhas
    #   por índice; (3) lê as 4 tabelas SYS_*; (4) monta as TableDef. Só faz isso uma vez (depois, load() devolve na
    #   hora).
    def load(self):
        # Já carregado: devolve na hora.
        if self.loaded:
            return self
        # Passo 1: páginas rasgadas são lidas pela cópia íntegra do doublewrite (só em memória).
        self.repaired_pages = self.space.apply_doublewrite(page_health.doublewrite_copies(self.space.path))
        if self.repaired_pages:
            self.log(f"Doublewrite buffer: {len(self.repaired_pages)} página(s) rasgada(s) do ibdata1 refeita(s) com a "
                     f"cópia íntegra (páginas {', '.join(map(str, self.repaired_pages[:10]))}"
                     f"{'...' if len(self.repaired_pages) > 10 else ''}).")
        # Passo 2: varre o ibdata1 agrupando as folhas por índice.
        self.space.scan()
        # Passo 3: lê as 4 tabelas do dicionário; Passo 4: monta as definições.
        sys_rows = {name: self._read_sys_table(name) for name in DICT_INDEX_IDS}
        self._build_tables(sys_rows)
        self.loaded = True
        self.log(f"Leitura direta: {self.space.n_pages} páginas no ibdata1, {len(self.tables)} tabelas no "
                 f"catálogo de tabelas (metadados), {self.space.bad_pages} páginas de índice com cabeçalho inválido.")
        return self

    # FAZ: o 'sys_parser': lê uma das tabelas internas (SYS_TABLES...) e devolve suas linhas VIVAS como dicionários.
    #   Se a mesma chave aparece em várias páginas, vale a de LSN mais alto; linhas apagadas são descartadas. Essas
    #   tabelas usam o formato REDUNDANT, com a planta em SYS_LAYOUT.
    def _read_sys_table(self, name):
        """sys_parser: registros vivos mais recentes de uma tabela SYS_* (formato REDUNDANT)."""
        # Prepara os campos da tabela interna a partir da planta.
        layout = SYS_LAYOUT[name]
        fields = [Column(n, mt, DATA_NOT_NULL, ln) for n, mt, ln in layout]
        # 'best' guarda a versão mais recente de cada linha (chave -> (LSN, transação), apagada?, campos).
        best = {}
        # Percorre as páginas de folha desta tabela interna (index_id fixo) e seus registros.
        for page_no in self.space.leaf_pages.get(DICT_INDEX_IDS[name], []):
            page = self.space.page(page_no)
            lsn = u64(page, FIL_PAGE_LSN)
            for origin in iter_records(page, compact=False):
                try:
                    deleted, raw = parse_redundant(page, origin, fields, read_external=lambda d: d)
                except (CarverError, IndexError, struct.error):
                    continue
                # A chave da linha e a versão (LSN da página, id da transação): vale a mais nova.
                key = tuple(raw[:SYS_PK_LEN[name]])
                trx = be_uint(raw[SYS_PK_LEN[name]] or b"")
                if key not in best or (lsn, trx) > best[key][0]:
                    best[key] = ((lsn, trx), deleted, raw)
        # Converte as linhas vivas em dicionários {coluna: valor} (texto ou número).
        rows = []
        for _, deleted, raw in best.values():
            if deleted:
                continue
            row = {}
            for (fname, mtype, _), value in zip(layout, raw):
                if value is None:
                    row[fname] = None
                elif mtype == DATA_VARCHAR:
                    row[fname] = bytes(value).decode("utf8", errors="replace")
                else:
                    row[fname] = be_uint(value)
            rows.append(row)
        return rows

    # FAZ: junta as 4 tabelas do dicionário: cria uma TableDef por linha de SYS_TABLES, acrescenta as colunas
    #   (SYS_COLUMNS), descobre o índice clusterizado e a chave primária (SYS_INDEXES + SYS_FIELDS) e guarda no
    #   dicionário self.tables.
    def _build_tables(self, sys_rows):
        # Passo 1: uma TableDef por linha de SYS_TABLES, indexada pelo id da tabela.
        by_id = {}
        for r in sys_rows["SYS_TABLES"]:
            if not r["NAME"] or r["ID"] is None or r["N_COLS"] is None:
                continue
            table = TableDef(r["NAME"], r["ID"], r["N_COLS"], flags=r.get("TYPE"), space_id=r.get("SPACE"))
            by_id[r["ID"]] = table
        # Passo 2: acrescenta as colunas a cada tabela.
        for r in sys_rows["SYS_COLUMNS"]:
            table = by_id.get(r["TABLE_ID"])
            if table and r["NAME"] is not None:
                table.columns.append(Column(r["NAME"], r["MTYPE"], r["PRTYPE"], r["LEN"], r["POS"]))
        # Passo 3: as colunas de cada índice (SYS_FIELDS). Índices com prefixo guardam a posição deslocada em 16 bits.
        index_fields = {}
        for r in sys_rows["SYS_FIELDS"]:
            pos = r["POS"] >> 16 if r["POS"] > 0xFFFF else r["POS"]  # índices com prefixo guardam pos<<16
            index_fields.setdefault(r["INDEX_ID"], []).append((pos, r["COL_NAME"]))
        # Passo 4: o índice clusterizado de cada tabela: id, página raiz e colunas da chave primária (ou chave oculta
        #   GEN_CLUST_INDEX).
        for r in sys_rows["SYS_INDEXES"]:
            if r["ID"] is not None:
                self.index_ids.add(r["ID"])
            table = by_id.get(r["TABLE_ID"])
            if not table or not (r["TYPE"] or 0) & DICT_CLUSTERED:
                continue
            table.clustered_index_id = r["ID"]
            table.root_page = r["PAGE_NO"]
            table.generated_pk = r["NAME"] == "GEN_CLUST_INDEX"
            table.pk_columns = [n for _, n in sorted(index_fields.get(r["ID"], []))]
        # Passo 5: ordena as colunas e guarda as tabelas completas no dicionário (pelo nome completo e pelo nome
        #   curto).
        for table in by_id.values():
            table.columns.sort(key=lambda c: c.pos)
            if table.clustered_index_id is None or not table.columns:
                continue
            if not table.generated_pk and not table.pk_columns:
                continue
            self.tables[table.name.lower()] = table
            self.tables.setdefault(table.name.split("/")[-1].lower(), table)

    # FAZ: devolve a TableDef de uma tabela pelo nome ('banco/tabela' ou só 'tabela'), carregando o dicionário se
    #   preciso; None se não existe.
    def table(self, name):
        self.load()
        return self.tables.get(name.lower())

    # FAZ: acrescenta uma tabela cuja definição NÃO veio do dicionário: foi reconstruída do .frm e casada com páginas
    #   (dictionary_rebuild).
    def register_table(self, full_name, table):
        """Acrescenta uma tabela cuja definição não veio do dicionário (reconstruída do .frm e casada com as páginas)."""
        table.name = full_name
        self.tables[full_name.lower()] = table
        self.tables.setdefault(full_name.split("/")[-1].lower(), table)

    # FAZ: devolve as chaves estrangeiras lidas das tabelas internas SYS_FOREIGN e SYS_FOREIGN_COLS: [{tabela, nome,
    #   colunas, ref_tabela, ref_colunas, on_delete, on_update}].
    # POR QUÊ: o .frm NÃO guarda chaves estrangeiras (quem as guarda é o InnoDB), então o DDL feito do .frm as perde.
    def foreign_keys(self):
        """[{tabela, nome, colunas, ref_tabela, ref_colunas, on_delete, on_update}] de SYS_FOREIGN/SYS_FOREIGN_COLS.
        O .frm não guarda chaves estrangeiras (quem as guarda é o InnoDB), então o DDL feito do .frm as perde."""
        # Sem as tabelas internas de FK, não há o que ler.
        if "sys_foreign" not in self.tables or "sys_foreign_cols" not in self.tables:
            return []
        try:
            fk_cols, fk_rows, _ = self.carve("sys_foreign")
            fc_cols, fc_rows, _ = self.carve("sys_foreign_cols")
        except (CarverError, KeyError, OSError):
            return []
        # Passo 1: lê as duas tabelas internas e mapeia o nome de cada coluna para a posição.
        pos = {n.upper(): i for i, n in enumerate(fk_cols)}
        cpos = {n.upper(): i for i, n in enumerate(fc_cols)}
        # Passo 2: junta as colunas de cada FK (podem ser várias).
        parts = {}
        for row in fc_rows:
            parts.setdefault(row[cpos["ID"]], []).append((row[cpos["POS"]], row[cpos["FOR_COL_NAME"]],
                                                          row[cpos["REF_COL_NAME"]]))
        found = []
        # Passo 3: monta cada FK com tabelas, colunas e as ações ON DELETE/ON UPDATE (bits em N_COLS >> 24).
        for row in fk_rows:
            fk_id = row[pos["ID"]]
            columns = sorted(parts.get(fk_id, []))
            if not columns or not row[pos["FOR_NAME"]] or not row[pos["REF_NAME"]]:
                continue
            flags = (row[pos["N_COLS"]] or 0) >> 24
            found.append({
                "tabela": row[pos["FOR_NAME"]], "nome": fk_id.split("/", 1)[-1],
                "colunas": [c[1] for c in columns], "ref_tabela": row[pos["REF_NAME"]],
                "ref_colunas": [c[2] for c in columns],
                "on_delete": next((v for k, v in FK_DELETE_ACTIONS.items() if flags & k), None),
                "on_update": next((v for k, v in FK_UPDATE_ACTIONS.items() if flags & k), None)})
        return found

    # ------------------------------------------------------------ onde estão os dados da tabela
    # FAZ: devolve o arquivo de páginas da tabela: o ibdata1, ou o <banco>/<tabela>.ibd dela (aberto e varrido na
    #   primeira vez).
    def table_space(self, table):
        """Tablespace com as páginas da tabela: o ibdata1, ou o <banco>/<tabela>.ibd dela (aberto sob demanda)."""
        # Espaço 0 = ibdata1; senão, a tabela tem arquivo .ibd próprio (innodb_file_per_table).
        if not table.space_id:
            return self.space
        ts = self.table_spaces.get(table.name)
        if ts is None:
            # Abre o arquivo <banco>/<tabela>.ibd, varre e guarda para reaproveitar.
            path = os.path.join(self.data_dir, *table.name.split("/")) + ".ibd"
            if not os.path.isfile(path):
                raise CarverError(f"arquivo {table.name}.ibd não encontrado (tabela fora do ibdata1)")
            ts = Tablespace(path)
            ts.scan()
            if ts.space_id != table.space_id:
                self.log(f"  {table.name}: o .ibd diz espaço {ts.space_id}, o dicionário diz {table.space_id}; "
                         f"lendo o arquivo mesmo assim.")
            self.table_spaces[table.name] = ts
        return ts

    # FAZ: o index_id do índice clusterizado DENTRO de um arquivo. Num .ibd, se o do dicionário não tem folhas, usa o
    #   índice da página 3 (a raiz).
    def clustered_id(self, table, ts):
        """index_id do índice clusterizado da tabela dentro de ts. Num .ibd, se o do dicionário não tem folhas,
        vale o índice da página 3 (a raiz do clusterizado no MySQL 5.5)."""
        if ts.leaf_pages.get(table.clustered_index_id) or ts is self.space:
            return table.clustered_index_id
        # O índice do dicionário não existe neste .ibd: usa o da página 3 (raiz do clusterizado no MySQL 5.5).
        alt = ts.root_index_id()
        if alt is not None and ts.leaf_pages.get(alt):
            self.log(f"  {table.name}: index_id {table.clustered_index_id} do dicionário não existe no .ibd; "
                     f"usando o índice da página 3 ({alt}).")
            return alt
        return table.clustered_index_id

    # FAZ: recusa tabelas ROW_FORMAT=COMPRESSED (páginas compactadas): a leitura direta não as entende.
    @staticmethod
    def require_readable(table):
        if table.row_format == "COMPRESSED":
            raise CarverError("tabela ROW_FORMAT=COMPRESSED (páginas compactadas) não é lida pela leitura direta")

    # ------------------------------------------------------------ dados
    # FAZ: lê uma coluna LONGA (text/blob grande) guardada FORA da linha: no registro ficam um prefixo local e uma
    #   'referência' de 20 bytes (página, posição, tamanho) para uma cadeia de páginas de BLOB; a função segue a
    #   cadeia e junta os pedaços.
    def read_external(self, data, ts=None):
        """Coluna longa: prefixo local (768 bytes no COMPACT/REDUNDANT, nenhum no DYNAMIC) + referência de 20 bytes."""
        ts = ts or self.space
        # Separa o prefixo local da 'referência' de 20 bytes (página, posição e tamanho total do conteúdo externo).
        local, ref = bytes(data[:-20]), data[-20:]
        page_no, offset, ext_len = u32(ref, 4), u32(ref, 8), u64(ref, 12) & 0xFFFFFFFF
        parts, total = [local], 0
        # Segue a cadeia de páginas de BLOB, juntando os pedaços, até atingir o tamanho total (o limite evita laço
        #   infinito).
        for _ in range(100000):
            if page_no in (FIL_NULL, 0) or total >= ext_len:
                break
            page = ts.page(page_no)
            part_len, next_page = u32(page, offset), u32(page, offset + 4)
            chunk = page[offset + 8:offset + 8 + min(part_len, ext_len - total)]
            parts.append(chunk)
            total += len(chunk)
            page_no, offset = next_page, 38  # páginas seguintes começam em FIL_PAGE_DATA
        return b"".join(parts)

    # ------------------------------------------------------------ saúde da árvore
    # FAZ: confere se uma página é utilizável como página do índice esperado: dentro do arquivo, tipo índice, número e
    #   index_id coerentes, nível certo e estrutura sã. Devolve o motivo do problema ou None.
    def _index_page_error(self, page_no, index_id, compact, leaf=None, ts=None):
        """Motivo de page_no não ser uma página INDEX utilizável do índice index_id, ou None."""
        ts = ts or self.space
        # Verificações em ordem: dentro do arquivo -> é página de índice -> número e index_id coerentes -> nível certo
        #   -> estrutura sã.
        if not 0 <= page_no < ts.n_pages:
            return f"página {page_no} fora do arquivo"
        page = ts.page(page_no)
        if u16(page, FIL_PAGE_TYPE) != FIL_PAGE_INDEX:
            return f"página {page_no} não é de índice (tipo {u16(page, FIL_PAGE_TYPE)})"
        if u32(page, FIL_PAGE_OFFSET) != page_no or u64(page, PAGE_INDEX_ID) != index_id:
            return f"página {page_no} pertence a outro índice ou está torta"
        if leaf is not None and (u16(page, PAGE_LEVEL) == 0) != leaf:
            return f"página {page_no} em nível errado da árvore"
        error = page_structure_error(page, compact)
        return f"página {page_no}: {error}" if error else None

    # FAZ: diz se ler a tabela pelo MySQL derrubaria o servidor e POR QUÊ (ou None se a árvore parece sã). O mysqldump
    #   caminha pela cadeia de folhas (ponteiro 'próxima'); uma página torta na cadeia, ou a raiz torta, é o que causa
    #   as quedas. Só confere o que o MySQL tocaria: serve para EVITAR derrubar o servidor só para descobrir o
    #   problema.
    def damaged_reason(self, name):
        """Motivo pelo qual ler a tabela pelo MySQL derrubaria o servidor, ou None se a árvore parece sã.
        O mysqldump percorre a cadeia de folhas (FIL_PAGE_NEXT); uma página torta nela, ou a raiz torta,
        é o que causa as quedas (assert page_is_comp / Page directory corruption). Só confere o que o
        MySQL tocaria: não substitui a leitura, apenas evita derrubar o servidor para descobrir."""
        table = self.table(name)
        if not table or table.row_format == "COMPRESSED":
            return None
        try:
            ts = self.table_space(table)
        except CarverError:
            return None
        # Passo 1: confere a página RAIZ do índice.
        index_id, compact = self.clustered_id(table, ts), table.compact
        if table.root_page not in (None, FIL_NULL, 0):
            err = self._index_page_error(table.root_page, index_id, compact, ts=ts)
            if err:
                return f"raiz do índice: {err}"
        # Passo 2: confere cada folha e a próxima citada por ela (a cadeia que o mysqldump percorreria).
        for page_no in ts.leaf_pages.get(index_id, []):
            page = ts.page(page_no)
            err = page_structure_error(page, compact)
            if err:
                return f"página {page_no}: {err}"
            nxt = u32(page, FIL_PAGE_NEXT)
            if nxt != FIL_NULL:
                err = self._index_page_error(nxt, index_id, compact, leaf=True, ts=ts)
                if err:
                    return f"cadeia de folhas quebrada após a página {page_no}: {err}"
        return None

    # FAZ: acha as páginas de folha que EXISTIRAM e sumiram (zeradas, sobrescritas, fora do arquivo). Cada folha
    #   aponta para a anterior e a próxima: quem é citado por uma folha sobrevivente e não é folha deste índice foi
    #   PERDIDO, com as linhas que tinha. É um mínimo: várias folhas perdidas em sequência aparecem só nas pontas (as
    #   de entre elas, zeradas, são deduzidas).
    def lost_leaf_pages(self, name):
        """Números das páginas de folha da tabela que existiram e não existem mais (zeradas, sobrescritas, fora do arquivo).
        Cada folha aponta para a anterior e a próxima: quem é citado por uma folha sobrevivente e não é folha deste índice
        foi perdido, com as linhas que tinha. Se não sobrou nenhuma folha, a raiz do dicionário é a página perdida.
        É um mínimo: várias folhas perdidas em sequência aparecem só nas pontas."""
        table = self.table(name)
        if not table:
            return []
        try:
            ts = self.table_space(table)
        except CarverError:
            return []
        index_id = self.clustered_id(table, ts)
        # 'leaves' = folhas deste índice que existem.
        leaves = set(ts.leaf_pages.get(index_id, []))
        lost = set()
        # Cada folha cita a anterior e a próxima; se a citada não é folha deste índice, ela se perdeu.
        for page_no in leaves:
            page = ts.page(page_no)
            for ref in (u32(page, FIL_PAGE_NEXT), u32(page, FIL_PAGE_PREV)):
                if ref != FIL_NULL and ref not in leaves:
                    lost.add(ref)
        # Se não sobrou nenhuma folha, a raiz do dicionário é a página perdida.
        if not leaves and table.root_page not in (None, FIL_NULL, 0):
            lost.add(table.root_page)
        ordered = sorted(lost)
        # entre duas folhas perdidas, páginas zeradas ou de outro tipo (nunca folha deste índice) também eram dela
        # Entre duas folhas perdidas, páginas zeradas (nunca folha deste índice) também eram dela: acrescenta.
        filled = set(ordered)
        for a, b in zip(ordered, ordered[1:]):
            between = range(a + 1, b)
            if 0 < len(between) <= 64 and all(n < ts.n_pages and (n not in leaves) and
                                              page_health.is_blank(ts.page(n)) for n in between):
                filled.update(between)
        return sorted(filled)

    # FAZ: números para o relatório: quantas folhas a tabela tem, quantas estão com a estrutura destruída e o total de
    #   páginas do índice. Não altera nada.
    def tree_health(self, name):
        """{"folhas", "ruins", "total"} do índice clusterizado: quantas folhas a tabela tem e quantas estão com a
        estrutura destruída. Serve ao relatório (não derruba nem altera nada)."""
        table = self.table(name)
        if not table:
            return None
        try:
            ts = self.table_space(table)
        except CarverError:
            return None
        index_id = self.clustered_id(table, ts)
        leaves = ts.leaf_pages.get(index_id, [])
        bad = sum(1 for n in leaves if page_structure_error(ts.page(n), table.compact))
        return {"folhas": len(leaves), "ruins": bad, "total": ts.page_total.get(index_id, 0)}

    # ------------------------------------------------------------ leitura dos registros
    # FAZ: prepara a leitura de uma tabela: aplica a precisão dos DECIMAL (vinda do DDL de apoio), monta a lista de
    #   campos do registro e escolhe o decodificador (COMPACT ou REDUNDANT).
    def _fields_and_parser(self, table, ddl_hint):
        # Aplica a precisão dos DECIMAL (p,s) lida do DDL de apoio às colunas.
        if ddl_hint:
            for m in DECIMAL_HINT.finditer(ddl_hint):
                for c in table.columns:
                    if c.name.lower() == m.group("col").lower():
                        c.decimal = (int(m.group("p")), int(m.group("s") or 0))
        fields = table.record_fields()
        n_key = 1 if table.generated_pk else len(table.pk_columns)
        parser = parse_compact if table.compact else parse_redundant
        # Quais campos viram colunas da saída (as colunas de sistema DB_ROW_ID/DB_TRX_ID/DB_ROLL_PTR ficam de fora).
        out_index = [i for i, f in enumerate(fields) if f.mtype != DATA_SYS or f.name not in
                     ("DB_ROW_ID", "DB_TRX_ID", "DB_ROLL_PTR")]
        return fields, n_key, parser, out_index

    # FAZ: o registro vem com a chave primária primeiro; esta função devolve as colunas (e os valores) na ordem do
    #   CREATE TABLE, que é a ordem que o usuário espera.
    @staticmethod
    def _in_column_order(table, fields, out_index, rows):
        """Volta as colunas (e os valores) para a ordem do CREATE TABLE."""
        names = [fields[i].name for i in out_index]
        order = {c.name: i for i, c in enumerate(table.columns)}
        perm = sorted(range(len(names)), key=lambda i: order.get(names[i], i))
        return [names[i] for i in perm], [[r[i] for i in perm] for r in rows]

    # FAZ: o 'c_parser' do TwinDB: decodifica TODAS as linhas de uma tabela pelo nome. Devolve (nomes das colunas,
    #   linhas, estatísticas).
    # progress(feitas, total): opcional; chamado de tempos em tempos com as páginas já lidas e o total de páginas da
    #   tabela (que se sabe antes de começar), para a tela mostrar o andamento da leitura.
    def carve(self, name, ddl_hint=None, progress=None):
        """
        c_parser: decodifica todas as folhas do índice clusterizado da tabela.
        Registros com a mesma PK vindos de várias páginas (páginas antigas liberadas que ainda têm
        dados) ficam com a versão da página de LSN mais alto; registros marcados como apagados saem.
        Retorna (nomes_das_colunas, linhas, estatísticas).
        """
        table = self.table(name)
        if not table:
            raise CarverError(f"tabela {name} não encontrada no dicionário interno")
        self.require_readable(table)
        ts = self.table_space(table)
        return self.carve_def(table, ts, self.clustered_id(table, ts), ddl_hint, progress)

    # FAZ: o laço principal da leitura: para cada página de folha do índice da tabela e para cada registro da página,
    #   decodifica os campos. Se a mesma chave primária aparece em várias páginas (páginas antigas liberadas que ainda
    #   têm dados), fica a versão da página de LSN mais alto; registros marcados como apagados saem. Funciona para
    #   qualquer definição de tabela (do dicionário ou do .frm).
    def carve_def(self, table, ts, index_id, ddl_hint=None, progress=None):
        """carve() para uma definição de tabela qualquer (do dicionário ou reconstruída do .frm) e o índice dado."""
        # Passo 1: prepara a leitura (campos do registro, tamanho da chave e decodificador).
        fields, n_key, parser, out_index = self._fields_and_parser(table, ddl_hint)
        # 'stats' conta páginas, registros ilegíveis, apagados e duplicados; 'best' guarda a melhor versão de cada
        #   chave.
        stats = {"pages": 0, "bad_records": 0, "deleted": 0, "duplicates": 0}
        best = {}

        # Auxiliar: lê colunas longas guardadas fora da linha.
        def external(data):
            return self.read_external(data, ts)

        # Passo 2: para cada folha do índice clusterizado... (a cada 64 páginas avisa o andamento, se pedido)
        leaves = ts.leaf_pages.get(index_id, [])
        for position, page_no in enumerate(leaves):
            if progress and position % 64 == 0:
                progress(position, len(leaves))
            page = ts.page(page_no)
            stats["pages"] += 1
            lsn = u64(page, FIL_PAGE_LSN)
            # ... e para cada registro da folha:
            for origin in iter_records(page, table.compact):
                try:
                    # decodifica os campos; registro de tipo estranho é ignorado.
                    parsed = parser(page, origin, fields, external)
                    if parsed is None:
                        continue
                    deleted, raw = parsed
                    # A CHAVE do registro (bytes da chave primária) e a VERSÃO (LSN da página, id da transação).
                    key = tuple(bytes(v) if v is not None else None for v in raw[:n_key])
                    trx = be_uint(raw[n_key] or b"")
                    # Mesma chave já vista: é uma versão duplicada (página antiga liberada). Só substitui se esta for
                    #   MAIS NOVA.
                    if key in best:
                        stats["duplicates"] += 1
                        if (lsn, trx) <= best[key][0]:
                            continue
                    # Converte cada campo (bytes) no valor Python certo.
                    values = [None if raw[i] is None else decode_value(fields[i], raw[i]) for i in out_index]
                    best[key] = ((lsn, trx), deleted, values)
                # Registro ilegível: conta e segue (um registro ruim não derruba a leitura da tabela).
                except (CarverError, IndexError, struct.error, ValueError):
                    stats["bad_records"] += 1
        if progress:
            progress(len(leaves), len(leaves))
        # Passo 3: separa os apagados dos vivos e põe as colunas na ordem do CREATE TABLE.
        rows = []
        for _, deleted, values in best.values():
            if deleted:
                stats["deleted"] += 1
            else:
                rows.append(values)
        names, rows = self._in_column_order(table, fields, out_index, rows)
        return names, rows, stats

    # ------------------------------------------------------------ o que o dicionário não explica
    # FAZ: lista os índices com folhas no ibdata1 que NENHUMA linha do dicionário cita: tabelas apagadas, truncadas ou
    #   com a linha do dicionário perdida (o 'lost_and_found' do SQLite). Cada item: {index_id, folhas, registros}.
    def orphan_indexes(self):
        """[{index_id, folhas, registros}] dos índices com folhas no ibdata1 que nenhuma linha do dicionário cita:
        tabelas apagadas, truncadas ou com a linha do dicionário perdida (o `lost_and_found` do SQLite)."""
        # Índices que o dicionário cita (mais os 4 fixos); o resto, com folhas, é 'sem dono'. Ignora ids < 10
        #   (dicionário) e enormes (insert buffer).
        known = set(self.index_ids) | set(DICT_INDEX_IDS.values())
        found = []
        for index_id, pages in sorted(self.space.leaf_pages.items()):
            if index_id in known or index_id < DICT_HDR_FIRST_ID or index_id >= 1 << 32:  # ids <10: dicionário; enormes: insert buffer
                continue
            records = sum(u16(self.space.page(n), PAGE_N_RECS) for n in pages)
            found.append({"index_id": index_id, "folhas": len(pages), "registros": records})
        return found


# ---------------------------------------------------------------------- saída SQL
# FAZ: transforma um valor Python em texto SQL: None -> NULL; número -> como está; bytes -> 0xHEX; texto -> entre
#   aspas com escapes. EXEMPLO: O'Brien -> 'O\'Brien'.
def sql_literal(value):
    # Cada tipo de valor vira o texto SQL certo.
    if value is None:
        return "NULL"
    if isinstance(value, SqlExpr):
        return str(value)
    if isinstance(value, bool):
        return str(int(value))
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, (bytes, bytearray)):
        return "0x" + bytes(value).hex() if value else "''"
    # Texto: escapa barra, aspa, nulo, quebras de linha e Ctrl-Z, e põe entre aspas simples.
    escaped = (value.replace("\\", "\\\\").replace("'", "\\'").replace("\0", "\\0")
               .replace("\n", "\\n").replace("\r", "\\r").replace("\x1a", "\\Z"))
    return f"'{escaped}'"


# FAZ: transforma linhas em comandos INSERT (500 linhas por comando, com a lista de colunas, como o mysqldump
#   --complete-insert) e devolve em bytes utf8.
def rows_to_sql(table_name, columns, rows, batch=500):
    """INSERTs com lista de colunas (como mysqldump --complete-insert), em bytes utf8."""
    if not rows:
        return b""
    # Cada INSERT leva 500 linhas (mais rápido que um INSERT por linha).
    cols = ", ".join(f"`{c}`" for c in columns)
    out = []
    for start in range(0, len(rows), batch):
        values = ",".join("(" + ",".join(sql_literal(v) for v in row) + ")" for row in rows[start:start + batch])
        out.append(f"INSERT INTO `{table_name}` ({cols}) VALUES {values};")
    return "\n".join(out).encode("utf8")


# ---------------------------------------------------------------------- DDL a partir do dicionário
# TABELAS PARA GERAR O TIPO SQL: INT_NAMES = tipo numérico -> nome SQL; BLOB_NAMES = tamanho do campo ->
#   tiny/médio/longo; TYPE_FAMILY = nome do tipo -> 'família' (int, texto, data...), usada para comparar um tipo do
#   dicionário com o do DDL de apoio.
INT_NAMES = {MYSQL_TINY: "tinyint", MYSQL_SHORT: "smallint", MYSQL_INT24: "mediumint", MYSQL_LONG: "int",
             MYSQL_LONGLONG: "bigint"}
# LEN do BLOB no dicionário é o pack_length do campo: 1..4 bytes de tamanho + o ponteiro (8 em MySQL 64-bit, 4 em 32-bit)
BLOB_NAMES = {9: "tiny", 10: "", 11: "medium", 12: "long", 5: "tiny", 6: "", 7: "medium", 8: "long"}
TYPE_FAMILY = {"tinyint": "int", "smallint": "int", "mediumint": "int", "int": "int", "integer": "int",
               "bigint": "int", "float": "real", "double": "real", "real": "real", "decimal": "decimal",
               "numeric": "decimal", "datetime": "datetime", "date": "date", "time": "time",
               "timestamp": "timestamp", "year": "year", "varchar": "text", "char": "text", "tinytext": "text",
               "text": "text", "mediumtext": "text", "longtext": "text", "tinyblob": "blob", "blob": "blob",
               "mediumblob": "blob", "longblob": "blob", "enum": "enum", "set": "enum", "bit": "bit"}
# PADRÕES para ler um CREATE TABLE de apoio: INDEX_LINE reconhece linhas de índice/restrição; HINT_COLUMN reconhece
#   linhas de coluna (nome e tipo); HINT_OPTIONS acha as opções do fim (ENGINE=...).
INDEX_LINE = re.compile(r"^(PRIMARY\s+KEY|UNIQUE|KEY|INDEX|FULLTEXT|SPATIAL|CONSTRAINT|FOREIGN)", re.I)
HINT_COLUMN = re.compile(r"^`(?P<name>[^`]+)`\s+(?P<type>[A-Za-z]+)", re.I)


# FAZ: a partir do que o InnoDB sabe da coluna (tipo interno, flags, tamanho), escreve o tipo SQL. EXEMPLOS: int
#   unsigned; varchar(100); decimal(10,2); datetime; longtext.
def column_type_sql(col):
    """Tipo SQL de uma coluna a partir de MTYPE/PRTYPE/LEN (o que o InnoDB sabe dela)."""
    # Primeiro os tipos numéricos e de data; depois DECIMAL e BIT; BLOB/TEXT; e por último char/varchar (binário ou
    #   texto).
    ty, unsigned = col.mysql_type, " unsigned" if col.unsigned else ""
    mbmax = charset_mbmaxlen(col.coll_id)
    # ENUM/SET chegam aqui como número (a lista de valores NÃO está no dicionário); sem o DDL de apoio viram smallint
    #   unsigned.
    if col.mtype == DATA_INT and ty in (MYSQL_STRING, MYSQL_ENUM, MYSQL_SET):
        return "smallint unsigned"  # ENUM/SET sem a lista de valores: guarda o índice numérico
    if ty in INT_NAMES:
        return INT_NAMES[ty] + unsigned
    simple = {MYSQL_FLOAT: "float", MYSQL_DOUBLE: "double", MYSQL_DATETIME: "datetime", MYSQL_DATE: "date",
              MYSQL_NEWDATE: "date", MYSQL_TIME: "time", MYSQL_TIMESTAMP: "timestamp", MYSQL_YEAR: "year(4)"}
    if ty in simple:
        return simple[ty]
    # DECIMAL sem precisão conhecida usa (30,10) como palpite.
    if ty == MYSQL_NEWDECIMAL:
        p, s = col.decimal or (30, 10)
        return f"decimal({p},{s}){unsigned}"
    if ty == MYSQL_BIT:
        return f"bit({max(1, col.len * 8)})"
    if col.mtype == DATA_BLOB or ty == MYSQL_BLOB:
        kind = BLOB_NAMES.get(col.len, "long")
        return f"{kind}blob" if col.coll_id in (0, CHARSET_BINARY) else f"{kind}text"
    # Texto fixo (char) ou variável (varchar), em caracteres (tamanho em bytes dividido pelo máximo de bytes por
    #   caractere).
    binary = col.coll_id == CHARSET_BINARY or col.mtype in (DATA_FIXBINARY, DATA_BINARY)
    if col.fixed_size(True) and col.mtype in (DATA_CHAR, DATA_MYSQL, DATA_FIXBINARY) or ty == MYSQL_STRING:
        return f"binary({col.len})" if binary else f"char({max(1, col.len // mbmax)})"
    return f"varbinary({col.len})" if binary else f"varchar({max(1, col.len // mbmax)})"


# FAZ: lê um CREATE TABLE de apoio (do servidor, do .frm ou do catálogo) e separa as linhas de COLUNAS das linhas de
#   ÍNDICES.
def _hint_lines(hint_ddl):
    """Linhas de definição (colunas e índices) de um CREATE TABLE, sem vírgulas finais."""
    # Sem DDL de apoio, não há o que separar.
    if not hint_ddl or "(" not in hint_ddl:
        return {}, []
    body = hint_ddl[hint_ddl.index("(") + 1:]
    body = re.split(r"\)\s*(?:ENGINE|TYPE|DEFAULT\s+CHARSET|$)", body, maxsplit=1, flags=re.I)[0]
    columns, indexes = {}, []
    # Cada linha do CREATE TABLE é uma coluna (guarda o texto e a 'família' do tipo) ou um índice.
    for raw in body.splitlines():
        line = raw.strip().rstrip(",").strip()
        if line.endswith(")") and line.count(")") > line.count("("):
            line = line[:-1].rstrip()
        if not line:
            continue
        m = HINT_COLUMN.match(line)
        if m:
            columns[m.group("name").lower()] = (line, TYPE_FAMILY.get(m.group("type").lower()))
        elif INDEX_LINE.match(line):
            indexes.append(line)
    return columns, indexes


# FAZ: True se o DDL disponível NÃO tem exatamente as mesmas colunas, na mesma ordem, do dicionário do InnoDB (aí o
#   dicionário manda: é a estrutura com que os dados foram gravados).
def dictionary_columns_differ(table, ddl):
    """True se o DDL disponível não tem exatamente as colunas (e ordem) do dicionário interno."""
    hinted, _ = _hint_lines(ddl)
    return [c.name.lower() for c in table.columns] != list(hinted)


# FAZ: a 'família' do tipo de uma coluna do dicionário (int, enum, decimal...), para comparar com a do DDL de apoio.
#   ENUM/SET são inteiros no dicionário.
def own_family(col):
    """Família do tipo (int, enum, decimal...) como o DDL de apoio a escreveria. ENUM/SET são inteiros no dicionário."""
    if col.mtype == DATA_INT and col.mysql_type in (MYSQL_STRING, MYSQL_ENUM, MYSQL_SET):
        return "enum"
    return TYPE_FAMILY.get(re.match(r"[a-z]+", column_type_sql(col)).group(0))


HINT_OPTIONS = re.compile(r"\)\s*(ENGINE\s*=.*)$", re.I | re.S)


# FAZ: extrai do DDL de apoio as opções da tabela (ENGINE, CHARSET, ROW_FORMAT...), sem o contador AUTO_INCREMENT.
def _hint_options(hint_ddl):
    """Opções de tabela (ENGINE, CHARSET, ROW_FORMAT...) do DDL de apoio, sem o contador AUTO_INCREMENT."""
    m = HINT_OPTIONS.search(hint_ddl or "")
    if not m:
        return None
    return re.sub(r"\s*AUTO_INCREMENT\s*=\s*\d+", "", m.group(1)).strip().rstrip(";")


# FAZ: monta o CREATE TABLE fiel à estrutura gravada no ibdata1. O que o dicionário NÃO guarda (AUTO_INCREMENT,
#   DEFAULT, índices secundários, ENUM/SET, precisão do DECIMAL) vem do DDL de apoio, desde que a coluna exista nele
#   com o mesmo tipo; colunas que o apoio não conhece usam o tipo do dicionário.
def ddl_from_dictionary(table, hint_ddl=None):
    """
    CREATE TABLE fiel à estrutura gravada no ibdata1. Atributos que o dicionário não guarda
    (AUTO_INCREMENT, DEFAULT, índices secundários, ENUM/SET, precisão do DECIMAL) vêm do DDL de apoio
    (SHOW CREATE, .frm ou catálogo) quando a coluna existe nele com o mesmo tipo; colunas que o apoio não conhece
    (ou com tipo diferente) usam o tipo do dicionário.
    """
    # Passo 1: separa colunas e índices do DDL de apoio.
    hinted, hint_indexes = _hint_lines(hint_ddl)
    name = table.name.split("/")[-1]
    lines = []
    # Passo 2: para cada coluna, usa a linha do apoio SE ela existir com a mesma 'família' de tipo (ela traz DEFAULT,
    #   AUTO_INCREMENT...); senão monta pelo dicionário (tipo, charset, NOT NULL).
    for col in table.columns:
        own_type = column_type_sql(col)
        hint = hinted.get(col.name.lower())
        if hint and hint[1] == own_family(col):
            lines.append(hint[0])
            continue
        charset = ""
        if col.mtype in (DATA_VARCHAR, DATA_CHAR, DATA_MYSQL, DATA_VARMYSQL, DATA_BLOB) and \
                (col.coll_id in UTF8_IDS or col.coll_id in UTF8MB4_IDS):
            charset = " CHARACTER SET utf8mb4" if col.coll_id in UTF8MB4_IDS else " CHARACTER SET utf8"
        lines.append(f"`{col.name}` {own_type}{charset}{'' if col.nullable else ' NOT NULL'}")
    # Passo 3: a PRIMARY KEY vem do dicionário.
    if not table.generated_pk and table.pk_columns:
        lines.append("PRIMARY KEY (" + ", ".join(f"`{c}`" for c in table.pk_columns) + ")")
    known = {c.name.lower() for c in table.columns}
    # Passo 4: índices secundários do apoio, só se todas as colunas deles existem (chaves estrangeiras ficam de fora
    #   para não travar a carga).
    for line in hint_indexes:
        if re.match(r"(PRIMARY|CONSTRAINT|FOREIGN)", line, re.I):
            continue  # PK vem do dicionário; FKs ficam de fora para não travar a carga
        refs = {r.lower() for r in re.findall(r"`([^`]+)`", line.split("(", 1)[-1])}
        if refs and refs <= known:
            lines.append(line)
    # Passo 5: opções da tabela (ENGINE, CHARSET...) e o ROW_FORMAT=DYNAMIC se for o caso.
    options = _hint_options(hint_ddl) or "ENGINE=InnoDB DEFAULT CHARSET=latin1"
    if table.row_format == "DYNAMIC" and "ROW_FORMAT" not in options.upper():
        options += " ROW_FORMAT=DYNAMIC"
    return f"CREATE TABLE `{name}` (\n  " + ",\n  ".join(lines) + f"\n) {options}"


# ---------------------------------------------------------------------- extensão física dos registros
# FAZ: calcula onde um registro COMEÇA e onde TERMINA na página, segundo uma definição de colunas. Numa página sem
#   buracos o fim de um registro é o começo do seguinte; por isso serve para saber se uma definição de colunas é a
#   CERTA (usado por dictionary_rebuild).
def record_extent(page, origin, fields, compact):
    """(início, fim) em bytes do registro dentro da página, segundo a definição `fields`: o cabeçalho que fica antes
    da origem (campos de NULL, tamanhos variáveis e o cabeçalho fixo) e os dados depois dela. Numa página sem buracos o
    fim de um registro é o início do seguinte; por isso serve para saber se uma definição de colunas é a certa."""
    # COMPACT: o começo é a origem menos o cabeçalho de 5 bytes, o bitmap de NULL e os bytes de tamanho; o fim é a
    #   origem mais os dados dos campos.
    if compact:
        nullable = [f for f in fields if f.nullable]
        null_bytes = (len(nullable) + 7) // 8
        len_ptr = origin - 6 - null_bytes
        null_ptr, null_i, pos, len_bytes = origin - 6, 0, origin, 0
        for f in fields:
            if f.nullable:
                is_null = page[null_ptr - null_i // 8] & (1 << (null_i % 8))
                null_i += 1
                if is_null:
                    continue
            size = f.fixed_size(True)
            if not size:
                size = page[len_ptr]
                len_ptr -= 1
                len_bytes += 1
                if f.two_byte_len_possible() and size & 0x80:
                    size = ((size & 0x3F) << 8) | page[len_ptr]
                    len_ptr -= 1
                    len_bytes += 1
            pos += size
        return origin - 5 - null_bytes - len_bytes, pos
    # REDUNDANT: o cabeçalho diz quantos campos há e a largura dos offsets; o último offset dá o fim do registro.
    n_fields = (u16(page, origin - 4) >> 1) & 0x3FF
    one_byte = page[origin - 3] & 0x01
    width = 1 if one_byte else 2
    end = (page[origin - 7 - (n_fields - 1)] & 0x7F) if one_byte else (u16(page, origin - 8 - 2 * (n_fields - 1)) & 0x3FFF)
    return origin - 6 - n_fields * width, origin + end
