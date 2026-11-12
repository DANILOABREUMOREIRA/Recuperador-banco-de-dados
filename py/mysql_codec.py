"""
mysql_codec.py - Formatos de valor do MySQL 5.x que aparecem tanto nos registros do InnoDB (innodb_carver) quanto no
registro padrão do .frm (frm_parser): DECIMAL binário e os inteiros empacotados de DATE, TIME e DATETIME.

As funções recebem o número já lido (a ordem dos bytes e o bit de sinal invertido dependem de quem lê) e devolvem o texto
SQL. Erros de dado levantam ValueError.
"""

# VISÃO GERAL: o MySQL não guarda datas e números decimais como texto; guarda como números 'empacotados' em poucos
#   bytes. Este arquivo sabe desempacotar esses formatos e devolver o texto SQL certo (ex.: '2024-01-10'). É usado por
#   dois leitores diferentes do projeto: frm_parser.py (valores padrão das colunas, lidos do .frm) e innodb_carver.py
#   (linhas lidas das páginas do InnoDB). Por isso fica num arquivo só: antes havia duas cópias iguais. Leitura fácil:
#   nenhuma função aqui depende de arquivo, rede ou servidor; todas recebem um número e devolvem um texto.

# DIG2BYTES: tabela de conversão do formato DECIMAL. O MySQL guarda os dígitos em grupos de 9 (4 bytes cada); o que
#   sobra (0 a 8 dígitos) usa menos bytes. Índice = quantos dígitos sobraram; valor = quantos bytes isso ocupa.
#   EXEMPLO: sobraram 7 dígitos -> DIG2BYTES[7] = 4 bytes.
DIG2BYTES = (0, 1, 1, 2, 2, 3, 3, 4, 4, 4)   # bytes para guardar 0..9 dígitos decimais restantes


# FAZ: calcula quantos bytes um DECIMAL(precisão, escala) ocupa no formato binário do MySQL.
# EXEMPLO: DECIMAL(10,2) tem 8 dígitos inteiros + 2 decimais -> ocupa 5 bytes. DECIMAL(40,15) ocupa 19.
def decimal_size(precision, scale):
    """Bytes que um DECIMAL(precision, scale) ocupa no formato binário (decimal.c decimal_bin_size)."""
    # Passo 1: separa a parte inteira e a decimal em grupos de 9 dígitos (divmod devolve quociente e resto): 'intg0' =
    #   grupos completos, 'intg0x' = dígitos que sobraram.
    intg0, intg0x = divmod(precision - scale, 9)
    frac0, frac0x = divmod(scale, 9)
    # Passo 2: cada grupo completo ocupa 4 bytes; os dígitos que sobraram ocupam o que a tabela DIG2BYTES diz.
    return intg0 * 4 + DIG2BYTES[intg0x] + frac0 * 4 + DIG2BYTES[frac0x]


# FAZ: converte os bytes de um DECIMAL em texto, sem perder nenhum dígito (por isso não usa float). Recebe os bytes, a
#   precisão (total de dígitos) e a escala (dígitos depois da vírgula).
# EXEMPLO: bytes 80 00 04 D2 38 com DECIMAL(10,2) -> '1234.56'; o mesmo número negativo -> '-1234.56'.
# POR QUÊ: o formato inverte o bit de sinal e, nos negativos, inverte todos os bits; o código desfaz isso antes de ler
#   os grupos.
def decode_newdecimal(data, precision, scale):
    """DECIMAL binário (decimal.c bin2decimal) -> texto, sem perder dígito."""
    # Passo 1: mesma divisão em grupos de 9 dígitos feita em decimal_size.
    intg0, intg0x = divmod(precision - scale, 9)
    frac0, frac0x = divmod(scale, 9)
    size = decimal_size(precision, scale)
    # Passo 2: pega só os bytes que pertencem ao número; se vierem menos que o esperado, o dado está truncado (erro).
    buf = bytearray(data[:size])
    if len(buf) < size:
        raise ValueError("DECIMAL truncado")
    # Passo 3: o primeiro bit é o sinal (invertido de propósito pelo MySQL, para os números ordenarem bem). Desfaz a
    #   inversão e, se for negativo, inverte todos os bits.
    negative = not (buf[0] & 0x80)
    buf[0] ^= 0x80
    if negative:
        buf = bytearray(b ^ 0xFF for b in buf)
    pos = 0

    # Auxiliar: lê os próximos n bytes como um número e avança a posição (pos) para o próximo grupo.
    def take(n):
        nonlocal pos
        chunk = int.from_bytes(buf[pos:pos + n], "big")
        pos += n
        return chunk

    # Passo 4: monta os dígitos inteiros (primeiro o grupo incompleto, depois os grupos de 9).
    int_digits = str(take(DIG2BYTES[intg0x])) if intg0x else ""
    for _ in range(intg0):
        int_digits += f"{take(4):09d}"
    # Passo 5: monta os dígitos depois da vírgula, preenchendo com zeros à esquerda quando preciso (:09d = 9 dígitos).
    frac_digits = "".join(f"{take(4):09d}" for _ in range(frac0))
    if frac0x:
        frac_digits += f"{take(DIG2BYTES[frac0x]):0{frac0x}d}"
    # Passo 6: tira zeros à esquerda, junta com a parte decimal e põe o '-' se negativo (e não for zero).
    text = (int_digits.lstrip("0") or "0") + (f".{frac_digits}" if scale else "")
    return f"-{text}" if negative and text.strip("0.") else text


# FAZ: transforma o número empacotado de um DATE em 'AAAA-MM-DD'. O MySQL guarda ano*512 + mês*32 + dia.
# EXEMPLO: 2020*512 + 3*32 + 9 -> '2020-03-09'.
def format_date(packed):
    """DATE (Field_newdate): ano*512 + mês*32 + dia."""
    return f"{packed >> 9:04d}-{(packed >> 5) & 15:02d}-{packed & 31:02d}"


# FAZ: transforma o número de um TIME antigo (3 bytes, HHMMSS com sinal) em '[-]HH:MM:SS'.
# EXEMPLO: 123456 -> '12:34:56'. Valores acima de 2^23 são negativos (complemento de 2 em 24 bits).
def format_time(packed):
    """TIME antigo (Field_time): HHMMSS em 3 bytes, com sinal."""
    # O bit mais alto de 24 bits indica negativo: subtrai 2^24 para obter o valor com sinal.
    if packed >= 1 << 23:
        packed -= 1 << 24
    sign, packed = ("-" if packed < 0 else ""), abs(packed)
    return f"{sign}{packed // 10000:02d}:{(packed // 100) % 100:02d}:{packed % 100:02d}"


# FAZ: transforma o número de um DATETIME antigo (formato AAAAMMDDhhmmss) em 'AAAA-MM-DD hh:mm:ss'.
# EXEMPLO: 20181231235959 -> '2018-12-31 23:59:59'. Zero ou negativo vira a data zero '0000-00-00 00:00:00' (o MySQL
#   aceita essa 'data inválida').
def format_datetime(number):
    """DATETIME antigo: o inteiro AAAAMMDDhhmmss; zero ou negativo é a data zero."""
    if number <= 0:
        return "0000-00-00 00:00:00"
    date, clock = divmod(number, 1000000)
    return (f"{date // 10000:04d}-{(date // 100) % 100:02d}-{date % 100:02d} "
            f"{clock // 10000:02d}:{(clock // 100) % 100:02d}:{clock % 100:02d}")
