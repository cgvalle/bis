# BIS Monitor – Adquisición de EEG Crudo

Script para adquirir señal de EEG crudo desde un monitor BIS de Aspect Medical (protocolo binario, sección 5).

---

## Requisitos

- Python 3.10 o superior
- Dependencias:

```bash
pip install pyserial matplotlib numpy
```

---

## Conexión

Conecta el monitor BIS al computador mediante el puerto serial (USB-Serial). Verifica el nombre del puerto:

- **macOS/Linux:** `/dev/tty.usbserial-XXXXXXXX` o `/dev/ttyUSB0`
- **Windows:** `COM3`, `COM4`, etc.

Configuración del puerto: **57600 baudios, 8N1, sin control de flujo**.

---

## Uso

### Detección automática del puerto

Si omites `--port`, el script escanea los puertos disponibles automáticamente:

- Si encuentra **un solo puerto**, lo selecciona sin preguntar.
- Si encuentra **varios puertos**, muestra una lista y te pide elegir.

```bash
python bis.py
```

### Grabación indefinida (recomendado)

Graba hasta que presiones **Ctrl+C** para detener:

```bash
python bis.py --port /dev/tty.usbserial-BG00TDBX
```

Al presionar Ctrl+C, el script detiene la adquisición, guarda el CSV y cierra la conexión automáticamente.

### Grabación con duración fija

```bash
python bis.py --port /dev/tty.usbserial-BG00TDBX --duration 60
```

---

## Opciones

| Opción | Descripción | Valor por defecto |
|---|---|---|
| `--port` | Puerto serial del monitor | auto-detectado |
| `--duration` | Duración en segundos (omitir = indefinido) | indefinido |
| `--rate` | Frecuencia de muestreo: `128` o `256` sps | `128` |
| `--output` | Nombre del archivo CSV de salida | auto (`bis_raw_eeg_YYYYMMDD_HHMMSS.csv`) |
| `--quiet` | Suprime la salida de paquetes en consola | desactivado |
| `--plot` | Muestra gráfica de EEG al terminar | desactivado |
| `--plot-output` | Guarda la gráfica en un archivo PNG/PDF | ninguno |

---

## Ejemplos

**Grabar indefinidamente a 128 sps y guardar en archivo específico:**

```bash
python bis.py --port /dev/ttyUSB0 --output mi_registro.csv
```

**Grabar 5 minutos a 256 sps y mostrar gráfica al terminar:**

```bash
python bis.py --port /dev/ttyUSB0 --duration 300 --rate 256 --plot
```

**Grabar sin mostrar datos en consola y guardar gráfica:**

```bash
python bis.py --port /dev/ttyUSB0 --quiet --plot-output resultado.png
```

---

## Archivo de salida (CSV)

El CSV generado contiene una fila por paquete recibido (8 paquetes/segundo). Las columnas son:

- `timestamp_s` – tiempo en segundos desde el inicio de la grabación
- `num_channels` – número de canales (2 o 4)
- `sample_rate` – frecuencia de muestreo (128 o 256 sps)
- `ch1_s0`, `ch1_s1`, … – muestras del canal 1
- `ch2_s0`, `ch2_s1`, … – muestras del canal 2 (y siguientes si aplica)

---

## Notas

- El monitor VISTA solo soporta 128 sps. El A-2000 soporta 128 y 256 sps.
- Si el puerto no es reconocido, verifica que el driver USB-Serial esté instalado.
- En macOS puede ser necesario otorgar permisos de acceso al puerto en *Preferencias del Sistema → Seguridad y Privacidad*.
