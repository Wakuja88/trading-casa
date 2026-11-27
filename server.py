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
NEWS_MAX = 300
# Cada sección es un tab. Si un feed deja de responder, se cambia aquí.
SECCIONES = {
    "cripto": [
        ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
        ("Cointelegraph", "https://cointelegraph.com/rss"),
        ("The Block", "https://www.theblock.co/rss.xml"),
        ("Decrypt", "https://decrypt.co/feed"),
        ("Bitcoin Magazine", "https://bitcoinmagazine.com/.rss/full/"),
    ],
    "mundo": [
        ("Fed", "https://www.federalreserve.gov/feeds/press_all.xml"),
        ("BCE", "https://www.ecb.europa.eu/rss/press.html"),
        ("CNBC", "https://www.cnbc.com/id/100003114/device/rss/rss.html"),
        ("CNBC", "https://www.cnbc.com/id/20910258/device/rss/rss.html"),
        ("CNBC", "https://www.cnbc.com/id/100727362/device/rss/rss.html"),
        ("MarketWatch", "https://feeds.content.dowjones.io/public/rss/mw_topstories"),
        ("Financial Times", "https://www.ft.com/rss/home/international"),
        ("NYT", "https://rss.nytimes.com/services/xml/rss/nyt/Business.xml"),
        ("BBC", "https://feeds.bbci.co.uk/news/business/rss.xml"),
        ("BBC", "https://feeds.bbci.co.uk/news/world/rss.xml"),
        ("Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml"),
    ],
}
# Fuentes oficiales: siempre cuentan como banco central. Solo son "alto impacto"
# si tratan de política monetaria (no avisos administrativos o sanciones a bancos).
OFICIALES = {"Fed", "BCE"}
_OFICIAL_ALTO = re.compile(r"fomc|monetary policy|interest rates?|policy rates?|minutes|press conference|projections|powell|lagarde|governing council|beige book", re.I)

# Temas y palabras clave (en inglés, como vienen los titulares).
# Peso 3 = suele mover el mercado el mismo día; 2 = relevante; 1 = contexto.
TEMAS = {
    "Bancos centrales": [
        (3, r"\bfed\b|\bfomc\b|powell|federal reserve|rate (cut|hike)|interest rates?|\becb\b|lagarde|bank of japan|\bboj\b|ueda|bank of england|\bboe\b|central bank|quantitative|balance sheet"),
        (2, r"\bbasis points?\b|\bdovish|\bhawkish|monetary policy|banxico|\bpboc\b"),
    ],
    "Macro": [
        (3, r"\bcpi\b|inflation|payrolls?|jobs report|unemployment|\bgdp\b|recession|\bpce\b|treasury yields?|bond yields?|10-year"),
        (2, r"\bpmi\b|retail sales|jobless claims|consumer (confidence|sentiment)|housing starts|deficit|debt ceiling|credit rating|downgrade"),
    ],
    "Geopolítica": [
        (3, r"\bwar\b|invasion|missile|airstrike|sanctions?|nuclear|ceasefire|military|attack"),
        (2, r"ukraine|russia|putin|china|chinese|beijing|taiwan|xi jinping|iran|israel|gaza|north korea|middle east|nato|red sea|strait of hormuz"),
    ],
    "Política": [
        (3, r"tariffs?|trade war|trade deal|executive order|government shutdown|white house"),
        (2, r"\btrump\b|congress|senate|election|treasury secretary|bessent|regulation|\bsec\b|antitrust"),
    ],
    "Negocios": [
        (3, r"earnings|guidance|bankruptcy|default|layoffs|merger|acquisition|takeover|\bipo\b"),
        (2, r"nvidia|apple|microsoft|tesla|amazon|alphabet|google|\bmeta\b|openai|jpmorgan|goldman|blackrock|wall street|s&p 500|nasdaq|dow jones|stocks?|market rally|sell-?off"),
    ],
    "Energía y metales": [
        (3, r"\boil\b|crude|\bopec\b|\bgold\b|\bsilver\b"),
        (2, r"natural gas|brent|\bwti\b|copper|commodit|precious metals?"),
    ],
    "Cripto": [
        (2, r"bitcoin|crypto|stablecoin|ethereum|\betf\b"),
    ],
}
_TEMAS_RE = {t: [(w, re.compile(rx, re.I)) for w, rx in reglas] for t, reglas in TEMAS.items()}
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"
_news = {}  # sección -> {"t": epoch, "data": dict}
_news_lock = threading.Lock()


def _clasificar(item):
    """Agrega temas y una puntuación de impacto a la noticia."""
    texto = item["titulo"] + " " + item["resumen"]
    temas, score = [], 0
    for tema, reglas in _TEMAS_RE.items():
        peso = max((w for w, rx in reglas if rx.search(texto)), default=0)
        if peso:
            temas.append(tema)
            score += peso
    if item["fuente"] in OFICIALES:
        score = 3 if _OFICIAL_ALTO.search(texto) else 1
        temas = ["Bancos centrales"] + [t for t in temas if t != "Bancos centrales"]
    item["temas"] = temas
    item["impacto"] = "alto" if score >= 3 else ("medio" if score >= 1 else "bajo")
    return item


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


def noticias(seccion):
    feeds = SECCIONES.get(seccion)
    if feeds is None:
        return None
    with _news_lock:
        cache = _news.get(seccion)
        if cache and time.time() - cache["t"] < NEWS_TTL:
            return cache["data"]
        with ThreadPoolExecutor(max_workers=len(feeds)) as ex:
            resultados = list(ex.map(_bajar, feeds))
        vistos, todas, fuentes = set(), [], {}
        for fuente, items, err in resultados:
            f = fuentes.setdefault(fuente, {"nombre": fuente, "ok": False, "total": 0, "error": None})
            if err is None:
                f["ok"] = True
            elif not f["ok"]:
                f["error"] = err
            for it in items:
                if it["link"] not in vistos:
                    vistos.add(it["link"])
                    todas.append(_clasificar(it))
                    f["total"] += 1
        for f in fuentes.values():
            if f["ok"]:
                f["error"] = None
        todas.sort(key=lambda x: x["fecha"] or 0, reverse=True)
        data = {"actualizado": int(time.time() * 1000), "seccion": seccion,
                "fuentes": list(fuentes.values()), "noticias": todas[:NEWS_MAX]}
        # Si todo falló, se sigue mostrando lo último bueno
        if todas or not cache:
            _news[seccion] = {"t": time.time(), "data": data}
        else:
            cache["data"]["fuentes"], cache["t"] = data["fuentes"], time.time()
        return _news[seccion]["data"]


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
        ruta, _, query = self.path.partition("?")
        if ruta.rstrip("/") == NEWS_API:
            params = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
            data = noticias(params.get("seccion", "cripto"))
            return self._json(200, data) if data else self._json(404, {"error": "sección desconocida"})
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
