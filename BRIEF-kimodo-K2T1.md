# Brief de ejecución — K2-T1: conversor Kimodo → Mixamo

Repo: `C:\Users\darth\puppet-mocap` · rama `master` · último commit `b23a11e` · árbol limpio.
Lee primero: `BRIEF-kimodo-K2.md` (fase completa) y `BRIEF-v0.4.md` (auditoría del addon).
Artefactos del spike: `C:\Users\darth\_kimodo_spike\` — **fuera del repo, no lo modifiques**.

**Esta tarea NO depende del gate de Llama-3.** El spike K2-T0 dejó un fixture sintético válido,
así que el conversor se construye y se testea entero sin que la generación funcione nunca.

**Un commit al terminar. Sin push a `origin`.**

---

## AVISO: documento obsoleto en el spike

`_kimodo_spike/skeleton_mapping.md` concluye que SOMA-77 *"mapea los 30 dedos (clave para
marionetas)"*. **Eso es falso** y se escribió antes del hallazgo posterior del propio spike.

`Kimodo-SOMA-RP-v1.1` corre internamente sobre `SOMASkeleton30` y expande a 77 en la salida
rellenando dedos, cara y extremos con una **pose relajada estática** (`output_to_SOMASkeleton77`
→ `relaxed_hands_rest_pose`). Los 77 joints existen en el array; **solo ~30 llevan movimiento
real**.

Usa ese archivo para los nombres, **no para las conclusiones**.

---

## Datos autoritativos (volcados del propio paquete, no de la doc)

### Joints que el modelo realmente anima — `SOMASkeleton30`, en orden

```
Hips, Spine1, Spine2, Chest, Neck1, Neck2, Head, Jaw, LeftEye, RightEye,
LeftShoulder, LeftArm, LeftForeArm, LeftHand, LeftHandThumbEnd, LeftHandMiddleEnd,
RightShoulder, RightArm, RightForeArm, RightHand, RightHandThumbEnd, RightHandMiddleEnd,
LeftLeg, LeftShin, LeftFoot, LeftToeBase,
RightLeg, RightShin, RightFoot, RightToeBase
```

El array de salida es de 77 joints (`SOMASkeleton77`); los índices que no están en la lista de
arriba son relleno estático.

### NPZ (verificado por round-trip; fixture real en `_kimodo_spike/synthetic_77.npz`, 34 KB)

```
posed_joints         (T, 77, 3)      float32   posiciones globales
global_rot_mats      (T, 77, 3, 3)   float32   orientación global por joint
local_rot_mats       (T, 77, 3, 3)   float32   orientación relativa al padre
foot_contacts        (T, 4)          float32
smooth_root_pos      (T, 3)          float32
root_positions       (T, 3)          float32
global_root_heading  (T, 2)          float32
```

fps = 30 (de `config.yaml` del modelo). Y-up, metros, root canonicalizado a XZ=(0,0) en frame 0.

Rest pose de referencia: `skeleton.neutral_joints` → `[J, 3]`, `root_idx = 0`, jerarquía en
`skeleton.bone_order_names_with_parents`.

---

## Las dos trampas de nomenclatura

**Mapear por nombre produce un rig roto en silencio.** Son coincidencias parciales, no aliases:

| SOMA | Mixamo | Trampa |
|---|---|---|
| `LeftLeg` | `LeftUpLeg` | SOMA llama "Leg" al **muslo** |
| `LeftShin` | `LeftLeg` | y "Shin" a lo que Mixamo llama "Leg" |
| `Spine1` | `Spine` | desplazamiento de uno en toda la columna |
| `Spine2` | `Spine1` | |
| `Chest` | `Spine2` | |
| `Neck1` | `Neck` | |

`LeftShoulder`, `LeftArm`, `LeftForeArm`, `LeftHand`, `LeftFoot`, `LeftToeBase`, `Hips` y `Head`
sí coinciden literalmente. Eso hace la trampa peor: un mapeo por nombre "funciona" para la mitad
del rig y cablea las piernas al revés.

**La tabla de correspondencia va explícita en código, nunca derivada de los nombres.**

Sin equivalente en Mixamo estándar y por tanto descartados: `Neck2`, `Jaw`, `LeftEye`,
`RightEye`, `*HandThumbEnd`, `*HandMiddleEnd`.

---

## Alcance: 22 huesos Mixamo

```
Hips, Spine, Spine1, Spine2, Neck, Head,
LeftShoulder, LeftArm, LeftForeArm, LeftHand,
RightShoulder, RightArm, RightForeArm, RightHand,
LeftUpLeg, LeftLeg, LeftFoot, LeftToeBase,
RightUpLeg, RightLeg, RightFoot, RightToeBase
```

Ojo: **no es la lista de `body.bone_names()`**. La captura no anima clavículas ni `ToeBase`; la
generación sí puede. No reutilices `bone_names()` como objetivo — define la lista propia del
conversor.

### Decisión tomada: dedos

**El conversor no emite ningún canal para los huesos de dedos.** Ni keyframes a rest, ni nada:
simplemente no aparecen en la action.

Motivo: no es destructivo (los dedos se quedan como estén) y deja abierta la puerta a combinar un
cuerpo generado con manos capturadas, que para una app de titiritero es el objetivo. Hornearlos a
rest cierra esa puerta.

*(Decisión del orquestador por delegación. Si el usuario la veta, es un cambio de una línea en la
lista de huesos.)*

---

## El núcleo: cómo convertir las rotaciones

**No copies `local_rot_mats` a `rotation_quaternion`.** Las rest bases de SOMA y de Mixamo no
coinciden; copiar rotaciones locales da un personaje retorcido.

El retarget correcto usa el **delta respecto a la rest pose de cada esqueleto**:

```
R_objetivo_world = R_soma_world(t) @ R_soma_rest⁻¹ @ R_mixamo_rest
```

Es decir: toma cuánto ha rotado el joint SOMA respecto a *su* rest, y aplícalo a la rest de
Mixamo. `R_soma_rest` se deriva de `neutral_joints` recorriendo la jerarquía; `R_mixamo_rest` es
`bone.matrix_local.to_3x3()`.

Después, para obtener el cuaternión local que Blender espera, resuelve contra el padre ya
calculado:

```
q = ((parent_world_3x3 @ rel_3x3)⁻¹ @ R_objetivo_world).to_quaternion()
donde rel_3x3 = (parent.bone.matrix_local⁻¹ @ bone.matrix_local).to_3x3()
```

Eso es exactamente la inversa de `chained_world_3x3()` en `retarget/common.py`. **Propaga las
matrices frescas por la cadena igual que hace `body.apply()`** — no leas `parent.matrix` del
depsgraph, que estará stale.

### Conversión de espacio

Kimodo es **Y-up**; arm-space del addon y Blender son **Z-up**. La rotación de cambio de base se
aplica a las matrices globales antes de todo lo anterior. Documenta la matriz elegida en un
comentario con su justificación — no la dejes como tres índices permutados sin explicación.

### Traslación de raíz — trampa que ya costó un bug

`pose_bone.location` está en el **rest basis del hueso**, no en espacio de armature. Esto ya
produjo un error de 90° en este repo (commit `59c53c2`): agacharse empujaba al personaje hacia
atrás.

```python
M = hips.bone.matrix_local.to_3x3()
loc = M.inverted_safe() @ delta_arm_space
```

Usa `root_positions`, no `smooth_root_pos` (ese está suavizado para paths). Y hazlo **relativo**:
el primer frame define la referencia, igual que `_apply_root_translation` en `body.py`, para que
el personaje se mueva desde donde está y no salte al origen.

### Escala

Kimodo trabaja en metros, cadera a ~0.9 m. Deriva el factor del rig con
`_rig_torso_length(arm, prefix)`, que ya existe en `body.py`. No hardcodees.

### Tiempo

30 fps fijo → fps de escena. Reusa el criterio de `use_scene_fps` que ya existe en properties.

---

## Estructura

`puppet_mocap/kimodo/convert.py`. Sin `bpy` donde se pueda. Puede importar de
`retarget/common.py` y `log.py`; **prohibido importar de `server.py` o `capture/`** (requisito de
independencia del brief K2).

Salida: el mismo dict `{(data_path, index, group): [(frame, valor)]}` que consume
`_bake_channels()`. No inventes un formato nuevo.

---

## Criterio de cierre

Tests en `tests/test_kimodo_convert.py`. Corren bajo el Python de Blender (`mathutils` no tiene
wheel de Windows; Blender ignora `PYTHONPATH` salvo con `--python-use-system-env`).

1. **Identidad**: rotaciones identidad en Kimodo → rest pose de Mixamo, sin deriva.
2. **Ejes, uno por uno**: una rotación conocida sobre X/Y/Z en Kimodo produce la rotación
   equivalente en Blender. **Verificar por eje. Nunca por magnitud** — la magnitud es invariante
   bajo rotación y fue justo lo que enmascaró el bug anterior.
3. **Traslación**: subir en Kimodo sube en Blender; ir hacia atrás va hacia atrás. Los tres ejes
   por separado.
4. **Trampa de piernas**: test explícito de que `LeftLeg` (SOMA) llega a `LeftUpLeg` (Mixamo) y
   `LeftShin` a `LeftLeg`. Si alguien "arregla" el mapeo por nombre, falla.
5. **Fixture real**: cargar el `.npz`, convertir, y comprobar shapes y número de canales
   (22 huesos × 4 rot + 3 loc del Hips). **Copia el fixture a `tests/data/`** — no dependas de una
   ruta fuera del repo. Son 34 KB y `.gitignore` no lo bloquea (verificado).
6. **Sin dedos**: comprobar que ningún canal apunta a un hueso de dedo.

`py -m pytest tests/` y `py -m ruff check .` en verde, con la salida real pegada en el reporte.

---

## Reglas de reporte

- **Nada verde sin ejecutar.** Pega la salida real. Ya se reportó "ruff en verde" con 7 findings
  abiertos una vez.
- Geometría: **por eje, nunca magnitud**.
- Si algo de este brief está mal diagnosticado, dilo y para. No improvises un diseño alternativo
  sin avisar.

---

## Lo que NO hay que tocar

Todo lo de `BRIEF-v0.4.md` y `BRIEF-kimodo-K2.md`, y además:

- **`_bake_channels` desasigna la action a propósito** (fix P0-1). Las tomas de Kimodo siguen la
  misma regla.
- **Marcadores `puppet_mocap_take` / `puppet_mocap_owner`** — las tomas generadas los llevan
  igual.
- **No toques `_drain_tick`, la `POSE_QUEUE` ni el puerto 9878.** Son de MediaPipe.
- **No modifiques `_kimodo_spike/`.** Es material de referencia; copia lo que necesites al repo.
