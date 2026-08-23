# Brief de ejecución — Puppet Mocap: texto → animación con Kimodo (fase K2)

Repo: `C:\Users\darth\puppet-mocap` · rama `master` · último commit `b23a11e` · árbol limpio.
Contexto previo: `BRIEF-v0.4.md` en el repo (auditoría v0.3.1→v0.4.0) y el ciclo v0.4.1.
Alcance de este brief: **solo texto → animación**. Corrección de tomas y capa DeepSeek quedan fuera.
Uso declarado: **educativo**.

**Un commit por tarea. Sin push a `origin`.**

---

## Contexto: qué es Kimodo y qué NO estamos construyendo

[Kimodo](https://github.com/nv-tlabs/kimodo) es un modelo de difusión cinemática de NVIDIA
(marzo 2026). Código Apache-2.0, pesos bajo NVIDIA Open Model License (variante SMPL-X bajo
R&D License). Genera a **30 fps, hasta 512 frames**, entrenado con secuencias de máximo 10 s.

**No** vamos a usar [kimodo.cpp](https://github.com/localai-org/kimodo.cpp): su README declara
que los constraints no están implementados, y aunque esta fase no los necesita, la fase
siguiente sí. Nos quedamos con la implementación PyTorch oficial para no cambiar de caballo a
mitad.

**No** es tiempo real. Es un job por lotes de segundos. **No debe tocar `_drain_tick` ni el
servidor TCP** — esos son para el streaming de MediaPipe y meterlos aquí rompería ambos.
Patrón nuevo: request → subproceso → archivo → poll → bake.

---

## REQUISITO TRANSVERSAL — el módulo generativo es INDEPENDIENTE

Esto no es un detalle de UI: es una restricción de diseño que atraviesa T2, T3 y los criterios
de cierre. **La generación por texto debe funcionar sin captura previa, sin animación previa y
sin MediaPipe instalado.** No es "corrección asistida" ni un post-proceso de una toma: es una
fuente de animación por derecho propio.

### Qué significa en concreto

1. **Cero dependencia de MediaPipe.** En una máquina donde `mediapipe`/`opencv` nunca se
   instalaron, generar por texto debe funcionar de punta a punta. Los dos checks de
   dependencias son independientes: `PUPPET_OT_check_deps` (captura) y
   `PUPPET_OT_check_kimodo_deps` (generación) no se llaman entre sí ni comparten estado.
2. **Cero dependencia del estado de captura.** `PUPPET_OT_generate_motion` **no** debe consultar
   `server.is_running()`, `server.is_client_connected()`, `props.capture_pid` ni
   `_capture_state`. Ningún mensaje de error suyo puede decir "inicia la captura primero".
3. **Cero dependencia de una toma previa.** No requiere `_baked_state`, ni una action existente,
   ni `props.is_recording`. Sobre una escena recién abierta con solo un rig Mixamo importado,
   debe generar.
4. **Independiente de los toggles de módulos.** `enable_body` / `enable_hands` / `enable_face`
   y sus `record_*` son settings de captura. La generación **no los lee**. Que Cuerpo esté
   apagado no debe impedir generar un movimiento de cuerpo.
5. **No destructivo.** La toma generada es una action nueva y marcada. Nunca pisa, edita ni
   borra una toma capturada. Capturado y generado conviven en el mismo archivo.
6. **Aislado en el árbol de código.** Todo vive bajo `puppet_mocap/kimodo/`. Ese subpaquete
   puede importar de `retarget/common.py` (matemáticas de huesos) y de `log.py`, pero **no de
   `server.py` ni de `capture/`**. Si mañana esto se separa en un addon propio, el corte debe
   ser limpio.

### Prerrequisitos legítimos (los únicos)

- Un armature en la escena y un prefijo de huesos que coincida (misma validación que ya hace
  `start_capture`, extraída a una función compartida — no duplicada).
- El venv de Kimodo configurado y los pesos descargados.

### Criterio de cierre del requisito

Prueba explícita y reportada: **en un Blender con el addon instalado y MediaPipe NO instalado**,
abrir una escena nueva, importar un FBX Mixamo, escribir un prompt y obtener una action
horneada. Sin iniciar captura en ningún momento. Si algún camino de código exige MediaPipe o
estado de captura para llegar ahí, es un bug de esta fase, no una limitación aceptable.

---

## Arquitectura decidida (no re-litigar)

```
UI del addon (prompt + duración)
  → subprocess.Popen(python_kimodo, kimodo_runner.py, --prompt, --duration, --out X.npz)
     [venv PROPIO, separado del de MediaPipe: torch+CUDA no convive con mediapipe]
  → escribe X.npz y termina
  → bpy.app.timers poll: ¿terminó el proceso? ¿existe el .npz?
  → addon lee el .npz con el numpy de Blender (2.3.4, viene de fábrica — verificado)
  → conversor Kimodo → Mixamo
  → _bake_channels() existente → action marcada con puppet_mocap_take / puppet_mocap_owner
```

Handoff **por archivo, no por TCP**. El server TCP existente es para streaming a 20 Hz; aquí hay
una sola respuesta de varios MB. Un archivo temporal es más simple, depurable, y no pelea por el
puerto 9878 — además de respetar el requisito de independencia.

Blender **no debe congelarse** durante la generación. El poll va por timer, igual que
`_drain_timer`, y con el mismo watchdog de subprocess muerto que ya existe en `_drain_tick`
(mapeo de returncode a mensaje de UI).

---

## K2-T0 — Spike: validar antes de escribir código de addon

**Nada de esto toca el repo.** Es la tarea que decide si las demás valen la pena.

1. Instalar Kimodo en un venv propio (`py -3.11 -m venv`, fuera del repo).
2. Generar 4-5 movimientos por CLI:
   ```
   kimodo_gen "a person walks forward and waves" --model Kimodo-SOMA-RP-v1 --output test.npz --duration 5.0
   ```
3. **VRAM: el text encoder DEBE ir en CPU.** Con encoder en GPU pide ~17 GB y la 4080 tiene 16 —
   no cabe. Con encoder en CPU baja a <3 GB. Encontrar el flag y documentarlo.
4. **Volcar los nombres/orden de joints de los esqueletos disponibles** (SOMA, SMPL-X, G1) y
   compararlos contra la lista de huesos Mixamo que ya usa `retarget/body.py` (`bone_names()`).
5. Volcar la estructura real del `.npz`: claves, shapes, dtypes. La doc menciona
   `posed_joints [T,J,3]`, matrices de rotación global y local `[T,J,3,3]`, foot contacts, root
   position y heading — **verificar contra el archivo real**, no contra la doc.

### Entregable
Un reporte (no código) con: tiempo de generación de 5 s en la 4080, VRAM real, el dump del
`.npz`, y una **recomendación de esqueleto con la tabla de correspondencia joint→hueso Mixamo**.

### Criterio de decisión del esqueleto

**Default: SOMA.** `nvidia/Kimodo-SOMA-RP-v1` NO está gated, va bajo NVIDIA Open Model License
(apto comercial), y su ficha declara que no depende de ningún body model externo. Cero registros.
Solo desviarse de este default si el mapeo a Mixamo resulta claramente peor que el de SMPL-X, y
en ese caso **reportarlo y esperar aprobación** — no cambiar de esqueleto por cuenta propia.

`nvidia/Kimodo-SMPLX-RP-v1` sí está gated (hay que aceptar condiciones en HF y autenticarse con
`hf auth login`), va bajo NVIDIA R&D License, solo investigación no comercial, y podría exigir
además el body model de MPI (registro en smpl-x.is.tue.mpg.de). Ese último punto **está sin
resolver**: el body model se necesita para generar la malla, pero aquí solo queremos rotaciones
de joints para retargetear. Si el spike acaba en SMPL-X, contestar esa pregunta es parte del
entregable. El usuario se registra él mismo; no lo hagas por él ni asumas que ya está.

### Dos incógnitas que este spike DEBE resolver

1. **Conteo de joints contradictorio.** El repo de GitHub menciona un `somaskel77` de 77 joints;
   la ficha de HF de SOMA-RP-v1 dice 30 joints. Volcar los nombres reales del checkpoint que se
   vaya a usar y reportar cuál aplica. No asumir ninguno de los dos.
2. **¿Hay dedos?** El addon anima 15 falanges por mano vía captura. Ni SOMA-30 ni SMPLX-22
   alcanzan para eso, así que lo más probable es que **las animaciones generadas salgan con las
   manos estáticas**. Confirmarlo explícitamente: si es así, es una limitación de fondo para una
   app de titiritero y hay que decidir qué hacer con los huesos de dedos en la toma horneada
   (¿dejarlos sin keyframes? ¿fijarlos a rest?). Reportar, no decidir solo.

**Esta tarea es bloqueante. No empieces K2-T1 sin su reporte aprobado.**

---

## K2-T1 — Conversor Kimodo → Mixamo (una sola dirección)

Módulo nuevo `puppet_mocap/kimodo/convert.py`. **Sin `bpy`** donde se pueda, para que sea
testeable.

### Lo que hay que resolver

| De Kimodo | A Blender/Mixamo |
|---|---|
| **Y-up**, metros | **Z-up**, unidades de escena |
| Matrices de rotación `[T,J,3,3]` globales y locales | Cuaterniones por pose bone, en rest basis |
| Root canonicalizado a XZ=(0,0) en frame 0 | `Hips.location` relativo a la pose actual |
| 30 fps fijo | fps de escena |

### Trampa conocida — leer antes de escribir una línea

`pose_bone.location` **no está en espacio de armature**, está en el *rest basis del hueso*. Esto
ya causó un bug de 90° en este mismo repo (fix T1, commit `59c53c2`): agacharse empujaba al
personaje hacia atrás. La conversión es:

```python
M = hips.bone.matrix_local.to_3x3()
loc = M.inverted_safe() @ delta_arm_space
```

Lo mismo aplica a las rotaciones: una matriz global de Kimodo no se asigna directo a
`rotation_quaternion`. Hay que llevarla al rest basis del hueso, exactamente como hacen
`bone_rest_arm_3x3()` / `chained_world_3x3()` en `retarget/common.py`. **Reutiliza esas
funciones, no escribas unas nuevas.**

### Escala

Kimodo trabaja en metros con altura de cadera ~0.9 m. Los rigs Mixamo importados de FBX suelen
venir a otra escala. Derivar el factor del rig, no hardcodearlo: `_rig_torso_length(arm, prefix)`
ya existe en `retarget/body.py` y hace justo eso.

### Criterio de cierre

- Tests en `tests/` que verifiquen la conversión **por eje, nunca por magnitud**. La magnitud es
  invariante bajo rotación y fue exactamente lo que enmascaró el bug anterior.
- Test con un `.npz` sintético mínimo (armado a mano, sin descargar nada) que compruebe: Y-up→
  Z-up correcto en los 3 ejes, y que una rotación identidad en Kimodo produce la rest pose en
  Mixamo.
- Los tests de retarget corren bajo el Python de Blender (`mathutils` no tiene wheel de
  Windows). Blender ignora `PYTHONPATH` salvo con `--python-use-system-env`.

---

## K2-T2 — Runner externo

`puppet_mocap/kimodo/kimodo_runner.py`, espejo estructural de `capture/capture_runner.py`.

- Args: `--prompt`, `--duration`, `--model`, `--out`, `--seed`, `--skeleton`.
- Salida: el `.npz` en `--out`. Progreso por stdout.
- **Códigos de retorno con significado**, como el runner de captura (que mapea 0/1/2 a mensajes
  de usuario): 0 ok, 1 faltan deps o modelo, 2 falló la generación, 3 OOM de VRAM. El OOM merece
  su propio código porque tiene una acción de usuario clara (encoder a CPU).
- stdout/stderr al log **separado** `puppet_mocap_kimodo.log`. No reuses el log del addon ni el
  de captura: ese problema ya se arregló una vez (P1-3), no lo reintroduzcas.
- El runner **no importa nada de `capture/`**. Es un ejecutable independiente.

---

## K2-T3 — Integración en el addon

### Properties nuevas
`kimodo_python_path` (venv propio, separado de `python_path`), `kimodo_prompt`
(StringProperty), `kimodo_duration`, `kimodo_model`, `kimodo_seed`, y runtime `kimodo_running` /
`kimodo_status` con `SKIP_SAVE`.

### Operadores
- `PUPPET_OT_generate_motion` — lanza el runner. Valida **solo** armature + prefijo.
- `PUPPET_OT_cancel_generate` — mata el subproceso.
- `PUPPET_OT_check_kimodo_deps` — clona el patrón de `PUPPET_OT_check_deps`, incluido el
  `creationflags=CREATE_NO_WINDOW` y el timeout.

La validación de armature + prefijo que hoy vive dentro de `PUPPET_OT_start_capture` debe
**extraerse a una función compartida** y usarse desde ambos operadores. Duplicarla es cómo se
desincronizan.

### Timer de poll
Timer **separado** de `_drain_timer`. Intervalo ~0.25 s (no 0.02: no hay nada que drenar). Al
terminar: leer npz → convertir → `_bake_channels(..., owner=arm.name)` → marcar como
`puppet_mocap_take` para que "Borrar Keyframes" y "Reproducir toma" sigan funcionando sin
tocarlos.

### Panel
Caja nueva "Generar (Kimodo)", **colapsada por defecto**. El panel ya está largo; no lo hagas
peor. La caja **no se desactiva** cuando la captura está parada — es independiente.

### Reutilizar sin reescribir
`_bake_channels` (con `escape_identifier` y el marcador de dueño ya dentro),
`_terminate_subprocess`, el patrón de watchdog de returncode, `log.banner`, y el cacheo de
armature por nombre. Todo eso ya está probado en producción en este repo.

---

## K2-T4 — Troceado para duraciones > 10 s

El modelo está entrenado a máximo 10 s y admite hasta 512 frames (~17 s). El CLI acepta múltiples
prompts con duraciones (`--duration "5.0 4.0"`) y `--num_transition_frames` para mezclar.

Exponer duración y frames de transición en la UI, y **avisar en el panel** cuando la duración
pedida supere los 10 s, igual que ya se avisa de `send_fps > fps_efectivos`.

No implementes cosido propio. Si las junturas salen mal, repórtalo — es un límite del modelo, no
un bug a parchear a ciegas.

---

## Orden

| Tarea | Bloquea a | Paralelizable |
|---|---|---|
| **K2-T0** spike | todas | no — es bloqueante |
| **K2-T1** conversor | T3 | sí, con T2 |
| **K2-T2** runner | T3 | sí, con T1 |
| **K2-T3** addon | T4 | no |
| **K2-T4** troceado | — | sí |

---

## Reglas de reporte

- **No declarar verde nada que no se haya ejecutado.** Pegar la salida real de
  `py -m pytest tests/` y `py -m ruff check .`, no un resumen. Esto ya falló una vez: se reportó
  "ruff en verde" con 7 findings abiertos.
- Para cualquier cosa geométrica, reportar **por eje**. Nunca la magnitud.
- Reportar explícitamente la prueba de independencia (Blender sin MediaPipe, escena nueva,
  generación completa).
- Si el spike (K2-T0) muestra que la calidad no sirve o que no cabe en la 4080, **para y
  repórtalo**. No sigas construyendo encima.
- Si algo de este brief resulta mal diagnosticado, dilo y detén esa tarea. No improvises un
  diseño alternativo sin avisar.

---

## Lo que NO hay que tocar

Vigente todo lo de `BRIEF-v0.4.md`, y además:

- **El servidor TCP y `_drain_tick` son de MediaPipe.** Kimodo no pasa por ahí. Ni un timer
  compartido, ni el puerto 9878, ni la `POSE_QUEUE`.
- **`_bake_channels` desasigna la action a propósito** (`ad.action = None` + `use_fake_user`).
  Es el fix P0-1: si la dejas asignada, sus fcurves pisan la captura en vivo. Las tomas de
  Kimodo deben seguir la misma regla.
- **Los marcadores `puppet_mocap_take` y `puppet_mocap_owner`** son lo que mantiene funcionando
  "Borrar Keyframes" acotado al rig objetivo. Las tomas generadas los llevan igual.
- **`snapshot_pose` re-lee RNA a propósito.** No lo "optimices".
- **Venvs separados.** MediaPipe y torch+CUDA en el mismo entorno es pedir un conflicto de
  dependencias; el proyecto ya usa venvs por herramienta.
