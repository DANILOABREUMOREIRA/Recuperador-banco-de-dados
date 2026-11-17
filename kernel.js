// kernel.js - carrega o kernel WebAssembly (kernel.wasm) e o expõe ao Python do Pyodide.
//
// O kernel faz, por página de 16 KB, o que o Python fazia registro por registro: percorre os registros, separa os campos,
// decodifica os valores e escreve o texto SQL de cada linha (veja dev/wasm/assembly/kernel.ts).
// Uso (worker.js e testes no Node): const kernel = await loadKernel(bytesDoWasm); o Python recebe esse objeto em
// kernel_bridge.set_kernel(kernel) e chama kernel.setSchema(bytes) e kernel.convert(pagina, modo).
(function (root) {
  const PAGE_SIZE = 16384;

  root.loadKernel = async function loadKernel(wasmBytes) {
    const { instance } = await WebAssembly.instantiate(wasmBytes, { env: { abort() { throw new Error("o kernel abortou"); } } });
    const x = instance.exports;
    x.init();
    const mem = () => x.memory.buffer;            // pode trocar de buffer se a memória crescer: sempre pegar de novo
    const pagePtr = x.pagePtr(), schemaPtr = x.schemaPtr(), outPtr = x.outPtr(), specPtr = x.specPtr(), metaPtr = x.metaPtr(), hdrPtr = x.hdrPtr();

    return {
      // O Python entrega os bytes (little-endian, inteiros de 32 bits) da descrição das colunas da tabela.
      setSchema(bytesProxy) {
        const buf = bytesProxy.getBuffer();
        try { new Uint8Array(mem(), schemaPtr, buf.data.length).set(buf.data); } finally { buf.release(); }
      },

      // Converte uma página (bytes de 16 KB) e devolve um Uint8Array: cabeçalho (8 inteiros), registros especiais (1 inteiro
      // cada), metadados (9 inteiros por registro, só no modo 1) e os bytes de texto.
      convert(pageProxy, mode) {
        const buf = pageProxy.getBuffer();
        try {
          if (buf.data.length !== PAGE_SIZE) throw new Error("página com tamanho errado: " + buf.data.length);
          new Uint8Array(mem(), pagePtr, PAGE_SIZE).set(buf.data);
        } finally { buf.release(); }
        x.convertPage(mode);
        const m = mem();
        const header = new Int32Array(m, hdrPtr, 8);
        const nSpecial = header[4], nMeta = header[5], outLen = header[6];
        const pack = new Uint8Array(32 + 4 * nSpecial + 36 * nMeta + outLen);
        pack.set(new Uint8Array(m, hdrPtr, 32), 0);
        let at = 32;
        pack.set(new Uint8Array(m, specPtr, 4 * nSpecial), at); at += 4 * nSpecial;
        pack.set(new Uint8Array(m, metaPtr, 36 * nMeta), at); at += 36 * nMeta;
        pack.set(new Uint8Array(m, outPtr, outLen), at);
        return pack;
      },
    };
  };
})(typeof self !== "undefined" ? self : globalThis);
