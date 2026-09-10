#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
COMPARTIR POR RED  —  servidor HTTP con interfaz gráfica (Windows / Linux)

Comparte una carpeta local por HTTP para que cualquier dispositivo de la misma
red la abra desde el navegador (PC, celular, tablet).

Características
---------------
- Selector de carpeta con ruta visible + arrastrar y soltar.
- Botones: Compartir / Detener, Copiar dirección, Abrir en navegador, Abrir carpeta.
- Muestra la dirección completa http://IP:PUERTO y detecta TODAS las interfaces de red.
- Código QR de la dirección (dibujado en canvas, no requiere Pillow).
- Indicadores en vivo: archivos en la raíz, conexiones activas, descargas, bytes servidos.
- Descarga directa de archivos (sin abrirlos) y de CARPETAS COMPLETAS en .zip
  (generado al vuelo con Transfer-Encoding: chunked, sin archivos temporales).
- NO cambia el directorio global del proceso: usa SimpleHTTPRequestHandler(directory=...).

Dependencias
------------
Obligatorias: solo la librería estándar (tkinter incluido).
Opcionales:   qrcode      -> código QR            (pip install qrcode)
              tkinterdnd2 -> arrastrar y soltar   (pip install tkinterdnd2)
              Pillow      -> generar el icono     (pip install pillow)

Uso
---
    python compartir_red.py
    python compartir_red.py --dir "C:/Users/yo/Documentos" --port 8080
    python compartir_red.py --crear-icono      # genera icon.png / icon.ico
"""

from __future__ import annotations

import argparse
import html
import io
import ipaddress
import os
import socket
import string
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
import zipfile
from datetime import datetime
from functools import partial
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from queue import Queue, Empty

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

APP_NOMBRE = "Compartir por red"
APP_VERSION = "1.0"
PUERTO_DEFECTO = 8000
DIR_APP = os.path.dirname(os.path.abspath(__file__))

# --- Dependencias opcionales -------------------------------------------------
try:
    import qrcode  # type: ignore
except Exception:
    qrcode = None

try:  # arrastrar y soltar
    from tkinterdnd2 import DND_FILES, TkinterDnD  # type: ignore
    DND_DISPONIBLE = True
except Exception:
    DND_FILES = None
    TkinterDnD = None
    DND_DISPONIBLE = False


# =============================================================================
#  RED: detección de interfaces
# =============================================================================
def ip_principal() -> str | None:
    """IP de la interfaz que usa el sistema para salir a la red (no envía datos)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.settimeout(0.4)
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def detectar_interfaces() -> list[tuple[str, str]]:
    """Devuelve [(ip, etiqueta), ...] con todas las IPv4 utilizables del equipo."""
    encontradas: dict[str, str] = {}

    def agregar(ip: str, etiqueta: str) -> None:
        if not ip:
            return
        try:
            dir_ip = ipaddress.ip_address(ip)
        except ValueError:
            return
        if dir_ip.version != 4:
            return
        encontradas.setdefault(ip, etiqueta)

    # 1) psutil da el nombre real de cada interfaz (si está instalado)
    try:
        import psutil  # type: ignore

        for nombre, direcciones in psutil.net_if_addrs().items():
            for d in direcciones:
                if d.family == socket.AF_INET:
                    agregar(d.address, nombre)
    except Exception:
        pass

    # 2) Interfaz de salida principal
    agregar(ip_principal() or "", "red principal")

    # 3) Todas las IPv4 asociadas al nombre del equipo
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            agregar(info[4][0], "equipo")
    except Exception:
        pass

    agregar("127.0.0.1", "solo este equipo")

    # LAN primero, luego el resto, loopback al final
    def orden(par: tuple[str, str]) -> tuple[int, str]:
        ip = par[0]
        dir_ip = ipaddress.ip_address(ip)
        if dir_ip.is_loopback:
            return (2, ip)
        if dir_ip.is_private:
            return (0, ip)
        return (1, ip)

    return sorted(encontradas.items(), key=orden)


def puerto_libre(puerto: int, bind: str = "0.0.0.0") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((bind, puerto))
            return True
        except OSError:
            return False


# =============================================================================
#  ESTADO COMPARTIDO entre el servidor (hilos) y la interfaz
# =============================================================================
class Estado:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.conexiones = 0        # peticiones en curso
        self.descargas = 0         # archivos/zip servidos
        self.bytes = 0             # bytes enviados
        self.clientes: set[str] = set()
        self.eventos: "Queue[str]" = Queue()

    def log(self, texto: str) -> None:
        self.eventos.put(f"{datetime.now():%H:%M:%S}  {texto}")

    def entrar(self, ip: str) -> None:
        with self.lock:
            self.conexiones += 1
            self.clientes.add(ip)

    def salir(self) -> None:
        with self.lock:
            self.conexiones = max(0, self.conexiones - 1)

    def sumar_descarga(self, n_bytes: int) -> None:
        with self.lock:
            self.descargas += 1
            self.bytes += n_bytes

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "conexiones": self.conexiones,
                "descargas": self.descargas,
                "bytes": self.bytes,
                "clientes": len(self.clientes),
            }


def formato_bytes(n: float) -> str:
    for unidad in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unidad == "TB":
            return f"{n:.0f} {unidad}" if unidad == "B" else f"{n:.1f} {unidad}"
        n /= 1024
    return f"{n:.1f} TB"


# =============================================================================
#  ESCRITOR "CHUNKED": permite enviar un .zip sin conocer su tamaño final
# =============================================================================
class EscritorChunked:
    """Adaptador de escritura con Transfer-Encoding: chunked."""

    def __init__(self, wfile) -> None:
        self.wfile = wfile
        self.enviados = 0

    def write(self, datos) -> int:  # zipfile solo necesita write()/flush()
        if not datos:
            return 0
        n = len(datos)
        self.wfile.write(b"%X\r\n" % n)
        self.wfile.write(datos)
        self.wfile.write(b"\r\n")
        self.enviados += n
        return n

    def flush(self) -> None:
        try:
            self.wfile.flush()
        except OSError:
            pass

    def cerrar(self) -> None:
        self.wfile.write(b"0\r\n\r\n")
        self.flush()


# =============================================================================
#  PLANTILLA HTML del listado de carpetas
# =============================================================================
PLANTILLA = string.Template("""<!DOCTYPE html>
<html lang="es"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>$titulo</title>
<style>
  :root { color-scheme: light dark; }
  * { box-sizing: border-box; }
  body { margin:0; font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
         background:#f4f5f7; color:#16181d; }
  header { background:#1f2937; color:#fff; padding:14px 18px; }
  header h1 { margin:0; font-size:17px; font-weight:600; }
  header .ruta { font-size:13px; opacity:.8; margin-top:4px; word-break:break-all; }
  main { max-width:960px; margin:0 auto; padding:16px; }
  .barra { display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin-bottom:12px; }
  .btn { display:inline-block; padding:8px 12px; border-radius:8px; text-decoration:none;
         font-size:14px; border:1px solid #d3d7de; background:#fff; color:#16181d; cursor:pointer; }
  .btn.primario { background:#2563eb; border-color:#2563eb; color:#fff; }
  input[type=search] { flex:1; min-width:180px; padding:8px 10px; border-radius:8px;
         border:1px solid #d3d7de; font-size:14px; background:#fff; color:#16181d; }
  table { width:100%; border-collapse:collapse; background:#fff; border-radius:10px; overflow:hidden;
          box-shadow:0 1px 3px rgba(0,0,0,.08); }
  th, td { padding:10px 12px; text-align:left; font-size:14px; border-bottom:1px solid #eceef2; }
  th { background:#fafbfc; font-size:12px; text-transform:uppercase; letter-spacing:.04em; color:#6b7280; }
  tr:last-child td { border-bottom:none; }
  td a.nombre { color:#16181d; text-decoration:none; font-weight:500; word-break:break-all; }
  td a.nombre:hover { text-decoration:underline; }
  .tam, .fecha { color:#6b7280; white-space:nowrap; }
  .acc { text-align:right; white-space:nowrap; }
  .pie { margin:14px 0 30px; color:#6b7280; font-size:12px; text-align:center; }
  @media (max-width:600px) { .fecha { display:none; } }
  @media (prefers-color-scheme: dark) {
    body { background:#14161a; color:#e6e8ec; }
    table { background:#1c1f25; box-shadow:none; }
    th { background:#22262d; color:#9aa2af; }
    th, td { border-color:#2a2f37; }
    td a.nombre { color:#e6e8ec; }
    .btn { background:#22262d; border-color:#333a44; color:#e6e8ec; }
    .btn.primario { background:#2563eb; border-color:#2563eb; color:#fff; }
    input[type=search] { background:#22262d; border-color:#333a44; color:#e6e8ec; }
  }
</style>
</head><body>
<header>
  <h1>📂 $titulo</h1>
  <div class="ruta">$ruta</div>
</header>
<main>
  <div class="barra">
    $arriba
    <a class="btn primario" href="?zip=1">⬇ Descargar esta carpeta (.zip)</a>
    <input type="search" id="filtro" placeholder="Filtrar por nombre…" autocomplete="off">
  </div>
  <table id="tabla">
    <thead><tr><th>Nombre</th><th>Tamaño</th><th class="fecha">Modificado</th><th class="acc">Descargar</th></tr></thead>
    <tbody>
$filas
    </tbody>
  </table>
  <div class="pie">$resumen · $app v$version</div>
</main>
<script>
  var f = document.getElementById('filtro');
  f.addEventListener('input', function () {
    var q = f.value.toLowerCase();
    var filas = document.querySelectorAll('#tabla tbody tr');
    for (var i = 0; i < filas.length; i++) {
      var n = filas[i].getAttribute('data-nombre') || '';
      filas[i].style.display = n.indexOf(q) === -1 ? 'none' : '';
    }
  });
</script>
</body></html>
""")


# =============================================================================
#  MANEJADOR HTTP
# =============================================================================
class ManejadorCompartir(SimpleHTTPRequestHandler):
    """SimpleHTTPRequestHandler con listado propio, descarga forzada y zip al vuelo."""

    protocol_version = "HTTP/1.1"
    server_version = f"CompartirRed/{APP_VERSION}"

    def __init__(self, *args, estado: Estado | None = None, **kwargs) -> None:
        self.estado = estado
        # OJO: BaseHTTPRequestHandler procesa la petición dentro de __init__,
        # por eso self.estado debe asignarse ANTES de llamar a super().
        super().__init__(*args, **kwargs)

    # ---------- contabilidad de conexiones ----------
    def handle_one_request(self) -> None:
        if self.estado:
            self.estado.entrar(self.client_address[0])
        try:
            super().handle_one_request()
        finally:
            if self.estado:
                self.estado.salir()

    def log_message(self, formato: str, *args) -> None:
        if self.estado:
            self.estado.log(f"{self.client_address[0]} → {formato % args}")

    def log_error(self, formato: str, *args) -> None:  # evita ruido en stderr
        self.log_message(formato, *args)

    # ---------- utilidades ----------
    @property
    def raiz(self) -> str:
        return os.path.abspath(self.directory)

    def dentro_de_raiz(self, ruta: str) -> bool:
        try:
            return os.path.commonpath([os.path.abspath(ruta), self.raiz]) == self.raiz
        except ValueError:
            return False

    # ---------- enrutado ----------
    def do_GET(self) -> None:
        partes = urllib.parse.urlsplit(self.path)
        params = urllib.parse.parse_qs(partes.query)
        ruta_fs = self.translate_path(self.path)

        if not self.dentro_de_raiz(ruta_fs):
            self.send_error(HTTPStatus.FORBIDDEN, "Ruta no permitida")
            return

        if "zip" in params and os.path.isdir(ruta_fs):
            self.enviar_zip(ruta_fs)
            return

        if "dl" in params and os.path.isfile(ruta_fs):
            self.enviar_adjunto(ruta_fs)
            return

        super().do_GET()

    # ---------- descarga forzada de un archivo ----------
    def enviar_adjunto(self, ruta_fs: str) -> None:
        try:
            f = open(ruta_fs, "rb")
        except OSError:
            self.send_error(HTTPStatus.NOT_FOUND, "Archivo no encontrado")
            return
        with f:
            st = os.fstat(f.fileno())
            nombre = os.path.basename(ruta_fs)
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(st.st_size))
            self.send_header("Content-Disposition", self.cabecera_adjunto(nombre))
            self.send_header("Last-Modified", self.date_time_string(st.st_mtime))
            self.end_headers()
            try:
                self.copyfile(f, self.wfile)
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True
                return
        if self.estado:
            self.estado.sumar_descarga(st.st_size)
            self.estado.log(f"descargó {nombre} ({formato_bytes(st.st_size)})")

    # ---------- carpeta completa en .zip generado al vuelo ----------
    def enviar_zip(self, ruta_dir: str) -> None:
        base = os.path.basename(os.path.normpath(ruta_dir)) or "compartido"
        archivos: list[tuple[str, str]] = []
        for carpeta, dirs, nombres in os.walk(ruta_dir):
            dirs[:] = [d for d in dirs if not d.startswith(".")]  # coherente con el listado
            for n in nombres:
                if n.startswith("."):
                    continue
                completa = os.path.join(carpeta, n)
                if os.path.isfile(completa) and not os.path.islink(completa):
                    archivos.append((completa, os.path.relpath(completa, ruta_dir)))

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition", self.cabecera_adjunto(base + ".zip"))
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        salida = EscritorChunked(self.wfile)
        try:
            # ZIP_STORED = sin comprimir: mucho más rápido y sin uso de disco/RAM.
            with zipfile.ZipFile(salida, "w", zipfile.ZIP_STORED, allowZip64=True) as z:
                for completa, relativa in archivos:
                    try:
                        z.write(completa, arcname=os.path.join(base, relativa))
                    except OSError:
                        continue
            salida.cerrar()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
            if self.estado:
                self.estado.log(f"descarga de {base}.zip interrumpida")
            return
        if self.estado:
            self.estado.sumar_descarga(salida.enviados)
            self.estado.log(
                f"descargó {base}.zip · {len(archivos)} archivos · {formato_bytes(salida.enviados)}"
            )

    @staticmethod
    def cabecera_adjunto(nombre: str) -> str:
        seguro = nombre.encode("ascii", "replace").decode("ascii").replace('"', "_")
        codificado = urllib.parse.quote(nombre, safe="")
        return f'attachment; filename="{seguro}"; filename*=UTF-8\'\'{codificado}'

    # ---------- listado HTML propio ----------
    def list_directory(self, path):  # noqa: N802  (nombre impuesto por la clase base)
        try:
            entradas = list(os.scandir(path))
        except OSError:
            self.send_error(HTTPStatus.FORBIDDEN, "No se puede listar la carpeta")
            return None

        entradas.sort(key=lambda e: (not e.is_dir(), e.name.lower()))
        ruta_url = urllib.parse.unquote(self.path.split("?", 1)[0], errors="replace")
        titulo = os.path.basename(os.path.normpath(path)) or "Carpeta compartida"

        filas: list[str] = []
        n_arch = n_dir = 0
        for e in entradas:
            nombre = e.name
            if nombre.startswith("."):  # oculta archivos de sistema
                continue
            try:
                st = e.stat()
            except OSError:
                continue
            es_dir = e.is_dir()
            enlace = urllib.parse.quote(nombre) + ("/" if es_dir else "")
            if es_dir:
                n_dir += 1
                icono, tam = "📁", "—"
                accion = f'<a class="btn" href="{enlace}?zip=1" title="Descargar carpeta en zip">⬇ .zip</a>'
            else:
                n_arch += 1
                icono, tam = "📄", formato_bytes(st.st_size)
                accion = f'<a class="btn" href="{enlace}?dl=1" title="Descargar archivo">⬇</a>'
            fecha = datetime.fromtimestamp(st.st_mtime).strftime("%d/%m/%Y %H:%M")
            filas.append(
                f'      <tr data-nombre="{html.escape(nombre.lower(), quote=True)}">'
                f'<td>{icono} <a class="nombre" href="{enlace}">{html.escape(nombre)}</a></td>'
                f'<td class="tam">{tam}</td><td class="fecha">{fecha}</td>'
                f'<td class="acc">{accion}</td></tr>'
            )

        if not filas:
            filas.append('      <tr><td colspan="4">Carpeta vacía</td></tr>')

        arriba = "" if ruta_url in ("/", "") else '<a class="btn" href="../">⬆ Subir</a>'
        pagina = PLANTILLA.substitute(
            titulo=html.escape(titulo),
            ruta=html.escape(ruta_url),
            arriba=arriba,
            filas="\n".join(filas),
            resumen=f"{n_dir} carpetas · {n_arch} archivos",
            app=APP_NOMBRE,
            version=APP_VERSION,
        ).encode("utf-8", "surrogateescape")

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(pagina)))
        self.end_headers()
        return io.BytesIO(pagina)


# =============================================================================
#  SERVIDOR
# =============================================================================
class ServidorCompartir:
    def __init__(self, carpeta: str, puerto: int, bind: str, estado: Estado) -> None:
        # directory=... evita os.chdir(): no se toca el directorio global del proceso.
        manejador = partial(ManejadorCompartir, directory=carpeta, estado=estado)
        self.httpd = ThreadingHTTPServer((bind, puerto), manejador)
        self.httpd.daemon_threads = True
        self.hilo = threading.Thread(target=self.httpd.serve_forever,
                                     name="http-compartir", daemon=True)

    def iniciar(self) -> None:
        self.hilo.start()

    def detener(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.hilo.join(timeout=3)


# =============================================================================
#  CÓDIGO QR (dibujado sobre un Canvas; no requiere Pillow)
# =============================================================================
def dibujar_qr(canvas: tk.Canvas, texto: str, lado_max: int = 190) -> None:
    canvas.delete("all")
    if qrcode is None:
        canvas.configure(width=lado_max, height=lado_max)
        canvas.create_text(lado_max / 2, lado_max / 2, width=lado_max - 20,
                           justify="center", fill="#6b7280",
                           text="QR no disponible\n\npip install qrcode")
        return
    if not texto:
        canvas.configure(width=lado_max, height=lado_max)
        return

    qr = qrcode.QRCode(border=2, error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(texto)
    qr.make(fit=True)
    matriz = qr.get_matrix()
    n = len(matriz)
    escala = max(2, lado_max // n)
    lado = escala * n
    canvas.configure(width=lado, height=lado)
    canvas.create_rectangle(0, 0, lado, lado, fill="white", outline="")
    for y, fila in enumerate(matriz):
        x = 0
        while x < n:
            if fila[x]:
                inicio = x
                while x < n and fila[x]:
                    x += 1
                canvas.create_rectangle(inicio * escala, y * escala,
                                        x * escala, (y + 1) * escala,
                                        fill="black", outline="")
            else:
                x += 1


# =============================================================================
#  UTILIDADES DE SISTEMA
# =============================================================================
def abrir_en_explorador(ruta: str) -> None:
    try:
        if sys.platform.startswith("win"):
            os.startfile(ruta)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", ruta])
        else:
            subprocess.Popen(["xdg-open", ruta])
    except Exception:
        pass


def contar_elementos(carpeta: str) -> tuple[int, int]:
    """(archivos, carpetas) en el primer nivel."""
    archivos = carpetas = 0
    try:
        for e in os.scandir(carpeta):
            if e.name.startswith("."):
                continue
            if e.is_dir():
                carpetas += 1
            else:
                archivos += 1
    except OSError:
        pass
    return archivos, carpetas


def normalizar_soltado(datos: str) -> str | None:
    """Convierte lo que entrega tkinterdnd2 en una ruta de carpeta."""
    datos = datos.strip()
    if datos.startswith("{") and datos.endswith("}"):
        datos = datos[1:-1]
    ruta = datos.split("} {")[0].strip("{}").strip()
    if not ruta:
        return None
    if os.path.isfile(ruta):
        ruta = os.path.dirname(ruta)
    return ruta if os.path.isdir(ruta) else None


# =============================================================================
#  INTERFAZ GRÁFICA
# =============================================================================
class Aplicacion:
    def __init__(self, carpeta_inicial: str | None = None, puerto: int = PUERTO_DEFECTO) -> None:
        self.root = TkinterDnD.Tk() if DND_DISPONIBLE else tk.Tk()
        self.root.title(f"{APP_NOMBRE} v{APP_VERSION}")
        self.root.minsize(700, 560)

        self.estado = Estado()
        self.servidor: ServidorCompartir | None = None
        self.inicio_ts: float | None = None

        self.var_carpeta = tk.StringVar(value=carpeta_inicial or os.path.expanduser("~"))
        self.var_puerto = tk.StringVar(value=str(puerto))
        self.var_ip = tk.StringVar()
        self.var_direccion = tk.StringVar(value="—")
        self.var_solo_esta_ip = tk.BooleanVar(value=False)
        self.var_estado = tk.StringVar(value="Detenido")
        self.var_metricas = tk.StringVar(value="Conexiones: 0   ·   Descargas: 0   ·   Enviado: 0 B")
        self.var_contenido = tk.StringVar(value="—")

        self._poner_icono()
        self._construir()
        self._refrescar_ips(inicial=True)
        self._actualizar_contenido()
        self._bucle_eventos()
        self.root.protocol("WM_DELETE_WINDOW", self._cerrar)

    # ------------------------------------------------------------------ icono
    def _poner_icono(self) -> None:
        try:
            png = os.path.join(DIR_APP, "icon.png")
            if os.path.exists(png):
                self._img_icono = tk.PhotoImage(file=png)
                self.root.iconphoto(True, self._img_icono)
            ico = os.path.join(DIR_APP, "icon.ico")
            if sys.platform.startswith("win") and os.path.exists(ico):
                self.root.iconbitmap(ico)
        except Exception:
            pass

    # ---------------------------------------------------------------- widgets
    def _construir(self) -> None:
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(3, weight=1)
        pad = {"padx": 10, "pady": 6}

        # --- Carpeta -------------------------------------------------------
        f1 = ttk.LabelFrame(self.root, text="Carpeta a compartir")
        f1.grid(row=0, column=0, sticky="ew", **pad)
        f1.columnconfigure(0, weight=1)

        self.entrada_carpeta = ttk.Entry(f1, textvariable=self.var_carpeta)
        self.entrada_carpeta.grid(row=0, column=0, sticky="ew", padx=(8, 6), pady=8)
        ttk.Button(f1, text="Examinar…", command=self._elegir_carpeta).grid(row=0, column=1, pady=8)
        ttk.Button(f1, text="Abrir carpeta", command=lambda: abrir_en_explorador(self.var_carpeta.get())
                   ).grid(row=0, column=2, padx=(6, 8), pady=8)

        texto_dnd = ("Arrastra aquí una carpeta (o un archivo: se comparte su carpeta)"
                     if DND_DISPONIBLE else
                     "Arrastrar y soltar deshabilitado · pip install tkinterdnd2")
        self.zona_dnd = ttk.Label(f1, text=f"⤓  {texto_dnd}", anchor="center",
                                  relief="groove", padding=10)
        self.zona_dnd.grid(row=1, column=0, columnspan=3, sticky="ew", padx=8, pady=(0, 8))
        if DND_DISPONIBLE:
            for w in (self.zona_dnd, self.root):
                w.drop_target_register(DND_FILES)          # type: ignore[attr-defined]
                w.dnd_bind("<<Drop>>", self._al_soltar)    # type: ignore[attr-defined]

        self.lbl_contenido = ttk.Label(f1, textvariable=self.var_contenido, foreground="#555")
        self.lbl_contenido.grid(row=2, column=0, columnspan=3, sticky="w", padx=8, pady=(0, 8))

        # --- Red -----------------------------------------------------------
        f2 = ttk.LabelFrame(self.root, text="Red")
        f2.grid(row=1, column=0, sticky="ew", **pad)
        f2.columnconfigure(1, weight=1)

        ttk.Label(f2, text="Interfaz / IP:").grid(row=0, column=0, sticky="w", padx=8, pady=6)
        self.combo_ip = ttk.Combobox(f2, textvariable=self.var_ip, state="readonly")
        self.combo_ip.grid(row=0, column=1, sticky="ew", padx=6, pady=6)
        self.combo_ip.bind("<<ComboboxSelected>>", lambda _e: self._actualizar_direccion())
        ttk.Button(f2, text="Redetectar", command=self._refrescar_ips).grid(row=0, column=2, padx=(0, 8), pady=6)

        ttk.Label(f2, text="Puerto:").grid(row=1, column=0, sticky="w", padx=8, pady=6)
        marco_p = ttk.Frame(f2)
        marco_p.grid(row=1, column=1, columnspan=2, sticky="w", padx=6, pady=6)
        self.spin_puerto = ttk.Spinbox(marco_p, from_=1024, to=65535, width=8,
                                       textvariable=self.var_puerto,
                                       command=self._actualizar_direccion)
        self.spin_puerto.grid(row=0, column=0)
        self.spin_puerto.bind("<KeyRelease>", lambda _e: self._actualizar_direccion())
        ttk.Checkbutton(marco_p, text="Escuchar solo en esta IP",
                        variable=self.var_solo_esta_ip).grid(row=0, column=1, padx=12)

        # --- Dirección + QR ------------------------------------------------
        f3 = ttk.LabelFrame(self.root, text="Dirección de acceso")
        f3.grid(row=2, column=0, sticky="ew", **pad)
        f3.columnconfigure(0, weight=1)

        izq = ttk.Frame(f3)
        izq.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)
        izq.columnconfigure(0, weight=1)

        self.lbl_direccion = ttk.Label(izq, textvariable=self.var_direccion,
                                       font=("Consolas" if sys.platform.startswith("win")
                                             else "monospace", 15, "bold"),
                                       foreground="#1a56db")
        self.lbl_direccion.grid(row=0, column=0, sticky="w")

        botones = ttk.Frame(izq)
        botones.grid(row=1, column=0, sticky="w", pady=(10, 0))
        self.btn_compartir = ttk.Button(botones, text="▶ Compartir", command=self._alternar)
        self.btn_compartir.grid(row=0, column=0)
        self.btn_copiar = ttk.Button(botones, text="⧉ Copiar dirección", command=self._copiar)
        self.btn_copiar.grid(row=0, column=1, padx=6)
        self.btn_navegador = ttk.Button(botones, text="🌐 Abrir en navegador", command=self._abrir_navegador)
        self.btn_navegador.grid(row=0, column=2)

        self.lbl_estado = ttk.Label(izq, textvariable=self.var_estado, foreground="#b91c1c")
        self.lbl_estado.grid(row=2, column=0, sticky="w", pady=(10, 0))
        ttk.Label(izq, textvariable=self.var_metricas, foreground="#555").grid(row=3, column=0, sticky="w")

        self.canvas_qr = tk.Canvas(f3, width=190, height=190, highlightthickness=1,
                                   highlightbackground="#d3d7de", background="white")
        self.canvas_qr.grid(row=0, column=1, padx=(8, 12), pady=8)

        # --- Actividad -----------------------------------------------------
        f4 = ttk.LabelFrame(self.root, text="Actividad")
        f4.grid(row=3, column=0, sticky="nsew", **pad)
        f4.columnconfigure(0, weight=1)
        f4.rowconfigure(0, weight=1)

        self.txt_log = tk.Text(f4, height=8, wrap="none", state="disabled",
                               background="#0f1115", foreground="#d7dae0",
                               insertbackground="#d7dae0", relief="flat")
        self.txt_log.grid(row=0, column=0, sticky="nsew", padx=(8, 0), pady=8)
        barra = ttk.Scrollbar(f4, orient="vertical", command=self.txt_log.yview)
        barra.grid(row=0, column=1, sticky="ns", pady=8, padx=(0, 8))
        self.txt_log.configure(yscrollcommand=barra.set)
        ttk.Button(f4, text="Limpiar", command=self._limpiar_log).grid(
            row=1, column=0, columnspan=2, sticky="e", padx=8, pady=(0, 8))

        self._estado_botones(False)

    # ------------------------------------------------------------- acciones
    def _elegir_carpeta(self) -> None:
        inicial = self.var_carpeta.get() if os.path.isdir(self.var_carpeta.get()) else os.path.expanduser("~")
        elegida = filedialog.askdirectory(title="Selecciona la carpeta a compartir", initialdir=inicial)
        if elegida:
            self._fijar_carpeta(elegida)

    def _al_soltar(self, evento) -> None:
        ruta = normalizar_soltado(evento.data)
        if ruta:
            self._fijar_carpeta(ruta)
            self.estado.log(f"carpeta soltada: {ruta}")
        else:
            messagebox.showwarning(APP_NOMBRE, "No se reconoció una carpeta válida.")

    def _fijar_carpeta(self, ruta: str) -> None:
        ruta = os.path.abspath(ruta)
        if self.servidor:
            if not messagebox.askyesno(APP_NOMBRE, "El servidor está activo.\n¿Reiniciarlo con la nueva carpeta?"):
                return
            self.var_carpeta.set(ruta)
            self._detener()
            self._iniciar()
        else:
            self.var_carpeta.set(ruta)
        self.entrada_carpeta.xview_moveto(1.0)
        self._actualizar_contenido()

    def _refrescar_ips(self, inicial: bool = False) -> None:
        self.interfaces = detectar_interfaces()
        valores = [f"{ip}   ({etiqueta})" for ip, etiqueta in self.interfaces]
        self.combo_ip["values"] = valores
        if valores:
            actual = self.var_ip.get()
            if inicial or actual not in valores:
                self.var_ip.set(valores[0])
        if not inicial:
            self.estado.log(f"interfaces detectadas: {len(valores)}")
        self._actualizar_direccion()

    def _ip_elegida(self) -> str:
        texto = self.var_ip.get()
        return texto.split()[0] if texto else "127.0.0.1"

    def _puerto_elegido(self) -> int:
        try:
            return max(1, min(65535, int(self.var_puerto.get().strip())))
        except ValueError:
            return PUERTO_DEFECTO

    def _url(self) -> str:
        return f"http://{self._ip_elegida()}:{self._puerto_elegido()}/"

    def _actualizar_direccion(self) -> None:
        url = self._url()
        self.var_direccion.set(url)
        dibujar_qr(self.canvas_qr, url)

    def _actualizar_contenido(self) -> None:
        carpeta = self.var_carpeta.get()
        if os.path.isdir(carpeta):
            a, c = contar_elementos(carpeta)
            self.var_contenido.set(f"📦 Contenido en la raíz: {a} archivos · {c} carpetas")
        else:
            self.var_contenido.set("⚠ La ruta no existe o no es una carpeta")

    def _alternar(self) -> None:
        self._detener() if self.servidor else self._iniciar()

    def _iniciar(self) -> None:
        carpeta = os.path.abspath(self.var_carpeta.get())
        if not os.path.isdir(carpeta):
            messagebox.showerror(APP_NOMBRE, "Selecciona una carpeta válida.")
            return
        puerto = self._puerto_elegido()
        bind = self._ip_elegida() if self.var_solo_esta_ip.get() else "0.0.0.0"
        if not puerto_libre(puerto, bind):
            messagebox.showerror(APP_NOMBRE,
                                 f"El puerto {puerto} está ocupado.\nPrueba con otro (por ejemplo {puerto + 1}).")
            return
        try:
            self.servidor = ServidorCompartir(carpeta, puerto, bind, self.estado)
            self.servidor.iniciar()
        except OSError as e:
            self.servidor = None
            messagebox.showerror(APP_NOMBRE, f"No se pudo iniciar el servidor:\n{e}")
            return

        self.inicio_ts = time.time()
        self.var_estado.set(f"● Compartiendo «{os.path.basename(carpeta) or carpeta}» en {bind}:{puerto}")
        self.lbl_estado.configure(foreground="#15803d")
        self.btn_compartir.configure(text="■ Detener")
        self._estado_botones(True)
        self.estado.log(f"servidor iniciado en {bind}:{puerto} · carpeta: {carpeta}")
        self._actualizar_direccion()
        self._actualizar_contenido()

    def _detener(self) -> None:
        if not self.servidor:
            return
        try:
            self.servidor.detener()
        except Exception:
            pass
        self.servidor = None
        self.inicio_ts = None
        self.var_estado.set("Detenido")
        self.lbl_estado.configure(foreground="#b91c1c")
        self.btn_compartir.configure(text="▶ Compartir")
        self._estado_botones(False)
        self.estado.log("servidor detenido")

    def _estado_botones(self, activo: bool) -> None:
        estado = "normal" if activo else "disabled"
        self.btn_copiar.configure(state="normal")   # copiar siempre disponible
        self.btn_navegador.configure(state=estado)

    def _copiar(self) -> None:
        url = self._url()
        self.root.clipboard_clear()
        self.root.clipboard_append(url)
        self.root.update()
        self.estado.log(f"dirección copiada: {url}")
        self.btn_copiar.configure(text="✓ Copiado")
        self.root.after(1500, lambda: self.btn_copiar.configure(text="⧉ Copiar dirección"))

    def _abrir_navegador(self) -> None:
        webbrowser.open(self._url())

    def _limpiar_log(self) -> None:
        self.txt_log.configure(state="normal")
        self.txt_log.delete("1.0", "end")
        self.txt_log.configure(state="disabled")

    # ------------------------------------------------------- bucle periódico
    def _bucle_eventos(self) -> None:
        lineas = []
        for _ in range(200):
            try:
                lineas.append(self.estado.eventos.get_nowait())
            except Empty:
                break
        if lineas:
            self.txt_log.configure(state="normal")
            self.txt_log.insert("end", "\n".join(lineas) + "\n")
            # conserva las últimas 500 líneas
            total = int(self.txt_log.index("end-1c").split(".")[0])
            if total > 500:
                self.txt_log.delete("1.0", f"{total - 500}.0")
            self.txt_log.see("end")
            self.txt_log.configure(state="disabled")

        s = self.estado.snapshot()
        tiempo = ""
        if self.inicio_ts:
            seg = int(time.time() - self.inicio_ts)
            tiempo = f"   ·   Activo: {seg // 3600:02d}:{seg % 3600 // 60:02d}:{seg % 60:02d}"
        self.var_metricas.set(
            f"Conexiones activas: {s['conexiones']}   ·   Dispositivos: {s['clientes']}"
            f"   ·   Descargas: {s['descargas']}   ·   Enviado: {formato_bytes(s['bytes'])}{tiempo}"
        )
        self.root.after(400, self._bucle_eventos)

    def _cerrar(self) -> None:
        if self.servidor and not messagebox.askyesno(APP_NOMBRE, "El servidor está activo. ¿Salir y detenerlo?"):
            return
        self._detener()
        self.root.destroy()

    def ejecutar(self) -> None:
        self.root.mainloop()


# =============================================================================
#  ICONO (opcional, requiere Pillow)
# =============================================================================
def crear_icono(destino: str = DIR_APP) -> list[str]:
    from PIL import Image, ImageDraw  # import local: solo se usa aquí

    lado = 512
    img = Image.new("RGBA", (lado, lado), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([16, 16, lado - 16, lado - 16], radius=96, fill=(31, 41, 55, 255))
    # carpeta
    d.polygon([(96, 190), (200, 190), (232, 226), (416, 226), (416, 250), (96, 250)],
              fill=(250, 204, 21, 255))
    d.rounded_rectangle([96, 226, 416, 396], radius=18, fill=(253, 224, 71, 255))
    # ondas de red
    for r, w in ((54, 16), (96, 16), (138, 16)):
        d.arc([256 - r, 300 - r, 256 + r, 300 + r], start=205, end=335,
              fill=(37, 99, 235, 255), width=w)
    d.ellipse([246, 292, 266, 312], fill=(37, 99, 235, 255))

    generados = []
    png = os.path.join(destino, "icon.png")
    img.resize((256, 256), Image.LANCZOS).save(png)
    generados.append(png)
    ico = os.path.join(destino, "icon.ico")
    img.save(ico, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    generados.append(ico)
    return generados


# =============================================================================
#  MAIN
# =============================================================================
def main() -> None:
    ap = argparse.ArgumentParser(description=f"{APP_NOMBRE} v{APP_VERSION}")
    ap.add_argument("--dir", dest="carpeta", help="carpeta a compartir al iniciar")
    ap.add_argument("--port", dest="puerto", type=int, default=PUERTO_DEFECTO, help="puerto (por defecto 8000)")
    ap.add_argument("--crear-icono", action="store_true", help="genera icon.png e icon.ico y sale")
    args = ap.parse_args()

    if args.crear_icono:
        try:
            for ruta in crear_icono():
                print("Creado:", ruta)
        except ImportError:
            print("Se requiere Pillow:  pip install pillow")
        return

    Aplicacion(args.carpeta, args.puerto).ejecutar()


if __name__ == "__main__":
    main()
