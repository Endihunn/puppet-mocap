# Puppet Mocap

Mocap de webcam para Blender: MediaPipe (pose + manos + cara) → armature Mixamo, en vivo y con grabación de tomas. Sin dependencias dentro de Blender — MediaPipe corre en un proceso Python externo que manda JSON por TCP local.

**Versión actual: 0.3.1** · Blender 4.4+ (probado en 5.0.1, Windows 11)

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

- Blender 4.4+ (usa la API de slotted actions; en 5.0 ya no existe `action.fcurves`).
- Python externo con `mediapipe`, `opencv-python`, `numpy` (`py -m pip install mediapipe opencv-python`). Verifícalo con el botón **Verificar dependencias** del panel.
- Rig Mixamo (prefijo `mixamorig:` o el que detecte **Detectar prefijo**).

## Instalación

1. `py scripts/09_pack_addon.py` → genera `addon/puppet_mocap.zip`.
2. Blender: Edit > Preferences > Add-ons > Install from Disk → el zip.
   (o headless: `blender --background --python scripts/10_install_addon.py`)

## Uso

Panel: `View3D > N > Puppet Mocap`.

1. Elige el **Rig** (o deja vacío = primer armature de la escena) y revisa el contador de huesos.
2. **Iniciar Captura** — abre la ventana de webcam. Q/ESC o la X la cierran.
3. (Opcional) **Calibrar postura**: párate derecho de frente y presiona; corrige la inclinación de la cámara y tu orientación neutral (~1.5 s de captura).
4. **Grabar** — cuenta regresiva (default 3 s), actúa, **Grabar** otra vez para detener. La toma se hornea a una action `PuppetTake`; las tomas anteriores se conservan con fake user (Blender no las purga al guardar).
5. **Modo espejo**: el personaje se mueve como tu reflejo (natural para titerear de frente).

### Módulos

| Módulo | Qué mueve | Notas |
|---|---|---|
| Cuerpo | hips (con giro), spine ×3 con twist, brazos, piernas, pies, cuello, cabeza | gating por visibilidad (oclusión = mantiene pose) |
| Manos | muñeca + 15 falanges por lado | requiere el modelo de pose para anclar muñecas (se activa solo) |
| Cara | 52 blendshapes ARKit → shape keys | requiere `face_landmarker.task` (botón de descarga) y un mesh con shape keys ARKit |

## Solución de problemas

- **El rig no se mueve, frames suben**: prefijo de huesos — usa **Detectar prefijo**.
- **"Captura terminó: faltan dependencias..."**: botón **Verificar dependencias**; instala en ese Python.
- **Puerto ocupado**: cambia el puerto en Settings (otro Blender o una sesión anterior lo tiene).
- **Todo tiembla**: sube *Suavizado de rotación*; para lag, bájalo.
- **El personaje siempre inclinado**: **Calibrar postura** (la webcam ve desde abajo/arriba).
- Log: `%TEMP%\puppet_mocap.log` (botón Abrir Log; incluye stdout del proceso externo).

## Estructura

```
addon/puppet_mocap/
  __init__.py          registro + cleanup total en unregister/load_pre
  properties.py        settings (los tiempos de grabación viven en dicts Python:
                       RNA float es float32 y destroza epochs)
  operators.py         start/stop, REC con countdown, bake, calibración,
                       watchdog del subprocess, check de deps
  panel.py             UI
  server.py            TCP thread → queue (bind en el hilo del operador)
  log.py               log persistente con fsync throttled
  retarget/
    common.py          arm-space, One Euro de cuaterniones, aim/orient,
                       calibración, espejo
    body.py            cuerpo con propagación de matrices frescas por cadena
    hands.py           palm basis + anti-flip + auto-swap por proximidad
    face.py            blendshapes ARKit (mesh y mapa de keys cacheados)
  capture/
    capture_runner.py  proceso externo (webcam + MediaPipe)
    euro_filter.py     One Euro escalar/vectorial
  models/              *.task de MediaPipe
scripts/               empaquetado, instalación headless, pruebas viejas
```

## Referencias

Técnicas adaptadas de: BlendArMocap (cgtinker) — twist de torso, euler-compat; Rokoko Studio Live — buffer→bake, offsets de pose de referencia; docs/issues de MediaPipe — visibilidad, timestamps monotónicos, manos sin suavizado interno, handedness con imagen espejada.
