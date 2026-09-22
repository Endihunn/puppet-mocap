# Brief de ejecución: apoyo de pies fiable en Puppet Mocap

Fecha: 2026-09-09  
Proyecto: `C:\Users\darth\puppet-mocap`  
Objetivo: corregir el despegue y patinaje de los pies durante captura y postproceso, sin impedir que el pie de balanceo se levante naturalmente.  
Alcance de este documento: especificación de implementación y validación. No constituye una ejecución del fix.

## 1. Problema observado

El usuario reporta que, aun con **Clavar pies al suelo** activo, con ciertos movimientos uno o ambos pies se separan del piso. La imagen aportada muestra al personaje inclinado/agachado, con un pie claramente elevado y el otro cerca de la superficie. La imagen por sí sola no permite distinguir si el pie elevado era un balanceo intencional o una pérdida falsa de contacto; el arreglo debe poder distinguir ambos casos.

Resultado deseado:

- Un pie clasificado como **apoyo** debe conservar sin desplazamiento su posición y orientación de contacto en coordenadas de mundo.
- Si los dos pies están en `LOCKED`, los dos objetivos son restricciones duras e independientes. No se permite repartir, promediar o trasladar el error entre ellos.
- Mientras un pie esté `LOCKED`, el movimiento capturado de cadera no debe articular su muslo, pierna ni pie. La cadena de apoyo conserva la pose capturada al plantar.
- Con ambos pies `LOCKED`, `Hips`, ambas cadenas de piernas y ambos parches de contacto permanecen estables. El movimiento de cadera se remapea exclusivamente al torso mediante `Spine`, `Spine1` y `Spine2`.
- Un pie clasificado como **balanceo** debe seguir la animación corporal y poder elevarse.
- Agacharse, inclinar el torso, girar la cadera o transferir peso no debe despegar un pie que sigue apoyado.
- La transición entre cadena libre y cadena clavada no debe producir saltos de pelvis, rodilla o tobillo.

## 2. Diagnóstico del código actual

### Captura en vivo

`retarget/body.py::_apply_foot_lock()` presenta cuatro limitaciones estructurales:

1. El supuesto “speed” es desplazamiento por muestra, no velocidad por segundo. Su comportamiento cambia con el FPS real: a más FPS, un pie móvil puede parecer quieto; a menos FPS, un pie apoyado puede parecer móvil.
2. El contacto se decide únicamente con el desplazamiento de `(ankle + toe) / 2`. No comprueba distancia al suelo, velocidad vertical, orientación de la planta ni persistencia temporal.
3. El ancla usa el `head` del hueso `Foot`, que representa aproximadamente el tobillo, no el parche de contacto de la suela.
4. Solo desplaza `Hips`. Con dos pies apoyados y errores distintos promedia sus deltas; una sola traslación rígida no puede satisfacer dos anclas independientes. Además, ese enfoque mueve el padre común de torso y piernas, justo lo contrario del comportamiento solicitado.

Además, una pérdida breve de visibilidad elimina inmediatamente el ancla. Esto convierte ruido de tracking en liberaciones y reanclajes visibles.

### Postproceso de la toma

`retarget/foot_lock.py::lock_action()` agrega una IK analítica, pero todavía no garantiza el resultado:

- Solo ejecuta IK en cuadros clasificados como apoyo bilateral; el apoyo unilateral sigue dependiendo únicamente de `Hips`.
- El “suelo automático” es el mínimo Z del punto medio del hueso `Foot` en toda la toma. No representa una superficie física ni corrige una captura que ya esté elevada o hundida.
- El plano de flexión se obtiene de la pose previa. Si el objetivo nuevo sale de ese plano, rotar alrededor de su normal anterior ya no satisface necesariamente la geometría de dos segmentos.
- El solver llama `common.aim()`, que comparte `smooth_q`, `tick_t` y filtros con la captura en vivo. `lock_action()` no establece un tiempo por cuadro ni aísla ese estado; con suavizado activo pueden repetirse orientaciones.
- La fase IK evalúa y escribe sobre la misma acción corregida cuadro por cuadro. Las claves recién insertadas pueden alterar la evaluación de cuadros posteriores.

### Cobertura insuficiente

La prueba de integración actual no demuestra la deriva bilateral descrita: `UpLeg` izquierdo y derecho reciben una sola clave en el cuadro 3, sin pose neutral claveada en los cuadros 1 y 2. Por extrapolación, esa rotación puede existir también en cuadros anteriores. La prueba tampoco demuestra apoyo unilateral, separación real del suelo, pérdida temporal de tracking ni que la rama IK haya sido necesaria.

## 3. Principio de diseño

No “pegar ambos pies al piso” indiscriminadamente. El sistema debe resolver cada lado de forma independiente:

```text
SWING → CANDIDATE → LOCKED → RELEASING → SWING
                         ↘ tracking breve ↗
```

- `SWING`: FK del mocap para esa pierna.
- `CANDIDATE`: posible apoyo; aún no crear ancla rígida.
- `LOCKED`: transform de suela y pose de la cadena de apoyo congelados respecto al mundo o a la superficie golpeada.
- `RELEASING`: mezcla gradual de la pose clavada a FK.

`LOCKED` es un contrato duro: el transform del parche de apoyo y la pose local de `UpLeg/Leg/Foot` no cambian mientras el estado siga activo. `Hips` no recibe el movimiento corporal que arrastraría a sus hijos; ese delta se deriva al torso. Un pie en `SWING` sí permanece libre. Para eliminar ambigüedad de interfaz deben existir controles explícitos **Clavar ambos ahora**, **Clavar izquierdo**, **Clavar derecho** y **Soltar**; el modo automático conserva la detección stance/swing, pero solo muestra `LOCKED` cuando la restricción dura ya está aplicada.

## 4. Arquitectura propuesta

### A. Parche de contacto por pie

Crear una representación independiente de los huesos del rig:

```python
FootContactPatch(
    heel_local,
    toe_local,
    sole_normal_local,
    foot_length,
)
```

Orden de resolución recomendado:

1. Si existe `ToeBase`, usar `Foot.head` como referencia trasera y `ToeBase.head`/`ToeBase.tail` para la zona delantera.
2. Si no existe, derivar talón y punta desde `Foot.head`, `Foot.tail`, longitud y ejes de reposo.
3. Permitir offsets manuales por rig para corregir personajes cuyo pivote no coincide con la planta.

La posición que se valida debe ser el parche talón–metatarso, no el centro del tobillo. Conservar también la orientación del pie al plantar: dirección talón→punta proyectada sobre el suelo y normal de la superficie.

### B. Suelo explícito y fallback calibrado

Añadir a propiedades y panel:

- `ground_mode`: `PLANE_Z`, `OBJECT_RAYCAST`, `AUTO_CALIBRATED`.
- `ground_object`: objeto o colección opcional excluyendo armature, malla del personaje y helpers.
- `ground_z`: altura explícita para escenarios planos.
- `sole_offset`: separación de la planta respecto a la superficie.
- Botón **Calibrar suelo con postura actual**.

En `OBJECT_RAYCAST`, lanzar rayos verticales en mundo desde talón y punta con el depsgraph evaluado. Guardar el objeto golpeado, punto y normal; si el suelo se mueve, guardar el ancla en espacio local de ese objeto. Blender 5.2 ofrece `Scene.ray_cast()` sobre geometría evaluada en world-space.

`AUTO_CALIBRATED` debe estimar un plano robusto solo a partir de candidatos de contacto bajos y estables, por ejemplo percentil bajo/mediana recortada; no usar el mínimo global de toda la toma.

### C. Contacto multivariable e independiente del FPS

Calcular velocidades con `dt = sample_time - previous_time`:

```text
candidate = confidence_ok
         and horizontal_speed <= enter_speed
         and abs(vertical_speed) <= enter_vertical_speed
         and sole_distance <= enter_height
         and foot_up·ground_normal >= min_alignment
```

Requisitos de histéresis:

- Entrar en `LOCKED` tras 80–120 ms continuos como candidato.
- Umbrales de salida más amplios que los de entrada.
- Liberar inmediatamente ante intención clara de levantar: distancia creciente y velocidad vertical positiva suficiente.
- Mantener el ancla durante pérdidas de visibilidad de hasta 100–150 ms; después pasar a `RELEASING`, no borrar de golpe.
- Guardar confianza, tiempo en estado, última muestra válida y motivo de transición por pie.

Todos los umbrales espaciales deben escalar con longitud de pierna o pie. El panel puede mostrar valores normalizados y un modo avanzado con metros/segundo.

### D. Raíz estabilizada y movimiento transferido al torso

Por cuadro:

1. Evaluar la pose FK del mocap completa.
2. Detectar estado de cada pie y actualizar sus anclas.
3. Calcular `delta_hips`, la diferencia entre el movimiento solicitado por el mocap y el transform estabilizado de `Hips`.
4. Restaurar `Hips` al transform de apoyo y restaurar `UpLeg/Leg/Foot` de cada lado `LOCKED` a la pose capturada al plantar.
5. Transferir `delta_hips` al torso: rotación distribuida entre `Spine`, `Spine1` y `Spine2`; traslación aplicada a `Spine.location` o a un control de torso equivalente, con límites configurables para no romper la unión visual de la pelvis.
6. Aplicar FK únicamente a las piernas en `SWING`.
7. Validar al final del cuadro que cada parche `LOCKED` conserva su transform de mundo; si existe residual, restaurar el snapshot duro y reportar el fallo, nunca compensarlo moviendo el pie.

Guardar por lado un snapshot explícito:

```python
StanceSnapshot(
    hips_world,
    up_leg_local,
    leg_local,
    foot_local,
    toe_local,
    contact_world,
    ground_object,
)
```

El comportamiento solicitado no es el foot planting convencional que mueve la pelvis y deja que IK recalcule la flexión de las piernas. En el modo **Hard Plant**, las piernas de apoyo se mantienen sin articulación causada por la cadera. Una IK puede conservarse como modo alternativo futuro, pero no forma parte del camino predeterminado ni puede ejecutarse silenciosamente cuando el usuario eligió clavado duro.

Descomponer `delta_hips` antes de transferirlo:

- `yaw`, `roll` y `pitch`: repartir por pesos configurables en la cadena de columna.
- desplazamiento lateral y frontal: trasladar el control de torso dentro de un límite; el excedente se recorta.
- desplazamiento vertical: mapear a compresión/inclinación del torso o recortarlo; no bajarlo mediante flexión automática de las piernas.

Usar `blend_in` antes de declarar `LOCKED` y `blend_out` después de abandonar ese estado. Mientras el estado visible sea `LOCKED`, la influencia de la pose guardada debe ser 1.0 y no puede introducir deriva.

### E. Captura en vivo y horneado comparten núcleo

Extraer a un módulo nuevo, por ejemplo `retarget/foot_plant.py`:

- geometría del parche;
- estimación/consulta de suelo;
- máquina de estados;
- snapshots duros por cadena de apoyo;
- estabilización de `Hips` y remapeo de su delta al torso;
- métricas de residual.

`body.py` debe consumir ese núcleo en vivo. `foot_lock.py` debe usar el mismo núcleo sobre muestras previamente medidas.

Para horneado:

1. Muestrear la acción fuente completa e inmutable: matrices FK, parche de pie, tiempos, visibilidad y suelo.
2. Resolver contactos y todas las correcciones en memoria, secuencialmente pero sin consultar la acción de salida.
3. Escribir al final una acción duplicada con claves de `Hips`, cadenas de piernas y `Spine/Spine1/Spine2`; la acción fuente no se consulta durante esta escritura.
4. No usar ni modificar filtros globales de captura.
5. Restaurar acción, frame y modo aunque ocurra una excepción.

En apoyo unilateral, congelar únicamente la cadena de apoyo y dejar la otra pierna en FK. En apoyo bilateral, congelar ambas cadenas. En ambos casos, estabilizar `Hips` y enviar su movimiento solicitado al torso.

## 5. Cambios de interfaz

En **Cuerpo → Clavar pies al suelo** mostrar:

- Estado en vivo: `L: SWING/LOCKED`, `R: SWING/LOCKED`.
- Selector `Automático / Ambos / Izquierdo / Derecho` y botones **Clavar ahora** / **Soltar**.
- Indicador `Hips estabilizado; movimiento enviado al torso` mientras exista cualquier apoyo duro.
- Indicador de suelo detectado y objeto golpeado.
- Modo de suelo y offset de suela.
- Controles simples: sensibilidad y suavidad de entrada/salida.
- Diagnóstico desplegable: altura, velocidad XY/Z, confianza y residual del contacto respecto al snapshot.

El botón **Clavar toma actual** debe informar:

- cuadros de apoyo L/R;
- residual máximo y RMS por pie;
- cuadros fuera de alcance;
- si se utilizó suelo explícito, raycast o calibrado.

Evitar el mensaje “pies clavados” si solo se movió `Hips`, si cambió una cadena clavada o si el residual excede el criterio de aceptación.

## 6. Plan de implementación

### P0 — Reproducción y observabilidad

- Añadir overlay/log de estados y métricas por pie.
- Capturar una secuencia de regresión: postura neutral, agacharse, inclinarse lateralmente, girar cadera, levantar un pie y pérdida breve de landmark.
- Medir el parche de contacto en mundo antes de modificar el comportamiento.

### P1 — Contacto y suelo

- Implementar parche talón–punta.
- Añadir `dt` real y máquina de estados con histéresis/gracia.
- Añadir plano Z manual y calibración; después raycast opcional.
- Mantener el pie de balanceo completamente fuera del lock.

### P2 — Hard Plant y desacoplamiento del torso

- Capturar/restaurar la pose local completa de cada cadena al entrar en `LOCKED`.
- Suspender la traslación y rotación de `Hips` que afecte a piernas clavadas.
- Transferir el delta solicitado de cadera a `Spine/Spine1/Spine2` con límites y pesos explícitos.
- Mantener independiente la pierna en `SWING` en apoyo unilateral.

### P3 — Horneado determinista

- Separar medición, solución y escritura.
- Retirar la IK actual del camino **Hard Plant** y eliminar cualquier dependencia de `common.aim()`/`smooth_q` durante el horneado duro.
- Garantizar que la acción fuente no cambie y que el resultado sea reproducible.

### P4 — UX, empaquetado y validación real

- Añadir controles y diagnósticos al panel.
- Probar sobre `01-02_CINE_v001.blend` y `test_project.blend` sin sobrescribirlos.
- Empaquetar e instalar solo después de superar pruebas y revisión visual.

## 7. Pruebas obligatorias

### Unitarias

- Misma secuencia a 20, 30 y 60 FPS: estados equivalentes con tolerancia de un cuadro temporal.
- Pie lento a 8 cm del suelo: nunca entra en `LOCKED`.
- Pie quieto a menos de la tolerancia del suelo: entra tras el tiempo mínimo.
- Pérdida de tracking de 1–3 muestras: conserva el ancla; pérdida prolongada: libera suavemente.
- Elevación intencional: salida de `LOCKED` sin volver a anclar en el aire.
- Snapshot de cadena: cualquier cambio posterior de `Hips` o de landmarks de cadera deja invariantes los transforms locales de la pierna clavada y el contacto en mundo.
- Remapeo de torso: el delta de cadera produce movimiento en la columna y cero cambio en la cadena clavada.

### Integración en Blender

- Crear claves neutrales explícitas antes de la deriva y curvas distintas para ambos pies.
- Demostrar que la prueba falla al desactivar la restauración de cadena o el bloqueo de `Hips`.
- Casos separados: apoyo izquierdo, apoyo derecho, bilateral, transición bilateral→unilateral y swing.
- Mover deliberadamente la señal de cadera con ambos pies clavados: `Spine/Spine1/Spine2` deben cambiar y `Hips/UpLeg/Leg/Foot/ToeBase` deben conservar la pose de apoyo.
- Plano horizontal, rampa y plataforma animada para raycast/ancla local.
- Acción con cuadros negativos o inicio distinto de 1.
- Ejecutar el horneado dos veces desde la misma fuente y comparar resultados.

### Regresión en el rig real

Pasar la secuencia por la ruta real `apply_pose()` y por **Clavar toma actual**. No basta con asignar cuaterniones manualmente.

Medir talón y punta, no solo `Foot.head`:

- Agachamiento con ambos pies apoyados.
- Inclinación lateral y transferencia de peso.
- Giro de torso/cadera manteniendo apoyo.
- Paso con un pie en swing.
- Ruido y oclusión breve de tobillo/punta.

## 8. Criterios de aceptación

Normalizar por escala del rig cuando corresponda. Para el personaje de prueba en metros:

- Requisito funcional durante `LOCKED`: el transform de cada ancla es invariante; no se acepta deriva acumulada ni compensación promedio.
- Tolerancia exclusivamente numérica por cuadro: traslación máxima ≤ 0.5 mm y rotación máxima ≤ 0.2° respecto al transform capturado al entrar en `LOCKED`.
- Talón y punta permanecen sobre la superficie con separación/penetración absoluta ≤ 0.5 mm en el fixture métrico.
- En apoyo bilateral, ambos pies cumplen simultáneamente aunque cambie la señal de cadera; una prueba basada únicamente en el promedio de ambos errores debe fallar.
- Durante `LOCKED`, los cuaterniones/localizaciones de la cadena de apoyo no cambian por movimiento de cadera, salvo la transformación de una plataforma de suelo móvil explícitamente seleccionada.
- El delta de cadera se observa en `Spine/Spine1/Spine2`, no en `Hips` ni en las piernas. Si excede los límites seguros del torso, se recorta sin transferirlo al tren inferior.
- En `SWING`, el pie conserva al menos 95 % de la altura FK prevista y puede superar 5 cm sin ser atraído al piso.
- Pérdidas de tracking ≤ 120 ms no crean pop ni reancla.
- Cambio angular de rodilla continuo, sin flip de rama; ningún cuadro con NaN o cuaternión no normalizado.
- El residual reportado por el operador coincide con la medición reproducida después de hornear.
- La acción original permanece byte/lógicamente inalterada y la nueva acción es determinista.
- Las pruebas deben fallar de forma demostrable si se desactiva contacto, suelo, snapshot de cadena o remapeo de torso.

## 9. Archivos previstos

- `puppet_mocap/retarget/foot_plant.py` — nuevo núcleo compartido.
- `puppet_mocap/retarget/body.py` — integración en vivo.
- `puppet_mocap/retarget/foot_lock.py` — horneado determinista.
- `puppet_mocap/retarget/common.py` — únicamente estado/reset, sin reutilizar filtros de captura para IK offline.
- `puppet_mocap/properties.py` — suelo, offsets, tiempos y sensibilidad.
- `puppet_mocap/panel.py` — controles y diagnóstico.
- `puppet_mocap/operators.py` — reporte y selección de superficie.
- `tests/test_foot_contact.py` — estados y geometría pura.
- `tests/test_foot_hard_lock.py` — snapshots, desacoplamiento de torso e invariancia.
- `tests/test_hands_retarget.py` o `tests/test_foot_lock_blender.py` — integración Blender.

## 10. Fuera de alcance

- Modificar pesos de la malla.
- Reorientar o aplicar transforms destructivamente al armature.
- Forzar al suelo un pie identificado como swing.
- Sustituir la animación corporal completa por una simulación procedural de marcha.
- Sobrescribir las escenas de producción o la acción fuente.

## 11. Referencias públicas revisadas

- Blender 5.2 `Scene.ray_cast`: https://docs.blender.org/api/5.2/bpy.types.Scene.html#bpy.types.Scene.ray_cast
- Blender 5.2, IK Constraint y Pole Target: https://docs.blender.org/manual/es/latest/animation/constraints/tracking/ik_solver.html
- Mwni/blender-animation-retargeting: objetivos IK explícitos y cadenas configurables: https://github.com/Mwni/blender-animation-retargeting/blob/stable/ik.py
- BlenderBoi/mixamo_blender: estructura dedicada `foot_ik`, `foot_snap` y `foot_ik_target`: https://github.com/BlenderBoi/mixamo_blender/blob/master/mixamo_rig.py
- Blender Add-ons Contrib Mocap: correcciones de mocap representadas como restricciones actualizables: https://github.com/blender/blender-addons-contrib/blob/main/mocap/__init__.py

Las referencias IK se revisaron para entender el patrón convencional. **Hard Plant** se aparta deliberadamente de él: no recoloca las piernas para seguir una cadera móvil, sino que conserva el tren inferior y deriva el movimiento al torso. No se copiará código sin revisar licencias, compatibilidad y convenciones del rig.

## 12. Definición de terminado

El fix se considera cerrado únicamente cuando:

1. las pruebas unitarias e integradas anteriores pasan;
2. se demuestra que fallan al retirar el componente de bloqueo o remapeo que pretenden validar;
3. una captura real de agachamiento, inclinación y paso conserva el apoyo correcto sin fijar el pie de swing;
4. una prueba mueve deliberadamente la señal de cadera: el torso responde mediante la columna, mientras `Hips`, las piernas clavadas y sus contactos permanecen invariantes por encima de la tolerancia numérica;
5. el horneado produce la misma calidad que el modo en vivo;
6. el paquete instalado coincide por hash con la fuente revisada;
7. el usuario valida visualmente la escena de producción con talón y punta en contacto.
