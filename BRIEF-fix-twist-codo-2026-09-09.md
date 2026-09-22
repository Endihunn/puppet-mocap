# Brief de ejecución: eliminar el “nudo de globo” en codo y muñeca

Fecha: 2026-09-09  
Proyecto: `C:\Users\darth\puppet-mocap`  
Alcance: retarget de brazos y manos Mixamo durante captura, grabación y reproducción.  
Este documento especifica el fix; no ejecuta cambios sobre el addon, el rig ni la escena.

## 1. Regresión observada

Después de trasladar el roll de la palma desde `Hand` hacia `ForeArm`, el colapso tipo *candy wrapper* dejó de concentrarse en la muñeca y apareció en el codo.

La captura aportada muestra una deformación localizada en la unión del brazo. La oscuridad y resolución no permiten cuantificarla desde la imagen, pero el síntoma reportado coincide con el comportamiento comprobado en el código.

## 2. Causa comprobada

La ruta actual hace lo siguiente:

1. `body.py` orienta `UpperArm` con `aim(shoulder → elbow)`. Esto determina la dirección longitudinal, pero no obtiene roll de la palma.
2. `hands.py::_orient_forearm_from_palm()` aplica a `ForeArm` un `orient_yz(elbow → wrist, palm_normal)` completo.
3. `Hand` vuelve a alinearse con `wrist → middleMCP` y la misma normal palmar.

Consecuencia: la muñeca deja de absorber el giro relativo, pero `ForeArm` recibe prácticamente todo el roll palma↔dorso respecto a `UpperArm`. En un giro cercano a 180°, la discontinuidad se concentra en la costura del codo. Con skinning lineal y pesos compartidos `UpperArm/ForeArm`, esa diferencia angular reduce la sección de la malla.

La suite actual refuerza el error de diseño:

- `test_h1_resolved_in_apply_pose` exige expresamente que `ForeArm` “absorba” el cambio de palma.
- `test_mesh_wrist_knot_elimination_on_real_mesh` solo observa la costura `ForeArm/Hand`.
- Esa prueba asigna cuaterniones manualmente; no recorre `apply_pose()`.
- No existe una medición equivalente para vértices compartidos `UpperArm/ForeArm`.

Por tanto, la prueba puede aprobar aunque el volumen perdido se haya desplazado de la muñeca al codo.

## 3. Resultado requerido

- La orientación final de la palma debe coincidir con los landmarks.
- Las direcciones hombro→codo y codo→muñeca deben conservarse.
- El roll total palma↔dorso debe repartirse entre `UpperArm`, `ForeArm` y `Hand`; ningún empalme debe recibir el salto completo.
- El reparto debe ser continuo temporalmente y simétrico entre izquierda y derecha.
- Deben validarse simultáneamente hombro, codo y muñeca para impedir que el defecto vuelva a migrar.
- No modificar automáticamente `bone.roll`, transforms del objeto, rest pose ni pesos de la malla en el fix básico.

## 4. Arquitectura propuesta sin cambiar el rig

### A. Separar swing y twist

No usar la normal palmar como Z absoluta de `ForeArm` al 100 %. Construir primero una orientación base para cada segmento:

- `UpperArm swing`: alinea su eje longitudinal con hombro→codo.
- `ForeArm swing`: alinea su eje longitudinal con codo→muñeca usando la matriz fresca del padre.
- `Hand target`: base completa de palma obtenida de `palm_forward` y `palm_normal`.

Después, calcular la rotación residual que lleva desde la mano resultante de los dos swings hasta `Hand target`. Descomponer ese residual en:

```text
residual = swing_residual × twist_about_limb_axis
```

El twist debe extraerse con cuaterniones sobre el eje longitudinal actual, no con Euler.

### B. Reparto por la cadena con ejes no colineales

`UpperArm`, `ForeArm` y `Hand` no comparten el mismo eje en un codo flexionado. Por ello, no se debe multiplicar un único quaternion de twist del antebrazo y aplicarlo directamente a los tres huesos, ni interpretar porcentajes como ángulos locales que simplemente suman.

Cada contribución debe girar alrededor del eje longitudinal **propio y actualizado** de su segmento. Los pesos son un presupuesto inicial de deformación, no la solución cinemática:

```text
UpperArm: 0.25
ForeArm:  0.50
Hand:     0.25
```

Estos valores no son un criterio final. Deben optimizarse sobre la malla real para minimizar la peor pérdida de sección entre hombro, codo y muñeca.

Formular el reparto con dos restricciones duras:

- conservar exactamente las direcciones hombro→codo y codo→muñeca;
- reproducir exactamente la base palmar objetivo al final de `Hand`.

Y un objetivo de optimización:

```text
minimizar max(
    |twist relativo en hombro| / capacidad_hombro,
    |twist relativo en codo|   / capacidad_codo,
    |twist relativo en muñeca| / capacidad_muñeca
) + penalización_de_cambio_temporal
```

Las capacidades se calibran con la malla real. Si todavía no existen, usar límites iguales como semilla, no asumir que tres deltas locales de `Θ/3` producirán exactamente la orientación final cuando los ejes no son colineales.

Orden obligatorio por muestra:

1. Resolver `UpperArm swing + twist_upper` alrededor del eje de `UpperArm`, nunca alrededor del eje de `ForeArm`.
2. Recalcular su matriz mundial fresca.
3. Volver a resolver `ForeArm swing` para recuperar exactamente codo→muñeca y aplicar `twist_forearm` alrededor de su nuevo eje.
4. Recalcular su matriz mundial fresca.
5. Recalcular el swing de `Hand` hacia `palm_forward` y resolver sobre su propio eje el residual exacto que alinea `palm_normal`.
6. Propagar esa matriz a los dedos.

Tras añadir twist al padre, hay que volver a resolver el swing del hijo con la nueva matriz parental. Sumar cuaterniones locales sin recomponer la cadena cambiaría las direcciones del brazo. El optimizador puede ajustar `twist_upper` y `twist_forearm`; `Hand` cierra siempre el residual exacto. La solución elegida debe ser la más cercana a la muestra anterior para evitar saltos entre mínimos equivalentes.

### C. Continuidad temporal

Mantener por lado:

- último ángulo de twist desenrollado;
- última normal palmar válida;
- última normal del plano del brazo;
- pesos efectivos;
- calidad de landmarks.

Desenrollar el ángulo alrededor de ±π seleccionando la rama más cercana al valor anterior. Filtrar el escalar de twist después de desenrollarlo; no filtrar tres veces el mismo objetivo mediante los nombres de los huesos.

Cuando el brazo esté casi recto y el plano hombro–codo–muñeca degenere, conservar la última normal válida o usar una referencia del torso. Cuando la mano pierda calidad, mezclar gradualmente hacia el roll estable del brazo corporal; no saltar inmediatamente a `aim()` sin roll.

### D. Coordinación cuerpo–manos

Cuando exista una mano válida, el módulo de manos debe finalizar la cadena completa `UpperArm → ForeArm → Hand`, no solo sobrescribir `ForeArm` después de que cuerpo resolvió `UpperArm`.

El coordinador debe:

- dejar que cuerpo calcule direcciones y matrices base;
- entregar esas direcciones al solver coordinado de brazo/mano;
- realizar una sola asignación final por hueso y muestra;
- conservar un fallback corporal completo cuando falten landmarks de mano;
- grabar todos los huesos modificados, incluido `UpperArm`.

## 5. Protección adicional de deformación

### Opción inmediata y no destructiva

Detectar el modificador Armature de la malla y ofrecer **Preserve Volume** como opción recomendada, nunca activada silenciosamente. Blender usa interpolación cuaterniónica para reducir la pérdida de volumen, aunque puede presentar discontinuidades cerca de 180° y no todos los destinos de exportación reproducen el mismo resultado.

### Opción de mayor calidad

Ofrecer en una fase independiente una mejora de rig sobre duplicados:

- huesos auxiliares `UpperArmTwist` y `ForeArmTwist`;
- distribución gradual de pesos a lo largo del brazo;
- bake opcional a deform bones compatibles con el destino.

Esta opción requiere migración de pesos, pruebas de exportación FBX/glTF y rollback. No debe mezclarse con el fix matemático inicial ni modificar el rig de producción sin confirmación explícita.

## 6. Pruebas que deben reemplazar la falsa seguridad actual

### Unitarias de orientación

- Descomposición swing–twist y reconstrucción con error angular menor a `1e-5` rad.
- Secuencia 0°→180°→0° sin salto de rama en ±π.
- Izquierda/derecha con signos anatómicos coherentes.
- Brazo recto, ligeramente flexionado y flexionado 90°.
- Landmarks de mano ausentes durante 1–5 muestras y recuperación sin flip.
- La orientación final de la palma mantiene error ≤ 3°.
- Las direcciones de `UpperArm` y `ForeArm` mantienen error ≤ 2° respecto a landmarks.
- Ninguna costura recibe por sí sola más del límite configurado de twist relativo.

### Integración real de `apply_pose()`

Crear landmarks sintéticos corporales y de mano para ambos lados y ejecutar la ruta pública completa:

```text
neutral → 30° → 60° → 90° → 120° → 150° → 180°
        → 150° → 120° → 90° → 60° → 30° → neutral
```

La prueba debe:

- llamar `retarget.apply_pose()` en cada muestra con timestamps crecientes;
- medir cuaterniones locales y matrices mundiales finales;
- fallar con la implementación actual que concentra el roll en `ForeArm`;
- probar con objeto Armature identidad y rotado 90° X, escala 1 y 0.01;
- probar izquierda y derecha;
- repetir con suavizado 0 y con el valor predeterminado.

### Medición válida sobre la malla real

Definir tres zonas por pesos compartidos:

- hombro: `Shoulder/UpperArm` o `UpperArm/torso`, según grupos existentes;
- codo: `UpperArm/ForeArm`;
- muñeca: `ForeArm/Hand`.

Para cada zona:

1. Obtener coordenadas de la malla evaluada y hacer `.co.copy()` antes de `to_mesh_clear()`.
2. Transformarlas a mundo.
3. Limitar cada zona a una banda axial estrecha alrededor de la articulación; los pesos compartidos por sí solos pueden abarcar demasiada longitud.
4. Proyectar los vértices sobre el plano perpendicular al eje local de la extremidad.
5. Medir área de sección mediante envolvente convexa o los dos autovalores principales de covarianza. No usar distancia media a un centroide de una franja axial extensa como sustituto de radio transversal.
6. Comparar cada muestra contra la sección neutral de esa misma zona.

La prueba no debe asignar manualmente `ForeArm=180°`; debe llegar a cada pose mediante `apply_pose()`.

## 7. Criterios de aceptación

- Área transversal conservada entre 80 % y 120 % de neutral en hombro, codo y muñeca durante toda la secuencia.
- Ninguna zona puede mejorar a costa de que otra caiga fuera del límite.
- Error final de orientación palmar ≤ 3°.
- Error de dirección hombro→codo y codo→muñeca ≤ 2°.
- Sin cambio discontinuo de twist mayor a 25° entre muestras consecutivas de la secuencia de prueba.
- Sin NaN, cuaterniones no normalizados ni cambio de signo visible.
- Mismos resultados funcionales a 20, 30 y 60 FPS usando timestamps reales.
- La prueba geométrica falla deliberadamente al restaurar el comportamiento “ForeArm absorbe todo”.
- Validación visual del usuario con palma↔dorso, ambos brazos y codo flexionado/extendido.

## 8. Archivos previstos

- `puppet_mocap/retarget/common.py`: descomposición swing–twist, ángulo continuo y composición segura.
- `puppet_mocap/retarget/body.py`: exposición de direcciones/base del brazo sin finalizar dos veces la cadena.
- `puppet_mocap/retarget/hands.py`: solver coordinado `UpperArm/ForeArm/Hand` y reparto de twist.
- `puppet_mocap/retarget/__init__.py`: propiedad de una sola escritura por hueso/muestra.
- `puppet_mocap/retarget/__init__.py` y registro de toma: incluir `UpperArm` si manos modifica su roll.
- `puppet_mocap/properties.py` y `panel.py`: pesos avanzados y opción Preserve Volume, si se exponen al usuario.
- `tests/test_arm_twist.py`: matemáticas y continuidad.
- `tests/test_hands_retarget.py`: integración pública y regresión temporal.
- `tests/test_mesh_joint_volume.py`: hombro, codo y muñeca sobre malla real.

## 9. No hacer

- No volver a trasladar el 100 % del giro a `UpperArm`, porque movería el nudo al hombro.
- No corregir únicamente activando Preserve Volume.
- No editar `bone.roll`, aplicar transforms ni reorientar el armature automáticamente.
- No validar solo la muñeca.
- No usar poses manuales como sustituto del pipeline del addon.
- No añadir twist bones o cambiar pesos en el rig original sin una fase separada y copia recuperable.

## 10. Referencias

- Blender 5.2, Armature Modifier y Preserve Volume: https://docs.blender.org/manual/en/latest/modeling/modifiers/deform/armature.html
- Implementación de Blender de separación swing–twist: https://github.com/blender/blender/blob/master/source/blender/blenlib/intern/math_rotation.c
- RCSF, propósito de huesos `UpperArmTwist/LowerArmTwist`: https://github.com/meshula/LabRCSF/blob/dev/proposal.md#twist-bones-twist
- BlendArMocap, separación de orientación de mano y ángulos de dedos: https://github.com/cgtinker/BlendArMocap/blob/ecb4c91c01befbe29b75196eb72321867810ddf0/src/cgt_core/cgt_calculators_nodes/mp_calc_hand_rot.py

## 11. Definición de terminado

El fix se considera cerrado cuando la misma secuencia real palma↔dorso:

1. atraviesa `apply_pose()` y una toma grabada/reproducida;
2. mantiene dirección del brazo y orientación de la palma;
3. conserva simultáneamente las secciones de hombro, codo y muñeca;
4. no concentra el giro completo en ningún empalme;
5. funciona en izquierda y derecha, con brazo recto y flexionado;
6. queda instalado con paridad de hashes;
7. es validado visualmente por el usuario en la escena de producción.
