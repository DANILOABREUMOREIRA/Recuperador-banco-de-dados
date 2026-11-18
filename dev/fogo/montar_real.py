"""
montar_real.py - Teste de fogo com bancos REAIS: pega uma pasta data de verdade (vários bancos dentro de um único ibdata1),
guarda o original, estraga uma cópia e compacta. Os bancos não dá para separar (moram no mesmo ibdata1 e o disco não
comporta cinco cópias de 8 GB), então a pasta toda é estragada de uma vez, como acontece no mundo real.

  (o original é a própria pasta que o usuário mandou: só é lida, nunca copiada nem alterada)
  <raiz>/2 - Corrompido/data_corrompida.zip   a mesma pasta, estragada de forma aleatória (pasta `data` dentro do .zip)
  <raiz>/3 - Recuperados/                     vazia: é onde ficam os .sql/.zip que o site gerar
  <raiz>/_gabarito (nao abrir)/<banco>/       original/ (hash de cada linha) e corrupcao/ (o que foi estragado), um por banco

Tudo reiniciável (o que já existe é pulado).
Uso: python montar_real.py <pasta data de origem> <raiz> <área de trabalho> [semente] [só este banco]
"""
import json
import os
import shutil
import subprocess
import sys
import time
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fogo_common import say
from montar_teste import run

SKIP = (".err", ".pid")


def stamp():
    return time.strftime("%H:%M:%S")


def copy_data(source, target):
    os.makedirs(target + ".parcial", exist_ok=True)
    for base, _dirs, files in os.walk(source):
        rel = os.path.relpath(base, source)
        os.makedirs(os.path.join(target + ".parcial", rel), exist_ok=True)
        for name in files:
            if name.lower().endswith(SKIP):
                continue
            shutil.copyfile(os.path.join(base, name), os.path.join(target + ".parcial", rel, name))
    os.replace(target + ".parcial", target)


def databases(data):
    return sorted(e for e in os.listdir(data) if e.lower() not in ("mysql", "performance_schema", "information_schema") and os.path.isdir(os.path.join(data, e))
                  and any(f.lower().endswith(".frm") for f in os.listdir(os.path.join(data, e))))


def zip_folder(folder, target):
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=1, allowZip64=True) as z:
        for base, _dirs, files in os.walk(folder):
            for name in sorted(files):
                path = os.path.join(base, name)
                z.write(path, os.path.join("data", os.path.relpath(path, folder)))


def main():
    source, root, work = sys.argv[1], sys.argv[2], sys.argv[3]
    seed = int(sys.argv[4]) if len(sys.argv) > 4 else 20261116
    only = sys.argv[5] if len(sys.argv) > 5 else None   # só este banco: o estrago e o gabarito são dele
    if only:
        os.environ["FOGO_SO_BANCO"] = only
    orig = source
    broken = os.path.join(root, "2 - Corrompido")
    gab_root = os.path.join(root, "_gabarito (nao abrir)")
    os.makedirs(broken, exist_ok=True)
    os.makedirs(os.path.join(root, "3 - Recuperados"), exist_ok=True)
    os.makedirs(gab_root, exist_ok=True)
    os.makedirs(work, exist_ok=True)

    dbs = [only] if only else databases(orig)
    say(f"[{stamp()}] bancos: {', '.join(dbs)}")

    hashes = os.path.join(work, "hashes_originais")
    if not os.path.exists(os.path.join(hashes, "FIM")):
        say(f"[{stamp()}] hash das linhas do original (leva um bom tempo)...")
        shutil.rmtree(hashes, ignore_errors=True)
        out = run("recuperar_banco.py", orig, hashes, ",".join(dbs))
        open(os.path.join(hashes, "FIM"), "w", encoding="utf8").write(out[-300:])
    for db in dbs:                                   # um gabarito por banco: o comparador lê um banco por vez
        target = os.path.join(gab_root, db, "original")
        if os.path.isdir(target):
            continue
        os.makedirs(target)
        for name in os.listdir(hashes):
            if name == db + ".json" or name.startswith(db + "."):
                shutil.copyfile(os.path.join(hashes, name), os.path.join(target, name))

    zip_path = os.path.join(broken, f"data_{only}_corrompida.zip" if only else "data_corrompida.zip")
    if not os.path.exists(zip_path):
        copy = os.path.join(work, "corrompida")
        shutil.rmtree(copy, ignore_errors=True)
        say(f"[{stamp()}] copiando o original para estragar...")
        shutil.copytree(orig, os.path.join(copy, "data"))
        if only:   # a pasta entregue tem só o banco do teste (mais mysql e o ibdata1 compartilhado)
            for entry in os.listdir(os.path.join(copy, "data")):
                path = os.path.join(copy, "data", entry)
                if os.path.isdir(path) and entry.lower() not in ("mysql", "performance_schema", only.lower()):
                    shutil.rmtree(path)
        common = os.path.join(work, "corrupcao")
        shutil.rmtree(common, ignore_errors=True)
        say(f"[{stamp()}] corrompendo (semente {seed})...")
        say("   " + run("corruptor_aleatorio.py", orig, os.path.join(copy, "data"), common, seed).strip().replace("\n", "\n   "))
        for db in dbs:
            shutil.rmtree(os.path.join(gab_root, db, "corrupcao"), ignore_errors=True)
            shutil.copytree(common, os.path.join(gab_root, db, "corrupcao"))
        say(f"[{stamp()}] compactando...")
        zip_folder(os.path.join(copy, "data"), zip_path + ".parcial")
        os.replace(zip_path + ".parcial", zip_path)
        shutil.rmtree(copy)
    say(f"[{stamp()}] pronto | zip {os.path.getsize(zip_path) / 1048576:.0f} MB")
    say("FIM")


if __name__ == "__main__":
    main()
