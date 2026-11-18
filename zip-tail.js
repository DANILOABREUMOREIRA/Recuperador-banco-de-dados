// zip-tail.js - Deixa um .zip que vem DEPOIS de um texto (o .bat que se extrai sozinho) legível pelo tar do Windows.
//
// PROBLEMA: um .zip guarda a posição de cada arquivo contando do início DELE. Com um texto na frente, todas as posições ficam
//   erradas pelo tamanho do texto. O tar (libarchive) corrige isso sozinho no .zip comum, mas NÃO no .zip64 (usado quando o dump
//   passa de 2 GB ou há vários bancos): lá dá "Unrecognized archive format". Os instaladores autoextraíveis resolvem somando o
//   tamanho do texto a todas as posições. Aqui é a mesma conta, só nas estruturas do FIM do .zip (diretório central, fim do
//   .zip64 e fim do .zip); o resto do arquivo não muda e é gravado como veio.
//
// tailPatcher(destino, deslocamento): WritableStream que repassa o .zip ao destino guardando só os últimos ~1 MB; no fim corrige
//   essas posições e grava. Serve para o .zip montado aqui e para um .zip pronto copiado (vários bancos).

const EOCD = 0x06054b50, ZIP64_LOCATOR = 0x07064b50, ZIP64_EOCD = 0x06064b50, CENTRAL = 0x02014b50, NO32 = 0xFFFFFFFF;

// FAZ: soma `shift` às posições guardadas em `tail` (os últimos bytes do .zip; tailStart = onde eles começam, contando do início
//   do .zip). Muda `tail` no lugar, sem mudar o tamanho. Lança erro se o fim do .zip não for reconhecido.
export function patchZipTail(tail, shift, tailStart) {
  const v = new DataView(tail.buffer, tail.byteOffset, tail.byteLength);
  const u16 = o => v.getUint16(o, true), u32 = o => v.getUint32(o, true), u64 = o => Number(v.getBigUint64(o, true));
  const put64 = (o, n) => v.setBigUint64(o, BigInt(n), true);
  let eocd = -1;
  for (let i = tail.length - 22; i >= 0; i--) if (u32(i) === EOCD) { eocd = i; break; }
  if (eocd < 0) throw new Error('o fim do .zip não foi encontrado');
  let central = u32(eocd + 16);
  const locator = eocd - 20;
  if (locator >= 0 && u32(locator) === ZIP64_LOCATOR) {
    const end64 = u64(locator + 8);
    const at = end64 - tailStart;
    if (at < 0 || at + 56 > tail.length || u32(at) !== ZIP64_EOCD) throw new Error('o fim do .zip64 não foi encontrado');
    central = u64(at + 48);
    put64(locator + 8, end64 + shift);
    put64(at + 48, central + shift);
  } else if (central !== NO32) {
    if (central + shift >= NO32) throw new Error('o .zip precisaria ser .zip64');
    v.setUint32(eocd + 16, central + shift, true);
  }
  let p = central - tailStart;
  if (p < 0) throw new Error('o diretório central do .zip é maior que o trecho final guardado');
  while (p + 46 <= tail.length && u32(p) === CENTRAL) {
    const compressed32 = u32(p + 20), size32 = u32(p + 24), offset32 = u32(p + 42);
    const nameLen = u16(p + 28), extraLen = u16(p + 30), commentLen = u16(p + 32);
    if (offset32 !== NO32) {
      if (offset32 + shift >= NO32) throw new Error('o .zip precisaria ser .zip64');
      v.setUint32(p + 42, offset32 + shift, true);
    } else {
      // posição só no campo extra zip64 (tag 1): vem depois do tamanho e do tamanho compactado, se estes também forem 0xFFFFFFFF
      for (let x = p + 46 + nameLen, end = x + extraLen; x + 4 <= end; x += 4 + u16(x + 2)) {
        if (u16(x) !== 1) continue;
        let k = x + 4;
        if (size32 === NO32) k += 8;
        if (compressed32 === NO32) k += 8;
        put64(k, u64(k) + shift);
        break;
      }
    }
    p += 46 + nameLen + extraLen + commentLen;
  }
}

// FAZ: um WritableStream que grava tudo em `target` (outro WritableStream), segurando os últimos `hold` bytes; ao fechar, corrige
//   as posições do .zip (patchZipTail) e grava o resto. `shift` = bytes de texto gravados antes do .zip.
export function tailPatcher(target, shift, hold = 1 << 20) {
  const out = target.getWriter();
  const queue = [];
  let size = 0, written = 0;
  return new WritableStream({
    async write(chunk) {
      const copy = chunk.slice();              // quem escreve pode reaproveitar o buffer
      queue.push(copy);
      size += copy.length;
      while (queue.length > 1 && size - queue[0].length >= hold) {
        const first = queue.shift();
        size -= first.length;
        written += first.length;
        await out.write(first);
      }
    },
    async close() {
      const tail = new Uint8Array(size);
      let at = 0;
      for (const piece of queue) { tail.set(piece, at); at += piece.length; }
      patchZipTail(tail, shift, written);
      await out.write(tail);
      await out.close();
    },
    abort(reason) { return out.abort(reason); },
  });
}
