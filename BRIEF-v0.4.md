# Brief de ejecución — Puppet Mocap v0.3.1 → v0.4

Auditoría de código completa (16 archivos, ~3 970 líneas). El zip `puppet_mocap-v0.3.1.zip`
es byte-idéntico al árbol de trabajo (SHA-256 de los 16 miembros) — no hay deriva zip/fuente.

Ordenado por severidad. Cada punto trae ubicación, síntoma observable y fix propuesto.
**No ejecutar nada de esto sin cerrar primero P0-0 (repo bajo control de versiones).**

---

## P0 — Bugs que rompen funcionalidad

### P0-0. La fuente no está versionada; solo el zip

`git ls-files` devuelve `.gitignore`, `README.md` y `puppet_mocap-v0.3.1.zip`. El directorio
`puppet_mocap/` está **untracked** (`?? puppet_mocap/`). El único commit del repo mete un blob
binario de 10.8 MB y ningún `.py`.

Consecuencia: no hay historia, no hay diff, no hay rollback. Cualquier refactor de este brief
es irreversible hoy.

**Fix (primer paso, antes que todo lo demás):**

1. `git add puppet_mocap/` — pero decidir antes qué hacer con `models/*.task` (13.5 MB, ver P3-2).
2. `git rm --cached puppet_mocap-v0.3.1.zip` y añadir `*.zip` al `.gitignore`; los releases
   se publican como artefactos, no como commits.
3. Commit base "import v0.3.1 source" ANTES de tocar una línea.

---

### P0-1. La action horneada pisa la captura en vivo

`operators.py:_bake_channels` (~línea 180) hace `ad.action = action` al terminar cada toma.
A partir de ese momento el armature tiene una action asignada con fcurves sobre
`pose.bones[...].rotation_quaternion`.

El sistema de animación de Blender re-evalúa esas fcurves en cada update del depsgraph y
sobrescribe los valores que `retarget.apply_pose` acaba de escribir en las pose bones. Además
`operators.py:_drain_tick` hace `scene.frame_current = last_frame` en cada mensaje, que es
justamente un disparador de evaluación de animación.

**Síntoma esperado:** la primera toma se graba bien; a partir de la segunda sesión de preview
en vivo el rig "rebota" a la pose horneada o se queda pegado, y el usuario cree que la captura
dejó de funcionar. `Borrar Keyframes` lo "arregla" — pista de que es esto.

**Verificar primero (repro mínimo, 5 min):** grabar una toma, dejar la captura corriendo, mover
el cuerpo. Si el rig no responde o parpadea → confirmado.

**Fix propuesto:** tras hornear, desasignar la action del slot en vivo conservándola en el
archivo:

```python
action.use_fake_user = True
ad.action = None
```

y exponer en el panel un toggle explícito "Reproducir toma" que la vuelva a asignar. Alternativa
menos invasiva: influencia 0 / mute del NLA mientras `server.is_running()`.
Aplica igual al bake de shape keys (`PuppetTake_cara`, mismo mecanismo sobre `sk.value`).

---

### P0-2. Carrera entre el hilo de cámara y `cap.release()`

`capture/capture_runner.py:532-534` — el bloque `finally` hace:

```python
reader.stop()      # solo baja un flag
cap.release()      # libera el VideoCapture
```

`CameraReader.run` puede estar bloqueado dentro de `self.cap.read()` (línea 86) cuando se
libera el objeto por debajo. Es un uso-después-de-liberar en el lado C++ de OpenCV: crash duro
o cuelgue del proceso al cerrar, no excepción de Python.

**Síntoma:** el proceso de captura a veces no muere limpio al cerrar la ventana; la webcam
queda tomada y el siguiente `Iniciar Captura` falla con rc=2.

**Fix:** `reader.stop(); reader.join(timeout=2.0)` antes de `cap.release()`.

---

### P0-3. `SO_REUSEADDR` tiene semántica de secuestro en Windows

`server.py:115` — `sock.setsockopt(SOL_SOCKET, SO_REUSEADDR, 1)`.

En Windows (a diferencia de POSIX) `SO_REUSEADDR` permite hacer bind sobre un puerto que otro
socket está escuchando **activamente**; la entrega de conexiones entre los dos sockets queda
indefinida. Combinado con `server.stop()` (línea 142), que pone `_state["thread"] = None`
aunque el `join(timeout=2.0)` haya expirado, se puede llegar a dos servers vivos en el 9878:
el nuevo hace bind sin error, el subprocess se conecta a cualquiera de los dos y las poses se
pierden en el hilo zombi.

**Fix:** en `sys.platform == "win32"` no poner `SO_REUSEADDR`; opcionalmente poner
`SO_EXCLUSIVEADDRUSE`. Y que `stop()` reporte si el join expiró en vez de limpiar el estado a
ciegas — si el hilo sigue vivo, `is_running()` debe seguir devolviendo True.

---

### P0-4. Puntero stale al armature tras File > Open / undo

`retarget/common.py:_state["arm"]` guarda el objeto bpy directamente (línea 47, `_cache_arm`).
`operators._on_load_pre` llama a `_cleanup_capture`, que **no toca `retarget.common._state`**.

`get_armature()` intenta protegerse con `except ReferenceError`, pero eso solo cubre el caso en
que Blender ya invalidó el wrapper de Python. Tras un undo o un File>Open, los ID datablocks se
re-asignan sin invalidar necesariamente el wrapper → acceso a memoria liberada → crash de
Blender, no excepción.

Nótese que `face.py` ya usa el patrón correcto (`_state["face_mesh_name"]`: guarda el nombre y
resuelve con `bpy.data.objects.get`).

**Fix:** que `_state["arm"]` guarde solo el nombre, igual que el mesh facial. `bpy.data.objects.get()`
es una búsqueda en hash, es barata incluso a 50 Hz. Y añadir `reset_smoothing()` +
`_state["arm"] = None` dentro del handler `load_pre`.

---

### P0-5. `data_path` sin escapar rompe con nombres con comillas

`operators.py:_bake_take`:

```python
path = f'pose.bones["{name}"].rotation_quaternion'
(f'key_blocks["{name}"].value', 0, None)
```

Un hueso o shape key cuyo nombre contenga `"` o `\` produce un data_path inválido y la fcurve se
crea apuntando a la nada (o revienta). Los nombres de shape key los edita el usuario a mano, así
que el caso del `key_blocks` es plausible, no teórico.

**Fix:** `bpy.utils.escape_identifier(name)` en ambos.

---

## P1 — Correctitud y rendimiento

### P1-1. El panel escanea toda la escena en cada redraw cuando la cara está activa

`panel.py:121` llama a `face_mod.get_cached_mesh(get_armature())` en `draw()`. Si **no** encuentra
mesh, `get_cached_mesh` no cachea el fallo (`face.py:106-110`: solo guarda el nombre en caso de
éxito) → `find_face_mesh` recorre `bpy.data.objects` completo, mirando `shape_keys` y comparando
contra los 52 nombres ARKit, **en cada redibujo**. Y `panel.py:125` lo repite vía
`shape_key_names_in_mesh`.

Durante la captura el panel se redibuja continuamente (`_tag_redraw_ui`, ver P1-2), así que en
una escena pesada esto es un escaneo completo a ~50 Hz.

**Fix:** cachear también el resultado negativo con TTL (reusar `_fs_cache` del panel, que ya tiene
ventana de 2 s), y que `draw()` haga una sola llamada reutilizando el objeto devuelto.

### P1-2. Redraw y escritura RNA incondicionales a 50 Hz

`operators.py:_drain_tick` retorna `0.02` y llama a `_tag_redraw_ui()` en cada tick, además de
escribir `props.frames_received` (escritura RNA sobre la escena → tagging del depsgraph) aunque
la UI no haya cambiado nada visible.

**Fix:** desacoplar. Aplicar poses a 50 Hz; refrescar contadores + `tag_redraw` a ~10 Hz con un
acumulador de tiempo. Escribir `frames_received` en el mismo pulso lento.

### P1-3. El addon y el subprocess escriben al MISMO archivo de log

`operators.py` abre `log.get_log_path()` en modo `"ab"` y se lo pasa como stdout/stderr al
subprocess, mientras el `_FlushingFileHandler` de `log.py` tiene el mismo archivo abierto.

Dos handles independientes con buffering distinto → líneas entrelazadas a media línea. Y
`PUPPET_OT_clear_log` trunca el archivo mientras el subprocess mantiene su handle.

**Fix:** `puppet_mocap_capture.log` separado para el subprocess. El botón "Abrir Log" puede
ofrecer los dos, o concatenarlos por timestamp al abrir.

### P1-4. `StreamHandler(sys.stdout)` con Blender sin consola

`log.py:96`. En Blender GUI en Windows `sys.stdout` puede ser `None`; `logging.StreamHandler(None)`
cae a `sys.stderr`, que también puede ser `None` → `AttributeError` dentro de `emit` en cada
registro (lo absorbe el manejo de errores de logging, pero ensucia y cuesta).

**Fix:** añadir el StreamHandler solo si `sys.stdout is not None`.

### P1-5. El auto-swap desactiva en silencio la corrección de roll del antebrazo

`hands.py:270` — la condición para llamar a `_orient_forearm_from_palm` es
`data_side == bone_side`. El auto-swap correctivo (líneas 371-377) intercambia `l_data_side` y
`r_data_side`, con lo que la igualdad deja de cumplirse y el fix de roll se apaga sin aviso: la
muñeca vuelve a tener roll indeterminado justo en el escenario más frágil (manos cruzadas,
aplauso).

La condición mezcla dos cosas distintas: "¿de qué lado físico vino el dato?" y "¿el usuario
activó el swap manual?".

**Fix:** separar. La corrección de roll depende de que los landmarks del cuerpo correspondan al
mismo brazo al que se está aplicando; eso se resuelve mirando qué muñeca del cuerpo se usó, no
comparando `data_side` con `bone_side`.

### P1-6. `fix_orientation` se comporta distinto la primera vez que las siguientes

`common.py:fix_orientation` usa el flag persistente `PUPPET_INIT_FLAG` guardado como custom
property **en el objeto** → sobrevive al guardado del .blend. La primera captura sobre un rig
resetea todas las pose bones a identidad (destruyendo la pose del usuario, sin undo push); las
siguientes no.

**Fix:** decidir una semántica y documentarla. Recomendado: el arranque normal nunca pisa la
pose; el reseteo vive solo en el botón "Reset Rig" (que ya pasa `force=True`).

### P1-7. `server.stop()` limpia el estado aunque el join expire

Ver P0-3. Mismo sitio (`server.py:136-150`), consecuencia distinta: `is_running()` miente.

---

## P2 — Calidad de la captura

### P2-1. Doble filtrado de la cara (lag acumulado sin ganancia)

Los blendshapes pasan por One Euro **dos veces**: en `capture_runner.smooth_blendshape`
(línea 377) y otra vez en `face.apply` vía `common.smooth_scalar` (línea 158). Dos pasadas
seguidas con el mismo min_cutoff duplican la latencia efectiva y no duplican el suavizado útil.

Los landmarks de cuerpo/manos tienen un caso análogo pero defendible (posición en capture,
cuaternión en el addon: dominios distintos). La cara no: es el mismo escalar filtrado dos veces.

**Fix:** quitar el filtrado de cara del `capture_runner` y dejar solo `smooth_scalar`, que sí
responde al slider `rotation_smooth` de la UI.

### P2-2. Los landmarks de mano se filtran en espacio absoluto

`capture_runner.py:475` — `hand_to_absolute` suma la muñeca del pose ANTES del One Euro
(líneas 167-174 y 475). El cutoff adaptativo del One Euro reacciona a la velocidad de la señal:
al mover el brazo rápido, la velocidad de los 21 puntos se dispara por el desplazamiento del
brazo, el filtro abre el cutoff y **deja de suavizar los dedos** justo cuando más ruido tiene
MediaPipe.

**Fix:** filtrar en espacio relativo a la muñeca y sumar la posición del pose después. El
retarget solo usa vectores internos de la mano, así que es un cambio contenido.

### P2-3. La resolución de grabación está capada por `send_fps`

`send_fps` por defecto 20; `rec_fps` (o los FPS de escena) por defecto 30. `_bake_take` mapea
`frame = start + round(elapsed * fps)`, así que a 20 muestras/s sobre una línea de 30 fps quedan
~10 frames por segundo sin key (los rellena la interpolación LINEAR, aceptable) — pero si el
usuario sube `send_fps` por encima de los fps de escena, `frame_map[f] = ...` **descarta**
muestras en silencio (colisión → gana la última).

**Fix:** avisar en el panel cuando `send_fps > fps_efectivos` ("se descartarán muestras") o
promediar las muestras que caen en el mismo frame en lugar de quedarse con la última.

### P2-4. Sin traslación: el personaje nunca se desplaza

Solo se escribe `rotation_quaternion`; `Hips` nunca recibe `location`. El personaje gira e
inclina pero está clavado en el origen. Para una app de titiritero esta es la limitación
funcional más visible.

**Fix (feature, no bug):** derivar el desplazamiento del mid-hip en arm-space, escalarlo por
la altura del rig respecto a la altura observada del torso, y escribirlo como `Hips.location`
con su propio suavizado y un toggle en la UI ("Traslación de raíz"). Requiere añadir el canal
de location a `snapshot_pose` y al bake.

### P2-5. Interpolación LINEAR sobre componentes de cuaternión

`_bake_channels` fuerza `interpolation = 1` (LINEAR) en las 4 componentes. Interpolar
linealmente componentes de cuaternión no es slerp: entre keys separados el eje de rotación
"se acorta" ligeramente. Con keys densos (20-30/s) el error es invisible; si se implementa
decimado de keys, deja de serlo.

**Fix:** ninguno urgente. Anotarlo y revisitarlo si se añade decimado.

---

## P3 — Repo, empaquetado y entrega

### P3-1. El README documenta `scripts/` que no existe

README dice `py scripts/09_pack_addon.py` y `blender --background --python scripts/10_install_addon.py`,
y la sección "Estructura" lista `scripts/ empaquetado, instalación headless, pruebas viejas`.
No hay directorio `scripts/` en el repo. Las instrucciones de instalación son inejecutables.

**Fix:** recrear `scripts/pack_addon.py` (zip reproducible del paquete) o reescribir el README
para el flujo real. La primera opción es mejor: el zip debe ser generable, no un blob commiteado.

### P3-2. 13.5 MB de modelos en el árbol, con criterio inconsistente

`pose_landmarker_lite.task` (5.8 MB) y `hand_landmarker.task` (7.8 MB) van dentro del addon;
`face_landmarker.task` se descarga bajo demanda con un operador que ya maneja bien el caso
(timeout, `.tmp`, validación de tamaño, `os.replace` atómico).

**Fix:** unificar. Extender `PUPPET_OT_download_face_model` a un operador genérico de descarga
de modelos y sacar los tres `.task` del control de versiones. Baja el zip de 10.8 MB a ~60 KB
y hace el repo clonable sin LFS.

### P3-3. `bl_info` legacy vs extensiones de Blender 4.2+

El addon se declara con `bl_info` (formato legacy). Desde 4.2 el formato de primera clase es
`blender_manifest.toml` (extensión). Sigue funcionando vía "Install from Disk", pero es deuda
que vence sola en alguna versión futura.

**Fix:** añadir `blender_manifest.toml` en paralelo. Bajo riesgo; hacerlo junto con P3-1.

### P3-4. Sin tests ni lint

Cero tests. Hay lógica pura y testeable sin `bpy`:

- `capture/euro_filter.py` — completamente independiente.
- `retarget/common.py` — `compute_calibration`, `mirror_pose_landmarks`, `mp_to_arm`,
  `calibrate_pose_landmarks`, `QuatOneEuro`. Solo necesita `mathutils`, que se instala por pip.

**Fix:** `tests/` con pytest cubriendo: identidad de calibración (pose neutral perfecta → R≈I),
round-trip `mp_to_arm` / `calibrate_pose_landmarks`, doble espejo = identidad, y el gate de
outlier de `QuatOneEuro`. Añadir `ruff` con config mínima.

### P3-5. `check_deps` no valida versión de MediaPipe

`PUPPET_OT_check_deps` reporta las versiones pero no las compara contra un mínimo. La Tasks API
(`hand_world_landmarks`, `output_face_blendshapes`) tiene deriva entre versiones.

**Fix:** fijar un mínimo conocido-bueno y marcarlo en rojo por debajo de él.

---

## P4 — Limpieza

- **`hands.py:173-178`** — el bloque `[palmaV3]` loguea cada 1.5 s **por lado** durante toda la
  sesión (con flush, y fsync hasta 1/s). Es instrumentación de depuración en código de producción.
  Ponerlo tras una propiedad `debug_hands` apagada por defecto.
- **`hands.py:238-243`** — el docstring de `data_side` describe el comportamiento **anterior**
  ("determina el signo del cross-product en `_palm_basis`"). Desde el fix de handedness,
  `_palm_basis` decide el signo por `handedness` (líneas 118-127) y `side` solo se usa como clave
  del cache de histéresis. Corregir el docstring o renombrar el parámetro a `hysteresis_key`.
- **`capture_runner.py:377-384`** — `smooth_blendshape` instancia un `OneEuroVec(1)` (= 3 filtros
  escalares, 2 desperdiciados) por blendshape. Usar `OneEuro` directamente. Se cae solo si se
  aplica P2-1.
- **`common.py:151-153`** — el loop de construcción de `_MIRROR_IDX` deja `_a` y `_b` en el
  namespace del módulo. Envolverlo en una función.
- **`retarget/__init__.py:snapshot_pose`** — re-lee ~40 `rotation_quaternion` vía RNA justo
  después de haberlos escrito. `apply_pose` ya conoce los cuaterniones que asignó; devolverlos
  ahorra el round-trip en cada muestra grabada.
- **`operators.py:_record_state["samples"]`** — sin cota. Una toma larga crece sin límite en RAM
  y sin aviso. Añadir un tope configurable con warning en el panel.
- **`__init__.py:register()`** — sin manejo de errores; un fallo a media iteración deja Blender
  con registro parcial. Envolver y desregistrar lo ya hecho.

---

## Orden de ejecución sugerido

| Fase | Contenido | Criterio de cierre |
|---|---|---|
| **0** | P0-0 (repo bajo git) | `git log` muestra la fuente; el zip fuera del índice |
| **1** | P0-1 … P0-5 | Repro de P0-1 confirmado y arreglado; captura en vivo funciona tras hornear 2 tomas seguidas |
| **2** | P1-1 … P1-7 | Perfilado del panel: sin escaneo de escena por redraw; logs separados |
| **3** | P3-1, P3-2, P3-4 | `scripts/pack_addon.py` genera el zip; modelos fuera del repo; pytest en verde |
| **4** | P2-1, P2-2, P2-3 | Comparativa A/B de latencia facial y jitter de dedos |
| **5** | P2-4 (traslación de raíz) | Feature nueva, detrás de toggle, con su propia validación |
| **6** | P4 | Limpieza |

Las fases 1 y 2 son independientes entre archivos y se pueden paralelizar entre ejecutores;
la fase 0 es bloqueante para todas.

---

## Cosas que verificar antes de arreglar (no asumir)

1. **P0-1** — repro de 5 min descrito arriba. Si el rig SÍ responde tras hornear, el diagnóstico
   está mal y hay que re-analizar el flujo del depsgraph antes de tocar `_bake_channels`.
2. **API de slotted actions** — `cb.groups.new()` sobre un `ActionChannelbag` en la versión
   concreta de Blender que usas (5.0.1 según el README). Verificar en consola antes de refactorizar
   el bake.
3. **P0-3** — confirmar con `netstat -ano | findstr 9878` que efectivamente se puede levantar un
   segundo listener en el mismo puerto en esta máquina.

---

## Lo que está bien y NO hay que tocar

Para que ningún ejecutor "mejore" algo que ya se ganó a pulso — estos puntos tienen comentarios
en el código explicando por qué son así:

- Epochs en `dict` de Python en vez de `FloatProperty` (RNA es float32; a ~1.78e9 el ULP es 128 s).
- Ausencia de `view_layer.update()` en `apply_pose` (fuente confirmada de crashes en Blender 5.x).
- Bind + listen en el hilo llamador, no en el hilo del server (puerto ocupado se reporta antes
  de lanzar el subprocess).
- `_capture_state["scene"]` en vez de `bpy.context.scene` en el timer.
- Buffer→bake en vez de `keyframe_insert` en vivo.
- Sello `_rx` en el hilo del server, no en el drain.
- Truncado en sitio del log en vez de `unlink()` (Windows).
- Patrón "última frame" del `CameraReader` y timestamps monotónicos para MediaPipe.
