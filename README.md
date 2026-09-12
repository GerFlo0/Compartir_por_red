# Compartir por red — servidor HTTP con interfaz gráfica

Comparte una carpeta local por HTTP para que cualquier dispositivo de la misma red la abra desde el navegador (PC, celular, tablet). Windows y Linux.

## 1. Archivos

| Archivo | Función |
|---|---|
| `compartir_red.py` | Aplicación completa (interfaz + servidor). Único archivo necesario. |
| `icon.png` / `icon.ico` | Icono de la ventana y del ejecutable. Se cargan solos si están junto al `.py`. |
| `requirements.txt` | Dependencias opcionales, para instalar todas de una vez. |

## 2. Entorno virtual

Recomendado para no mezclar las dependencias del proyecto con las del sistema.

### Linux / macOS

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Si tkinter no está disponible dentro del venv (viene del sistema, no de pip):

```bash
sudo apt install python3-tk      # Debian/Ubuntu
sudo dnf install python3-tkinter # Fedora
```

### Windows

```bat
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

Para salir del entorno en cualquier plataforma: `deactivate`.

`requirements.txt`:

```
qrcode
tkinterdnd2
psutil
pillow
```

(`pillow` solo hace falta si vas a regenerar el icono con `--crear-icono`; el resto son opcionales para la app pero se recomiendan para tener QR, arrastrar y soltar, y detección completa de interfaces.)

## 3. Requisitos

| Paquete | Obligatorio | Para qué |
|---|---|---|
| Python 3.9+ con tkinter | Sí | Interfaz. En Linux: `sudo apt install python3-tk` |
| `qrcode` | No | Código QR (`pip install qrcode`) |
| `tkinterdnd2` | No | Arrastrar y soltar (`pip install tkinterdnd2`) |
| `psutil` | No | Nombre real de cada interfaz de red (`pip install psutil`) |
| `pillow` | No | Solo para regenerar el icono (`pip install pillow`) |

Sin las opcionales la app funciona igual: el QR se sustituye por un aviso y el arrastrar y soltar queda deshabilitado.

## 4. Uso

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
- **Permitir que quien se conecte también pueda subir archivos aquí**: desmarcada por defecto. Se puede prender o apagar en cualquier momento, incluso con el servidor ya corriendo — no hace falta reiniciarlo.
- El panel **Actividad** registra cada petición, con IP del dispositivo, y los contadores muestran conexiones activas, dispositivos distintos, descargas, bytes enviados, subidas recibidas y bytes recibidos.

## 5. Lo que ve quien entra

Listado propio (responsive, modo oscuro automático) con:

| Acción | Cómo |
|---|---|
| Abrir archivo en el navegador | clic en el nombre |
| **Descargar archivo sin abrirlo** | botón `⬇️` de la fila → `?dl=1` (`Content-Disposition: attachment`) |
| **Descargar una carpeta completa** | botón `⬇️ .zip` de la fila → `?zip=1` |
| Descargar toda la carpeta compartida | botón `⬇️ Descargar esta carpeta (.zip)` |
| Filtrar por nombre | campo de búsqueda |
| **Subir archivos** (si está habilitado) | botón **Seleccionar archivos**, o arrastrar y soltar sobre la zona punteada |

El `.zip` se genera **al vuelo** con `Transfer-Encoding: chunked` y `ZIP_STORED`: no crea archivos temporales, no consume RAM proporcional al tamaño y la descarga empieza al instante (soporta >4 GB con Zip64). Los archivos y carpetas ocultos (`.algo`) se omiten tanto del listado como del zip.

Cuando la subida está habilitada, la página permite seleccionar varios archivos a la vez (o arrastrarlos) y muestra, por cada uno, una barra de progreso, el porcentaje, la velocidad de transferencia y el tamaño enviado; al terminar toda la cola, la página se recarga sola para mostrar los archivos nuevos. Las subidas se reciben en bloques directo a disco (no se acumula el archivo completo en memoria), y quedan registradas en el panel **Actividad** con la IP de quien subió, por ejemplo:


11:32:05  192.168.1.25 → subió foto.jpg (4.2 MB)


Si dos personas suben un archivo con el mismo nombre, el segundo se renombra automáticamente (`foto (1).jpg`) en vez de sobrescribir el original. Cualquier intento de subir con un nombre que contenga `../` o rutas se sanea del lado del servidor antes de tocar el disco. 

## 6. Notas técnicas

- Usa `SimpleHTTPRequestHandler(directory=...)` mediante `functools.partial`: **nunca** se llama a `os.chdir()`, el directorio de trabajo del proceso queda intacto.
- `ThreadingHTTPServer` + `protocol_version = "HTTP/1.1"`: varias descargas simultáneas y conexiones persistentes (probado con 12 zips en paralelo).
- Rutas confinadas a la carpeta compartida (`translate_path` + verificación con `os.path.commonpath`); los intentos de `../` devuelven 404/403.
- El servidor corre en un hilo demonio; la interfaz se comunica con él por una `Queue` y un `root.after()`, sin tocar widgets desde otros hilos.

## 7. Convertirlo en ejecutable

PyInstaller empaqueta para el sistema **en el que lo ejecutas**: no se puede generar el `.exe` de Windows desde Linux ni viceversa. Corre el comando correspondiente en cada SO (activando primero el entorno virtual del paso 2).

### Windows (.exe)

```bat
venv\Scripts\activate
pip install pyinstaller

pyinstaller --noconfirm --onedir --noupx --windowed ^
  --name "CompartirRed" ^
  --icon icon.ico ^
  --add-data "icon.png;." --add-data "icon.ico;." ^
  --collect-all tkinterdnd2 ^
  --hidden-import qrcode ^
  compartir_red.py
```

El ejecutable queda en `dist\CompartirRed.exe`, listo para copiar a otra PC Windows **sin Python instalado**.

> Al primer arranque Windows Defender pedirá permiso de red: marca **Redes privadas** y acepta. Si no aparece el aviso:
> `netsh advfirewall firewall add rule name="CompartirRed" dir=in action=allow protocol=TCP localport=8000`

### Linux (binario + .desktop)

```bash
source venv/bin/activate
pip install pyinstaller

pyinstaller --noconfirm --onedir --noupx --windowed \
  --name CompartirRed \
  --icon icon.ico \
  --add-data "icon.png:." --add-data "icon.ico:." \
  --collect-all tkinterdnd2 \
  --hidden-import qrcode \
  compartir_red.py
```

El binario queda en `dist/CompartirRed` y corre en otra máquina Linux compatible **sin Python instalado** (solo necesita las librerías gráficas de Tk/X11 que trae cualquier distro con escritorio).

En Linux, un ejecutable (ELF) no puede llevar un icono incrustado como en Windows — por eso **siempre** se ve el icono genérico si abres el binario directo desde el gestor de archivos. Para que el icono correcto aparezca en el menú de aplicaciones y en la barra de tareas, instala un lanzador `.desktop`:

```bash
mkdir -p ~/.local/share/icons/hicolor/256x256/apps
cp icon.png ~/.local/share/icons/hicolor/256x256/apps/compartir-red.png
gtk-update-icon-cache -f ~/.local/share/icons/hicolor

mkdir -p ~/.local/share/applications
cat > ~/.local/share/applications/compartir-red.desktop <<'EOF'
[Desktop Entry]
Type=Application
Name=Compartir por red
Comment=Comparte una carpeta por HTTP en la red local
Exec=/ruta/completa/a/dist/CompartirRed
Icon=compartir-red
Terminal=false
Categories=Network;FileTransfer;Utility;
StartupWMClass=CompartirRed
EOF

chmod +x ~/.local/share/applications/compartir-red.desktop
update-desktop-database ~/.local/share/applications
```

Notas:

- `StartupWMClass=CompartirRed` hace que el icono también se vea en la barra de tareas / Alt-Tab, no solo en el menú.
- Si lo pones en el Escritorio y lo abres con doble clic, GNOME Files puede bloquearlo: clic derecho → **Permitir lanzamiento** (o `gio set archivo.desktop metadata::trusted true` + `chmod +x`).
- Si prefieres no empaquetar, usa `Exec=/ruta/al/venv/bin/python3 /ruta/compartir_red.py` en el `.desktop` en lugar del binario.
- Verifica la sintaxis del `.desktop` con `desktop-file-validate archivo.desktop` (paquete `desktop-file-utils`) si el icono no aparece y no sabes por qué.

## 8. Seguridad

- **No hay autenticación**: cualquiera en la red con la dirección puede ver y descargar la carpeta (y subir archivos, si lo habilitaste). Úsalo en redes de confianza y detén el servidor al terminar.
- Por defecto es solo lectura: la casilla de subida viene **desmarcada**. Solo permite subir, borrar o modificar nada si tú la activas explícitamente.
- Si habilitas la subida, no hay límite de tamaño por archivo ni revisión de contenido — alguien podría llenar el disco o subir algo no deseado. Actívala solo cuando la necesites y con gente de confianza.
- No expongas el puerto a Internet (sin port forwarding).
- Comparte la carpeta más específica posible, no la raíz del disco ni tu perfil de usuario completo.