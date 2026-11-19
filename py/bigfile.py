"""
bigfile.py - abre arquivos de mais de 2 GB no Pyodide.

O Python do navegador (WebAssembly de 32 bits) converte toda posição de seek/pread para um `long` de 32 bits: a partir de 2 GB
dá OverflowError (ou posição negativa na leitura sequencial). Um ibdata1 de 8 GB é comum. Aqui a leitura passa pelo sistema
de arquivos do próprio Pyodide (FS.read, em JavaScript), que usa números de 64 bits. O resto do programa só vê um objeto
com seek/read/close, igual a um arquivo comum.
"""
import os
import sys

LIMIT = 2 ** 31 - 2 ** 22      # a partir daqui o arquivo comum não é seguro no WebAssembly
READ_AHEAD = 1 << 20           # leitura sequencial: lê 1 MB de uma vez


class BigFile:
    """Arquivo somente-leitura com posições de 64 bits. Só existe dentro do Pyodide."""

    def __init__(self, path):
        import js
        import pyodide_js
        self._js, self._fs = js, pyodide_js.FS
        self._stream = self._fs.open(path, "r")
        self.size = int(self._fs.stat(path).size)
        self.pos, self._chunk, self._chunk_at, self._last_end = 0, b"", 0, -1

    def seek(self, offset, whence=0):
        self.pos = offset if whence == 0 else self.pos + offset if whence == 1 else self.size + offset
        if self.pos < 0:
            raise OSError("posição negativa")
        return self.pos

    def tell(self):
        return self.pos

    def _raw(self, at, count):
        buffer = self._js.Uint8Array.new(count)
        got = int(self._fs.read(self._stream, buffer, 0, count, at))
        data = buffer.to_bytes()
        return data if got == count else data[:got]

    def read(self, count=-1):
        if count is None or count < 0:
            count = self.size - self.pos
        count = min(count, self.size - self.pos)
        if count <= 0:
            return b""
        end = self.pos + count
        if not (self._chunk_at <= self.pos and end <= self._chunk_at + len(self._chunk)):
            # Leitura de um ponto qualquer: só o pedido. Em sequência (varredura do arquivo): adianta 1 MB.
            ahead = READ_AHEAD if self.pos == self._last_end else 0
            want = min(max(count, ahead), self.size - self.pos)
            self._chunk, self._chunk_at = self._raw(self.pos, want), self.pos
        start = self.pos - self._chunk_at
        data = self._chunk[start:start + count]
        self.pos, self._last_end = end, end
        return data

    def close(self):
        if self._stream is not None:
            self._fs.close(self._stream)
            self._stream = None
        self._chunk = b""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def open_rb(path):
    """open(path, "rb"), ou um BigFile se for o Pyodide: lá ele serve para os arquivos de mais de 2 GB e, nos menores, para ler 1 MB de uma vez
    na leitura em sequência (no navegador cada leitura de 16 KB do disco é uma chamada lenta; o ibdata1 é varrido página por página)."""
    if sys.platform == "emscripten" or os.path.getsize(path) >= LIMIT:
        try:
            return BigFile(path)
        except ImportError:   # Python comum (64 bits): o arquivo normal serve
            pass
    return open(path, "rb")
