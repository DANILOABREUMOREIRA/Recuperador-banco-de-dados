"""
frm_parser.py - Lê o .frm do MySQL 5.x (e os arquivos de view e de trigger) em Python puro e refaz o CREATE TABLE,
sem precisar do mysqld. É o que o `dbsake frmdump` e o porte do `open_binary_frm()` do innodump fazem.

Serve quando o mysqld não consegue abrir a tabela: o .frm guarda os tipos exatos, NULL/NOT NULL, valores padrão,
listas de ENUM/SET, charset por coluna e os índices, que o dicionário do InnoDB não guarda (ele perde a precisão do
DECIMAL, os rótulos de ENUM/SET, o tamanho do BIT, os padrões e os prefixos de índice).

Formato (sql/table.cc open_binary_frm, sql/unireg.cc, sql/field.h):
  0..63        cabeçalho (versão, engine, comprimentos, charset, versão do MySQL em 51..54)
  64           nomes de arquivo + posição do forminfo (u32 em 64 + u16@4)
  4096         informação dos índices; logo depois (4096 + tamanho do bloco) o registro padrão (defaults + bits de NULL)
  forminfo     288 bytes (nº de colunas em +258), telas, 17 bytes por coluna, nomes, listas de ENUM/SET, comentários
Só entende tabelas de MySQL 5.0 a 5.7 (.frm versão 9 ou 10); partições e formatos antigos viram FrmError.
"""

# VISÃO GERAL: cada tabela do MySQL tem um arquivo .frm: uma 'ficha de cadastro' binária com o nome e o tipo exato de
#   cada coluna, NULL/NOT NULL, valores padrão, listas de ENUM/SET, charset e índices. Este arquivo LÊ essa ficha em
#   Python puro, SEM o servidor, e reescreve o comando CREATE TABLE. Serve quando o mysqld não consegue abrir a
#   tabela: o dicionário do InnoDB não guarda tudo isso (perde a precisão do DECIMAL, os rótulos do ENUM, os valores
#   padrão, os prefixos de índice), mas o .frm guarda. Lê também VIEWS (arquivo de texto 'TYPE=VIEW') e TRIGGERS
#   (arquivos .TRG). Imita o open_binary_frm() do MySQL. Se algo não bate, levanta FrmError em vez de ADIVINHAR.
# COMO LER: 1) as constantes (tabelas de códigos do MySQL); 2) as classes FrmColumn/FrmKey/FrmTable (onde o resultado
#   é guardado); 3) parse_frm (leitura do arquivo); 4) as funções que escrevem o SQL (column_sql, key_sql,
#   frm_to_ddl); 5) views e triggers no final. MAPA DO ARQUIVO .frm: bytes 0..63 = cabeçalho; a partir do 64 = nomes e
#   a posição do 'forminfo'; a partir de 4096 = informação dos índices e, logo depois, o 'registro padrão' (valores
#   default + bits de NULL); o forminfo tem 288 bytes de cabeçalho, 17 bytes por coluna, os nomes, as listas de
#   ENUM/SET e os comentários.

# Importações: 'os' = caminhos; 're' = padrões de texto; 'struct' = ler números de bytes; 'xml.etree' = ler o
#   Index.xml dos charsets do MySQL; mysql_codec = conversão de DECIMAL, datas e horas (compartilhado com
#   innodb_carver).
import os
import re
import struct
import xml.etree.ElementTree as ET

from mysql_codec import decimal_size, decode_newdecimal, format_date, format_datetime, format_time

# TAMANHOS FIXOS: cada coluna ocupa 17 bytes na tabela de colunas do .frm; o cabeçalho 'forminfo' tem 288 bytes;
#   FRM_MAGIC = os 2 primeiros bytes de todo .frm de tabela.
FIELD_PACK_LENGTH = 17
FORMINFO_LENGTH = 288
FRM_MAGIC = b"\xfe\x01"

# TIPOS DE COLUNA do MySQL (enum_field_types, mysql_com.h): o .frm guarda cada tipo como um número. T_TINY=tinyint,
#   T_LONG=int, T_VARCHAR=varchar, T_BLOB=text/blob etc.
# enum_field_types (mysql_com.h)
T_DECIMAL, T_TINY, T_SHORT, T_LONG, T_FLOAT, T_DOUBLE = 0, 1, 2, 3, 4, 5
T_TIMESTAMP, T_LONGLONG, T_INT24, T_DATE, T_TIME, T_DATETIME, T_YEAR, T_NEWDATE = 7, 8, 9, 10, 11, 12, 13, 14
T_VARCHAR, T_BIT = 15, 16
T_NEWDECIMAL, T_ENUM, T_SET = 246, 247, 248
T_TINY_BLOB, T_MEDIUM_BLOB, T_LONG_BLOB, T_BLOB = 249, 250, 251, 252
T_VAR_STRING, T_STRING, T_GEOMETRY = 253, 254, 255

# INT_TYPES: tipo numérico -> (nome SQL, bytes). BLOB_KIND: tipo de blob -> (prefixo do nome, bytes do tamanho).
#   EXEMPLO: T_MEDIUM_BLOB -> ('medium', 3) = mediumblob/mediumtext.
INT_TYPES = {T_TINY: ("tinyint", 1), T_SHORT: ("smallint", 2), T_INT24: ("mediumint", 3), T_LONG: ("int", 4),
             T_LONGLONG: ("bigint", 8)}
BLOB_KIND = {T_TINY_BLOB: ("tiny", 1), T_BLOB: ("", 2), T_MEDIUM_BLOB: ("medium", 3), T_LONG_BLOB: ("long", 4)}

# FLAGS DA COLUNA (pack_flag, field.h): bits que dizem se a coluna aceita NULL, tem valor padrão, é com sinal, tem
#   zerofill, quantas casas decimais...
# pack_flag (field.h)
F_DECIMAL, F_NUMBER, F_ZEROFILL = 1, 2, 4
F_BITFIELD = 512
F_NO_DEFAULT, F_MAYBE_NULL = 16384, 32768
F_DEC_SHIFT, F_MAX_DEC = 8, 31
NOT_FIXED_DEC = 31

# UTYPE: códigos extras da coluna: 15 = AUTO_INCREMENT; 21/22/23 = comportamento do TIMESTAMP (atualiza ao alterar,
#   valor padrão = agora, ou os dois).
# Field::utype (field.h)
U_NEXT_NUMBER = 15
U_TIMESTAMP_UN, U_TIMESTAMP_DN, U_TIMESTAMP_DNUN = 21, 22, 23

# FLAGS DE ÍNDICE (my_base.h): HA_NOSAME = único; HA_FULLTEXT; HA_SPATIAL.
# HA_* de chaves (my_base.h), já com o XOR com HA_NOSAME desfeito
HA_NOSAME, HA_FULLTEXT, HA_SPATIAL = 1, 128, 1024

# LEGACY_ENGINES: número antigo do 'tipo de arquivo' -> nome do engine (MyISAM, InnoDB...), usado quando o nome do
#   engine não está gravado. ROW_FORMATS: número -> nome do ROW_FORMAT.
LEGACY_ENGINES = {3: "MyISAM", 6: "MEMORY", 9: "MyISAM", 10: "MRG_MYISAM", 12: "InnoDB", 14: "ndbcluster",
                  16: "ARCHIVE", 17: "CSV", 18: "FEDERATED", 19: "BLACKHOLE", 20: "partition", 28: "PERFORMANCE_SCHEMA"}
ROW_FORMATS = {1: "FIXED", 2: "DYNAMIC", 3: "COMPRESSED", 4: "REDUNDANT", 5: "COMPACT", 6: "PAGE"}
# MBMAXLEN: máximo de bytes por caractere de cada charset multibyte (utf8 = 3). Serve para converter 'tamanho em
#   bytes' em 'tamanho em caracteres' (varchar(100) de utf8 = 300 bytes).
MBMAXLEN = {"utf8": 3, "utf8mb4": 4, "ucs2": 2, "utf16": 4, "utf16le": 4, "utf32": 4, "big5": 2, "gbk": 2, "gb2312": 2,
            "sjis": 2, "cp932": 2, "ujis": 3, "eucjpms": 3, "euckr": 2, "gb18030": 4}

# CHARSET_INDEX: o Index.xml do MySQL embutido, que lista todos os charsets e collations com seus números.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CHARSET_INDEX = os.path.join(BASE_DIR, "mysql", "share", "charsets", "Index.xml")
# BUILTIN_COLLATIONS: reserva embutida com os charsets mais comuns, caso o Index.xml não esteja à mão. Formato: id ->
#   (charset, collation, é a padrão do charset?).
# Plano B se o Index.xml do MySQL embutido não estiver à mão: os charsets mais comuns.
BUILTIN_COLLATIONS = {8: ("latin1", "latin1_swedish_ci", True), 47: ("latin1", "latin1_bin", False),
                      48: ("latin1", "latin1_general_ci", False), 49: ("latin1", "latin1_general_cs", False),
                      15: ("latin1", "latin1_danish_ci", False), 31: ("latin1", "latin1_german2_ci", False),
                      33: ("utf8", "utf8_general_ci", True), 83: ("utf8", "utf8_bin", False),
                      45: ("utf8mb4", "utf8mb4_general_ci", True), 46: ("utf8mb4", "utf8mb4_bin", False),
                      63: ("binary", "binary", True), 11: ("ascii", "ascii_general_ci", True),
                      65: ("ascii", "ascii_bin", False), 3: ("dec8", "dec8_swedish_ci", True),
                      5: ("latin1", "latin1_german1_ci", False), 24: ("gb2312", "gb2312_chinese_ci", True)}


# FAZ: o erro específico deste leitor: 'este .frm não pode ser lido com segurança'. Quem chama captura e usa outra
#   fonte de estrutura (dicionário, catálogo).
class FrmError(Exception):
    pass


# _charset_cache: guarda a tabela de charsets já lida, para ler o XML uma vez só.
_charset_cache = {}


# FAZ: devolve {id da collation: (charset, collation, é_a_padrão)}, lido do Index.xml do MySQL embutido (com a reserva
#   BUILTIN_COLLATIONS) e guardado em cache.
def load_charsets(index_path=CHARSET_INDEX):
    """{id da collation: (charset, collation, é_a_padrão_do_charset)} lido do Index.xml do MySQL embutido."""
    # Já lido antes? Devolve do cache.
    if index_path in _charset_cache:
        return _charset_cache[index_path]
    # Começa com a reserva embutida e acrescenta/atualiza com o que o Index.xml trouxer; se o XML não existir ou
    #   estiver ruim, fica só a reserva.
    table = dict(BUILTIN_COLLATIONS)
    try:
        root = ET.parse(index_path).getroot()
        for charset in root.iter("charset"):
            for collation in charset.iter("collation"):
                flags = [f.text for f in collation.iter("flag")]
                table[int(collation.get("id"))] = (charset.get("name"), collation.get("name"), "primary" in flags)
    except (OSError, ET.ParseError, ValueError, TypeError):
        pass
    _charset_cache[index_path] = table
    return table


# FAZ: lê 2 bytes como um número, no formato do .frm (little-endian: o byte menos significativo vem primeiro).
def u16(b, o):
    return b[o] | (b[o + 1] << 8)


# FAZ: lê 3 bytes como um número (little-endian).
def u24(b, o):
    return b[o] | (b[o + 1] << 8) | (b[o + 2] << 16)


# FAZ: lê 4 bytes como um número (little-endian).
def u32(b, o):
    return struct.unpack_from("<I", b, o)[0]


# FAZ: lê 'count' listas de textos gravadas num bloco de bytes, no formato do MySQL: <sep>texto<sep>texto<sep>\0, onde
#   <sep> é um byte separador qualquer. Usado para a lista de NOMES das colunas e para os VALORES de ENUM/SET.
#   EXEMPLO: b',a,b,\0' -> [[b'a', b'b']].
def split_typelibs(block, count):
    """Lê `count` listas no formato do fix_type_pointers(): <sep>nome<sep>nome<sep>\\0, uma atrás da outra."""
    # Formato: o primeiro byte de cada lista é o SEPARADOR; os textos ficam entre separadores; a lista termina com o
    #   separador seguido de 0.
    result, pos = [], 0
    for _ in range(count):
        if pos >= len(block):
            result.append([])
            continue
        sep = block[pos]
        if sep == 0:
            result.append([])
            pos += 1
            continue
        names = []
        while True:
            end = block.find(bytes([sep]), pos + 1)
            if end < 0:
                pos = len(block)
                break
            names.append(block[pos + 1:end])
            pos = end
            if block[pos + 1:pos + 2] in (b"\0", b""):  # separador final seguido do 0 que fecha a lista
                pos += 2
                break
        result.append(names)
    return result


# FAZ: uma COLUNA da tabela como o .frm a descreve: nome, tipo (ftype e 'kind' em palavras), tamanho, NULL, sinal,
#   valor padrão, charset, rótulos de ENUM/SET etc.
class FrmColumn:
    # FAZ: cria a coluna com tudo vazio; parse_frm preenche campo a campo.
    def __init__(self):
        self.name = ""
        self.ftype = 0           # enum_field_types
        self.length = 0          # field_length (bytes; para números, a largura de exibição)
        self.recpos = 0
        self.pack_flag = 0
        self.unireg = 0
        self.interval_nr = 0
        self.charset_id = 0
        self.comment = ""
        self.labels = []         # ENUM/SET
        self.nullable = False
        self.unsigned = False
        self.zerofill = False
        self.decimals = 0
        self.auto_increment = False
        self.no_default = False
        self.kind = ""           # int, float, decimal, date, time, datetime, timestamp, year, char, varchar, blob, enum,
        #                          set, bit, geometry
        self.default = None      # texto do valor padrão (já sem aspas), None = DEFAULT NULL ou sem padrão
        self.has_default = False
        self.default_unknown = False
        self.default_now = False
        self.on_update_now = False
        self.null_bit = None     # (byte, máscara) no registro padrão

    # FAZ: True se a coluna é binária (charset 63): binary/varbinary/blob em vez de char/varchar/text.
    @property
    def is_binary_charset(self):
        return self.charset_id == 63

    # FAZ: quantos bytes a coluna inteira ocupa numa chave (só para char/varchar); decide se um índice usa a coluna
    #   inteira ou só um prefixo.
    def key_bytes(self):
        """Bytes que a coluna inteira ocupa como parte de uma chave (None = prefixo obrigatório ou fixo)."""
        if self.kind in ("char", "varchar"):
            return self.length
        return None


# FAZ: um ÍNDICE da tabela: nome, flags (único? fulltext?) e as partes (coluna + tamanho do prefixo, se for índice
#   parcial).
class FrmKey:
    # FAZ: cria o índice vazio.
    def __init__(self):
        self.name = ""
        self.flags = 0
        self.length = 0
        self.algorithm = 0
        self.parts = []          # [(coluna, prefixo em bytes ou None)]

    # FAZ: True se o índice é único.
    @property
    def unique(self):
        return bool(self.flags & HA_NOSAME)

    # FAZ: o tipo do índice em palavras: PRIMARY, UNIQUE, FULLTEXT, SPATIAL ou KEY (comum).
    @property
    def kind(self):
        if self.flags & HA_FULLTEXT:
            return "FULLTEXT"
        if self.flags & HA_SPATIAL:
            return "SPATIAL"
        return "PRIMARY" if self.name == "PRIMARY" else ("UNIQUE" if self.unique else "KEY")


# FAZ: a TABELA inteira como o .frm a descreve: engine, colunas, índices, charset, ROW_FORMAT, comentário, versão do
#   MySQL.
class FrmTable:
    # FAZ: cria a tabela vazia.
    def __init__(self):
        self.name = ""
        self.engine = ""
        self.columns = []
        self.keys = []
        self.charset_id = 0
        self.row_type = 0
        self.key_block_size = 0
        self.min_rows = self.max_rows = self.avg_row_length = 0
        self.comment = ""
        self.mysql_version = 0
        self.options = 0         # db_create_options (HA_OPTION_PACK_RECORD = 1 ...)
        self.reclength = 0
        self.charsets = {}

    # FAZ: devolve o índice que o InnoDB usa como chave clusterizada: a PRIMARY KEY; sem ela, o primeiro índice ÚNICO
    #   cujas colunas não aceitam NULL; senão None (o InnoDB cria uma chave oculta).
    def primary_key(self):
        """Colunas da chave que o InnoDB usa como índice clusterizado: a PRIMARY, senão a primeira UNIQUE sem NULL."""
        for key in self.keys:
            if key.kind == "PRIMARY":
                return key
        for key in self.keys:
            if key.unique and all(not col.nullable for col, _ in key.parts):
                return key
        return None


# ---------------------------------------------------------------------- leitura do .frm
# FAZ: a função principal. Recebe os BYTES de um .frm e devolve um FrmTable. Cada passo confere que as posições estão
#   dentro do arquivo; qualquer incoerência levanta FrmError. Fluxo: cabeçalho -> posição do forminfo -> registro
#   padrão -> nomes das colunas -> ficha de cada coluna -> bits de NULL e valores padrão -> índices -> engine e
#   comentário.
def parse_frm(data, charsets=None):
    """bytes de um .frm -> FrmTable. Levanta FrmError se não for um .frm de tabela que dê para ler com segurança."""
    # Passo 1: validações de cabeçalho. Recusa: view/parser, arquivo curto ou sem a assinatura FE 01, formato muito
    #   antigo e tabelas particionadas.
    charsets = charsets or load_charsets()
    if data[:5] == b"TYPE=":
        raise FrmError("arquivo de view ou de parser, não de tabela")
    if len(data) < 4096 or data[:2] != FRM_MAGIC:
        raise FrmError("não é um .frm (cabeçalho inválido ou arquivo curto)")
    if data[2] < 9:
        raise FrmError(f"formato .frm antigo (versão {data[2]})")
    # Passo 2: lê do cabeçalho de 64 bytes: tamanhos, charset da tabela, ROW_FORMAT, versão do MySQL, opções.
    table = FrmTable()
    table.charsets = charsets
    legacy_type = data[3]
    if legacy_type == 20 or data[61]:
        raise FrmError("tabela particionada não é lida")
    io_size = u16(data, 6)
    names_len = u16(data, 4)
    key_info_len = u16(data, 28)
    keybuf_len = u16(data, 14)
    if keybuf_len == 0xFFFF:
        keybuf_len = u32(data, 47)
    table.reclength = u16(data, 16)
    table.max_rows, table.min_rows, table.avg_row_length = u32(data, 18), u32(data, 22), u32(data, 34)
    table.charset_id = data[38] | (data[41] << 8)
    table.row_type = data[40]
    table.key_block_size = u16(data, 62)
    table.mysql_version = u32(data, 51)
    table.options = u16(data, 30)
    # Passo 3: confere que a posição do 'forminfo' e do registro padrão caem DENTRO do arquivo (senão o arquivo está
    #   truncado ou destruído).
    if io_size < 64 or io_size > 65536 or 64 + names_len + 4 > len(data):
        raise FrmError("cabeçalho do .frm inconsistente")
    form_pos = u32(data, 64 + names_len)
    if form_pos + FORMINFO_LENGTH > len(data) or form_pos < io_size:
        raise FrmError("posição do forminfo fora do arquivo")
    record_pos = io_size + keybuf_len
    if record_pos + table.reclength > len(data) or table.reclength == 0:
        raise FrmError("registro padrão fora do arquivo")
    record = data[record_pos:record_pos + table.reclength]

    # Passo 4: lê o 'forminfo' (288 bytes): quantas colunas, onde começam os nomes, as listas de ENUM/SET e os
    #   comentários.
    form = data[form_pos:form_pos + FORMINFO_LENGTH]
    n_fields = u16(form, 258)
    info_length = u16(form, 260)
    n_length = u16(form, 268)
    int_count, int_length = u16(form, 270), u16(form, 274)
    com_length = u16(form, 284)
    if not 1 <= n_fields <= 4096:
        raise FrmError(f"número de colunas inválido ({n_fields})")
    # Calcula onde está cada bloco (fichas das colunas, nomes, listas, comentários) e confere que cabem no arquivo.
    fields_pos = form_pos + FORMINFO_LENGTH + info_length
    names_pos = fields_pos + n_fields * FIELD_PACK_LENGTH
    end = names_pos + n_length + int_length + com_length
    if end > len(data):
        raise FrmError("definição das colunas passa do fim do arquivo")

    # Passo 5: lê os nomes das colunas e as listas de valores de ENUM/SET.
    names = split_typelibs(data[names_pos:names_pos + n_length], 1)[0]
    if len(names) != n_fields:
        raise FrmError("quantidade de nomes de coluna não bate com o número de colunas")
    intervals = split_typelibs(data[names_pos + n_length:names_pos + n_length + int_length], int_count)
    comments = data[names_pos + n_length + int_length:end]

    # Passo 6: para cada coluna, lê a ficha de 17 bytes: tamanho, posição no registro, flags, tipo, charset,
    #   comentário e a lista de rótulos (se ENUM/SET).
    comment_pos = 0
    for i in range(n_fields):
        rec = data[fields_pos + i * FIELD_PACK_LENGTH:fields_pos + (i + 1) * FIELD_PACK_LENGTH]
        col = FrmColumn()
        col.name = names[i].decode("utf8", errors="replace")
        col.length, col.recpos, col.pack_flag = u16(rec, 3), u24(rec, 5), u16(rec, 8)
        col.unireg, col.interval_nr, col.ftype = rec[10], rec[12], rec[13]
        col.charset_id = rec[14] | (rec[11] << 8)
        comment_len = u16(rec, 15)
        if comment_len:
            col.comment = comments[comment_pos:comment_pos + comment_len].decode("utf8", errors="replace")
            comment_pos += comment_len
        if col.interval_nr:
            if col.interval_nr > len(intervals):
                raise FrmError(f"coluna {col.name}: lista de valores inexistente")
            col.labels = [x.decode(_label_charset(charsets, col.charset_id), errors="replace")
                          for x in intervals[col.interval_nr - 1]]
        _classify(col, charsets)
        table.columns.append(col)

    # Passo 7: distribui os bits de NULL e lê o valor padrão de cada coluna.
    _read_null_bits(table)
    for col in table.columns:
        _read_default(col, record, table)

    # Passo 8: lê os índices, o engine e o comentário; por fim confere que o charset da tabela é conhecido.
    table.keys = _read_keys(data, io_size, key_info_len, table)
    table.engine = _read_engine(data, record_pos + table.reclength, legacy_type)
    table.comment = _read_comment(form)
    if not table.charset_id or table.charset_id not in charsets:
        raise FrmError(f"charset da tabela desconhecido ({table.charset_id})")
    return table


# FAZ: escolhe o 'encoding' do Python para decifrar textos de uma coluna conforme o charset do MySQL (latin1 do MySQL
#   é na prática cp1252).
def _label_charset(charsets, charset_id):
    name = charsets.get(charset_id, ("latin1",))[0]
    return {"latin1": "cp1252", "binary": "latin1"}.get(name, name if name in ("utf8", "ascii") else "latin1")


# FAZ: traduz os números do .frm (tipo, flags) para uma 'kind' em palavras (int, varchar, decimal, date, enum...) e
#   preenche nullable, unsigned, zerofill, casas decimais, auto_increment e comportamento do TIMESTAMP. ENUM/SET vêm
#   gravados como STRING com a lista de valores; um flag diz qual dos dois é.
def _classify(col, charsets):
    # Primeiro as propriedades que vêm dos flags: aceita NULL? tem padrão? tem sinal? zerofill? quantas casas
    #   decimais? é AUTO_INCREMENT?
    t, flag = col.ftype, col.pack_flag
    col.nullable = bool(flag & F_MAYBE_NULL)
    col.no_default = bool(flag & F_NO_DEFAULT)
    col.unsigned = not (flag & F_DECIMAL) if t in INT_TYPES or t in (T_FLOAT, T_DOUBLE, T_DECIMAL, T_NEWDECIMAL) else False
    col.zerofill = bool(flag & F_ZEROFILL)
    col.decimals = (flag >> F_DEC_SHIFT) & F_MAX_DEC
    col.auto_increment = (col.unireg & 127) == U_NEXT_NUMBER
    # Depois o 'kind' (a categoria em palavras). ENUM/SET: gravados como STRING com lista de valores; o flag decide
    #   qual.
    if col.interval_nr and t in (T_STRING, T_VAR_STRING, T_ENUM, T_SET, T_VARCHAR):
        # ENUM/SET são guardados como STRING com a lista de valores; o flag diz qual dos dois
        is_set = t == T_SET or (flag & (F_BITFIELD | F_NUMBER)) == F_BITFIELD
        col.kind = "set" if is_set else "enum"
    elif t == T_ENUM:
        col.kind = "enum"
    elif t == T_SET:
        col.kind = "set"
    # Os demais tipos, um a um: inteiros, float, decimal, datas, bit, blob, geometria, varchar (tamanho variável) e
    #   char (tamanho fixo).
    elif t in INT_TYPES:
        col.kind = "int"
    elif t in (T_FLOAT, T_DOUBLE):
        col.kind = "float"
    elif t in (T_DECIMAL, T_NEWDECIMAL):
        col.kind = "decimal"
    elif t in (T_DATE, T_NEWDATE):
        col.kind = "date"
    elif t == T_TIME:
        col.kind = "time"
    elif t == T_DATETIME:
        col.kind = "datetime"
    elif t == T_TIMESTAMP:
        col.kind = "timestamp"
    elif t == T_YEAR:
        col.kind = "year"
    elif t == T_BIT:
        col.kind = "bit"
    elif t in BLOB_KIND:
        col.kind = "blob"
    elif t == T_GEOMETRY:
        col.kind = "geometry"
    elif t in (T_VARCHAR, T_VAR_STRING):
        col.kind = "varchar"
    elif t == T_STRING:
        col.kind = "char"
    else:
        # Tipo desconhecido: recusa em vez de adivinhar.
        raise FrmError(f"coluna {col.name}: tipo {t} desconhecido")
    # Colunas de texto precisam ter um charset conhecido.
    if col.kind in ("char", "varchar", "blob", "enum", "set") and col.charset_id not in charsets:
        raise FrmError(f"coluna {col.name}: charset {col.charset_id} desconhecido")
    # TIMESTAMP: o código extra diz se o padrão é 'agora' e se atualiza sozinho ao alterar a linha.
    ts = (col.unireg & 127)
    if col.kind == "timestamp":
        col.default_now = ts in (U_TIMESTAMP_DN, U_TIMESTAMP_DNUN)
        col.on_update_now = ts in (U_TIMESTAMP_UN, U_TIMESTAMP_DNUN)


# FAZ: quantos bytes a coluna ocupa no 'registro padrão' (necessário para achar o valor default dela). EXEMPLO: int =
#   4; varchar(100) = 101 (1 byte de tamanho + 100).
def pack_length(col):
    """Bytes que a coluna ocupa no registro padrão (Field::pack_length)."""
    k, t = col.kind, col.ftype
    if k == "int":
        return INT_TYPES[t][1]
    if k == "float":
        return 4 if t == T_FLOAT else 8
    if k == "decimal":
        return decimal_size(*decimal_precision(col))
    if k == "date":
        return 3
    if k == "time":
        return 3
    if k == "datetime":
        return 8
    if k == "timestamp":
        return 4
    if k == "year":
        return 1
    if k == "bit":
        return (col.length + 7) // 8
    if k == "enum":
        return 1 if len(col.labels) < 256 else 2
    if k == "set":
        n = (len(col.labels) + 7) // 8
        return n if n <= 4 else 8
    if k == "varchar":
        return col.length + (1 if col.length < 256 else 2)
    if k == "char":
        return col.length
    if k == "blob":
        return BLOB_KIND[t][1] + 8
    return col.length


# FAZ: converte 'largura de exibição + casas decimais' do .frm na (precisão, escala) do DECIMAL. A largura inclui o
#   sinal e o ponto, por isso se desconta.
def decimal_precision(col):
    """(precisão, escala) do DECIMAL a partir da largura de exibição e do flag de sinal (my_decimal_length_to_precision)."""
    scale = col.decimals
    precision = col.length - (1 if scale else 0) - (0 if col.unsigned else 1)
    if precision < 1:
        precision = col.length
    return precision, scale


# FAZ: distribui os bits de NULL: um bit por coluna que aceita NULL, na ordem das colunas. O InnoDB começa no bit 0; o
#   formato de registro fixo, no bit 1.
def _read_null_bits(table):
    """Distribui os bits de NULL: um por coluna que aceita NULL, na ordem das colunas (open_binary_frm)."""
    # O primeiro bit disponível: 0 para tabelas InnoDB (registro 'empacotado'), 1 para o formato de registro fixo.
    bit = 0 if table.options & 1 else 1  # HA_OPTION_PACK_RECORD: o InnoDB começa no bit 0; registro fixo, no 1
    # Cada coluna que aceita NULL ganha o próximo bit: (byte, máscara). Colunas BIT(n) consomem os bits que sobram do
    #   byte.
    for col in table.columns:
        if col.nullable:
            col.null_bit = (bit // 8, 1 << (bit % 8))
            bit += 1
        if col.kind == "bit":
            bit += col.length & 7  # os bits que sobram do BIT(n) moram na área de NULLs


# FAZ: descobre o VALOR PADRÃO (DEFAULT) de uma coluna lendo o registro padrão: se o bit de NULL está ligado, o padrão
#   é NULL; senão decodifica os bytes pelo tipo. Sem padrão, deixa vazio.
def _read_default(col, record, table):
    # Sem valor padrão (NOT NULL sem DEFAULT): nada a ler.
    if col.no_default and not col.default_now and not col.nullable:
        return
    # AUTO_INCREMENT e BLOB/geometria não têm padrão literal.
    if col.auto_increment or col.kind in ("blob", "geometry"):
        return
    # O bit de NULL do registro padrão está ligado: o padrão é NULL.
    if col.nullable and col.null_bit and col.null_bit[0] < len(record) and record[col.null_bit[0]] & col.null_bit[1]:
        col.has_default, col.default = True, None   # DEFAULT NULL
        return
    if col.no_default and not col.nullable:
        return
    # BIT: parte dos bits mora na área de NULLs; sem decodificar com certeza, é melhor não afirmar um padrão.
    if col.kind == "bit":   # parte dos bits mora na área de NULLs: sem decodificar, melhor não afirmar um padrão
        col.default_unknown = True
        return
    # Pega os bytes da coluna no registro padrão (a posição conta a partir de 1) e os decodifica pelo tipo.
    start = col.recpos - 1  # recpos é contado a partir de 1 (open_binary_frm usa default_values - 1)
    raw = record[start:start + pack_length(col)]
    if len(raw) < pack_length(col):
        return
    try:
        value = _decode_default(col, raw, table)
    except (ValueError, IndexError, struct.error, UnicodeDecodeError):
        return
    if value is not None:
        col.has_default, col.default = True, value


# FAZ: decodifica os bytes do valor padrão conforme o tipo da coluna (int, float, decimal, data, hora, char, enum,
#   set...) e devolve o texto.
def _decode_default(col, raw, table):
    # Um ramo por tipo. As datas e horas usam mysql_codec.
    k = col.kind
    if k == "int":
        value = int.from_bytes(raw, "little", signed=not col.unsigned)
        return str(value).zfill(col.length) if col.zerofill and value >= 0 else str(value)
    # float/double: respeita a precisão do MySQL (6 dígitos para float, 15 para double).
    if k == "float":
        value = struct.unpack("<f" if col.ftype == T_FLOAT else "<d", raw)[0]
        if value != value or value in (float("inf"), float("-inf")):
            return None
        text = ("%.6g" if col.ftype == T_FLOAT else "%.15g") % value  # FLT_DIG / DBL_DIG, como o MySQL
        return text.zfill(col.length) if col.zerofill and value >= 0 else text
    if k == "decimal":
        return decode_newdecimal(raw, *decimal_precision(col))
    if k == "date":
        return format_date(int.from_bytes(raw, "little"))
    if k == "time":
        return format_time(int.from_bytes(raw, "little"))
    if k == "datetime":
        return format_datetime(int.from_bytes(raw, "little"))
    if k == "timestamp":
        v = int.from_bytes(raw, "little")
        return None if col.default_now else ("0000-00-00 00:00:00" if v == 0 else None)
    if k == "year":
        return "0000" if raw[0] == 0 else str(1900 + raw[0])
    if k == "char":
        text = raw.rstrip(b" ").decode(_label_charset(table.charsets, col.charset_id), errors="replace")
        return None if col.is_binary_charset else text
    if k == "varchar":
        size = raw[0] if col.length < 256 else u16(raw, 0)
        body = raw[1 if col.length < 256 else 2:][:size]
        return None if col.is_binary_charset else body.decode(_label_charset(table.charsets, col.charset_id), errors="replace")
    # ENUM: o número é a POSIÇÃO do valor na lista (a partir de 1); SET: cada bit ligado é um valor, separados por
    #   vírgula.
    if k == "enum":
        index = int.from_bytes(raw, "little")
        return col.labels[index - 1] if 1 <= index <= len(col.labels) else None
    if k == "set":
        bits = int.from_bytes(raw, "little")
        picked = [label for i, label in enumerate(col.labels) if bits >> i & 1]
        return ",".join(picked)
    return None


# FAZ: lê a informação dos índices (a partir do byte 4096): cabeçalho de 6 bytes, 8 bytes por índice, 9 bytes por
#   parte de índice e, no fim, os nomes. Para cada parte, decide se o índice usa a coluna inteira ou só um PREFIXO
#   (ex.: KEY (nome(20))).
def _read_keys(data, io_size, key_info_len, table):
    """Índices (sql/unireg.cc pack_keys): 6 bytes de cabeçalho, 8 por índice, 9 por parte de índice, depois os nomes."""
    # Passo 1: o bloco dos índices começa depois do cabeçalho (io_size) e tem o tamanho dado.
    block = data[io_size:io_size + key_info_len]
    if len(block) < 6:
        return []
    # Passo 2: quantos índices e quantas partes (o formato muda quando há muitos índices).
    if block[0] & 0x80:
        n_keys, n_parts = (block[1] << 7) | (block[0] & 0x7F), u16(block, 2)
    else:
        n_keys, n_parts = block[0], block[1]
    names_len = u16(block, 4)
    pos = 6
    keys = []
    # Passo 3: para cada índice: flags (único/fulltext), e a lista de partes.
    for _ in range(n_keys):
        if pos + 8 > len(block):
            raise FrmError("informação de índices truncada")
        key = FrmKey()
        key.flags = u16(block, pos) ^ HA_NOSAME
        key.length = u16(block, pos + 2)
        count = block[pos + 4]
        key.algorithm = block[pos + 5]
        pos += 8
        # Cada parte referencia uma coluna (número a partir de 1) e um tamanho.
        for _ in range(count):
            if pos + 9 > len(block):
                raise FrmError("informação de partes de índice truncada")
            field_nr = (u16(block, pos) & 0x3FFF) - 1
            part_length = u16(block, pos + 7)
            pos += 9
            if not 0 <= field_nr < len(table.columns):
                raise FrmError("índice aponta para coluna inexistente")
            col = table.columns[field_nr]
            whole = col.key_bytes()
            # Só é PREFIXO se o tamanho gravado for menor que a coluna inteira (ou se for blob). Em FULLTEXT/SPATIAL o
            #   'tamanho' gravado não é prefixo.
            prefix = part_length if (col.kind == "blob" or (whole is not None and part_length < whole)) else None
            if key.flags & (HA_FULLTEXT | HA_SPATIAL):
                prefix = None   # nessas chaves o "comprimento" gravado não é um prefixo
            key.parts.append((col, prefix))
        keys.append(key)
    # Passo 4: lê os nomes dos índices, no fim do bloco.
    names = split_typelibs(block[pos:pos + names_len], 1)[0]
    if len(names) != len(keys):
        raise FrmError("quantidade de nomes de índice não bate")
    for key, name in zip(keys, names):
        key.name = name.decode("utf8", errors="replace")
    return keys


# FAZ: lê o nome do engine (InnoDB, MyISAM...) gravado depois do registro padrão; se não achar, usa o código antigo do
#   cabeçalho.
def _read_engine(data, extra_pos, legacy_type):
    """Nome do engine gravado depois do registro padrão (connect string, depois o engine)."""
    # O nome do engine vem logo depois do registro padrão (primeiro uma 'connect string', depois o engine, cada um com
    #   2 bytes de tamanho).
    try:
        pos = extra_pos
        connect_len = u16(data, pos)
        pos += 2 + connect_len
        engine_len = u16(data, pos)
        name = data[pos + 2:pos + 2 + engine_len].decode("ascii")
        if name and engine_len < 40:
            return name
    except (IndexError, UnicodeDecodeError):
        pass
    return LEGACY_ENGINES.get(legacy_type, "")


# FAZ: lê o comentário da tabela (COMMENT='...') do 'forminfo'.
def _read_comment(form):
    length = form[46]
    if length == 255:
        return ""
    return form[47:47 + length].decode("utf8", errors="replace")


# ---------------------------------------------------------------------- CREATE TABLE
# FAZ: coloca crases em volta de um nome, dobrando as crases internas, para ser seguro num SQL. EXEMPLO: nome ->
#   `nome`.
def _q(text):
    return "`" + text.replace("`", "``") + "`"


# FAZ: transforma um texto em literal SQL entre aspas simples, escapando barras, aspas e caracteres especiais.
def _lit(text):
    return "'" + text.replace("\\", "\\\\").replace("'", "''").replace("\0", "\\0").replace("\n", "\\n") \
        .replace("\r", "\\r").replace("\x1a", "\\Z") + "'"


# FAZ: devolve o TIPO da coluna em SQL. EXEMPLOS: int(11) unsigned; varchar(100); decimal(10,2); enum('a','b');
#   datetime.
def column_type_sql(col, table):
    k = col.kind
    cs = table.charsets.get(col.charset_id, (None, None, False))
    # Converte o tamanho em BYTES gravado no .frm para tamanho em CARACTERES (divide pelo máximo de bytes por
    #   caractere do charset).
    charset_chars = lambda: max(1, col.length // MBMAXLEN.get(cs[0], 1))  # noqa: E731
    # Um ramo por categoria de tipo; no fim, ' unsigned' e ' zerofill' são acrescentados aos numéricos.
    if k == "int":
        sql = f"{INT_TYPES[col.ftype][0]}({col.length})"
    elif k == "float":
        name = "float" if col.ftype == T_FLOAT else "double"
        sql = name if col.decimals == NOT_FIXED_DEC else f"{name}({col.length},{col.decimals})"
    elif k == "decimal":
        precision, scale = decimal_precision(col)
        sql = f"decimal({precision},{scale})"
    elif k in ("date", "time", "datetime", "timestamp"):
        return k
    elif k == "year":
        return f"year({col.length})"
    elif k == "bit":
        return f"bit({col.length})"
    elif k == "enum" or k == "set":
        return f"{k}(" + ",".join(_lit(x) for x in col.labels) + ")"
    elif k == "char":
        return f"binary({col.length})" if col.is_binary_charset else f"char({charset_chars()})"
    elif k == "varchar":
        return f"varbinary({col.length})" if col.is_binary_charset else f"varchar({charset_chars()})"
    elif k == "blob":
        kind = BLOB_KIND[col.ftype][0]
        return f"{kind}blob" if col.is_binary_charset else f"{kind}text"
    elif k == "geometry":
        return "geometry"
    else:
        raise FrmError(f"tipo {k} sem SQL")
    if col.unsigned:
        sql += " unsigned"
    if col.zerofill:
        sql += " zerofill"
    return sql


# FAZ: escreve a linha INTEIRA de uma coluna no CREATE TABLE: `nome` tipo [CHARACTER SET] [NOT NULL] [AUTO_INCREMENT |
#   DEFAULT ...] [ON UPDATE ...] [COMMENT ...].
def column_sql(col, table):
    # A linha é montada por partes: nome, tipo, charset (só se difere do da tabela), NULL, padrão, ON UPDATE,
    #   comentário.
    parts = [_q(col.name), column_type_sql(col, table)]
    if col.kind in ("char", "varchar", "blob", "enum", "set") and not col.is_binary_charset:
        cs = table.charsets.get(col.charset_id)
        if cs and col.charset_id != table.charset_id:
            parts.append(f"CHARACTER SET {cs[0]}")
            if not cs[2]:
                parts.append(f"COLLATE {cs[1]}")
    parts.append("NULL" if col.nullable and col.kind == "timestamp" else ("" if col.nullable else "NOT NULL"))
    # Padrão: AUTO_INCREMENT; ou 'CURRENT_TIMESTAMP' (TIMESTAMP); ou o valor lido; ou DEFAULT NULL (coluna que aceita
    #   NULL).
    if col.auto_increment:
        parts.append("AUTO_INCREMENT")
    elif col.kind == "timestamp" and col.default_now:
        parts.append("DEFAULT CURRENT_TIMESTAMP")
    elif col.has_default:
        parts.append("DEFAULT NULL" if col.default is None else f"DEFAULT {_lit(col.default)}")
    elif col.nullable and col.kind not in ("blob", "geometry") and not col.default_unknown:
        parts.append("DEFAULT NULL")
    if col.on_update_now:
        parts.append("ON UPDATE CURRENT_TIMESTAMP")
    if col.comment:
        parts.append(f"COMMENT {_lit(col.comment)}")
    return " ".join(p for p in parts if p)


# FAZ: escreve a linha de um índice no CREATE TABLE. EXEMPLOS: PRIMARY KEY (`id`); UNIQUE KEY `uk` (`a`,`b`); KEY `ix`
#   (`nome`(20)).
def key_sql(key, table):
    cols = []
    # Cada parte do índice, com o tamanho do prefixo convertido de bytes para caracteres.
    for col, prefix in key.parts:
        text = _q(col.name)
        if prefix:
            width = prefix
            cs = table.charsets.get(col.charset_id, (None,))
            if col.kind != "blob" or not col.is_binary_charset:
                width = max(1, prefix // MBMAXLEN.get(cs[0], 1)) if not col.is_binary_charset else prefix
            text += f"({width})"
        cols.append(text)
    body = "(" + ",".join(cols) + ")"
    # A PRIMARY KEY não tem nome; os outros índices sim.
    if key.kind == "PRIMARY":
        return f"PRIMARY KEY {body}"
    prefix = {"UNIQUE": "UNIQUE KEY", "FULLTEXT": "FULLTEXT KEY", "SPATIAL": "SPATIAL KEY"}.get(key.kind, "KEY")
    return f"{prefix} {_q(key.name)} {body}"


# FAZ: escreve as opções do fim do CREATE TABLE: ENGINE=..., DEFAULT CHARSET=..., ROW_FORMAT=..., COMMENT=... (só as
#   que existem).
def table_options_sql(table):
    options = []
    if table.engine:
        options.append(f"ENGINE={table.engine}")
    if table.max_rows:
        options.append(f"MAX_ROWS={table.max_rows}")
    if table.min_rows:
        options.append(f"MIN_ROWS={table.min_rows}")
    if table.avg_row_length:
        options.append(f"AVG_ROW_LENGTH={table.avg_row_length}")
    cs = table.charsets.get(table.charset_id)
    if cs:
        options.append(f"DEFAULT CHARSET={cs[0]}")
        if not cs[2]:
            options.append(f"COLLATE={cs[1]}")
    if table.row_type in ROW_FORMATS:
        options.append(f"ROW_FORMAT={ROW_FORMATS[table.row_type]}")
    if table.key_block_size:
        options.append(f"KEY_BLOCK_SIZE={table.key_block_size}")
    if table.comment:
        options.append(f"COMMENT={_lit(table.comment)}")
    return " ".join(options)


# FAZ: monta o CREATE TABLE completo: linhas das colunas + linhas dos índices + opções da tabela.
def frm_to_ddl(table, name=None):
    lines = [column_sql(col, table) for col in table.columns]
    lines += [key_sql(key, table) for key in table.keys]
    return f"CREATE TABLE {_q(name or table.name)} (\n  " + ",\n  ".join(lines) + f"\n) {table_options_sql(table)}"


# FAZ: ATALHO usado pelo motor: recebe o caminho de um .frm e devolve o CREATE TABLE, ou None se o arquivo não puder
#   ser lido com segurança (aí o chamador usa outra fonte).
def read_table_ddl(path, name=None, charsets=None):
    """CREATE TABLE do arquivo .frm, ou None se ele não for legível com segurança (o chamador usa outra fonte)."""
    # Qualquer problema (arquivo ilegível, formato estranho) vira None: o chamador usa outra fonte de estrutura.
    try:
        with open(path, "rb") as f:
            table = parse_frm(f.read(), charsets)
        return frm_to_ddl(table, name or os.path.splitext(os.path.basename(path))[0])
    except (OSError, FrmError, KeyError, IndexError, struct.error, ValueError):
        return None


# ---------------------------------------------------------------------- views e triggers (arquivos de texto)
# FAZ: desfaz as sequências de escape (\n, \r, \t, \0...) dos arquivos de texto de view e trigger.
def _unescape(text):
    out, i = [], 0
    mapping = {"n": "\n", "r": "\r", "t": "\t", "0": "\0", "Z": "\x1a"}
    while i < len(text):
        c = text[i]
        if c == "\\" and i + 1 < len(text):
            out.append(mapping.get(text[i + 1], text[i + 1]))
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


# FAZ: lê o texto de uma VIEW (arquivo 'TYPE=VIEW', com linhas 'chave=valor') e devolve um dicionário (consulta,
#   algoritmo, definer, segurança...).
def parse_view_frm(text):
    """dict com os campos do arquivo `TYPE=VIEW` (query, source, algorithm, definer_user, suid, ...)."""
    if not text.startswith("TYPE=VIEW"):
        raise FrmError("não é um arquivo de view")
    # Cada linha é 'chave=valor' (o primeiro 'TYPE=VIEW' já foi conferido).
    fields = {}
    for line in text.splitlines()[1:]:
        key, sep, value = line.partition("=")
        if sep:
            fields[key] = _unescape(value)
    if not (fields.get("source") or fields.get("view_body_utf8") or fields.get("query")):
        raise FrmError("view sem texto")
    return fields


# FAZ: monta o comando CREATE ... VIEW a partir do dicionário lido: algoritmo, DEFINER, SQL SECURITY, a consulta e, se
#   houver, WITH CHECK OPTION.
def view_to_ddl(name, fields):
    # Traduz os códigos do arquivo: algoritmo (TEMPTABLE/MERGE/UNDEFINED), DEFINER, SQL SECURITY e WITH CHECK OPTION.
    algorithm = {"1": "TEMPTABLE", "2": "MERGE"}.get(fields.get("algorithm", "0"), "UNDEFINED")
    definer = ""
    if fields.get("definer_user"):
        definer = f" DEFINER={_q(fields['definer_user'])}@{_q(fields.get('definer_host', ''))}"
    security = "INVOKER" if fields.get("suid") == "0" else "DEFINER"
    body = (fields.get("source") or fields.get("view_body_utf8") or fields.get("query")).strip().rstrip(";")
    check = {"1": " WITH LOCAL CHECK OPTION", "2": " WITH CASCADED CHECK OPTION"}.get(fields.get("with_check_option"), "")
    return f"CREATE ALGORITHM={algorithm}{definer} SQL SECURITY {security} VIEW {_q(name)} AS {body}{check}"


# FAZ: lê um arquivo .TRG e devolve a lista de comandos CREATE TRIGGER (estão na linha 'triggers=' entre aspas
#   simples, um após o outro).
def parse_trigger_file(text):
    """[CREATE TRIGGER ...] de um arquivo .TRG (`triggers='...' '...'`)."""
    if not text.startswith("TYPE=TRIGGERS"):
        raise FrmError("não é um arquivo de triggers")
    match = re.search(r"^triggers=(.*)$", text, re.M)
    if not match:
        return []
    # Percorre a linha 'triggers=' procurando textos entre aspas simples (um por trigger), respeitando as barras de
    #   escape.
    statements, line, i = [], match.group(1), 0
    while i < len(line):
        if line[i] != "'":
            i += 1
            continue
        j, chunk = i + 1, []
        while j < len(line) and line[j] != "'":
            if line[j] == "\\" and j + 1 < len(line):
                chunk.append(line[j:j + 2])
                j += 2
            else:
                chunk.append(line[j])
                j += 1
        statements.append(_unescape("".join(chunk)))
        i = j + 1
    return statements


# FAZ: tira a cláusula DEFINER=``@`` (conta vazia, de um servidor sem tabelas de permissão), que não existe no banco
#   de destino e faria o comando falhar.
def strip_empty_definer(statement):
    """DEFINER=``@`` (conta vazia, de servidor sem grant tables) não existe no destino: tira a cláusula."""
    return re.sub(r"\s+DEFINER=``@``", "", statement, count=1)
