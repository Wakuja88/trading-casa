#!/usr/bin/env python3
"""Servidor del dashboard de trading.

Sirve site/, guarda los escenarios en DATA_DIR/escenarios.json y junta
noticias cripto de varios feeds RSS en /api/noticias.
Solo usa la librería estándar de Python.
"""
import email.utils
import html
import json
import os
import re
import tempfile
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", "80"))
SITE = os.environ.get("SITE_DIR", "/opt/interes/site")
DATA_DIR = os.environ.get("DATA_DIR", "/var/lib/interes")
DATA = os.path.join(DATA_DIR, "escenarios.json")
MAX_BYTES = 2 * 1024 * 1024
API = "/api/escenarios"

# ---------------------------------------------------------------------------
# Noticias: el servidor descarga los feeds (el navegador no puede por CORS)
# y los guarda en memoria NEWS_TTL segundos para no saturar a los medios.
# ---------------------------------------------------------------------------
NEWS_API = "/api/noticias"
NEWS_TTL = 60
NEWS_MAX = 200
FEEDS = [
    ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("Cointelegraph", "https://cointelegraph.com/rss"),
    ("The Block", "https://www.theblock.co/rss.xml"),
    ("Decrypt", "https://decrypt.co/feed"),
    ("Bitcoin Magazine", "https://bitcoinmagazine.com/.rss/full/"),
]
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"
_news = {"t": 0.0, "data": None}
_news_lock = threading.Lock()


def _local(tag):
    return tag.rsplit("}", 1)[-1].lower() if isinstance(tag, str) else ""


def _fecha(texto):
    """Fecha RSS (RFC 822) o Atom (ISO 8601) -> milisegundos epoch, o None."""
    texto = (texto or "").strip()
    if not texto:
        return None
    try:
        d = email.utils.parsedate_to_datetime(texto)
    except (TypeError, ValueError, IndexError):
        try:
            d = datetime.fromisoformat(texto.replace("Z", "+00:00"))
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return int(d.timestamp() * 1000)


def _limpio(texto, n=240):
    texto = re.sub(r"<[^>]+>", " ", texto or "")
    texto = re.sub(r"\s+", " ", html.unescape(texto)).strip()
    return texto if len(texto) <= n else texto[: n - 1].rsplit(" ", 1)[0] + "…"


def _parsear(fuente, raw):
    root = ET.fromstring(raw)
    out = []
    for el in root.iter():
        if _local(el.tag) not in ("item", "entry"):
            continue
        d = {"fuente": fuente, "titulo": "", "link": "", "fecha": None, "resumen": ""}
        for c in el:
            n = _local(c.tag)
            if n == "title":
                d["titulo"] = _limpio(c.text, 300)
            elif n == "link":
                href = c.get("href")
                if href and c.get("rel", "alternate") == "alternate":
                    d["link"] = href.strip()
                elif not href and c.text:
                    d["link"] = c.text.strip()
            elif n in ("pubdate", "published", "updated", "date") and d["fecha"] is None:
                d["fecha"] = _fecha(c.text)
            elif n in ("description", "summary") and not d["resumen"]:
                d["resumen"] = _limpio(c.text)
        if d["titulo"] and d["link"].startswith(("http://", "https://")):
            out.append(d)
    return out


def _bajar(fuente_url):
    fuente, url = fuente_url
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*"})
        with urllib.request.urlopen(req, timeout=10) as r:
            items = _parsear(fuente, r.read(5 * 1024 * 1024))
        return fuente, items, None
    except Exception as e:  # un medio caído no debe tumbar a los demás
        return fuente, [], f"{type(e).__name__}: {e}"[:160]


def noticias():
    with _news_lock:
        if _news["data"] and time.time() - _news["t"] < NEWS_TTL:
            return _news["data"]
        with ThreadPoolExecutor(max_workers=len(FEEDS)) as ex:
            resultados = list(ex.map(_bajar, FEEDS))
        vistos, todas, fuentes = set(), [], []
        for fuente, items, err in resultados:
            fuentes.append({"nombre": fuente, "ok": err is None, "total": len(items), "error": err})
            for it in items:
                if it["link"] not in vistos:
                    vistos.add(it["link"])
                    todas.append(it)
        todas.sort(key=lambda x: x["fecha"] or 0, reverse=True)
        data = {"actualizado": int(time.time() * 1000), "fuentes": fuentes, "noticias": todas[:NEWS_MAX]}
        # Si todo falló, se sigue mostrando lo último bueno
        if todas or not _news["data"]:
            _news["data"], _news["t"] = data, time.time()
        else:
            _news["data"]["fuentes"], _news["t"] = fuentes, time.time()
        return _news["data"]


def leer():
    try:
        with open(DATA, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def guardar(lista):
    os.makedirs(DATA_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=DATA_DIR, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(lista, f, ensure_ascii=False, indent=1)
        os.replace(tmp, DATA)  # escritura atómica: nunca queda un archivo a medias
    except BaseException:
        os.unlink(tmp)
        raise


class Handler(SimpleHTTPRequestHandler):
    def end_headers(self):
        # El navegador revalida siempre: al actualizar index.html se ve sin Ctrl+F5
        if not self.path.startswith("/api/"):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def list_directory(self, path):
        self.send_error(404)
        return None

    def _json(self, status, obj):
        raw = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _is_api(self):
        return self.path.split("?")[0].rstrip("/") == API

    def do_GET(self):
        if self._is_api():
            return self._json(200, leer())
        if self.path.split("?")[0].rstrip("/") == NEWS_API:
            return self._json(200, noticias())
        return super().do_GET()

    def do_PUT(self):
        if not self._is_api():
            return self._json(404, {"error": "no existe"})
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            n = -1
        if n < 0:
            return self._json(400, {"error": "Content-Length inválido"})
        if n > MAX_BYTES:
            return self._json(413, {"error": "demasiado grande"})
        try:
            lista = json.loads(self.rfile.read(n) or b"null")
        except ValueError:
            lista = None
        if not isinstance(lista, list):
            return self._json(400, {"error": "se esperaba una lista JSON"})
        guardar(lista)
        return self._json(200, {"ok": True, "total": len(lista)})

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    print(f"Dashboard en http://0.0.0.0:{PORT} — datos en {DATA}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), partial(Handler, directory=SITE)).serve_forever()
