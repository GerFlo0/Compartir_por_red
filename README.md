# Compartir por red — servidor HTTP con interfaz gráfica

Comparte una carpeta local por HTTP para que cualquier dispositivo de la misma red la abra desde el navegador (PC, celular, tablet). Windows y Linux.

## 1. Archivos

| Archivo | Función |
|---|---|
| `compartir_red.py` | Aplicación completa (interfaz + servidor). Único archivo necesario. |
| `icon.png` / `icon.ico` | Icono de la ventana y del ejecutable. Se cargan solos si están junto al `.py`. |

## 2. Requisitos

| Paquete | Obligatorio | Para qué |
|---|---|---|
| Python 3.9+ con tkinter | Sí | Interfaz. En Linux: `sudo apt install python3-tk` |
| `qrcode` | No | Código QR (`pip install qrcode`) |
| `tkinterdnd2` | No | Arrastrar y soltar (`pip install tkinterdnd2`) |
| `psutil` | No | Nombre real de cada interfaz de red (`pip install psutil`) |
| `pillow` | No | Solo para regenerar el icono (`pip install pillow`) |

Instalación recomendada de una sola vez:

```bash
pip install qrcode tkinterdnd2 psutil pillow
```

Sin las opcionales la app funciona igual: el QR se sustituye por un aviso y el arrastrar y soltar queda deshabilitado.

## 3. Uso

```bash
python compartir_red.py
python compartir_red.py --dir "C:/Users/yo/Documentos" --port 8080
python compartir_red.py --crear-icono     # regenera icon.png / icon.ico
```

1. Elige la carpeta (botón **Examinar…**, escribiendo la ruta, o arrastrándola a la zona punteada).
2. Elige la **IP** de la interfaz por la que se accederá y el **puerto**.
3. **▶ Compartir**.
4. Comparte la dirección con **⧉ Copiar dirección** o escaneando el **QR** desde el celular.

- **Escuchar solo en esta IP**: sin marcar, el servidor escucha en `0.0.0.0` (accesible por todas las interfaces); marcado, solo por la IP seleccionada. Selecciona `127.0.0.1` + esa casilla para una prueba local que nadie más ve.
- El panel **Actividad** registra cada petición, con IP del dispositivo, y los contadores muestran conexiones activas, dispositivos distintos, descargas y bytes enviados.

## 4. Lo que ve quien entra

Listado propio (responsive, modo oscuro automático) con:

| Acción | Cómo |
|---|---|
| Abrir archivo en el navegador | clic en el nombre |
| **Descargar archivo sin abrirlo** | botón `⬇` de la fila → `?dl=1` (`Content-Disposition: attachment`) |
| **Descargar una carpeta completa** | botón `⬇ .zip` de la fila → `?zip=1` |
| Descargar toda la carpeta compartida | botón `⬇ Descargar esta carpeta (.zip)` |
| Filtrar por nombre | campo de búsqueda |

El `.zip` se genera **al vuelo** con `Transfer-Encoding: chunked` y `ZIP_STORED`: no crea archivos temporales, no consume RAM proporcional al tamaño y la descarga empieza al instante (soporta >4 GB con Zip64). Los archivos y carpetas ocultos (`.algo`) se omiten tanto del listado como del zip.

## 5. Notas técnicas

- Usa `SimpleHTTPRequestHandler(directory=...)` mediante `functools.partial`: **nunca** se llama a `os.chdir()`, el directorio de trabajo del proceso queda intacto.
- `ThreadingHTTPServer` + `protocol_version = "HTTP/1.1"`: varias descargas simultáneas y conexiones persistentes (probado con 12 zips en paralelo).
- Rutas confinadas a la carpeta compartida (`translate_path` + verificación con `os.path.commonpath`); los intentos de `../` devuelven 404/403.
- El servidor corre en un hilo demonio; la interfaz se comunica con él por una `Queue` y un `root.after()`, sin tocar widgets desde otros hilos.

## 6. Convertirlo en aplicación con icono

### Windows (.exe)

```bat
pip install pyinstaller
pyinstaller --noconsole --onefile --icon icon.ico ^
  --add-data "icon.png;." --add-data "icon.ico;." ^
  --name "CompartirRed" compartir_red.py
```

El ejecutable queda en `dist\CompartirRed.exe`. Si usas `tkinterdnd2` añade `--collect-all tkinterdnd2`.

> Al primer arranque Windows Defender pedirá permiso de red: marca **Redes privadas** y acepta. Si no, ábrelo manualmente:
> `netsh advfirewall firewall add rule name="CompartirRed" dir=in action=allow protocol=TCP localport=8000`

### Linux (.desktop)

```bash
pip install pyinstaller
pyinstaller --noconsole --onefile --add-data "icon.png:." --name compartir-red compartir_red.py
mkdir -p ~/.local/share/icons && cp icon.png ~/.local/share/icons/compartir-red.png
```

`~/.local/share/applications/compartir-red.desktop`:

```ini
[Desktop Entry]
Type=Application
Name=Compartir por red
Comment=Comparte una carpeta por HTTP en la red local
Exec=/ruta/completa/a/dist/compartir-red
Icon=compartir-red
Terminal=false
Categories=Network;FileTransfer;Utility;
```

Luego: `update-desktop-database ~/.local/share/applications`

Si prefieres no empaquetar, usa `Exec=python3 /ruta/compartir_red.py`.

## 7. Seguridad

- **No hay autenticación**: cualquiera en la red con la dirección puede ver y descargar la carpeta. Úsalo en redes de confianza y detén el servidor al terminar.
- Es solo lectura: no permite subir, borrar ni modificar nada.
- No expongas el puerto a Internet (sin port forwarding).
- Comparte la carpeta más específica posible, no la raíz del disco ni tu perfil de usuario completo.
