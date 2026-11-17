// kernel.ts - O laço quente do Recuperador, compilado para WebAssembly.
//
// FAZ: recebe UMA página de índice do InnoDB (16 KB, formato COMPACT) e a descrição das colunas da tabela e devolve o texto SQL
//   de cada linha, ex.: (1,'abc',NULL,'2020-01-31'). É o que o Python do site fazia, registro por registro, em
//   innodb_carver.iter_records + parse_compact + decode_value + sql_literal: o trecho que mais gasta tempo.
// EQUIVALÊNCIA: o texto de saída tem de ser IDÊNTICO ao do Python (o hash de cada linha é conferido nos testes). O que o
//   kernel não sabe fazer com segurança vira registro ESPECIAL e o Python o refaz do jeito antigo: valor fora da página
//   (BLOB longo), FLOAT/DOUBLE (o repr do Python não é reproduzido aqui), texto UTF-8 inválido e tamanhos incomuns.
//
// MODOS: 0 = leitura em lotes (só texto das linhas vivas, juntas por vírgula); 1 = leitura completa (por registro: chave,
//   transação, apagado e texto, para o Python tirar as duplicatas das páginas antigas).
//
// SAÍDA (em memória do WebAssembly; o JS copia): cabeçalho de 8 inteiros, lista de especiais, metadados e bytes de texto.

const PAGE_SIZE: i32 = 16384;
const INFIMUM: i32 = 99;
const SUPREMUM: i32 = 112;
const MAX_RECS: i32 = 3276;           // PAGE_SIZE // 5, como em innodb_carver.MAX_RECS_PER_PAGE
const OUT_CAP: i32 = 12 * 1024 * 1024;
const SPEC_CAP: i32 = 4096;
const META_CAP: i32 = 4096;
const META_INTS: i32 = 9;              // por registro: origem, apagado, chave(off,len), trx(hi,lo), texto(off,len), reservado
const MAX_FIELDS: i32 = 4096;
const SCHEMA_HEAD: i32 = 8;            // inteiros antes dos campos: nFields, nNullable, nKey, nOut
const FIELD_INTS: i32 = 8;             // por campo: kind, fixed, nullable, twoByte, flags, p, s, reservado

// kinds (iguais aos de kernel_bridge.py)
const K_SKIP: i32 = 0;
const K_INT: i32 = 1;
const K_DATETIME: i32 = 2;
const K_DATE: i32 = 3;
const K_TIME: i32 = 4;
const K_TIMESTAMP: i32 = 5;
const K_YEAR: i32 = 6;
const K_FLOAT: i32 = 7;
const K_DECIMAL: i32 = 9;
const K_BIT: i32 = 10;
const K_BINARY: i32 = 11;
const K_UTF8: i32 = 12;
const K_CP1252: i32 = 13;

// flags do campo
const F_UNSIGNED: i32 = 1;
const F_RSTRIP: i32 = 2;

// resultados de decodificar um campo
const R_OK: i32 = 0;
const R_SPECIAL: i32 = 1;
const R_BAD: i32 = 2;

let PAGE: i32 = 0;
let SCHEMA: i32 = 0;
let OUT: i32 = 0;
let SPEC: i32 = 0;
let META: i32 = 0;
let HDR: i32 = 0;
let VISITED: i32 = 0;
let FOFF: i32 = 0;      // início de cada campo na página (-1 = NULL)
let FLEN: i32 = 0;
let FEXT: i32 = 0;
let TMP: i32 = 0;       // área de trabalho (DECIMAL)

let outPos: i32 = 0;
let overflow: bool = false;

export function init(): void {
  PAGE = <i32>heap.alloc(PAGE_SIZE);
  SCHEMA = <i32>heap.alloc((SCHEMA_HEAD + MAX_FIELDS * (FIELD_INTS + 1)) * 4);
  OUT = <i32>heap.alloc(OUT_CAP);
  SPEC = <i32>heap.alloc(SPEC_CAP * 4);
  META = <i32>heap.alloc(META_CAP * META_INTS * 4);
  HDR = <i32>heap.alloc(64);
  VISITED = <i32>heap.alloc(PAGE_SIZE);
  FOFF = <i32>heap.alloc(MAX_FIELDS * 4);
  FLEN = <i32>heap.alloc(MAX_FIELDS * 4);
  FEXT = <i32>heap.alloc(MAX_FIELDS * 4);
  TMP = <i32>heap.alloc(512);
}

export function pagePtr(): i32 { return PAGE; }
export function schemaPtr(): i32 { return SCHEMA; }
export function outPtr(): i32 { return OUT; }
export function specPtr(): i32 { return SPEC; }
export function metaPtr(): i32 { return META; }
export function hdrPtr(): i32 { return HDR; }
export function schemaCapacity(): i32 { return MAX_FIELDS; }

// ------------------------------------------------------------------------------------------ leitura de bytes
@inline function pg(i: i32): i32 { return <i32>load<u8>(PAGE + i); }
@inline function sch(i: i32): i32 { return load<i32>(SCHEMA + (i << 2)); }
@inline function fld(f: i32, k: i32): i32 { return load<i32>(SCHEMA + ((SCHEMA_HEAD + f * FIELD_INTS + k) << 2)); }
// ordem de saída: depois dos campos, nOut inteiros com o índice do campo de cada coluna de saída
@inline function outField(j: i32): i32 { return load<i32>(SCHEMA + ((SCHEMA_HEAD + sch(0) * FIELD_INTS + j) << 2)); }

// ------------------------------------------------------------------------------------------ escrita na saída
@inline function put(b: i32): void { store<u8>(OUT + outPos, <u8>b); outPos++; }

function putDec(value: u64): void {
  if (value == 0) { put(48); return; }
  let n = 0;
  let v = value;
  while (v > 0) { store<u8>(TMP + n, <u8>(48 + <i32>(v % 10))); v = v / 10; n++; }
  while (n > 0) { n--; put(<i32>load<u8>(TMP + n)); }
}

function putSigned(value: i64): void {
  if (value < 0) {
    put(45);
    putDec(<u64>(0 - value));      // wrap correto também para o menor i64
  } else {
    putDec(<u64>value);
  }
}

// número com zeros à esquerda até `width` dígitos (como f"{x:04d}")
function putPadded(value: u64, width: i32): void {
  let n = 0;
  let v = value;
  if (v == 0) { store<u8>(TMP, 48); n = 1; }
  while (v > 0) { store<u8>(TMP + n, <u8>(48 + <i32>(v % 10))); v = v / 10; n++; }
  let pad = width - n;
  while (pad > 0) { put(48); pad--; }
  while (n > 0) { n--; put(<i32>load<u8>(TMP + n)); }
}

function putNull(): void { put(78); put(85); put(76); put(76); }

const HEX: StaticArray<u8> = [48, 49, 50, 51, 52, 53, 54, 55, 56, 57, 97, 98, 99, 100, 101, 102];

// CP1252: pontos de código de 0x80 a 0x9F (0 = indefinido)
const CP1252: StaticArray<u16> = [
  0x20AC, 0, 0x201A, 0x0192, 0x201E, 0x2026, 0x2020, 0x2021, 0x02C6, 0x2030, 0x0160, 0x2039, 0x0152, 0, 0x017D, 0,
  0, 0x2018, 0x2019, 0x201C, 0x201D, 0x2022, 0x2013, 0x2014, 0x02DC, 0x2122, 0x0161, 0x203A, 0x0153, 0, 0x017E, 0x0178,
];

const DIG2BYTES: StaticArray<u8> = [0, 1, 1, 2, 2, 3, 3, 4, 4, 4];

// UTF-8 estrito (como o decoder do Python): sem sobrelongos, sem substitutos, até U+10FFFF
function validUtf8(off: i32, len: i32): bool {
  let i = off;
  let end = off + len;
  while (i < end) {
    let b = pg(i);
    if (b < 0x80) { i++; continue; }
    let need = 0;
    let lo = 0x80;
    let hi = 0xBF;
    if (b >= 0xC2 && b <= 0xDF) { need = 1; }
    else if (b == 0xE0) { need = 2; lo = 0xA0; }
    else if (b >= 0xE1 && b <= 0xEC) { need = 2; }
    else if (b == 0xED) { need = 2; hi = 0x9F; }
    else if (b == 0xEE || b == 0xEF) { need = 2; }
    else if (b == 0xF0) { need = 3; lo = 0x90; }
    else if (b >= 0xF1 && b <= 0xF3) { need = 3; }
    else if (b == 0xF4) { need = 3; hi = 0x8F; }
    else { return false; }
    if (i + need >= end) return false;
    let c = pg(i + 1);
    if (c < lo || c > hi) return false;
    for (let k = 2; k <= need; k++) {
      let d = pg(i + k);
      if (d < 0x80 || d > 0xBF) return false;
    }
    i += need + 1;
  }
  return true;
}

// escreve um ponto de código em UTF-8, já com os escapes do sql_literal
function putCodePoint(cp: i32): void {
  if (cp < 0x80) {
    if (cp == 92) { put(92); put(92); }
    else if (cp == 39) { put(92); put(39); }
    else if (cp == 0) { put(92); put(48); }
    else if (cp == 10) { put(92); put(110); }
    else if (cp == 13) { put(92); put(114); }
    else if (cp == 26) { put(92); put(90); }
    else { put(cp); }
  } else if (cp < 0x800) {
    put(0xC0 | (cp >> 6));
    put(0x80 | (cp & 0x3F));
  } else {
    put(0xE0 | (cp >> 12));
    put(0x80 | ((cp >> 6) & 0x3F));
    put(0x80 | (cp & 0x3F));
  }
}

// ------------------------------------------------------------------------------------------ valores
function bigEndian(off: i32, len: i32): u64 {
  let v: u64 = 0;
  for (let i = 0; i < len; i++) v = (v << 8) | <u64>pg(off + i);
  return v;
}

// inteiro comum (com o bit de sinal invertido pelo InnoDB quando há sinal)
function putInteger(off: i32, len: i32, unsigned: bool): i32 {
  if (len > 8) return R_SPECIAL;
  let v = bigEndian(off, len);
  if (unsigned) { putDec(v); return R_OK; }
  if (len == 0) return R_BAD;          // o Python levanta erro (deslocamento negativo)
  if (len == 8) { putSigned(<i64>(v ^ 0x8000000000000000)); return R_OK; }
  let half: i64 = (<i64>1) << (<i64>(8 * len - 1));
  putSigned(<i64>v - half);
  return R_OK;
}

function putDecimal(off: i32, len: i32, p: i32, s: i32): i32 {
  let intg = p - s;
  let intg0 = intg / 9;
  let intg0x = intg % 9;
  let frac0 = s / 9;
  let frac0x = s % 9;
  let size = intg0 * 4 + <i32>DIG2BYTES[intg0x] + frac0 * 4 + <i32>DIG2BYTES[frac0x];
  if (len < size) return R_BAD;          // "DECIMAL truncado"
  if (size > 64) return R_SPECIAL;
  let buf = TMP + 128;                   // cópia de trabalho: o TMP baixo é usado pelos números
  for (let i = 0; i < size; i++) store<u8>(buf + i, load<u8>(PAGE + off + i));
  let negative = (load<u8>(buf) & 0x80) == 0;
  store<u8>(buf, load<u8>(buf) ^ 0x80);
  if (negative) { for (let i = 0; i < size; i++) store<u8>(buf + i, load<u8>(buf + i) ^ 0xFF); }
  // dígitos em um vetor de caracteres: inteiros e depois decimais
  let digits = TMP + 192;                // até 64 caracteres
  let nd = 0;
  let pos = 0;
  let intDigits = 0;
  // parte inteira: grupo incompleto (sem zeros à esquerda), depois grupos de 9
  if (intg0x > 0) {
    let n = <i32>DIG2BYTES[intg0x];
    let v: u64 = 0;
    for (let i = 0; i < n; i++) v = (v << 8) | <u64>load<u8>(buf + pos + i);
    pos += n;
    // escreve v em decimal sem zeros à esquerda
    let t = 0;
    let w = v;
    if (w == 0) { store<u8>(digits + nd, 48); nd++; intDigits++; }
    else {
      let start = nd;
      while (w > 0) { store<u8>(digits + nd, <u8>(48 + <i32>(w % 10))); w = w / 10; nd++; }
      // inverte o trecho escrito
      let a = start; let b = nd - 1;
      while (a < b) { let x = load<u8>(digits + a); store<u8>(digits + a, load<u8>(digits + b)); store<u8>(digits + b, x); a++; b--; }
      intDigits += nd - start;
    }
  }
  for (let g = 0; g < intg0; g++) {
    let v: u64 = 0;
    for (let i = 0; i < 4; i++) v = (v << 8) | <u64>load<u8>(buf + pos + i);
    pos += 4;
    if (v >= 1000000000) return R_SPECIAL;
    for (let k = 8; k >= 0; k--) { store<u8>(digits + nd + k, <u8>(48 + <i32>(v % 10))); v = v / 10; }
    nd += 9; intDigits += 9;
  }
  let fracStart = nd;
  for (let g = 0; g < frac0; g++) {
    let v: u64 = 0;
    for (let i = 0; i < 4; i++) v = (v << 8) | <u64>load<u8>(buf + pos + i);
    pos += 4;
    if (v >= 1000000000) return R_SPECIAL;
    for (let k = 8; k >= 0; k--) { store<u8>(digits + nd + k, <u8>(48 + <i32>(v % 10))); v = v / 10; }
    nd += 9;
  }
  if (frac0x > 0) {
    let n = <i32>DIG2BYTES[frac0x];
    let v: u64 = 0;
    for (let i = 0; i < n; i++) v = (v << 8) | <u64>load<u8>(buf + pos + i);
    pos += n;
    let limit: u64 = 1;
    for (let k = 0; k < frac0x; k++) limit = limit * 10;
    if (v >= limit) return R_SPECIAL;
    for (let k = frac0x - 1; k >= 0; k--) { store<u8>(digits + nd + k, <u8>(48 + <i32>(v % 10))); v = v / 10; }
    nd += frac0x;
  }
  // tira zeros à esquerda da parte inteira (sobra pelo menos "0")
  let first = 0;
  while (first < intDigits - 1 && load<u8>(digits + first) == 48) first++;
  let allZero = true;
  for (let i = first; i < intDigits; i++) if (load<u8>(digits + i) != 48) allZero = false;
  if (s > 0) for (let i = fracStart; i < nd; i++) if (load<u8>(digits + i) != 48) allZero = false;
  if (intDigits == 0) { first = 0; }
  if (negative && !allZero) put(45);
  if (intDigits == 0) put(48);
  else for (let i = first; i < intDigits; i++) put(<i32>load<u8>(digits + i));
  if (s > 0) {
    put(46);
    for (let i = fracStart; i < nd; i++) put(<i32>load<u8>(digits + i));
  }
  return R_OK;
}

function putText(off: i32, len: i32, cp1252: bool, rstrip: bool): i32 {
  let end = off + len;
  if (rstrip) { while (end > off && pg(end - 1) == 32) end--; }
  put(39);
  if (!cp1252) {
    if (!validUtf8(off, end - off)) { return R_SPECIAL; }
    for (let i = off; i < end; i++) {
      let b = pg(i);
      if (b >= 0x80) put(b);
      else putCodePoint(b);
    }
  } else {
    // o "latin1" do MySQL é cp1252; se houver byte indefinido, o Python decodifica TUDO como latin1
    let undefinedByte = false;
    for (let i = off; i < end; i++) {
      let b = pg(i);
      if (b >= 0x80 && b <= 0x9F && CP1252[b - 0x80] == 0) { undefinedByte = true; break; }
    }
    for (let i = off; i < end; i++) {
      let b = pg(i);
      if (b >= 0x80 && b <= 0x9F && !undefinedByte) putCodePoint(<i32>CP1252[b - 0x80]);
      else putCodePoint(b);
    }
  }
  put(39);
  return R_OK;
}

function putDatetime(off: i32): void {
  let n = <i64>(bigEndian(off, 8) ^ 0x8000000000000000);
  put(39);
  if (n <= 0) {
    put(48); put(48); put(48); put(48); put(45); put(48); put(48); put(45); put(48); put(48);
    put(32); put(48); put(48); put(58); put(48); put(48); put(58); put(48); put(48);
  } else {
    let date = n / 1000000;
    let clock = n % 1000000;
    putPadded(<u64>(date / 10000), 4); put(45);
    putPadded(<u64>((date / 100) % 100), 2); put(45);
    putPadded(<u64>(date % 100), 2); put(32);
    putPadded(<u64>(clock / 10000), 2); put(58);
    putPadded(<u64>((clock / 100) % 100), 2); put(58);
    putPadded(<u64>(clock % 100), 2);
  }
  put(39);
}

// escreve o valor do campo f; devolve R_OK, R_SPECIAL ou R_BAD
function putField(f: i32): i32 {
  let off = load<i32>(FOFF + (f << 2));
  if (off < 0) { putNull(); return R_OK; }
  let len = load<i32>(FLEN + (f << 2));
  let kind = fld(f, 0);
  let flags = fld(f, 4);
  if (kind == K_DATETIME && len == 8) { putDatetime(off); return R_OK; }
  if (kind == K_DATE && len == 3) {
    let packed = <i32>(bigEndian(off, 3) ^ 0x800000);
    put(39);
    putPadded(<u64>(packed >> 9), 4); put(45);
    putPadded(<u64>((packed >> 5) & 15), 2); put(45);
    putPadded(<u64>(packed & 31), 2);
    put(39);
    return R_OK;
  }
  if (kind == K_TIME && len == 3) {
    let packed = <i32>(bigEndian(off, 3) ^ 0x800000);
    if (packed >= (1 << 23)) packed -= (1 << 24);
    put(39);
    if (packed < 0) { put(45); packed = 0 - packed; }
    putPadded(<u64>(packed / 10000), 2); put(58);
    putPadded(<u64>((packed / 100) % 100), 2); put(58);
    putPadded(<u64>(packed % 100), 2);
    put(39);
    return R_OK;
  }
  if (kind == K_TIMESTAMP && len == 4) {
    let seconds = bigEndian(off, 4);
    if (seconds != 0) {
      // FROM_UNIXTIME(n), sem aspas
      put(70); put(82); put(79); put(77); put(95); put(85); put(78); put(73); put(88); put(84); put(73); put(77); put(69); put(40);
      putDec(seconds);
      put(41);
    } else {
      put(39);
      put(48); put(48); put(48); put(48); put(45); put(48); put(48); put(45); put(48); put(48);
      put(32); put(48); put(48); put(58); put(48); put(48); put(58); put(48); put(48);
      put(39);
    }
    return R_OK;
  }
  if (kind == K_YEAR && len == 1) {
    let b = pg(off);
    putDec(b != 0 ? <u64>(1900 + b) : <u64>0);
    return R_OK;
  }
  if (kind == K_INT || kind == K_DATETIME || kind == K_DATE || kind == K_TIME || kind == K_TIMESTAMP || kind == K_YEAR) {
    return putInteger(off, len, (flags & F_UNSIGNED) != 0);
  }
  if (kind == K_FLOAT) return R_SPECIAL;
  if (kind == K_DECIMAL) return putDecimal(off, len, fld(f, 5), fld(f, 6));
  if (kind == K_BIT) {
    if (len > 8) return R_SPECIAL;
    putDec(bigEndian(off, len));
    return R_OK;
  }
  if (kind == K_BINARY) {
    if (len == 0) { put(39); put(39); return R_OK; }
    put(48); put(120);
    for (let i = 0; i < len; i++) { let b = pg(off + i); put(<i32>HEX[b >> 4]); put(<i32>HEX[b & 15]); }
    return R_OK;
  }
  if (kind == K_UTF8 || kind == K_CP1252) return putText(off, len, kind == K_CP1252, (flags & F_RSTRIP) != 0);
  return R_SPECIAL;
}

// ------------------------------------------------------------------------------------------ registros e páginas
let nSeen: i32 = 0;
let nDeleted: i32 = 0;
let nBad: i32 = 0;
let nRows: i32 = 0;
let nSpecial: i32 = 0;
let nMeta: i32 = 0;

function addSpecial(origin: i32): void {
  if (nSpecial < SPEC_CAP) { store<i32>(SPEC + (nSpecial << 2), origin); nSpecial++; }
  else overflow = true;
}

// separa os campos do registro (o equivalente de parse_compact); devolve R_OK, R_SPECIAL (valor fora da página) ou R_BAD
function splitRecord(origin: i32, nFields: i32, nNullable: i32): i32 {
  let nullBytes = (nNullable + 7) >> 3;
  let nullPtr = origin - 6;
  let lenPtr = origin - 6 - nullBytes;
  let pos = origin;
  let nullI = 0;
  let special = false;
  for (let f = 0; f < nFields; f++) {
    if (fld(f, 2) != 0) {
      let idx = nullPtr - (nullI >> 3);
      if (idx < 0) return R_BAD;
      let isNull = (pg(idx) & (1 << (nullI & 7))) != 0;
      nullI++;
      if (isNull) { store<i32>(FOFF + (f << 2), -1); store<i32>(FLEN + (f << 2), 0); continue; }
    }
    let size = fld(f, 1);
    let external = false;
    if (size == 0) {
      if (lenPtr < 0) return R_BAD;
      size = pg(lenPtr);
      lenPtr--;
      if (fld(f, 3) != 0 && (size & 0x80) != 0) {
        external = (size & 0x40) != 0;
        if (lenPtr < 0) return R_BAD;
        size = ((size & 0x3F) << 8) | pg(lenPtr);
        lenPtr--;
      }
    }
    if (pos + size > PAGE_SIZE - 8 || lenPtr < 0) return R_BAD;
    store<i32>(FOFF + (f << 2), pos);
    store<i32>(FLEN + (f << 2), size);
    pos += size;
    if (external) special = true;
  }
  return special ? R_SPECIAL : R_OK;
}

// processa um registro; mode 0 = só texto das linhas vivas, mode 1 = por registro com chave e transação
function processRecord(origin: i32, mode: i32, nFields: i32, nNullable: i32, nKey: i32, nOut: i32): void {
  if ((pg(origin - 3) & 7) != 0) return;                // não é registro comum (nó interno, etc.)
  let deleted = (pg(origin - 5) & 0x20) != 0;
  let split = splitRecord(origin, nFields, nNullable);
  if (split == R_BAD) { nBad++; return; }
  if (split == R_SPECIAL) { addSpecial(origin); return; }

  // o pior caso de bytes de saída deste registro: confere antes de escrever, sem checar byte a byte
  let dataBytes = 0;
  for (let f = 0; f < nFields; f++) dataBytes += load<i32>(FLEN + (f << 2));
  let bound = 4 * dataBytes + 64 * nOut + 64 + (mode == 1 ? 6 * nKey + 4 * dataBytes : 0);
  if (outPos + bound > OUT_CAP) { overflow = true; return; }

  let mark = outPos;
  let keyOff = 0;
  let keyLen = 0;
  let trxHi = 0;
  let trxLo = 0;
  if (mode == 1) {
    // chave = campos de chave concatenados (marcador de NULL ou tamanho + bytes); transação = o campo seguinte à chave
    keyOff = outPos;
    for (let f = 0; f < nKey; f++) {
      let off = load<i32>(FOFF + (f << 2));
      if (off < 0) { put(255); continue; }
      let len = load<i32>(FLEN + (f << 2));
      put(0); put(len & 0xFF); put((len >> 8) & 0xFF);
      for (let i = 0; i < len; i++) put(pg(off + i));
    }
    keyLen = outPos - keyOff;
    if (nKey < nFields) {
      let off = load<i32>(FOFF + (nKey << 2));
      if (off >= 0) {
        let len = load<i32>(FLEN + (nKey << 2));
        if (len > 8) { outPos = mark; addSpecial(origin); return; }
        let t = bigEndian(off, len);
        trxHi = <i32>(t >> 32);
        trxLo = <i32>(t & 0xFFFFFFFF);
      }
    }
  }
  let textOff = outPos;
  if (!deleted || mode == 1) {
    put(40);
    for (let j = 0; j < nOut; j++) {
      if (j > 0) put(44);
      let r = putField(outField(j));
      if (r != R_OK) {
        outPos = mark;
        if (r == R_BAD) nBad++; else addSpecial(origin);
        return;
      }
    }
    put(41);
  } else {
    // linha apagada no modo 0: o Python só conta e não decodifica
    outPos = mark;
    nDeleted++;
    return;
  }
  if (mode == 0) {
    nRows++;
    put(44);                                            // a vírgula separadora fica depois de cada linha; o JS tira a última
    return;
  }
  // modo 1: metadados do registro (o texto de uma linha apagada não é usado)
  if (nMeta >= META_CAP) { overflow = true; return; }
  let m = META + nMeta * META_INTS * 4;
  store<i32>(m, origin);
  store<i32>(m + 4, deleted ? 1 : 0);
  store<i32>(m + 8, keyOff);
  store<i32>(m + 12, keyLen);
  store<i32>(m + 16, trxHi);
  store<i32>(m + 20, trxLo);
  store<i32>(m + 24, textOff);
  store<i32>(m + 28, outPos - textOff);
  store<i32>(m + 32, 0);
  nMeta++;
  nRows++;
}

// converte a página que o JS copiou para PAGE. Devolve 0 se tudo certo; 1 se estourou a saída (o Python refaz a página).
export function convertPage(mode: i32): i32 {
  let nFields = sch(0);
  let nNullable = sch(1);
  let nKey = sch(2);
  let nOut = sch(3);
  nSeen = 0; nDeleted = 0; nBad = 0; nRows = 0; nSpecial = 0; nMeta = 0;
  outPos = 0;
  overflow = false;
  memory.fill(VISITED, 0, PAGE_SIZE);
  let origin = INFIMUM;
  for (let n = 0; n < MAX_RECS; n++) {
    let rel = (pg(origin - 2) << 8) | pg(origin - 1);
    if (rel >= 0x8000) rel -= 0x10000;
    origin = (origin + rel) & 0xFFFF;
    if (origin == SUPREMUM || origin <= SUPREMUM || origin >= PAGE_SIZE - 8) break;
    if (load<u8>(VISITED + origin) != 0) break;
    store<u8>(VISITED + origin, 1);
    nSeen++;
    processRecord(origin, mode, nFields, nNullable, nKey, nOut);
    if (overflow) break;
  }
  store<i32>(HDR, nSeen);
  store<i32>(HDR + 4, nDeleted);
  store<i32>(HDR + 8, nBad);
  store<i32>(HDR + 12, nRows);
  store<i32>(HDR + 16, nSpecial);
  store<i32>(HDR + 20, nMeta);
  store<i32>(HDR + 24, outPos);
  store<i32>(HDR + 28, overflow ? 1 : 0);
  return overflow ? 1 : 0;
}
