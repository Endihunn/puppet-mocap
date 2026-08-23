# Puppet Mocap

Mocap de webcam para Blender: MediaPipe (pose + manos + cara) → armature Mixamo, en vivo y con grabación de tomas. Sin dependencias dentro de Blender — MediaPipe corre en un proceso Python externo que manda JSON por TCP local.

**Versión actual: 0.4.0** · Blender 4.4+ (probado en 5.1, Windows 11)

## Arquitectura

```
webcam → capture_runner.py (py externo: MediaPipe pose/hands/face, One Euro,
         hilo lector de cámara, inferencia paralela)
       → TCP 127.0.0.1:9878 (JSON por línea)
       → addon (server thread → queue → bpy.app.timers)
       → retarget (hips+spine+extremidades+pies / muñecas+dedos / blendshapes ARKit)
       → grabación: buffer en RAM → bake a fcurves al detener (slotted actions)
```

## Requisitos

- Blender 4.4+ (usa la API de slotted actions; en 5.x ya no existe `action.fcurves`).
- Python externo con `mediapipe` (≥ 0.10.0), `opencv-python`, `numpy` (`py -m pip install mediapipe opencv-python`). Verifícalo con el botón **Verificar dependencias** del panel.
- Rig Mixamo (prefijo `mixamorig:` o el que detecte **Detectar prefijo**).
- Modelos MediaPipe (`.task`): se descargan bajo demanda con el botón **Descargar modelos** del panel (pose + manos + cara). No van en el repo ni en el zip.

## Instalación

1. `py scripts/pack_addon.py` → genera `addon/puppet_mocap.zip` (reproducible).
2. Blender: Edit > Preferences > Add-ons > Install from Disk → el zip.
   (o headless: `blender --background --python scripts/install_addon.py -- --zip addon/puppet_mocap.zip`)

## Uso

Panel: `View3D > N > Puppet Mocap`.

1. Elige el **Rig** (o deja vacío = primer armature de la escena) y revisa el contador de huesos.
2. **Iniciar Captura** — abre la ventana de webcam. Q/ESC o la X la cierran.
3. (Opcional) **Calibrar postura**: párate derecho de frente y presiona; corrige la inclinación de la cámara y tu orientación neutral (~1.5 s de captura).
4. **Grabar** — cuenta regresiva (default 3 s), actúa, **Grabar** otra vez para detener. La toma se hornea a una action `PuppetTake` (desasignada del rig para no pisar la captura en vivo); actívala con el toggle **Reproducir toma**. Las tomas se conservan con fake user.
5. **Modo espejo**: el personaje se mueve como tu reflejo (natural para titerear de frente).

### Módulos

| Módulo | Qué mueve | Notas |
|---|---|---|
| Cuerpo | hips (con giro), spine ×3 con twist, brazos, piernas, pies, cuello, cabeza | gating por visibilidad (oclusión = mantiene pose) |
| Manos | muñeca + 15 falanges por lado | requiere el modelo de pose para anclar muñecas (se activa solo) |
| Cara | 52 blendshapes ARKit → shape keys | requiere `face_landmarker.task` y un mesh con shape keys ARKit |

## Solución de problemas

- **El rig no se mueve, frames suben**: prefijo de huesos — usa **Detectar prefijo**.
- **"Captura terminó: faltan dependencias..."**: botón **Verificar dependencias**; instala en ese Python.
- **Falta un modelo `.task`**: botón **Descargar modelos** (Diagnóstico).
- **Puerto ocupado**: cambia el puerto en Settings (otro Blender o una sesión anterior lo tiene).
- **Todo tiembla**: sube *Suavizado de rotación*; para lag, bájalo.
- **El personaje siempre inclinado**: **Calibrar postura** (la webcam ve desde abajo/arriba).
- Logs: `%TEMP%\puppet_mocap.log` (addon) y `%TEMP%\puppet_mocap_capture.log` (subprocess). El botón **Abrir Log** abre ambos.

## Desarrollo

```
py -m pytest tests/                # euro_filter (sin deps) + retarget math
```

Los tests de `retarget/common.py` necesitan `mathutils` (pip en Linux, o nativo
dentro de Blender). Lint: `py -m ruff check .` (config en `pyproject.toml`).

## Estructura

```
puppet_mocap/
  __init__.py          registro + cleanup total en unregister/load_pre
  blender_manifest.toml extensión Blender 4.2+ (convive con bl_info legacy)
  properties.py        settings (los tiempos de grabación viven en dicts Python:
                       RNA float es float32 y destroza epochs)
  operators.py         start/stop, REC con countdown, bake, calibración,
                       watchdog del subprocess, check de deps, descarga de modelos
  panel.py             UI
  server.py            TCP thread → queue (bind en el hilo del operador)
  log.py               log persistente con fsync throttled (addon + subprocess)
  retarget/
    common.py          arm-space, One Euro de cuaterniones, aim/orient,
                       calibración, espejo
    body.py            cuerpo con propagación de matrices frescas por cadena
    hands.py           palm basis + anti-flip + auto-swap por proximidad
    face.py            blendshapes ARKit (mesh y mapa de keys cacheados)
  capture/
    capture_runner.py  proceso externo (webcam + MediaPipe)
    euro_filter.py     One Euro escalar/vectorial
  models/              *.task descargados on demand (no commiteados)
scripts/
  pack_addon.py        zip reproducible del paquete
  install_addon.py     instalación headless
tests/                 pytest (euro_filter + lógica pura de retarget)
```

## Referencias

Técnicas adaptadas de: BlendArMocap (cgtinker) — twist de torso, euler-compat; Rokoko Studio Live — buffer→bake, offsets de pose de referencia; docs/issues de MediaPipe — visibilidad, timestamps monotónicos, manos sin suavizado interno, handedness con imagen espejada.
