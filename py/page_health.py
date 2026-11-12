"""
page_health.py - Saúde das páginas InnoDB lida direto do arquivo, sem o mysqld.

Segue o que o MySQL 5.5 confere ao ler uma página (buf_page_is_corrupted, buf0buf.c) e o que o innochecksum
relata: página zerada, gravação interrompida (LSN do cabeçalho diferente do LSN do rodapé), número de página que
não bate com a posição, estrutura da página de índice destruída e checksum errado. Também lê o doublewrite buffer
do tablespace do sistema (trx0sys.h): as cópias de páginas gravadas por último, que o próprio InnoDB usa para
refazer páginas rasgadas por queda de energia.

O cálculo do checksum é em Python puro e custa alguns milissegundos por página; por isso scan_health confere o
checksum só de uma amostra (e de todas as páginas suspeitas), e informa quantas páginas conferiu.
"""

# VISÃO GERAL: o InnoDB divide seus arquivos em PÁGINAS de 16 KB. Este arquivo faz um 'exame de saúde' dessas páginas
#   direto do arquivo, sem precisar do MySQL, imitando o que a ferramenta oficial innochecksum faz. Procura quatro
#   tipos de problema: página ZERADA (em branco), gravação INTERROMPIDA (o número LSN no começo da página difere do do
#   fim: a página foi 'rasgada' no meio de uma gravação), NÚMERO de página errado (a página gravada não é a que
#   deveria estar naquela posição) e CHECKSUM errado (o conteúdo mudou por defeito). Também lê o 'doublewrite buffer',
#   uma cópia de segurança que o InnoDB guarda das últimas páginas gravadas, para refazer páginas rasgadas. Calcular
#   checksum em Python é lento (~6 ms por página), então scan_health confere uma AMOSTRA dentro de um limite de tempo
#   e informa quantas conferiu.

# Importações: 'os' = tamanho de arquivos; 'struct' = ler números dentro de bytes; 'time' = controlar o tempo gasto.
import os
import struct
import time

# PAGE_SIZE e as posições FIL_PAGE_*: o InnoDB 5.5 usa páginas de 16.384 bytes. Cada constante FIL_PAGE_X é a POSIÇÃO
#   (em bytes, a partir do começo da página) de um campo do cabeçalho: checksum (0), número da página (4), LSN (16),
#   tipo (24), id do espaço (34)... FIL_PAGE_INDEX = 17855 = 'página de índice'.
PAGE_SIZE = 16384
FIL_PAGE_SPACE_OR_CHKSUM = 0
FIL_PAGE_OFFSET = 4
FIL_PAGE_LSN = 16
FIL_PAGE_FILE_FLUSH_LSN = 26
FIL_PAGE_DATA = 38
FIL_PAGE_SPACE_ID = 34
FIL_PAGE_TYPE = 24
FIL_PAGE_END_LSN_OLD_CHKSUM = 8
FIL_PAGE_INDEX = 17855
# Constantes do cálculo do checksum (copiadas do código-fonte do MySQL 5.5): um valor mágico para 'checksum
#   desligado', duas máscaras usadas na mistura dos bytes e MASK32 para manter tudo em 32 bits.
BUF_NO_CHECKSUM_MAGIC = 0xDEADBEEF
UT_HASH_RANDOM_MASK = 1463735687
UT_HASH_RANDOM_MASK2 = 1653893711
MASK32 = 0xFFFFFFFF

# PAGE_TYPE_NAMES: nome legível de cada tipo de página, para o relatório (ex.: 17855 = 'índice').
PAGE_TYPE_NAMES = {0: "livre", 2: "undo", 3: "inode", 4: "ibuf livre", 5: "ibuf bitmap", 6: "sistema", 7: "trx sys",
                   8: "cabeçalho do arquivo", 9: "extent", 10: "blob", 11: "blob compactado", 12: "blob compactado 2",
                   17855: "índice"}

# trx0sys.h: o doublewrite fica na página 5 do tablespace do sistema, a 200 bytes do fim.
# DOUBLEWRITE: a página 5 do ibdata1 (TRX_SYS) guarda, a 200 bytes do fim, onde estão os dois blocos do doublewrite
#   buffer. Cada bloco tem 64 páginas.
TRX_SYS_PAGE_NO = 5
TRX_SYS_DOUBLEWRITE = PAGE_SIZE - 200
DOUBLEWRITE_MAGIC = 536853855
DOUBLEWRITE_BLOCK_PAGES = 64

# LIMITES: tempo máximo (12 s) gasto calculando checksums; custo estimado de uma página; e quantas páginas ruins
#   listar no relatório.
CHECKSUM_SAMPLE_BUDGET_S = 12.0   # tempo que scan_health gasta, no máximo, calculando checksums
CHECKSUM_SECONDS_PER_PAGE = 0.006   # custo do checksum em Python puro (ordem de grandeza)
MAX_LISTED_PAGES = 50


# FAZ: a função de 'mistura' (hash) que o InnoDB usa para o checksum: percorre os bytes e vai combinando-os num número
#   de 32 bits. É uma tradução fiel da função ut_fold_binary do MySQL 5.5; qualquer diferença daria checksums errados.
def fold_binary(data):
    """ut_fold_binary (ut0rnd.c): só os 32 bits baixos importam, e o resultado final é cortado em 32 bits."""
    # Começa de zero e, para cada byte, mistura-o com o valor acumulado (XOR, deslocamento de 8 bits, soma) mantendo
    #   32 bits.
    fold = 0
    for byte in data:
        fold = (((((fold ^ byte ^ UT_HASH_RANDOM_MASK2) << 8) + fold) & MASK32) ^ UT_HASH_RANDOM_MASK) + byte
        fold &= MASK32
    return fold


# FAZ: calcula o checksum NOVO da página (o gravado no começo): mistura dois trechos (bytes 4..26 e 38..16376),
#   pulando o próprio campo de checksum e o rodapé.
def new_checksum(page):
    """buf_calc_page_new_checksum: dois trechos da página, sem o campo de checksum nem o rodapé."""
    return (fold_binary(page[FIL_PAGE_OFFSET:FIL_PAGE_FILE_FLUSH_LSN]) +
            fold_binary(page[FIL_PAGE_DATA:PAGE_SIZE - FIL_PAGE_END_LSN_OLD_CHKSUM])) & MASK32


# FAZ: calcula o checksum ANTIGO (o gravado no rodapé): mistura só o começo da página (os 26 primeiros bytes).
def old_checksum(page):
    """buf_calc_page_old_checksum: só o cabeçalho, até o FIL_PAGE_FILE_FLUSH_LSN."""
    return fold_binary(page[:FIL_PAGE_FILE_FLUSH_LSN]) & MASK32


# FAZ: True se a página é só zeros (nunca usada ou zerada pelo chkdsk).
def is_blank(page):
    return not page.strip(b"\0")


# FAZ: True se os 4 últimos bytes do LSN do cabeçalho diferem dos do rodapé: sinal de que a gravação da página foi
#   interrompida no meio (página 'rasgada').
def lsn_torn(page):
    """True se os 4 bytes baixos do LSN do cabeçalho diferem dos do rodapé: a gravação da página foi interrompida."""
    return page[FIL_PAGE_LSN + 4:FIL_PAGE_LSN + 8] != page[PAGE_SIZE - 4:]


# FAZ: confere os DOIS checksums gravados (começo e fim) com os recalculados. Aceita também o valor mágico de quem
#   gravou com checksum desligado (innodb_checksums=0).
def checksum_ok(page):
    """Confere o checksum gravado (campo do cabeçalho e campo do rodapé) com o recalculado, como o MySQL 5.5.
    Aceita o valor mágico de quem gravou com innodb_checksums desligado."""
    stored_new = struct.unpack_from(">I", page, FIL_PAGE_SPACE_OR_CHKSUM)[0]
    stored_old = struct.unpack_from(">I", page, PAGE_SIZE - FIL_PAGE_END_LSN_OLD_CHKSUM)[0]
    if stored_new == BUF_NO_CHECKSUM_MAGIC and stored_old == BUF_NO_CHECKSUM_MAGIC:
        return True
    return stored_new == new_checksum(page) and stored_old == old_checksum(page)


# FAZ: acha problemas sem calcular checksum (barato): devolve 'zerada', 'interrompida', 'numero' (a página gravada ali
#   tem outro número) ou None se está tudo ok.
def quick_problem(page, page_no):
    """Problema visível sem calcular checksum, ou None. Retorna um código curto:
    'zerada', 'interrompida', 'numero' ou 'estrutura:<motivo>' (só páginas de índice)."""
    # Ordem das verificações: zerada -> interrompida -> número errado.
    if is_blank(page):
        return "zerada"
    if lsn_torn(page):
        return "interrompida"
    if struct.unpack_from(">I", page, FIL_PAGE_OFFSET)[0] != page_no:
        return "numero"
    return None


# FAZ: guarda o resultado do exame de UM arquivo: quantas páginas, quantas zeradas, quais interrompidas, quais com
#   número errado, quais de índice com estrutura destruída e quais com checksum errado.
class FileHealth:
    """Resultado de scan_health para um arquivo de tablespace."""

    # FAZ: cria o resultado vazio (contadores zerados e listas vazias) para o arquivo path com n_pages páginas.
    def __init__(self, path, n_pages):
        self.path = path
        self.n_pages = n_pages
        self.type_counts = {}            # tipo -> páginas
        self.blank = 0
        self.doublewrite = 0             # páginas do doublewrite buffer (cópias; o número gravado não é o da posição)
        self.torn = []                   # páginas com gravação interrompida
        self.wrong_number = []           # número gravado diferente da posição
        self.bad_structure = {}          # página de índice -> motivo
        self.checksum_checked = 0
        self.checksum_bad = []           # páginas com checksum errado (entre as conferidas)
        self.index_pages = {}            # index_id -> {"leaf": n, "total": n, "bad": n}
        self.seconds = 0.0

    # FAZ: total de páginas com algum problema (soma das listas de interrompidas, número errado, estrutura ruim e
    #   checksum errado).
    @property
    def damaged_total(self):
        return len(self.torn) + len(self.wrong_number) + len(self.bad_structure) + len(self.checksum_bad)

    # FAZ: resumo pequeno (só números e as primeiras páginas ruins) em formato simples, para o relatório HTML e o
    #   job_status.
    def summary(self):
        """Dicionário pequeno e serializável para o relatório."""
        return {
            "arquivo": os.path.basename(self.path), "paginas": self.n_pages, "zeradas": self.blank,
            "doublewrite": self.doublewrite, "interrompidas": len(self.torn), "numero_errado": len(self.wrong_number),
            "estrutura_ruim": len(self.bad_structure), "checksum_conferidas": self.checksum_checked,
            "checksum_ruins": len(self.checksum_bad),
            "tipos": {PAGE_TYPE_NAMES.get(t, f"tipo {t}"): n for t, n in sorted(self.type_counts.items())},
            "paginas_ruins": sorted(set(self.torn) | set(self.wrong_number) | set(self.bad_structure)
                                    | set(self.checksum_bad))[:MAX_LISTED_PAGES],
        }


# FAZ: examina o arquivo inteiro, página por página, com as verificações baratas, e confere o checksum de uma AMOSTRA
#   (todas as páginas suspeitas + 1 a cada N das demais, dentro do orçamento de tempo). structure_check é uma função
#   de fora que diz se uma página de índice tem a estrutura destruída.
def scan_health(path, structure_check=None, sample_budget_s=CHECKSUM_SAMPLE_BUDGET_S):
    """Varre o arquivo inteiro com as verificações baratas e confere o checksum de uma amostra.
    structure_check(page) -> motivo|None é o verificador de estrutura de página de índice (innodb_carver)."""
    # Passo 1: marca a hora de início e cria o resultado.
    started = time.time()
    n_pages = os.path.getsize(path) // PAGE_SIZE
    health = FileHealth(path, n_pages)
    # Passo 2: decide quantas páginas pular entre uma conferência de checksum e outra: se o arquivo é grande, confere
    #   1 a cada N para caber no tempo.
    sample_every = 1
    # Se o arquivo é grande, o checksum de todas as páginas estoura o orçamento: confere 1 a cada N.
    if n_pages * CHECKSUM_SECONDS_PER_PAGE > sample_budget_s:
        sample_every = int(n_pages * CHECKSUM_SECONDS_PER_PAGE / sample_budget_s) + 1
    deadline = started + sample_budget_s * 2
    # Passo 3: abre o arquivo e descobre quais páginas pertencem ao doublewrite (para não confundi-las com páginas
    #   normais).
    with open(path, "rb") as f:
        dblwr = doublewrite_page_numbers(f, n_pages)
        f.seek(0)
        # Passo 4: lê uma página por vez.
        for page_no in range(n_pages):
            page = f.read(PAGE_SIZE)
            if len(page) < PAGE_SIZE:
                break
            # Verificação barata (sem checksum).
            problem = quick_problem(page, page_no)
            if problem == "zerada":
                health.blank += 1
                continue
            if page_no in dblwr:
                health.doublewrite += 1
                continue
            # Conta quantas páginas existem de cada tipo.
            page_type = struct.unpack_from(">H", page, FIL_PAGE_TYPE)[0]
            health.type_counts[page_type] = health.type_counts.get(page_type, 0) + 1
            if problem == "interrompida":
                health.torn.append(page_no)
            elif problem == "numero":
                health.wrong_number.append(page_no)
            bad_structure = None
            # Página de índice íntegra no cabeçalho: confere se a estrutura interna está coerente (a que derrubaria o
            #   mysqld).
            if page_type == FIL_PAGE_INDEX and problem is None and structure_check:
                bad_structure = structure_check(page)
                if bad_structure:
                    health.bad_structure[page_no] = bad_structure
            # Registra a que índice (index_id) a página pertence e se é folha (nível 0).
            if page_type == FIL_PAGE_INDEX and problem is None:
                index_id = struct.unpack_from(">Q", page, FIL_PAGE_DATA + 28)[0]
                level = struct.unpack_from(">H", page, FIL_PAGE_DATA + 26)[0]
                info = health.index_pages.setdefault(index_id, {"leaf": 0, "total": 0, "bad": 0})
                info["total"] += 1
                info["leaf"] += level == 0
                info["bad"] += bad_structure is not None
            # Passo 5: confere o checksum de toda página suspeita e, das demais, só as da amostra.
            suspicious = problem is not None or bad_structure is not None
            if suspicious or (page_no % sample_every == 0 and time.time() < deadline):
                health.checksum_checked += 1
                if not checksum_ok(page):
                    health.checksum_bad.append(page_no)
    # Passo 6: anota o tempo gasto e devolve o resultado.
    health.seconds = time.time() - started
    return health


# ---------------------------------------------------------------------- doublewrite buffer
# FAZ: lê a página 5 e devolve os números das primeiras páginas dos dois blocos do doublewrite, ou None se a página
#   não tem o cabeçalho do doublewrite (arquivo que não é o do sistema, ou página destruída).
def read_doublewrite_blocks(sys_page):
    """(bloco1, bloco2) = números da primeira página de cada bloco do doublewrite, ou None se a página 5 não tem o
    cabeçalho do doublewrite (tablespace que não é o do sistema, ou página destruída)."""
    if len(sys_page) < PAGE_SIZE:
        return None
    base = TRX_SYS_DOUBLEWRITE
    magic = struct.unpack_from(">I", sys_page, base + 10)[0]
    if magic != DOUBLEWRITE_MAGIC:
        return None
    block1 = struct.unpack_from(">I", sys_page, base + 14)[0]
    block2 = struct.unpack_from(">I", sys_page, base + 18)[0]
    return block1, block2


# FAZ: lista os números de todas as páginas ocupadas pelo doublewrite buffer no arquivo (os 2 blocos de 64 páginas),
#   ou lista vazia se não houver.
def doublewrite_slots(f, n_pages):
    """Números das páginas ocupadas pelo doublewrite buffer no arquivo aberto f (nenhuma se não for o do sistema),
    na ordem em que ficam no arquivo."""
    if n_pages <= TRX_SYS_PAGE_NO:
        return []
    f.seek(TRX_SYS_PAGE_NO * PAGE_SIZE)
    blocks = read_doublewrite_blocks(f.read(PAGE_SIZE))
    if not blocks:
        return []
    return [first + i for first in blocks for i in range(DOUBLEWRITE_BLOCK_PAGES) if first + i < n_pages]


# FAZ: o mesmo, como conjunto (para testar 'esta página é do doublewrite?').
# POR QUÊ: as páginas do doublewrite guardam o número da página de ORIGEM, não o da posição onde estão, e não devem
#   ser contadas como 'número errado'.
def doublewrite_page_numbers(f, n_pages):
    """Conjunto de doublewrite_slots; as cópias lá guardadas têm o número da página de origem, não o da posição."""
    return set(doublewrite_slots(f, n_pages))


# FAZ: devolve {(espaço, página): bytes} com as cópias ÍNTEGRAS (não rasgadas e com checksum correto) guardadas no
#   doublewrite. Se há duas cópias da mesma página, vale a de LSN maior (a mais recente).
def doublewrite_copies(path):
    """{(space_id, page_no): página} das cópias íntegras guardadas no doublewrite buffer do tablespace do sistema.
    Só entram cópias sem gravação interrompida e com checksum correto."""
    # Passo 1: junta as cópias íntegras, indexadas por (id do espaço, número da página).
    found = {}
    try:
        n_pages = os.path.getsize(path) // PAGE_SIZE
        with open(path, "rb") as f:
            # Passo 2: lê cada página do doublewrite, descarta as em branco, rasgadas ou com checksum ruim.
            for slot in doublewrite_slots(f, n_pages):
                f.seek(slot * PAGE_SIZE)
                page = f.read(PAGE_SIZE)
                if len(page) < PAGE_SIZE or is_blank(page) or lsn_torn(page) or not checksum_ok(page):
                    continue
                key = (struct.unpack_from(">I", page, FIL_PAGE_SPACE_ID)[0],
                       struct.unpack_from(">I", page, FIL_PAGE_OFFSET)[0])
                # Se já há uma cópia da mesma página, fica com a de LSN maior.
                old = found.get(key)
                if old is None or page[FIL_PAGE_LSN:FIL_PAGE_LSN + 8] > old[FIL_PAGE_LSN:FIL_PAGE_LSN + 8]:
                    found[key] = page
    except OSError:
        return {}
    return found


# FAZ: decide quais páginas do arquivo podem ser REFEITAS com uma cópia do doublewrite: as que estão zeradas, rasgadas
#   ou com checksum errado e que têm uma cópia boa. Devolve {página: bytes da cópia}. NÃO escreve nada no arquivo;
#   quem chama usa a cópia só na leitura (sobreposição).
def torn_page_repairs(path, space_id, copies):
    """{página: cópia} das páginas do arquivo que estão zeradas/interrompidas/com checksum errado e para as quais o
    doublewrite tem uma cópia íntegra (com o mesmo número). Não escreve nada: quem chama aplica como sobreposição."""
    # Passo 1: só interessam cópias do espaço (arquivo) que está sendo examinado.
    repairs = {}
    wanted = {page_no: page for (space, page_no), page in copies.items() if space == space_id}
    if not wanted:
        return repairs
    # Passo 2: para cada cópia, lê a página atual do arquivo; se está ruim (problema rápido ou checksum errado), a
    #   cópia vira a correção.
    with open(path, "rb") as f:
        n_pages = os.path.getsize(path) // PAGE_SIZE
        for page_no, copy in sorted(wanted.items()):
            if page_no >= n_pages:
                continue
            f.seek(page_no * PAGE_SIZE)
            current = f.read(PAGE_SIZE)
            if len(current) < PAGE_SIZE:
                continue
            if quick_problem(current, page_no) is not None or not checksum_ok(current):
                repairs[page_no] = copy
    return repairs
