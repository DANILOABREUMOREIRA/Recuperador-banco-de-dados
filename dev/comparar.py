"""
comparar.py - Compara a recuperação do ORIGINAL com a da CÓPIA CORROMPIDA, tabela por tabela (linhas e impressão digital
do conteúdo), contra o que o plano de danos (corromper.py) esperava.

Uso: python comparar.py <original.json> <corrompida.json> <plano.json>
Cada tabela cai em uma classe:
  IGUAL      mesma contagem e mesmo conteúdo do original (dano nenhum ou desfeito pela recuperação)
  ESPERADO   perdeu exatamente as linhas das páginas que foram zeradas (conteúdo restante idêntico ao original)
  DIFERENTE  não bate com o original nem com o esperado: o detalhe vem junto
"""
import json
import sys

MASK = 0xFFFFFFFFFFFFFFFF
orig = json.load(open(sys.argv[1], encoding="utf8"))
broken = json.load(open(sys.argv[2], encoding="utf8"))
plan = json.load(open(sys.argv[3], encoding="utf8"))
expect = plan["esperado"]
touched = {a["tabela"] for a in plan["acoes"]}


def digest(info):
    return int(info["digest"], 16) if info.get("digest") else 0


classes, detail = {"IGUAL": [], "ESPERADO": [], "DIFERENTE": [], "AUSENTE": []}, {}
for name, o in orig["tabelas"].items():
    c = broken["tabelas"].get(name)
    if c is None:
        classes["AUSENTE"].append(name)
        continue
    same = (o["rows"], digest(o)) == (c["rows"], digest(c))
    e = expect.get(name, {})
    want = (o["rows"] - e.get("linhas_perdidas", 0), (digest(o) - e.get("digest_perdido", 0)) & MASK)
    if same:
        classes["IGUAL"].append(name)
    elif "linhas_perdidas" in e and (c["rows"], digest(c)) == want:
        classes["ESPERADO"].append(name)
    else:
        classes["DIFERENTE"].append(name)
        detail[name] = {"original": o["rows"], "corrompida": c["rows"], "esperado_se_so_perdesse_as_paginas": want[0],
                        "conteudo_bate_com_esperado": (digest(c) == want[1]), "nota": c["note"][:230]}

print(f"ORIGINAL : {orig['segundos']} s | {orig['registros']} registros | {orig['sql_bytes'] / 1048576:.0f} MB de SQL | pico de memória {orig.get('pico_memoria_mb')} MB")
print(f"CORROMPIDA: {broken['segundos']} s | {broken['registros']} registros | {broken['sql_bytes'] / 1048576:.0f} MB de SQL | pico de memória {broken.get('pico_memoria_mb')} MB | avisos {len(broken['avisos'])}")
print(f"\nTabelas: {len(orig['tabelas'])} | IGUAL {len(classes['IGUAL'])} | ESPERADO {len(classes['ESPERADO'])} | DIFERENTE {len(classes['DIFERENTE'])} | AUSENTE {len(classes['AUSENTE'])}")
print(f"Linhas: original {orig['registros']} -> corrompida {broken['registros']} (perda {orig['registros'] - broken['registros']})")

print("\n== tabelas atingidas pelo plano ==")
for action in plan["acoes"]:
    name = action["tabela"]
    if name == "SYS_TABLES":
        continue
    o, c = orig["tabelas"].get(name), broken["tabelas"].get(name)
    verdict = next((k for k, v in classes.items() if name in v), "?")
    print(f"- {name} [{action['dano'][:46]}]: {verdict} | original {o['rows'] if o else '-'} -> corrompida {c['rows'] if c else '-'}"
          f"{' | ' + c['status'] if c else ''}{' | ' + c['note'][:150] if c and c['note'] else ''}")

print("\n== dicionário (SYS_TABLES zerada) ==")
dict_note = [(n, i["note"]) for n, i in broken["tabelas"].items() if "dicion" in i["note"].lower() or "páginas (" in i["note"]]
print(f"tabelas com nota de reconstrução: {len(dict_note)}")
for n, note in dict_note[:8]:
    print(f"  {n}: {classes_name if (classes_name := next((k for k, v in classes.items() if n in v), '?')) else ''} | {note[:140]}")

print("\n== DIFERENTE (fora do que o plano explica) ==")
for name in classes["DIFERENTE"]:
    print(f"- {name}: {json.dumps(detail[name], ensure_ascii=False)}")
if classes["AUSENTE"]:
    print("\n== AUSENTE na corrompida ==", classes["AUSENTE"])
print("\nAvisos da corrompida (primeiros 12):")
for warning in broken["avisos"][:12]:
    print("  -", warning[:200])
