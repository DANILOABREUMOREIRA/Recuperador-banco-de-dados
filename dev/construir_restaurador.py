"""Gera web/static/restaurar_stub.exe: o programa que o site usa como começo do restaurador (o site cola o .zip com os .sql depois dele).
Uso:  python construir_restaurador.py      (precisa do PyInstaller; refazer só quando o motor do Recuperador mudar)

É o restaurar_pasta_data.py do programa de desktop, com o MySQL 5.5 embutido (mysql/), o esquema mysql limpo (mysql_template/) e o
catálogo de referência. Console visível: o usuário acompanha o andamento."""
import os
import shutil
import subprocess
import sys

AQUI = os.path.dirname(os.path.abspath(__file__))
RAIZ = os.path.dirname(os.path.dirname(AQUI))                 # recuperacao de banco/
SISTEMA = os.path.join(RAIZ, "programa", "sistema")
TRABALHO = os.path.join(AQUI, "build_restaurador")
SAIDA = os.path.join(os.path.dirname(AQUI), "static", "restaurar_stub.exe")

comando = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile", "--console",
           "--name", "restaurar_stub", "--paths", SISTEMA, "--icon", os.path.join(SISTEMA, "static", "icone.ico"),
           "--distpath", os.path.join(TRABALHO, "dist"), "--workpath", os.path.join(TRABALHO, "work"), "--specpath", TRABALHO]
for pasta in ("mysql", "mysql_template", "schema_reference"):
    comando += ["--add-data", f"{os.path.join(SISTEMA, pasta)}{os.pathsep}{pasta}"]
comando.append(os.path.join(SISTEMA, "restaurar_pasta_data.py"))
subprocess.run(comando, check=True)
shutil.copy2(os.path.join(TRABALHO, "dist", "restaurar_stub.exe"), SAIDA)
shutil.rmtree(TRABALHO, ignore_errors=True)
print(f"\nPronto: {SAIDA} ({os.path.getsize(SAIDA) / 1048576:.1f} MB)")
