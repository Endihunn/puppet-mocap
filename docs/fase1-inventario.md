# Fase 1 — Inventario mecánico de Puppet Mocap

Auditoría de solo lectura sobre `C:\Users\darth\puppet-mocap\puppet_mocap` (rama
`audit-2026-09-22`, commit `f29785a`). 17 archivos .py, ~8230 líneas.
Conteo real de operadores registrados: **24** (`operators.CLASSES`), no 25 —
el número del brief original estaba una unidad por encima; verificado por
grep de `bl_idname\s*=` en todo el paquete.

Ningún operador define `poll()` — no existe ni una sola definición de
`def poll` en el addon (verificado por grep). Todas las "precondiciones" de la
tabla son checks dentro de `execute()` que devuelven `{'CANCELLED'}`, no
`poll()` de Blender. Efecto práctico: los botones del panel nunca aparecen
grises por precondición de Blender; siempre son clicables, y el rechazo (si
lo hay) llega como `self.report({'ERROR'}, ...)` después del clic.

---

## Tabla de operadores

| bl_idname | archivo:línea | resumen | invocado desde panel | poll/precondición | efectos persistentes | bloqueante/IO |
|---|---|---|---|---|---|---|
| `puppet_mocap.start_capture` | operators.py:1011 | Valida rig/módulos, sincroniza calibración y target, fuerza rest pose, y lanza el subprocess externo de webcam+MediaPipe + arranca el server TCP + registra el timer de drenado. | Sí — panel.py:204 | Sin `poll()`. Inline: server no debe estar ya corriendo; ≥1 módulo (Cuerpo/Manos/Cara) activo; hay armature; prefijo con ≥1 hueso coincidente; `.task` del/los módulo(s) activo(s) presente(s) en disco. | Escribe `bones_matched/total`, `capture_pid`, `server_running`, `frames_received`, `last_error`; llama `retarget.fix_orientation()` (pone el rig en rest pose); registra `bpy.app.timers` persistente. | Sí — `subprocess.Popen` (proceso externo con webcam), abre socket TCP (`server.start`), abre archivo de log. |
| `puppet_mocap.stop_capture` | operators.py:1167 | Hornea la grabación pendiente (si la había) y apaga subprocess + server + timer. | Sí — panel.py:202 | Sin `poll()`; sin checks inline (siempre ejecuta). | Puede crear una o dos Actions nuevas (bake); resetea `server_running`/`capture_pid`; mata el subprocess. | Sí — `proc.terminate()`/`wait(timeout=2.0)`, cierre de socket con `join(timeout=2.0)`. |
| `puppet_mocap.toggle_record` | operators.py:1186 | Si ya graba: hornea el buffer a Action(s) nueva(s) y detiene. Si no: arma cuenta regresiva/grabación (samples=[]). | Sí — panel.py:228/230 | Sin `poll()`. Inline: exige `server.is_running()` para EMPEZAR. **No exige** `client_connected()` ni que algún `record_body/hands/face` esté activo (ver Hallazgo previo #6). | Crea Actions vía `_bake_take`→`bpy.data.actions.new`; escribe `is_recording`, `rec_start_frame`, `last_record_frame`. | No (delega al timer `_drain_timer`). |
| `puppet_mocap.clear_keyframes` | operators.py:1235 | Borra la Action activa del armature objetivo (cuerpo) y la de sus shape keys (cara); purga además otras tomas propias marcadas del mismo rig. | Sí — panel.py:254 | Sin `poll()`; sin checks (solo requiere que `_sync_target` resuelva un armature; si no hay, reporta error). | `bpy.data.actions.remove(..., do_unlink=True)` sobre la Action **activa** sin verificar que sea de Puppet Mocap (ver Hallazgo previo #1, CONFIRMADO); purga selectiva por marcador `puppet_mocap_owner` solo para las tomas ya desasignadas. | No. |
| `puppet_mocap.reset_rig` | operators.py:1252 | Fuerza rest pose completa del armature y pone a 0 los shape keys ARKit (no borra Actions). | Sí — panel.py:259 | Sin `poll()`; error si no hay armature. | Escribe `rotation_quaternion`/`location`/`scale` de todos los `pose.bones` y `value` de shape keys. | No. |
| `puppet_mocap.lock_current_take` | operators.py:1305 | Duplica la Action activa y hornea corrección de deslizamiento de pies (mueve Hips + IK de piernas) contra el suelo configurado (`ground_mode`/`ground_z`/`ground_object`/`sole_offset`). | **No** — huérfano | Sin `poll()`. Inline: server detenido y no grabando; armature con Action activa. | Crea una Action nueva (duplicado corregido); actualiza `_baked_state["body"]` y `play_take`. | No (cómputo síncrono en memoria, puede ser pesado con tomas largas pero no hace I/O). |
| `puppet_mocap.foot_lock_now` | operators.py:1375 | Fuerza bloqueo duro (LOCKED) de ambos pies escribiendo una bandera en el estado global del retarget. | **No** — huérfano | Sin `poll()`; sin checks. | `retarget.common._state["foot_force_lock"] = True` (bandera de proceso, no persiste en el .blend). | No. |
| `puppet_mocap.foot_release` | operators.py:1388 | Libera inmediatamente el bloqueo de ambos pies (bandera contraria a la anterior). | **No** — huérfano | Sin `poll()`; sin checks. | `retarget.common._state["foot_force_release"] = True`. | No. |
| `puppet_mocap.calibrate_ground` | operators.py:1271 | Fija `props.ground_z` al Z-mundo más bajo entre los huesos `*LeftFoot`/`*RightFoot` en la pose actual. | **No** — huérfano | Sin `poll()`; error si no hay armature o no hay huesos `Foot`. | Escribe `props.ground_z`. | No. |
| `puppet_mocap.calibrate` | operators.py:1401 | Arma la recolección de ~1.5 s de landmarks de pose neutral para computar la matriz de calibración (el cómputo real ocurre después, en `_drain_tick`). | Sí — panel.py:193 (además la UI ya lo deshabilita con `sub.enabled = running and connected`) | Sin `poll()`. Inline: exige server corriendo Y cliente conectado. | Arranca `_calib_state` (dict de proceso); el resultado final (`calib_matrix`/`calib_valid`) lo escribe el timer, no este operador. | No (async vía timer). |
| `puppet_mocap.clear_calibration` | operators.py:1417 | Descarta la calibración capturada. | Sí — panel.py:196 | Sin `poll()`; sin checks. | `props.calib_valid=False`; limpia `retarget.common._state["calib_R"]`. | No. |
| `puppet_mocap.detect_prefix` | operators.py:1431 | Busca un hueso `*Hips` en el armature y deduce el prefijo; recalcula huesos coincidentes. | Sí — panel.py:99 | Sin `poll()`; error si no hay armature o ningún hueso termina en `Hips`. | Escribe `bone_prefix`, `bones_matched`, `bones_total`. | No. |
| `puppet_mocap.check_deps` | operators.py:1480 | Ejecuta el Python externo para comprobar `mediapipe`/`cv2`/`numpy` y su versión mínima. | Sí — panel.py:265 | Sin `poll()`; sin checks previos (usa `props.python_path` tal cual). | Escribe `props.deps_status`. | **Sí** — `subprocess.run(timeout=30)` síncrono en el hilo principal. |
| `puppet_mocap.open_log` | operators.py:1527 | Abre con la app por defecto del sistema el log del addon y el del subprocess de captura, los que existan. | Sí — panel.py:285 | Sin `poll()`; cancela si ningún log existe. | Ninguno en datos de Blender; side effect de OS (`os.startfile`/`Popen`). | No (lanza visor externo, no espera). |
| `puppet_mocap.download_models` | operators.py:1555 | Descarga a `models/` los `.task` de MediaPipe que falten (pose/manos/cara) desde Google Storage. | Sí — panel.py:159, 275 | Sin `poll()`; cancela si no falta ninguno. | Escribe archivos `.task` en disco (con `.tmp` + `os.replace` atómico). | **Sí** — `urllib.request.urlopen(timeout=20)` síncrono por archivo, en el hilo principal; puede colgar la UI hasta ~20 s por modelo. |
| `puppet_mocap.clear_log` | operators.py:1579 | Trunca el archivo de log del addon en sitio. | Sí — panel.py:286 | Sin `poll()`. | I/O de archivo (`log.clear()`, truncado). | No (rápido). |
| `puppet_mocap.generate_motion` | operators.py:1595 | Lanza el subprocess de Kimodo (texto→animación) con el prompt/parámetros actuales; registra el timer de sondeo `_kimodo_poll_timer`. | Sí — panel.py:380 | Sin `poll()`. Inline: no debe haber otra generación corriendo; rig válido (`_validate_rig`); `kimodo_python_path` configurado; `kimodo_runner.py` existente. | Escribe `kimodo_running`/`kimodo_status`; lanza proceso externo (`subprocess.Popen`, torch); abre log de Kimodo. | No directamente (Popen async + timer), pero el proceso hijo es pesado (carga de modelo torch). |
| `puppet_mocap.cancel_generate` | operators.py:1674 | Mata el subprocess de Kimodo en curso. | Sí — panel.py:320 | Sin `poll()`. | `kimodo_running=False`, `kimodo_status="Cancelado"`. | `proc.terminate()`/`wait(timeout=2.0)`. |
| `puppet_mocap.check_kimodo_deps` | operators.py:1688 | Ejecuta el Python de Kimodo para comprobar `torch`+`transformers`+`kimodo`. | Sí — panel.py:393 | Sin `poll()`; error si `kimodo_python_path` vacío. | Escribe `kimodo_status`. | **Sí** — `subprocess.run(timeout=30)`. |
| `puppet_mocap.autodetect_kimodo_python` | operators.py:1725 | Busca automáticamente un Python válido para Kimodo entre rutas candidatas (env var, venvs conocidos) y lo valida. | Sí — panel.py:340 | Sin `poll()`. | Escribe `kimodo_python_path`, `kimodo_status`. | **Sí** — cada candidato pasa por `_kimodo_python_valid` → `subprocess.run(timeout=60)`. |
| `puppet_mocap.play_kimodo_take` | operators.py:1743 | Reasigna al armature la última toma generada por Kimodo y mueve el playhead al frame de inicio. **No inicia reproducción** (no llama `screen.animation_play` ni similar) — solo asigna y salta de frame (ver Hallazgo previo #3, CONFIRMADO). | Sí — panel.py:328 | Sin `poll()`. | `props.play_take=True`; reasigna Action vía `set_take_playback`; escribe `scene.frame_current`. | No. |
| `puppet_mocap.open_kimodo_license` | operators.py:1756 | Abre en el navegador la página de Hugging Face del modelo Llama-3 (gated). | Sí — panel.py:349, 390 | Sin `poll()`. | Ninguno en Blender; `bpy.ops.wm.url_open`. | No (delega al navegador). |
| `puppet_mocap.open_kimodo_log` | operators.py:1766 | Abre el log del subprocess de Kimodo con la app por defecto. | Sí — panel.py:395 | Sin `poll()`. | `os.startfile`. | No. |
| `puppet_mocap.enable_preserve_volume` | operators.py:1781 | Activa `use_deform_preserve_volume` en todos los modificadores Armature de mallas de la escena que usan el rig activo. | Sí — panel.py:148 | Sin `poll()`; warning si no hay rig activo. | Escribe `modifier.use_deform_preserve_volume=True` en N mallas. | No. |

---

## Tabla de propiedades

Todas viven en `PuppetMocapProperties` (properties.py), registrada como
`Scene.puppet_mocap` desde `__init__.py`. Es el único PropertyGroup del addon.

| nombre | tipo | default | consumidor(es) | expuesto en panel | huérfana |
|---|---|---|---|---|---|
| `server_running` | Bool | False | Se escribe en operators.py:214,1152. **Nunca se lee** (el panel usa `server.is_running()`, no esta prop). | No | **Sí — huérfana (write-only)** |
| `is_recording` | Bool | False | Escrita/leída en `_finish_recording`, `_drain_tick`, `toggle_record`, `lock_current_take`. | Sí — como texto de estado (panel.py:221), no como widget editable | No |
| `rec_start_frame` | Int | 1 | Escrita en operators.py:1224 (`toggle_record`). **Nunca se lee** en ningún módulo. | No | **Sí — huérfana (write-only)** |
| `last_record_frame` | Int | 0 | Escrita por `_flush_ui_pulse`/`toggle_record`; leída en panel.py:227 (label REC). | Sí — como texto | No |
| `frames_received` | Int | 0 | Escrita por `_flush_ui_pulse`; leída en panel.py:127. | Sí — como texto | No |
| `capture_pid` | Int | 0 | Escrita en operators.py:215,1151 (init y al lanzar subprocess). **Nunca se lee** (el panel muestra el PID leyendo `_subprocess_state["proc"].pid`, no esta prop). | No | **Sí — huérfana (write-only)** |
| `last_error` | String | "" | Escrita en varios puntos de `_drain_tick`; leída en panel.py:128-129. | Sí — como texto | No |
| `deps_status` | String | "" | Escrita por `check_deps`; leída en panel.py:276-278. | Sí — como texto | No |
| `bones_matched` | Int | -1 | Escrita por `_validate_rig`/`detect_prefix`; leída en panel.py:100-108 y operators.py:1044. | Sí — como texto | No |
| `bones_total` | Int | 0 | Idem anterior. | Sí — como texto | No |
| `play_take` | Bool | False | `update=_play_take_update` → `operators.set_take_playback`; leída/escrita por `start_capture`, `play_kimodo_take`, `lock_current_take`, `_finish_recording`. | Sí — panel.py:253 (widget) | No |
| `target_armature` | Pointer(Object) | None | `poll=_poll_armature`; leída por `_sync_target` (todo operador que llama `_sync_target`). | Sí — panel.py:95 | No |
| `mirror_motion` | Bool | False | `apply_pose(mirror=...)` en operators.py:942. | Sí — panel.py:176 | No |
| `min_visibility` | Float (FACTOR) | 0.5 | `apply_pose(min_visibility=...)`. | Sí — panel.py:293 | No |
| `use_calibration` | Bool | True | `apply_pose(calibrate=...)`. | Sí — panel.py:182 | No |
| `calib_valid` | Bool | False | Escrita por `_store_calibration`; leída por `_load_calibration` y panel.py:186,195. | Sí — como texto/estado de botón | No |
| `calib_matrix` | FloatVector[9] | identidad | Escrita/leída solo por `_store_calibration`/`_load_calibration` (no widget — es un dato calculado, no pensado para edición directa). | No (por diseño) | No |
| `use_scene_fps` | Bool | True | Leída en `toggle_record` y panel.py:240 (cálculo de `eff_fps`). | Sí — panel.py:233 | No |
| `rec_countdown` | Int | 3 | `toggle_record`. | Sí — panel.py:232 | No |
| `cam_index` | Int | 0 | `start_capture` (arg `--cam`). | Sí — panel.py:300 | No |
| `send_fps` | Float | 20.0 | `start_capture` (arg `--fps`); comparado en panel.py:242 contra `eff_fps`. | Sí — panel.py:301 | No |
| `rec_fps` | Float | 30.0 | `toggle_record` (si `use_scene_fps` es False). | Sí — panel.py:236 | No |
| `max_take_samples` | Int | 30000 | `_drain_tick` (tope de muestras). | Sí — panel.py:237 | No |
| `smooth_min_cutoff` | Float | 1.0 | `start_capture` (arg `--smooth-min-cutoff`). | Sí — panel.py:302 | No |
| `smooth_beta` | Float | 0.05 | `start_capture` (arg `--smooth-beta`). | Sí — panel.py:303 | No |
| `rotation_smooth` | Float (FACTOR) | 0.5 | `apply_pose(rotation_smooth=...)`. | Sí — panel.py:292 | No |
| `debug_hands` | Bool | False | `apply_pose(debug_hands=...)` → `common._state["debug_hands"]`. | Sí — panel.py:294 | No |
| `enable_body` | Bool | True | `_drain_tick`, `apply_pose`, `_validate_rig`, `start_capture`. | Sí — vía `_module_box` (panel.py:132) | No |
| `enable_hands` | Bool | True | Idem, módulo Manos. | Sí — vía `_module_box` (panel.py:142) | No |
| `enable_face` | Bool | False | Idem, módulo Cara. | Sí — vía `_module_box` (panel.py:153) | No |
| `enable_root_translation` | Bool | False | `apply_pose(root_translation=...)`, `snapshot_pose(include_root=...)`. | Sí — panel.py:137 | No |
| `foot_lock` | Bool | **True** | `apply_pose(foot_lock=...)` (operators.py:950); `snapshot_pose(include_root=...)` (operators.py:988). | **No** — sin `.prop()` en ningún lugar del panel | **Sí — se lee pero no se expone en UI** (ver Hallazgo previo #5, CONFIRMADO) |
| `foot_lock_speed` | Float | 0.02 | `apply_pose(foot_lock_speed=...)`. | No | Sí — leída, sin UI |
| `ground_mode` | Enum | PLANE_Z | `apply_pose`, `lock_current_take`. | No | Sí — leída, sin UI |
| `ground_z` | Float | 0.0 | `apply_pose`, `lock_current_take`; escrita por el operador huérfano `calibrate_ground`. | No | Sí — leída, sin UI (y su único escritor tampoco tiene UI) |
| `ground_object` | Pointer(Object) | None | `apply_pose`, `lock_current_take`. | No | Sí — leída, sin UI, y sin ningún escritor en todo el addon (permanece `None` salvo que se fije por script) |
| `sole_offset` | Float | 0.0 | `lock_current_take`. | No | Sí — leída, sin UI |
| `foot_lock_sensitivity` | Float | 1.0 | `apply_pose`, `lock_current_take`. | No | Sí — leída, sin UI |
| `foot_lock_mode` | Enum | AUTO | `apply_pose(foot_lock_mode=...)`. | No | Sí — leída, sin UI. Nota: los operadores huérfanos `foot_lock_now`/`foot_release` **no** tocan esta prop; escriben banderas aparte en `common._state`, así que el enum queda fijo en `AUTO` en la práctica. |
| `foot_state_left` | String | "SWING" | Escrita en operators.py:963 desde telemetría (`get_foot_telemetry`). **Nunca se lee.** | No | **Sí — huérfana (write-only)** |
| `foot_state_right` | String | "SWING" | Escrita en operators.py:964. **Nunca se lee.** | No | **Sí — huérfana (write-only)** |
| `foot_ground_detected` | Float | 0.0 | Escrita en operators.py:965. **Nunca se lee.** | No | **Sí — huérfana (write-only)** |
| `root_translation_scale` | Float | 1.0 | `apply_pose(root_scale=...)`. | Sí — panel.py:139 (condicional a `enable_root_translation`) | No |
| `record_body` | Bool | True | `_drain_tick` (`include_body`), panel.py:217 (etiqueta de módulos activos). | Sí — vía `_module_box` | No |
| `record_hands` | Bool | True | Idem. | Sí — vía `_module_box` | No |
| `record_face` | Bool | True | Idem. | Sí — vía `_module_box` | No |
| `flip_palm_normal` | Bool | False | `apply_pose` → `hands.apply(flip_normal=...)`. | Sí — panel.py:147 | No |
| `swap_hands` | Bool | False | `apply_pose(swap_hands=...)`. | Sí — panel.py:146 | No |
| `python_path` | String | "py" | `start_capture`, `check_deps`. | Sí — panel.py:306 | No |
| `bone_prefix` | String | "mixamorig:" | Prácticamente todos los operadores de rig/captura. | Sí — panel.py:98 | No |
| `server_port` | Int | 9878 | `start_capture` (`server.start`, arg `--addon-port`). | Sí — panel.py:305 | No |
| `kimodo_python_path` | String | "" | `generate_motion`, `check_kimodo_deps`, checklist del panel. | Sí — panel.py:341 | No |
| `kimodo_prompt` | String | ejemplo por defecto | `generate_motion` (arg `--prompt`); actualizada por `_kimodo_example_update`. | Sí — panel.py:352 | No |
| `kimodo_example` | Enum | (primer ejemplo) | `update=_kimodo_example_update` → escribe `kimodo_prompt`. | Sí — panel.py:353 | No |
| `kimodo_duration` | Float | 5.0 | `generate_motion` (arg `--duration`); panel.py:355-359 (aviso >10 s). | Sí — panel.py:354 | No |
| `kimodo_model` | Enum | Kimodo-SOMA-RP-v1.1 | `generate_motion` (arg `--model`). | Sí — panel.py:367 (bajo "Avanzado") | No |
| `kimodo_seed` | Int | -1 | `generate_motion`: `if props.kimodo_seed >= 0: cmd += ["--seed", ...]` (operators.py:1631). | Sí — panel.py:371, **condicional** a `kimodo_seed_use` | No (pero ver Hallazgo previo #2) |
| `kimodo_num_transition` | Int | 5 | `generate_motion` (arg `--num_transition_frames`, siempre, incondicional). | **No** — sin `.prop()` en el panel | **Sí — se lee pero no se expone en UI** (queda fijo en 5) |
| `kimodo_postprocess` | Bool | False | `generate_motion`: `if props.kimodo_postprocess: cmd += ["--postprocess"]`. | Sí — panel.py:374, pero con `pp.enabled = False` (visible y **deshabilitado**: el usuario no puede cambiarlo desde la UI aunque el widget exista) | No (consumida), pero congelada en False para el usuario |
| `kimodo_seed_use` | Bool | False | **Ningún consumidor lee esta prop fuera de la propia UI** (solo controla si se muestra el campo `kimodo_seed`, panel.py:369-371). | Sí — panel.py:369 | Ver Hallazgo previo #2: el comando real decide por `kimodo_seed >= 0`, no por este toggle — la prop existe y se expone, pero no gobierna el comportamiento que su nombre promete |
| `kimodo_foot_lock` | Bool | True | `_kimodo_bake` (operators.py:605). | Sí — panel.py:365 (bajo "Avanzado") | No |
| `kimodo_ground_snap` | Bool | True | `_kimodo_bake` (operators.py:620). | Sí — panel.py:366 (bajo "Avanzado") | No |
| `kimodo_advanced` | Bool | False | Solo controla el plegado de la sección "Avanzado" en el propio panel. | Sí — panel.py:362 | No |
| `kimodo_result_frames` | Int | 0 | Escrita por `_kimodo_bake`; leída en panel.py:326 y en el label de `play_kimodo_take`/timer. | Sí — como texto | No |
| `kimodo_running` | Bool | False | Escrita/leída por `generate_motion`, `cancel_generate`, `_kimodo_poll_timer`, panel.py:316. | Sí — controla ramas del panel | No |
| `kimodo_status` | String | "" | Escrita en múltiples puntos; leída en panel.py:318,336,346,386. | Sí — como texto | No |
| `kimodo_show` | Bool | False | Controla si se dibuja toda la sección Kimodo. | Sí — panel.py:311 | No |

**Resumen de huérfanas (guardan pero nunca se leen — 6 propiedades):**
`server_running`, `rec_start_frame`, `capture_pid`, `foot_state_left`,
`foot_state_right`, `foot_ground_detected`.

**Resumen de "se leen pero no se exponen en UI" (9 propiedades):**
`foot_lock`, `foot_lock_speed`, `ground_mode`, `ground_z`, `ground_object`,
`sole_offset`, `foot_lock_sensitivity`, `foot_lock_mode`, `kimodo_num_transition`.

No se encontró el caso inverso puro (propiedad con widget en el panel que
ningún operador/módulo lea) — todo lo que tiene `.prop()` en panel.py se
consume en algún operador o en `apply_pose`. El caso más cercano es
`kimodo_seed_use`, que tiene widget pero no gobierna nada por sí misma (ver
Hallazgo previo #2), y `kimodo_postprocess`, que tiene widget pero está
forzado a `enabled=False` en el propio panel.

---

## Hallazgos previos: confirmación

| # | Referencia original | Confirmado/No encontrado | Línea real actual |
|---|---|---|---|
| 1 | `retarget/common.py:131`, `retarget/face.py:212`, `operators.py:1235` — "Borrar Keyframes" borra el datablock de la acción activa aunque no sea de Puppet Mocap (`do_unlink=True` amplio). | **Confirmado** — `clear_all_keyframes()` remueve `arm.animation_data.action` y `sks.animation_data.action` sin comprobar el marcador `puppet_mocap_owner`; el chequeo de dueño solo aplica al segundo bucle (tomas ya desasignadas con fake_user). | `retarget/common.py:131` (def, igual), `common.py:139` (`do_unlink` sobre la action activa del armature, sin check), `retarget/face.py:219` (`do_unlink` sobre la action activa de shape keys, sin check — antes 212, corrido +7 líneas), `operators.py:1235` (clase `PUPPET_OT_clear_keyframes`, igual). |
| 2 | `properties.py:411`, `panel.py:369`, `operators.py:1631` — "Resultado repetible" (semilla Kimodo) solo oculta el campo; el comando sigue decidiendo por `kimodo_seed >= 0` sin mirar el toggle. | **Confirmado**, líneas exactas sin drift. `kimodo_seed_use` no aparece en ningún `if` de `generate_motion`; el gate real es `if props.kimodo_seed >= 0:`. | `properties.py:411` (def `kimodo_seed_use`), `panel.py:369` (`rr.prop(props, "kimodo_seed_use", ...)`), `operators.py:1631` (`if props.kimodo_seed >= 0:`). |
| 3 | `operators.py:132`, `operators.py:152`, `operators.py:1743` — "Reproducir toma" asigna pero no reproduce. | **Confirmado**, líneas exactas sin drift. `PUPPET_OT_play_kimodo_take.execute` llama `set_take_playback(True)` y mueve `frame_current`, pero no invoca `bpy.ops.screen.animation_play` ni equivalente — no hay reproducción real, solo asignación + salto de frame. | `operators.py:132` (`_assign_action`), `operators.py:152` (`set_take_playback`), `operators.py:1743` (clase `PUPPET_OT_play_kimodo_take`). |
| 4 | `panel.py:313`, `operators.py:395/456/482` — autodetección de Kimodo en `draw()` puede llamar validaciones síncronas con `subprocess.run(timeout=60)`. | **Confirmado.** La cadena es real: `draw()` → `_maybe_autodetect_kimodo` (solo la 1ª vez por sesión con path vacío) → `_autodetect_kimodo_python` → `_try` → `_kimodo_python_valid` → `subprocess.run(..., timeout=60)`. Puede bloquear el hilo de UI hasta 60 s si un candidato existe pero el import de `kimodo` se cuelga. | `panel.py:314` (línea de la llamada; el comentario "U1" queda en 313 — mismo lugar), `operators.py:482` (`_maybe_autodetect_kimodo`), `operators.py:456` (`_autodetect_kimodo_python`), `operators.py:395` (`_kimodo_python_valid`, con `timeout=60` en la línea 408). |
| 5 | `properties.py:219`, `panel.py:131`, `operators.py:950/1271/1305` — bloqueo de pies activado por defecto pero sin control visible en el panel; operadores de suelo/corrección de toma registrados sin acceso desde panel. | **Confirmado y ampliado.** `foot_lock` (default `True`) se usa en `apply_pose` sin ningún `.prop()` en `panel.py`. Además de `calibrate_ground` (suelo) y `lock_current_take` (corrección de toma), también son huérfanos `foot_lock_now` y `foot_release` (forzar/soltar bloqueo duro) — 4 operadores de foot-lock en total sin acceso desde el panel. | `properties.py:219` (def `foot_lock`, igual), `panel.py:131-132` (sección "Cuerpo" sin toggle de foot lock), `operators.py:950` (`foot_lock=props.foot_lock` en la llamada a `apply_pose`, igual), `operators.py:1271` (clase `PUPPET_OT_calibrate_ground`, igual), `operators.py:1305` (clase `PUPPET_OT_lock_current_take`, igual). |
| 6 | `panel.py:230`, `operators.py:1191` — "Grabar" visible sin exigir conexión/datos recientes/canal grabable. | **Confirmado**, líneas exactas sin drift. El botón "Grabar" (`toggle_record`) se dibuja siempre habilitado (a diferencia de "Calibrar", que sí tiene `sub.enabled = running and connected`); dentro de `execute()` solo se comprueba `server.is_running()`, nunca `is_client_connected()` ni que algún `record_body/hands/face` esté activo — se puede "grabar" sin cliente conectado (cero muestras) o con los 3 canales de grabación apagados. | `panel.py:230` (`row.operator("puppet_mocap.toggle_record", text="Grabar", ...)`, igual), `operators.py:1191` (`def execute` de `PUPPET_OT_toggle_record`, igual). |

---

## Operadores sin acceso desde el panel

4 de los 24 operadores registrados no tienen ningún `.operator(...)` que los
invoque en `panel.py` (ni en ningún otro archivo del addon; se buscó también
fuera de `puppet_mocap/` y solo aparecen en `operators.py` mismo y en un test):

- **`puppet_mocap.calibrate_ground`** (operators.py:1271) — hipótesis:
  calibración manual de la altura del suelo a partir del pie más bajo de la
  pose actual; probablemente pensado como paso previo a activar foot lock
  con `ground_mode="PLANE_Z"`, pero el flujo completo (fijar suelo → activar
  foot lock → grabar) no tiene ningún punto de entrada en el panel.
- **`puppet_mocap.lock_current_take`** (operators.py:1305) — hipótesis:
  corrección de toma ya grabada (duplica la Action y hornea anti-deslizamiento
  de pies); es un paso de post-proceso opcional sobre una toma existente, en
  la misma familia que "Kimodo: Clavar pie de apoyo/Aterrizar" que sí están en
  el panel — este parece ser el equivalente para tomas de captura en vivo,
  simplemente sin botón.
- **`puppet_mocap.foot_lock_now`** (operators.py:1375) — hipótesis: acción
  manual de "forzar bloqueo" en vivo (p. ej. para clavar ambos pies durante
  una pose estática antes de una transición); depende de las mismas banderas
  de `common._state` que el resto del sistema de foot lock, pero no hay
  fila en el panel para dispararla ni para mostrar el estado resultante.
- **`puppet_mocap.foot_release`** (operators.py:1388) — hipótesis: contraparte
  de la anterior, para soltar el bloqueo manual y volver a `SWING` libre;
  misma situación, sin fila en el panel.

Nota: no se califican como código muerto — los cuatro están completos,
registrados, y consumen/producen estado real (`ground_z`, Actions nuevas,
banderas de `common._state`). Simplemente no hay ningún widget en `panel.py`
que permita al usuario dispararlos sin usar el buscador de operadores de
Blender (F3) o una consola de Python.
