"""
comparar_fogo.py - Compara o que a recuperação devolveu com o banco ORIGINAL, linha a linha, descontando o que o corruptor
destruiu de fato (o gabarito de perda permitida).

Entrada recuperada: ou a pasta de saída do recuperar_banco.py (.u64 + .json), ou o arquivo .sql que o site gera.

Uso: python comparar_fogo.py <gabarito do banco> <pasta de hashes | arquivo .sql> [relatorio.json]
     (o gabarito do banco tem as pastas `original` e `corrupcao`)

Cada tabela do original recebe um veredito:
  IGUAL              as mesmas linhas, nem uma a mais, nem uma a menos
  PERDA_ESPERADA     faltam só linhas que estavam em páginas destruídas ou alteradas pelo corruptor (e não há linhas a mais)
  PERDA_INESPERADA   faltam linhas que NÃO estavam em página nenhuma atingida: a recuperação deixou de recuperar o que dava
  LINHAS_A_MAIS      aparecem linhas que o original não tinha (registro estragado lido como válido, ou apagado que reapareceu)
  AUSENTE            a tabela nem existe no resultado
  SEM_ESTRUTURA     o .frm foi apagado/estragado E o dicionário do ibdata1 também: ninguém sabe nome nem colunas da tabela. Não há
                     como recuperar, e é contado à parte (não é falha do motor)
  SEM_NOME           a recuperação devolveu os dados numa tabela `_sem_nome_N` (nome perdido); comparada só pela contagem de linhas
  SEM_FRM            o .frm da tabela foi estragado: o texto das linhas muda (enum vira número, decimal vira binário...), então
                     compara só a quantidade de linhas (tem de bater com o original menos o que podia sumir)
Além disso, SILENCIOSA = a tabela tem diferença e o relatório do motor não a marcou com aviso (o pior caso).
"""
import array
import bisect
import json
import os
import re
import sys
import zlib

TOKEN = re.compile(r"'(?:[^'\\]|\\.)*'|[^'()]+|[()]", re.S)


def read_u64(path):
    out = array.array("Q")
    if os.path.exists(path):
        with open(path, "rb") as f:
            out.frombytes(f.read())
    return out


def merge_diff(a, b):
    """a, b ordenados. Devolve (só em a, só em b) como multiconjuntos (listas)."""
    only_a, only_b, i, j = [], [], 0, 0
    while i < len(a) and j < len(b):
        if a[i] == b[j]:
            i += 1
            j += 1
        elif a[i] < b[j]:
            only_a.append(a[i])
            i += 1
        else:
            only_b.append(b[j])
            j += 1
    only_a.extend(a[i:])
    only_b.extend(b[j:])
    return only_a, only_b


def parse_dump(path):
    """Lê um .sql gerado pelo motor. Devolve (banco, {tabela: array ordenado de hashes de linha})."""
    db, tables = None, {}
    with open(path, "rb") as f:
        for raw in f:
            line = raw.decode("utf8", "replace")
            if db is None and line.startswith("CREATE DATABASE"):
                m = re.search(r"`([^`]+)`", line)
                db = m.group(1) if m else None
            if not line.startswith("INSERT INTO `"):
                continue
            table = line[13:line.index("`", 13)]
            bucket = tables.setdefault(table, array.array("Q"))
            depth, start = 0, 0
            for m in TOKEN.finditer(line, line.index(" VALUES ") + 8):
                token = m.group()
                if token == "(":
                    if depth == 0:
                        start = m.start()
                    depth += 1
                elif token == ")":
                    depth -= 1
                    if depth == 0:
                        text = line[start:m.end()].encode("utf8", "replace")
                        bucket.append((zlib.crc32(text) << 32) | zlib.adler32(text))
    return db, {t: array.array("Q", sorted(h)) for t, h in tables.items()}


def load_recovered(source, db):
    """Devolve ({tabela: hashes}, {tabela: info do relatório}, sem_nome {tabela: hashes}, relatório do motor)."""
    if os.path.isfile(source):
        _db, tables = parse_dump(source)
        return tables, {}, {}, {}
    tables, info, unnamed, report = {}, {}, {}, {}
    path = os.path.join(source, f"{db}.json")
    if os.path.exists(path):
        report = json.load(open(path, encoding="utf8"))
        info = report.get("tabelas", {})
    for name in os.listdir(source):
        if name.endswith(".u64"):
            owner, table = name[:-4].split(".", 1)
            if owner == db:
                tables[table] = read_u64(os.path.join(source, name))
            elif owner == "_sem_banco":
                unnamed[table] = read_u64(os.path.join(source, name))
    return tables, info, unnamed, report


by_count = set()   # tabelas pareadas só pela contagem (o texto não é comparável)


def match_unnamed(unnamed, baseline):
    """Para cada tabela sem nome, acha a tabela do original que ela é (por uma amostra de hashes). {tabela original: hashes}."""
    found = {}
    for name, hashes in unnamed.items():
        if not len(hashes):
            continue
        sample = [hashes[k * len(hashes) // 25] for k in range(min(25, len(hashes)))]
        best, best_hits = None, 0
        for table, ref in baseline.items():
            hits = 0
            for h in sample:
                k = bisect.bisect_left(ref, h)
                hits += k < len(ref) and ref[k] == h
            if hits > best_hits:
                best, best_hits = table, hits
        if best and best_hits >= max(1, len(sample) // 5):
            found[best] = (name, hashes)
    # Sem .frm o texto das linhas muda (enum vira número...), então o hash não casa: pareia pela contagem de linhas mais próxima
    #   (até 10% de diferença) entre as tabelas ainda sem par.
    taken = {n for n, _h in found.values()}
    for name, hashes in sorted(unnamed.items(), key=lambda kv: -len(kv[1])):
        if name in taken or not len(hashes):
            continue
        options = [(abs(len(ref) - len(hashes)), t) for t, ref in baseline.items() if t not in found and len(ref) and abs(len(ref) - len(hashes)) <= 0.1 * len(ref)]
        if options:
            t = min(options)[1]
            found[t] = (name, hashes)
            by_count.add(t)
    return found


def main():
    gab, source = sys.argv[1], sys.argv[2]
    out_path = sys.argv[3] if len(sys.argv) > 3 else None
    orig_dir, cor_dir = os.path.join(gab, "original"), os.path.join(gab, "corrupcao")
    orig_report = [f for f in os.listdir(orig_dir) if f.endswith(".json") and not f.startswith("_")][0]
    db = orig_report[:-5]
    expected = json.load(open(os.path.join(cor_dir, "esperado.json"), encoding="utf8")) if os.path.exists(os.path.join(cor_dir, "esperado.json")) else {"tabelas": {}}
    baseline = {n.split(".", 1)[1][:-4]: read_u64(os.path.join(orig_dir, n)) for n in os.listdir(orig_dir) if n.startswith(db + ".") and n.endswith(".u64")}
    danos = json.load(open(os.path.join(cor_dir, "danos.json"), encoding="utf8")) if os.path.exists(os.path.join(cor_dir, "danos.json")) else []
    dict_hit = any(d.get("dano") == "dicionario" for d in danos)
    frm_hit = {os.path.basename(f[0])[:-4] for d in danos if d.get("dano") == "frm" for f in d.get("arquivos", [])}
    orig_tables = json.load(open(os.path.join(orig_dir, orig_report), encoding="utf8")).get("tabelas", {})
    recovered, info, unnamed, report = load_recovered(source, db)
    named_from_unnamed = match_unnamed(unnamed, {t: h for t, h in baseline.items() if t not in recovered or len(recovered[t]) < len(h)}) if unnamed else {}
    warned = " ".join(report.get("avisos", [])) if report else ""
    rows = {"IGUAL": 0, "PERDA_ESPERADA": 0, "PERDA_INESPERADA": 0, "LINHAS_A_MAIS": 0, "AUSENTE": 0, "SEM_FRM": 0, "SEM_ESTRUTURA": 0, "SEM_NOME": 0}
    verdicts, totals = {}, {"original": 0, "recuperadas": 0, "perdidas": 0, "perdidas_dentro_do_permitido": 0, "perdidas_fora_do_permitido": 0, "a_mais": 0}
    for table, ref in sorted(baseline.items()):
        got = recovered.get(table)
        extra_note = ""
        if table in named_from_unnamed:
            name, hashes = named_from_unnamed[table]
            if got is None or len(hashes) > len(got):
                got, extra_note = hashes, f"recuperada sem nome ({name})"
        if got is None:
            got, absent = array.array("Q"), True
        else:
            absent = False
        missing, extra = merge_diff(ref, got)
        allowed = read_u64(os.path.join(cor_dir, f"permitido.{db}.{table}.u64"))
        beyond, _unused = merge_diff(sorted(missing), list(allowed)) if missing else ([], [])
        budget = expected.get("tabelas", {}).get(f"{db}/{table}", {}).get("linhas_que_podem_mudar", 0)
        if not missing and not extra:
            verdict = "IGUAL"
        elif beyond:
            verdict = "PERDA_INESPERADA"
        elif len(extra) > budget:
            verdict = "LINHAS_A_MAIS"
        else:
            verdict = "PERDA_ESPERADA"
        if table in by_count and (missing or extra):
            verdict = "SEM_NOME" if len(got) >= len(ref) - len(allowed) and len(got) <= len(ref) else "PERDA_INESPERADA"
            beyond = [] if verdict == "SEM_NOME" else beyond
        elif table in frm_hit and (missing or extra):
            verdict = "SEM_FRM" if len(got) >= len(ref) - len(allowed) and len(got) <= len(ref) else "PERDA_INESPERADA"
            beyond = [] if verdict == "SEM_FRM" else beyond
        if absent and missing and table in frm_hit and dict_hit:
            verdict, beyond = "SEM_ESTRUTURA", []
        elif absent and missing:
            verdict = "AUSENTE" if beyond else verdict
        meta = info.get(table, {})
        silent = bool(missing or extra) and (meta.get("status") == "OK" or (not meta and table not in warned)) and bool(report)
        verdicts[table] = {"veredito": verdict, "original": len(ref), "recuperadas": len(got), "faltam": len(missing), "faltam_fora_do_permitido": len(beyond),
                           "a_mais": len(extra), "silenciosa": silent, "status_motor": meta.get("status"), "nota": (extra_note or meta.get("note", ""))[:200]}
        rows[verdict] = rows.get(verdict, 0) + 1
        totals["original"] += len(ref)
        totals["recuperadas"] += len(got) if verdict in ("SEM_NOME", "SEM_FRM") else len(got) - len(extra)
        totals["perdidas"] += len(missing)
        totals["perdidas_dentro_do_permitido"] += len(missing) - len(beyond)
        totals["perdidas_fora_do_permitido"] += len(beyond)
        totals["a_mais"] += len(extra)
    summary = {"banco": db, "tabelas": len(baseline), "vereditos": rows, "linhas": totals,
               "silenciosas": sorted(t for t, v in verdicts.items() if v["silenciosa"]),
               "sem_nome_recuperadas": len(unnamed), "falha_do_motor": report.get("fatal") if report else None,
               "recuperado_pct": round(100 * totals["recuperadas"] / max(1, totals["original"]), 3),
               "recuperavel_pct": round(100 * (totals["original"] - totals["perdidas_dentro_do_permitido"]) / max(1, totals["original"]), 3)}
    result = {"resumo": summary, "tabelas": verdicts}
    if out_path:
        json.dump(result, open(out_path, "w", encoding="utf8"), ensure_ascii=False, indent=1)
    print(json.dumps(summary, ensure_ascii=False))
    for table, v in verdicts.items():
        if v["veredito"] not in ("IGUAL", "PERDA_ESPERADA"):
            print(f"  {v['veredito']:17} {table}: original {v['original']} recuperadas {v['recuperadas']} faltam {v['faltam']} (fora do permitido {v['faltam_fora_do_permitido']}) a_mais {v['a_mais']} | {v['status_motor']} {v['nota'][:90]}")


if __name__ == "__main__":
    main()
