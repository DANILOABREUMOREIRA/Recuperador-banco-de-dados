"""
dev_server.py - Servidor local para testar o site (só 127.0.0.1). Serve a pasta do site e, para o teste automático
(http://127.0.0.1:8765/?auto=1), uma pasta data do disco, sem precisar escolhê-la à mão.

Uso: python dev/dev_server.py [pasta data] [arquivo .zip/.rar]
?auto=1 testa a pasta data; ?auto=archive testa o arquivo (servido em /archive).
O resultado do navegador fica em dev/resultado_navegador.json.
"""
import functools
import http.server
import json
import os
import re
import sys
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
SITE = os.path.dirname(HERE)
DATA = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] != "-" else None
ARCHIVE = sys.argv[2] if len(sys.argv) > 2 else None
SKIP = re.compile(r"^(ib_logfile\d+|.*\.(err|pid))$", re.I)   # o mesmo que a página deixa de fora


def data_files():
    names = []
    for entry in sorted(os.listdir(DATA)) if DATA else []:
        path = os.path.join(DATA, entry)
        if os.path.isdir(path):
            names += [f"{entry}/{f}" for f in sorted(os.listdir(path)) if os.path.isfile(os.path.join(path, f))]
        elif not SKIP.match(entry):
            names.append(entry)
    return names


class Handler(http.server.SimpleHTTPRequestHandler):
    def translate_path(self, path):
        clean = urllib.parse.unquote(path.split("?")[0])
        if clean.startswith("/data/"):
            name = clean[6:]
            return os.path.join(DATA, *name.split("/")) if name in data_files() else os.path.join(HERE, "nao-existe")
        return super().translate_path(path)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")   # teste sempre com os arquivos atuais
        super().end_headers()

    def do_GET(self):
        if self.path == "/list":
            body = json.dumps(data_files()).encode("utf8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.split("?")[0] == "/archive" and ARCHIVE:
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(os.path.getsize(ARCHIVE)))
            self.send_header("X-Archive-Name", urllib.parse.quote(os.path.basename(ARCHIVE)))
            self.end_headers()
            with open(ARCHIVE, "rb") as f:
                while chunk := f.read(1 << 20):
                    self.wfile.write(chunk)
            return
        super().do_GET()

    def do_POST(self):
        if self.path != "/result":
            self.send_error(404)
            return
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        with open(os.path.join(HERE, "resultado_navegador.json"), "wb") as out:
            out.write(body)
        self.send_response(204)
        self.end_headers()


if __name__ == "__main__":
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 8765), functools.partial(Handler, directory=SITE))
    print("http://127.0.0.1:8765/")
    server.serve_forever()
