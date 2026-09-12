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
import json
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
APP_VERSION = "1.1"
PUERTO_DEFECTO = 8000
DIR_APP = os.path.dirname(os.path.abspath(__file__))
RUTA_FAVICON_APP = os.path.join(DIR_APP, "icon.ico")
TAM_BLOQUE_SUBIDA = 65536

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
        self.subidas = 0           # archivos recibidos por subida
        self.bytes_recibidos = 0   # bytes recibidos por subida
        self.clientes: set[str] = set()
        self.eventos: "Queue[str]" = Queue()
        self._permitir_subidas = False   # togglable en vivo, sin reiniciar el servidor

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

    def sumar_subida(self, n_bytes: int) -> None:
        with self.lock:
            self.subidas += 1
            self.bytes_recibidos += n_bytes

    def permitir_subidas(self, valor: bool) -> None:
        with self.lock:
            self._permitir_subidas = bool(valor)

    def subidas_permitidas(self) -> bool:
        with self.lock:
            return self._permitir_subidas

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "conexiones": self.conexiones,
                "descargas": self.descargas,
                "bytes": self.bytes,
                "clientes": len(self.clientes),
                "subidas": self.subidas,
                "bytes_recibidos": self.bytes_recibidos,
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
  .zona-subida { border:2px dashed #9aa2af; border-radius:10px; padding:18px; text-align:center;
                 margin-bottom:14px; color:#6b7280; font-size:14px; transition:.15s; }
  .zona-subida.sobre { border-color:#2563eb; background:rgba(37,99,235,.06); color:#2563eb; }
  .zona-subida input[type=file] { display:none; }
  .zona-subida .btn { margin-top:8px; }
  .aviso-deshabilitado { color:#6b7280; font-size:13px; font-style:italic; margin-bottom:14px; }
  .cola-subidas { margin-bottom:16px; display:flex; flex-direction:column; gap:8px; }
  .item-subida { background:#fff; border:1px solid #eceef2; border-radius:8px; padding:10px 12px;
                 font-size:13px; box-shadow:0 1px 3px rgba(0,0,0,.06); }
  .item-subida .fila-superior { display:flex; justify-content:space-between; gap:10px;
                                 margin-bottom:6px; word-break:break-all; }
  .item-subida .barra { height:8px; background:#e5e7eb; border-radius:6px; overflow:hidden; }
  .item-subida .barra > div { height:100%; background:#2563eb; width:0%; transition:width .15s; }
  .item-subida .detalle { display:flex; justify-content:space-between; margin-top:6px;
                           color:#6b7280; font-size:12px; }
  .item-subida.completado .barra > div { background:#15803d; }
  .item-subida.completado .detalle { color:#15803d; }
  .item-subida.error .barra > div { background:#b91c1c; }
  .item-subida.error .detalle { color:#b91c1c; }
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
    .zona-subida { border-color:#3a4049; color:#9aa2af; }
    .zona-subida.sobre { background:rgba(37,99,235,.14); }
    .item-subida { background:#1c1f25; border-color:#2a2f37; box-shadow:none; }
    .item-subida .barra { background:#2a2f37; }
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
  $bloque_subidas
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

  var SUBIDAS_HABILITADAS = $subidas_habilitadas_js;
  var RUTA_ACTUAL = $ruta_actual_js;

  if (SUBIDAS_HABILITADAS) {
    var zona = document.getElementById('zonaSubida');
    var input = document.getElementById('inputArchivos');
    var cola = document.getElementById('colaSubidas');
    var btnSel = document.getElementById('btnSeleccionar');

    btnSel.addEventListener('click', function () { input.click(); });
    input.addEventListener('change', function () { encolar(input.files); input.value = ''; });

    ['dragenter', 'dragover'].forEach(function (ev) {
      zona.addEventListener(ev, function (e) { e.preventDefault(); zona.classList.add('sobre'); });
    });
    ['dragleave', 'drop'].forEach(function (ev) {
      zona.addEventListener(ev, function (e) { e.preventDefault(); zona.classList.remove('sobre'); });
    });
    zona.addEventListener('drop', function (e) {
      if (e.dataTransfer && e.dataTransfer.files) encolar(e.dataTransfer.files);
    });

    var pendientes = [];
    var subiendoAhora = false;
    var seSubioAlgo = false;

    function encolar(lista) {
      for (var i = 0; i < lista.length; i++) pendientes.push(lista[i]);
      procesarCola();
    }

    function formatoBytes(n) {
      var unidades = ['B', 'KB', 'MB', 'GB'];
      var i = 0;
      while (n >= 1024 && i < unidades.length - 1) { n /= 1024; i++; }
      return n.toFixed(i === 0 ? 0 : 1) + ' ' + unidades[i];
    }

    function procesarCola() {
      if (subiendoAhora) return;
      if (pendientes.length === 0) {
        if (seSubioAlgo) { seSubioAlgo = false; setTimeout(function () { location.reload(); }, 700); }
        return;
      }
      subiendoAhora = true;
      var archivo = pendientes.shift();
      subirArchivo(archivo, function () {
        subiendoAhora = false;
        procesarCola();
      });
    }

    function subirArchivo(archivo, alTerminar) {
      var item = document.createElement('div');
      item.className = 'item-subida';
      item.innerHTML =
        '<div class="fila-superior"><span class="nombre"></span><span class="porcentaje">0%</span></div>' +
        '<div class="barra"><div></div></div>' +
        '<div class="detalle"><span class="tamano"></span><span class="velocidad"></span></div>';
      item.querySelector('.nombre').textContent = archivo.name;
      item.querySelector('.tamano').textContent = '0 B / ' + formatoBytes(archivo.size);
      cola.insertBefore(item, cola.firstChild);

      var relleno = item.querySelector('.barra > div');
      var porcentajeEl = item.querySelector('.porcentaje');
      var tamanoEl = item.querySelector('.tamano');
      var velocidadEl = item.querySelector('.velocidad');

      var formulario = new FormData();
      formulario.append('archivo', archivo, archivo.name);

      var xhr = new XMLHttpRequest();
      var urlSubida = '?subir=1' + (RUTA_ACTUAL ? '&ruta=' + encodeURIComponent(RUTA_ACTUAL) : '');
      xhr.open('POST', urlSubida);

      var ultimoTiempo = Date.now();
      var ultimoCargado = 0;

      xhr.upload.addEventListener('progress', function (e) {
        if (!e.lengthComputable) return;
        var porcentaje = Math.round((e.loaded / e.total) * 100);
        relleno.style.width = porcentaje + '%';
        porcentajeEl.textContent = porcentaje + '%';
        tamanoEl.textContent = formatoBytes(e.loaded) + ' / ' + formatoBytes(e.total);

        var ahora = Date.now();
        var delta = (ahora - ultimoTiempo) / 1000;
        if (delta >= 0.3) {
          var velocidad = (e.loaded - ultimoCargado) / delta;
          velocidadEl.textContent = formatoBytes(velocidad) + '/s';
          ultimoTiempo = ahora;
          ultimoCargado = e.loaded;
        }
      });

      xhr.addEventListener('load', function () {
        if (xhr.status >= 200 && xhr.status < 300) {
          item.classList.add('completado');
          relleno.style.width = '100%';
          porcentajeEl.textContent = '✓ Subida completada';
          velocidadEl.textContent = '';
          seSubioAlgo = true;
        } else {
          item.classList.add('error');
          porcentajeEl.textContent = '✗ Error';
          try {
            var resp = JSON.parse(xhr.responseText);
            velocidadEl.textContent = resp.error || ('HTTP ' + xhr.status);
          } catch (err) {
            velocidadEl.textContent = 'HTTP ' + xhr.status;
          }
        }
        alTerminar();
      });

      xhr.addEventListener('error', function () {
        item.classList.add('error');
        porcentajeEl.textContent = '✗ Error de red';
        alTerminar();
      });

      xhr.send(formulario);
    }
  }
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

        if partes.path == "/favicon.ico":
            self._servir_favicon()
            return

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

    def _servir_favicon(self) -> None:
        try:
            with open(RUTA_FAVICON_APP, "rb") as f:
                datos = f.read()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "image/vnd.microsoft.icon")
            self.send_header("Content-Length", str(len(datos)))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            self.wfile.write(datos)
        except OSError:
            self.send_response(HTTPStatus.NO_CONTENT)
            self.send_header("Content-Length", "0")
            self.end_headers()

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

    # ---------- subida de archivos (POST) ----------
    def do_POST(self) -> None:
        if not (self.estado and self.estado.subidas_permitidas()):
            self.send_error(HTTPStatus.FORBIDDEN, "La subida de archivos está deshabilitada")
            return

        partes = urllib.parse.urlsplit(self.path)
        params = urllib.parse.parse_qs(partes.query)
        carpeta_destino = self._resolver_carpeta_destino(params.get("ruta", [""])[0])
        if carpeta_destino is None:
            self.send_error(HTTPStatus.FORBIDDEN, "Carpeta de destino no permitida")
            return

        ctype = self.headers.get("Content-Type", "")
        if not ctype.startswith("multipart/form-data"):
            self.send_error(HTTPStatus.BAD_REQUEST, "Se esperaba multipart/form-data")
            return
        boundary = self._extraer_boundary(ctype)
        if not boundary:
            self.send_error(HTTPStatus.BAD_REQUEST, "Falta boundary en Content-Type")
            return

        try:
            restante = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self.send_error(HTTPStatus.LENGTH_REQUIRED, "Falta Content-Length")
            return
        if restante <= 0:
            self.send_error(HTTPStatus.BAD_REQUEST, "Cuerpo de la petición vacío")
            return

        try:
            nombre_final, recibidos = self._recibir_archivo_multipart(
                boundary.encode(), restante, carpeta_destino
            )
        except (ValueError, ConnectionError, OSError) as e:
            self._responder_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(e)})
            return

        if self.estado:
            self.estado.sumar_subida(recibidos)
            self.estado.log(
                f"{self.client_address[0]} → subió {nombre_final} ({formato_bytes(recibidos)})"
            )
        self._responder_json(HTTPStatus.OK, {"ok": True, "nombre": nombre_final, "bytes": recibidos})

    def _resolver_carpeta_destino(self, ruta_rel: str) -> str | None:
        """Convierte el parámetro ?ruta= (subcarpeta que el navegador está viendo)
        en una ruta absoluta, verificando que no se salga de la carpeta compartida."""
        ruta_rel = urllib.parse.unquote(ruta_rel).lstrip("/\\")
        candidata = os.path.normpath(os.path.join(self.raiz, ruta_rel))
        if not self.dentro_de_raiz(candidata) or not os.path.isdir(candidata):
            return None
        return candidata

    @staticmethod
    def _extraer_boundary(content_type: str) -> str | None:
        for trozo in content_type.split(";"):
            trozo = trozo.strip()
            if trozo.startswith("boundary="):
                return trozo[len("boundary="):].strip('"')
        return None

    @staticmethod
    def _parsear_content_disposition(encabezados: bytes) -> dict:
        resultado: dict = {}
        for linea in encabezados.decode("utf-8", errors="replace").split("\r\n"):
            if not linea.lower().startswith("content-disposition:"):
                continue
            for trozo in linea.split(";")[1:]:
                trozo = trozo.strip()
                if "=" in trozo:
                    clave, _, valor = trozo.partition("=")
                    resultado[clave.strip().lower()] = valor.strip().strip('"')
        return resultado

    @staticmethod
    def _nombre_de_archivo_seguro(nombre: str, carpeta_destino: str) -> str:
        """Descarta cualquier componente de ruta del nombre (protección contra ../ y
        rutas absolutas) y evita colisiones renombrando en vez de sobrescribir."""
        nombre = os.path.basename(nombre.replace("\\", "/").strip())
        if not nombre or nombre in (".", ".."):
            nombre = "archivo_subido"
        elif nombre.startswith("."):
            nombre = "_" + nombre.lstrip(".") or "_archivo_subido"

        base, ext = os.path.splitext(nombre)
        candidato = nombre
        contador = 1
        while os.path.exists(os.path.join(carpeta_destino, candidato)):
            candidato = f"{base} ({contador}){ext}"
            contador += 1
        return candidato

    def _recibir_archivo_multipart(self, boundary: bytes, restante: int,
                                    carpeta_destino: str) -> tuple[str, int]:
        """Lee el cuerpo multipart/form-data DIRECTO desde el socket, en bloques,
        escribiendo el archivo a disco sin acumular su contenido completo en RAM."""
        delimitador_medio = b"\r\n--" + boundary

        bloque = self.rfile.read(min(restante, 8192))
        restante -= len(bloque)
        fin_encabezados = bloque.find(b"\r\n\r\n")
        intentos = 0
        while fin_encabezados == -1 and restante > 0 and intentos < 8:
            extra = self.rfile.read(min(restante, 8192))
            if not extra:
                break
            restante -= len(extra)
            bloque += extra
            fin_encabezados = bloque.find(b"\r\n\r\n")
            intentos += 1
        if fin_encabezados == -1:
            raise ValueError("no se pudieron leer los encabezados de la subida")

        disposicion = self._parsear_content_disposition(bloque[:fin_encabezados])
        nombre_original = disposicion.get("filename")
        if not nombre_original:
            raise ValueError("la subida no incluye un archivo (falta filename)")

        nombre_final = self._nombre_de_archivo_seguro(nombre_original, carpeta_destino)
        ruta_destino = os.path.join(carpeta_destino, nombre_final)
        if not self.dentro_de_raiz(ruta_destino):  # cinturón y tirantes
            raise ValueError("nombre de archivo no permitido")

        ruta_temporal = ruta_destino + ".subiendo"
        buffer = bloque[fin_encabezados + 4:]
        escritos = 0
        try:
            with open(ruta_temporal, "wb") as destino:
                while True:
                    idx = buffer.find(delimitador_medio)
                    if idx != -1:
                        destino.write(buffer[:idx])
                        escritos += idx
                        break
                    seguro = len(buffer) - (len(delimitador_medio) - 1)
                    if seguro > 0:
                        destino.write(buffer[:seguro])
                        escritos += seguro
                        buffer = buffer[seguro:]
                    if restante <= 0:
                        raise ValueError("la subida se cortó antes de terminar")
                    trozo = self.rfile.read(min(TAM_BLOQUE_SUBIDA, restante))
                    if not trozo:
                        raise ConnectionError("la conexión se cortó durante la subida")
                    restante -= len(trozo)
                    buffer += trozo
            os.replace(ruta_temporal, ruta_destino)
        except BaseException:
            try:
                os.remove(ruta_temporal)
            except OSError:
                pass
            raise

        # drena lo que quede del cuerpo (boundary final) para no desincronizar keep-alive
        while restante > 0:
            trozo = self.rfile.read(min(TAM_BLOQUE_SUBIDA, restante))
            if not trozo:
                break
            restante -= len(trozo)

        return nombre_final, escritos

    def _responder_json(self, codigo: HTTPStatus, datos: dict) -> None:
        cuerpo = json.dumps(datos).encode("utf-8")
        self.send_response(codigo)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(cuerpo)))
        self.end_headers()
        try:
            self.wfile.write(cuerpo)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

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

        subidas_habilitadas = bool(self.estado and self.estado.subidas_permitidas())
        if subidas_habilitadas:
            bloque_subidas = (
                '<div class="zona-subida" id="zonaSubida">'
                '<div>⬆ Arrastra archivos aquí, o</div>'
                '<button type="button" class="btn primario" id="btnSeleccionar">Seleccionar archivos</button>'
                '<input type="file" id="inputArchivos" multiple>'
                '</div>'
                '<div class="cola-subidas" id="colaSubidas"></div>'
            )
        else:
            bloque_subidas = ('<div class="aviso-deshabilitado">'
                               'El anfitrión no permite subir archivos a esta carpeta.</div>')

        pagina = PLANTILLA.substitute(
            titulo=html.escape(titulo),
            ruta=html.escape(ruta_url),
            arriba=arriba,
            filas="\n".join(filas),
            resumen=f"{n_dir} carpetas · {n_arch} archivos",
            app=APP_NOMBRE,
            version=APP_VERSION,
            bloque_subidas=bloque_subidas,
            subidas_habilitadas_js=("true" if subidas_habilitadas else "false"),
            ruta_actual_js=json.dumps(ruta_url),
        ).encode("utf-8", "surrogateescape")

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(pagina)))
        self.end_headers()
        return io.BytesIO(pagina)


# =============================================================================
#  SERVIDOR
# =============================================================================
class ServidorHTTPCerrable(ThreadingHTTPServer):
    """ThreadingHTTPServer que puede matar sus conexiones persistentes (keep-alive).

    httpd.shutdown() por sí solo NO alcanza: solo detiene el bucle que acepta
    conexiones NUEVAS. Los clientes que ya tenían una conexión HTTP/1.1 abierta
    (cualquier pestaña de navegador ya cargada) se quedan con su hilo vivo y
    siguen siendo atendidos indefinidamente. Por eso rastreamos cada socket
    aceptado y, al detener, los cerramos explícitamente por la fuerza.
    """

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._conexiones: set = set()
        self._lock_conexiones = threading.Lock()

    def process_request(self, request, client_address):
        with self._lock_conexiones:
            self._conexiones.add(request)
        super().process_request(request, client_address)

    def shutdown_request(self, request) -> None:
        with self._lock_conexiones:
            self._conexiones.discard(request)
        super().shutdown_request(request)

    def handle_error(self, request, client_address) -> None:
        # Cerrar sockets a la fuerza desde otro hilo genera ConnectionError/OSError
        # esperados en el hilo que atendía esa conexión: no son fallas reales.
        pass

    def cerrar_conexiones_activas(self) -> None:
        with self._lock_conexiones:
            conexiones = list(self._conexiones)
        for sock in conexiones:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass


class ServidorCompartir:
    def __init__(self, carpeta: str, puerto: int, bind: str, estado: Estado) -> None:
        # directory=... evita os.chdir(): no se toca el directorio global del proceso.
        manejador = partial(ManejadorCompartir, directory=carpeta, estado=estado)
        self.httpd = ServidorHTTPCerrable((bind, puerto), manejador)
        self.hilo = threading.Thread(target=self.httpd.serve_forever,
                                     name="http-compartir", daemon=True)

    def iniciar(self) -> None:
        self.hilo.start()

    def detener(self) -> None:
        self.httpd.shutdown()                    # deja de aceptar conexiones nuevas
        self.httpd.cerrar_conexiones_activas()    # corta las conexiones ya abiertas
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
        self.var_permitir_subidas = tk.BooleanVar(value=False)
        self.var_estado = tk.StringVar(value="Detenido")
        self.var_metricas = tk.StringVar(value="Conexiones: 0   ·   Descargas: 0   ·   Enviado: 0 B"
                                                "   ·   Subidas: 0   ·   Recibido: 0 B")
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

        self.chk_subidas = ttk.Checkbutton(
            f1, text="Permitir que quien se conecte también pueda subir archivos aquí",
            variable=self.var_permitir_subidas, command=self._cambiar_permitir_subidas)
        self.chk_subidas.grid(row=3, column=0, columnspan=3, sticky="w", padx=8, pady=(0, 8))

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

    def _cambiar_permitir_subidas(self) -> None:
        habilitado = self.var_permitir_subidas.get()
        self.estado.permitir_subidas(habilitado)
        self.estado.log("subida de archivos " + ("habilitada" if habilitado else "deshabilitada"))

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
            f"   ·   Descargas: {s['descargas']}   ·   Enviado: {formato_bytes(s['bytes'])}"
            f"   ·   Subidas: {s['subidas']}   ·   Recibido: {formato_bytes(s['bytes_recibidos'])}{tiempo}"
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
