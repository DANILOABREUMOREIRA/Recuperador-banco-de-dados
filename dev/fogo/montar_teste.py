"""
montar_teste.py - Monta as pastas do teste de fogo, um banco por vez:

  <raiz>/1 - Originais/data_fogoNN/          a pasta data ÍNTEGRA (um banco de ~500 MB)
  <raiz>/2 - Corrompidos/fogoNN_corrompido.zip   a mesma pasta, estragada de forma aleatória e compactada (zip, com a pasta `data` dentro)
  <raiz>/3 - Recuperados/                    vazia: é onde ficam os .sql que o site gerar
  <raiz>/_gabarito (nao abrir)/fogoNN/       original/ (hash de cada linha), corrupcao/ (o que foi estragado e o que pode sumir), preflight/

Passos de cada banco: gerar -> hash das linhas do original -> copiar -> corromper -> (opcional) recuperar a cópia no Python nativo
-> compactar -> apagar a cópia. Tudo reiniciável: o que já existe é pulado.

Uso: python montar_teste.py <raiz> <área de trabalho> <primeiro> <último> [--sem-preflight]
"""
import os
import shutil
import subprocess
import sys
import time
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fogo_common import say

HERE = os.path.dirname(os.path.abspath(__file__))
PYTHON = sys.executable


def run(script, *args):
    done = subprocess.run([PYTHON, "-u", os.path.join(HERE, script), *map(str, args)], capture_output=True)
    out = done.stdout.decode("utf8", "replace")
    if done.returncode != 0:
        raise RuntimeError(f"{script} falhou ({done.returncode}): {done.stderr.decode('utf8', 'replace')[-800:]}\n{out[-400:]}")
    return out


def zip_folder(folder, target):
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=1, allowZip64=True) as z:
        for base, _dirs, files in os.walk(folder):
            for name in sorted(files):
                path = os.path.join(base, name)
                z.write(path, os.path.join("data", os.path.relpath(path, folder)))


def main():
    root, work = sys.argv[1], sys.argv[2]
    first, last = int(sys.argv[3]), int(sys.argv[4])
    preflight = "--sem-preflight" not in sys.argv
    originals, broken = os.path.join(root, "1 - Originais"), os.path.join(root, "2 - Corrompidos")
    os.makedirs(originals, exist_ok=True)
    os.makedirs(broken, exist_ok=True)
    os.makedirs(os.path.join(root, "3 - Recuperados"), exist_ok=True)
    for k in range(first, last + 1):
        name = f"fogo{k:02d}"
        gab = os.path.join(root, "_gabarito (nao abrir)", name)
        orig = os.path.join(originals, f"data_{name}")
        started = time.time()
        if not os.path.isdir(orig):
            gen = os.path.join(work, f"gerar_{name}")
            os.makedirs(gen, exist_ok=True)
            say(f"[{time.strftime('%H:%M:%S')}] {name}: gerando...")
            env = dict(os.environ, FOGO_DIR=work)
            done = subprocess.run([PYTHON, "-u", os.path.join(HERE, "gerar_bancos.py"), os.path.join(gen, "data"), "1", "500", "1", "fogo", str(k)],
                                  capture_output=True, env=env)
            if done.returncode != 0:
                raise RuntimeError(f"gerar_bancos falhou: {done.stderr.decode('utf8', 'replace')[-800:]}")
            os.makedirs(gab, exist_ok=True)
            for meta in ("manifesto.json", "contagens_mysql.json"):
                shutil.move(os.path.join(gen, meta), os.path.join(gab, meta))
            shutil.move(os.path.join(gen, "data"), orig)
        if not os.path.isdir(os.path.join(gab, "original")):
            say(f"[{time.strftime('%H:%M:%S')}] {name}: hash das linhas do original...")
            run("recuperar_banco.py", orig, os.path.join(gab, "original"), name)
        zip_path = os.path.join(broken, f"{name}_corrompido.zip")
        if not os.path.exists(zip_path):
            copy = os.path.join(work, f"corrompido_{name}")
            if os.path.exists(copy):
                shutil.rmtree(copy)
            say(f"[{time.strftime('%H:%M:%S')}] {name}: corrompendo...")
            shutil.copytree(orig, os.path.join(copy, "data"))
            seed = 20261113 + k * 7919
            say("   " + run("corruptor_aleatorio.py", orig, os.path.join(copy, "data"), os.path.join(gab, "corrupcao"), seed).strip().replace("\n", "\n   "))
            if preflight:
                say(f"[{time.strftime('%H:%M:%S')}] {name}: recuperando a cópia corrompida (pré-teste nativo)...")
                run("recuperar_banco.py", os.path.join(copy, "data"), os.path.join(gab, "preflight"), name, "--sem-banco")
                say("   " + run("comparar_fogo.py", gab, os.path.join(gab, "preflight"), os.path.join(gab, "preflight.json")).strip().replace("\n", "\n   ")[:1500])
            say(f"[{time.strftime('%H:%M:%S')}] {name}: compactando...")
            zip_folder(os.path.join(copy, "data"), zip_path + ".parcial")
            os.replace(zip_path + ".parcial", zip_path)
            shutil.rmtree(copy)
        say(f"[{time.strftime('%H:%M:%S')}] {name}: pronto em {time.time() - started:.0f} s | zip {os.path.getsize(zip_path) / 1048576:.0f} MB")
    say("FIM")


if __name__ == "__main__":
    main()
