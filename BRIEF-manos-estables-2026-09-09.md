# Puppet Mocap: brief de ejecución — manos estables y regresiones de apoyo

Fecha: 9 de septiembre de 2026. Estado: **propuesta; implementación NO autorizada ni ejecutada en esta revisión**.

## 1. Objetivo y base revisada

Corregir el «nudo de globo» al girar la muñeca de palma a dorso, conservando el giro real, el movimiento de dedos y la coherencia entre captura y grabación. Los problemas previos de ejes y pies se incluyen como regresiones y como bloque de reparación separado.

- Repositorio: [puppet-mocap](C:/Users/darth/puppet-mocap). HEAD: `7526ed6d8defd4b004222ecf1ea45001f4c06892`, con cambios locales previos que deben conservarse.
- Versión declarada: 0.4.2. Los ocho archivos relevantes comparados —manos, cuerpo, funciones comunes, coordinador, captura, operadores y ambos postprocesadores de pies— coinciden por SHA-256 con los instalados en Blender 5.2.
- Rig de referencia identificado anteriormente: `Armature`, nombres `mixamorig:`, rotación de objeto +90° X y escala uniforme 0.01. Revalidar esos datos en la copia de prueba; no asumir que describen una sesión actualmente abierta.
- Esta revisión es estática: lectura de código, fuentes públicas y consultas al notebook indicado. No se abrió Blender, no se ejecutaron pruebas de movimiento ni se modificó el addon o la escena. Las imágenes históricas no permiten diagnosticar por sí solas el giro de muñeca.

**Conclusión principal:** existe una ruta de código que descarta la corrección de torsión del antebrazo. Es un defecto comprobable por lectura, pero todavía no demuestra que explique por sí solo toda la deformación visible. Deben distinguirse: orientación incorrecta, torsión concentrada y colapso de la malla por skinning.

## 2. Hallazgos de manos, con evidencia

### H1 — P1: dos orientaciones del mismo antebrazo compiten dentro del mismo filtro

[apply_pose](C:/Users/darth/puppet-mocap/puppet_mocap/retarget/__init__.py:150) establece un solo timestamp y ejecuta cuerpo antes que manos. [body.py](C:/Users/darth/puppet-mocap/puppet_mocap/retarget/body.py:371) llama `aim` para `ForeArm`; después [hands.py](C:/Users/darth/puppet-mocap/puppet_mocap/retarget/hands.py:216) llama `orient_yz` para ese mismo hueso, ahora con normal palmar.

Ambas funciones filtran usando el nombre del hueso. [QuatOneEuro](C:/Users/darth/puppet-mocap/puppet_mocap/retarget/common.py:387) devuelve su resultado anterior cuando `dt <= 0.0001`. Si la primera llamada aceptó la muestra, la segunda recibe `dt=0` y su objetivo no se aplica. La rama de rechazo de outliers puede conservar otro timestamp; por eso no corresponde afirmar que ocurre incondicionalmente en todas las muestras.

Consecuencia probable: el antebrazo conserva el giro definido por el cuerpo y la mano absorbe más rotación relativa. Esto puede contribuir al estrangulamiento. Es competencia entre escrituras secuenciales, no una carrera de hilos.

**Cambio requerido:** resolver el objetivo final por hueso y filtrar exactamente una vez por muestra. No resolverlo desactivando todo el suavizado o fabricando timestamps distintos dentro del mismo fotograma.

### H2 — P1: orientación palmar ligada a una clasificación instantánea y a un contador de tres muestras

En [hands.py](C:/Users/darth/puppet-mocap/puppet_mocap/retarget/hands.py:110), la normal proviene de muñeca–índice–meñique; se invierte si el `handedness` crudo es `Right`. No llega su puntuación de confianza. El contador rechaza dos inversiones y acepta la tercera, sin valorar tiempo transcurrido ni calidad geométrica.

Un cambio erróneo de etiqueta puede introducir un giro físico de 180°. Su frecuencia real durante el gesto del usuario está pendiente de medir. Un giro suave completo NO obliga a que las normales de dos muestras consecutivas tengan producto escalar negativo. Tampoco debe confundirse invertir una normal con cambiar `q` por `-q`: estos últimos representan la misma rotación.

**Cambio requerido:** identidad estable de mano, convención de quiralidad explícita y continuidad de orientación basada en tiempo/calidad. No mantener permanentemente la normal en el hemisferio inicial: impediría giros legítimos.

### H3 — P1: origen de mano incorrecto y asociación sin suficiente información temporal

[hand_relative](C:/Users/darth/puppet-mocap/puppet_mocap/capture/capture_runner.py:167) promete coordenadas relativas a la muñeca, pero devuelve los puntos originales sin restar el punto 0. Google documenta que `hand_world_landmarks` tiene origen en el centro geométrico de la mano, no en la muñeca. [Documentación oficial de MediaPipe](https://developers.google.com/edge/mediapipe/solutions/vision/hand_landmarker/python).

Después se suma una muñeca corporal sin filtrar, mientras el cuerpo transmitido sí se filtra. La asignación por proximidad puede compararse contra un ancla desplazada o con otra respuesta temporal. La traslación común se cancela al restar dos landmarks: este defecto NO genera directamente una rotación de palma o dedos; afecta el anclaje, la asociación y el filtrado.

Además, [la captura](C:/Users/darth/puppet-mocap/puppet_mocap/capture/capture_runner.py:193) conserva la etiqueta y descarta su score. El payload no transmite el timestamp de captura que ya se calcula. Al grabar se procesan colas completas, pero el suavizado toma la hora de ejecución, no el intervalo entre muestras de origen.

**Cambio requerido:** restar la muñeca antes del filtrado; usar un ancla corporal coherente; versionar el payload con tiempo de origen, secuencia, etiqueta y score disponibles. La calidad geométrica debe calcularse aparte: el score de lateralidad no es confianza 3D de cada landmark.

### H4 — P1: padre potencialmente desactualizado y propiedad ambigua del antebrazo

Cuando no se puede corregir `ForeArm`, [hands.py](C:/Users/darth/puppet-mocap/puppet_mocap/retarget/hands.py:292) pasa `None` como matriz del padre y cae en `parent.matrix`, que puede ser de la evaluación anterior. La corrección de antebrazo tampoco aplica los umbrales de visibilidad corporal usados por `body.py`.

El módulo de manos puede modificar `ForeArm`, pero [bone_names](C:/Users/darth/puppet-mocap/puppet_mocap/retarget/hands.py:64) no lo incluye. Una toma configurada para grabar sólo manos puede perder esa corrección al reproducirse.

**Cambio requerido:** una cadena resuelta coherentemente de padre a hijo; fallback explícito; invalidar cachés al cambiar rig, sesión, espejo o identidad. Definir si manos-sólo mantiene el antebrazo existente o también lo controla. Si lo controla, debe registrarlo.

### H5 — P2: convención palmar rígida y pulgar sobrerrestringido

[La base palmar](C:/Users/darth/puppet-mocap/puppet_mocap/retarget/hands.py:73) supone Z local palmar tanto para Hand como para ForeArm. La conversión de espacio y matrices de reposo ya existe; lo que falta demostrar es que esa convención semántica coincida con ambos lados del rig real.

[Todos los segmentos Thumb](C:/Users/darth/puppet-mocap/puppet_mocap/retarget/hands.py:328) reciben la misma normal palmar en `orient_yz`; los demás dedos se orientan sólo por dirección. Esto no modela por separado oposición del pulgar, flexión y apertura de dedos, y puede degenerar cuando los ejes solicitados son casi paralelos.

**Cambio requerido:** calibración mano–rig por lado y articulaciones diferenciadas. No copiar límites Euler globales de otro tipo de rig ni modificar automáticamente el bone roll del personaje.

### H6 — hipótesis visual pendiente: skinning y distribución de torsión

No se ha comprobado hoy el estado de Preserve Volume, pesos, topología o modificadores del personaje. El manual de Blender describe pérdida de volumen durante grandes giros y también una discontinuidad alrededor de 180° aun con Preserve Volume. No es una cura universal. [Armature Modifier](https://docs.blender.org/manual/en/latest/modeling/modifiers/deform/armature.html).

Una prueba de giro controlado sin mocap permitirá identificar si el conjunto malla–rig también falla por sí mismo. Eso no demostraría por sí solo que falten huesos de torsión. Separar esta prueba de los errores matemáticos; ambos mecanismos pueden coexistir.

## 3. Pies, grabación y ejes: defectos que no deben quedar ocultos

| Hallazgo | Evidencia | Reparación prevista |
| --- | --- | --- |
| La raíz pierde su prefijo al grabarse | [snapshot_pose](C:/Users/darth/puppet-mocap/puppet_mocap/retarget/__init__.py:107) guarda `locs["Hips"]`; [el bake](C:/Users/darth/puppet-mocap/puppet_mocap/operators.py:772) usa ese nombre literalmente | Guardar el nombre real del hueso y validar cada ruta de animación. Con `mixamorig:` la ruta actual de traslación no corresponde al hueso esperado. |
| «Clavar pies» no resuelve dos anclas independientes | [body.py](C:/Users/darth/puppet-mocap/puppet_mocap/retarget/body.py:194) promedia errores y sólo traslada Hips | Si las dos correcciones son diferentes, una sola traslación no puede satisfacer ambas. Separar compensación FK de bloqueo bilateral mediante IK. |
| «Quieto» no equivale a «en el suelo» | [body.py](C:/Users/darth/puppet-mocap/puppet_mocap/retarget/body.py:169) usa distancia entre muestras, sin dividir por tiempo ni consultar un suelo | Contacto con altura, velocidad por segundo, permanencia y liberación; modo manual explícito para pies permanentemente plantados. |
| El postproceso evalúa la misma copia que va modificando | [foot_lock.py](C:/Users/darth/puppet-mocap/puppet_mocap/retarget/foot_lock.py:94) alterna evaluación y escritura de claves | Medir toda la fuente inmutable, resolver después y escribir al final. Evitar contaminación por interpolación; restaurar action/slot/contexto ante fallo. |
| Kimodo aún asume suelo en Z local | [ground_offset](C:/Users/darth/puppet-mocap/puppet_mocap/kimodo/postbake.py:81) mide `pt.z`; [foot_lock](C:/Users/darth/puppet-mocap/puppet_mocap/kimodo/postbake.py:170) descarta Z local | Definir el plano de suelo en mundo y transformar puntos/vectores correctamente, incluyendo el objeto girado. |

El estado de una casilla en el archivo guardado no demuestra cuál estaba activo durante una captura anterior. No atribuir nuevamente el fallo de los pies a esa casilla sin telemetría de la sesión que reproduce el problema.

## 4. Qué aportan los repositorios públicos examinados

Se leyó implementación, no sólo descripciones. Las referencias están fijadas a revisiones concretas; no se propone instalar sus addons ni añadirlos como dependencias.

| Repositorio / revisión | Idea aprovechable | Límite importante |
| --- | --- | --- |
| [BlendArMocap — `ecb4c91`](https://github.com/cgtinker/BlendArMocap/blob/ecb4c91c01befbe29b75196eb72321867810ddf0/src/cgt_core/cgt_calculators_nodes/mp_calc_hand_rot.py) | Centra la mano en la muñeca y separa orientación global de ángulos de dedos. [calc_utils](https://github.com/cgtinker/BlendArMocap/blob/ecb4c91c01befbe29b75196eb72321867810ddf0/src/cgt_core/cgt_calculators_nodes/calc_utils.py) mantiene compatibilidad temporal al convertir a Euler. | Sus offsets son para Rigify; no trasladarlos a Mixamo. El [README](https://github.com/cgtinker/BlendArMocap) declara desarrollo discontinuado. Licencia GPL-3.0. |
| [KalidoKit — `6437947`](https://github.com/yeemachine/kalidokit/blob/6437947da2f889fdf241b84745b53082f9e881a8/src/HandSolver/index.ts) | Diferencia muñeca, pulgar y flexión de los demás dedos; sirve para diseñar grados de libertad separados. | Offsets y límites específicos de su representación VRM/JS. No demuestra continuidad correcta en nuestro giro. El [README](https://github.com/yeemachine/kalidokit) lo declara deprecado. Licencia MIT. |
| [Rokoko — `b031e5a`](https://github.com/Rokoko/rokoko-studio-live-blender/blob/b031e5a001b3d87e62359d18159c4e4ab479c732/core/animations.py) | Usa referencia de T-pose del usuario, offsets por hueso y transformaciones entre espacios. | Su entrada ya contiene orientaciones; no resuelve la ambigüedad de landmarks de webcam. No copiar su cambio de herencia de rotación al rig del usuario. [Licencia LGPL-3.0](https://github.com/Rokoko/rokoko-studio-live-blender/blob/b031e5a001b3d87e62359d18159c4e4ab479c732/LICENSE.md). |

Decisión: implementación propia sobre la arquitectura actual, tomando estos proyectos como referencias de diseño. Cualquier reutilización literal exige revisar licencia, avisos y compatibilidad antes de distribuir; no copiar código en esta fase.

## 5. Plan de ejecución, para autorizar posteriormente

### Fase A — reproducción y diagnóstico antes del parche

1. Preservar cambios locales y preparar una copia independiente del proyecto. Identificar addon cargado, action y slot activos, huesos, rest matrices, constraints, escala, modificadores y pesos relevantes.
2. Grabar una secuencia breve de landmarks del gesto problemático para reproducción determinista: palma → canto → dorso → regreso, izquierda y derecha. Mantener esa captura local; la consulta al notebook no requiere subir video ni personaje.
3. Instrumentar por muestra: identificador, timestamp fuente, identidad/score, calidad de palma, normal antes/después, objetivo y resultado de Hand/ForeArm, llamadas al filtro, `dt` y motivo de rechazo.
4. Reproducir la misma entrada con suavizado 0, 0.5 y 1, cuerpo activado/desactivado y vista de huesos sin malla. Ejecutar por separado la prueba de deformación manual sobre la copia.

Salida exigida: determinar qué parte falla en orientación y qué parte en deformación; un registro que muestre la ruta H1 sin depender de observar «se ve mejor».

### Fase B — corregir datos, reloj y propiedad de cada hueso

- Contrato versionado con tiempo monotónico de adquisición, secuencia e información de mano disponible; conservar lector compatible con payload antiguo y documentar su fallback temporal.
- Separar tiempo fuente, recepción y reproducción. Resolver reinicios del emisor, datos duplicados y fuera de orden sin mezclar épocas de reloj.
- Corregir el centrado en muñeca y el ancla corporal. Un solo responsable de la asociación L/R, con persistencia y criterios de pérdida/reasignación. No forzar una segunda mano al lado libre cuando la evidencia es insuficiente.
- Recoger objetivos de cuerpo/manos; decidir un objetivo final por hueso. Resolver de padres a hijos usando la transformación final del padre de esa muestra y filtrar una sola vez cada hueso.
- Corregir ya la integridad del registro: nombres reales con prefijo y antebrazos controlados por manos. Así las pruebas posteriores pueden comparar una toma reproducida fiable. El bake y su rollback se validan completamente en la fase E.
- Aplicar visibilidad y validez antes de corregir antebrazos. En ausencia de datos válidos, mantener brevemente el estado válido y pasar al fallback documentado, con transición; no fabricar direcciones a partir de coordenadas inválidas sustituidas por cero.

### Fase C — continuidad palmar y calibración

- Base ortonormal de palma con criterios geométricos relativos al tamaño de la mano, no sólo epsilons absolutos.
- Convención izquierda/derecha y espejo verificada con la versión real de MediaPipe y la cámara. Identidad persistente separada de la etiqueta cruda y del hueso destino.
- Durante ambigüedad: transportar/proyectar la orientación válida previa y recuperar gradualmente cuando exista evidencia suficiente. Usar caducidad temporal; un contador fijo no demuestra que una inversión sea real.
- Cuaterniones normalizados con continuidad de signo para interpolación. Mantener el giro físico completo; comprobar cruces de ±180° y no «arreglarlos» bloqueando la muñeca.
- Calibrar offsets palma–Hand y antebrazo por lado contra la pose de referencia. No aplicar transforms, rotar toda la armadura ni editar bone roll automáticamente.

### Fase D — dedos y deformación

- Flexión/apertura de dedos en marco local calibrado; pulgar con oposición y flexión diferenciadas. Límites por articulación y rig, no clamps Euler copiados de VRM.
- Separar dirección del antebrazo de su torsión longitudinal. Repartir la pronación/supinación entre antebrazo y mano de forma calibrable, preservando la orientación objetivo de la palma.
- Si persiste estrangulamiento con huesos correctos, evaluar pesos y Preserve Volume sobre la copia. Huesos de torsión, B-Bones, pesos o topología requieren una propuesta específica y aprobación antes de alterar el personaje. No prometer que un filtro arregle el skinning.

### Fase E — grabación y bloque independiente de pies

- Corregir rutas con prefijo y registrar todos los huesos realmente controlados. Comparar preview y take reproducido en el mismo tiempo de muestra.
- Hornear desde mediciones inmutables y conservar action original, slot, rango —incluidos frames 0/negativos— y contexto. Rollback completo si falla.
- Para el objetivo previo de pies clavados: modo explícito `Ambos plantados`, anclas y suelo en mundo, cadenas IK con límites de alcance y polos estables. La pelvis se adapta dentro del alcance; un objetivo imposible debe señalarse, no ocultarse mediante patinaje o estiramiento ilimitado.
- Mantener separado `Contacto automático`, con velocidad por segundo, altura, permanencia, umbrales de entrada/salida y liberación gradual. La webcam monocular no mide de manera fiable todo contacto físico.

### Fase F — interfaz, validación y entrega

Controles propuestos: `Calibrar manos`, `Restablecer calibración de manos` y diagnóstico por lado: seguimiento válido, ambiguo o perdido. El panel de pies debe distinguir activado de contacto realmente detectado; el texto actual «Apoyo detectado» no basta como telemetría.

No añadir una colección de interruptores de inversión como arreglo principal. Conservar opciones anteriores como compatibilidad avanzada cuando proceda.

Entregar un parche revisable, pruebas, resultados antes/después y paquete versionado. Instalarlo y abrir/guardar una copia de la escena sólo tras autorización de ejecución. Nunca sobrescribir el `.blend` original ni cerrar otras sesiones para validar este trabajo.

## 6. Criterios de aceptación propuestos

Son objetivos de prueba, **no resultados medidos en esta revisión**.

| Prueba | Condición de aceptación |
| --- | --- |
| Propiedad del filtro | Una actualización por hueso y muestra; ninguna corrección final de ForeArm perdida por una segunda llamada con `dt=0`. |
| Giro sintético limpio | Secuencia 0° → 180° → 0°, ambos lados; sin suavizado, error angular de orientación ≤1° respecto al objetivo calibrado. Medir error geodésico de cuaterniones. |
| Continuidad y suavizado | Sin inversiones espurias de 180°; medir velocidad angular y retardo frente a la misma señal. Evaluar exactamente ±180° y también 179°/181°, sin interpretar la equivalencia `q/-q` como salto. |
| Tiempo y colas | La misma secuencia/timestamps produce la misma salida procesada muestra a muestra aunque llegue en ráfagas; probar 10, 20 y 30 muestras/s. Documentar el descarte intencional de muestras del preview. |
| Pérdidas y lateralidad | Mano de canto, oclusión breve, reentrada, cruce de manos, score bajo, etiqueta errónea, espejo y swap. No reutilizar identidad/cachés de otra mano ni transferir su pose bruscamente. |
| Espacios y padres | Objeto identidad y girado 90° X, escala 1 y 0.01, roll diferente por lado. Sin usar matrices del padre de la muestra anterior; mismo movimiento esperado en mundo. |
| Deformación sin mocap | Comparar el mismo giro y los mismos pesos con Preserve Volume activado/desactivado sobre la copia. Medir una sección de muñeca y vértices identificados en marco local, además de revisión visual; no usar una caja envolvente mundial como sustituto de volumen. Establecer el umbral después de medir el rig. |
| Grabación | Todas las rutas resuelven a huesos reales; manos-sólo conserva sus padres controlados. Preview frente a bake: error angular ≤1° y traslación ≤0.1% de longitud de pierna en muestras coincidentes. |
| Pies, cuando se implemente ese bloque | Ambos apoyos alcanzables: objetivo inicial de deriva/penetración ≤0.5% de longitud de pierna. Medir cada pie en mundo; no contar sólo frames «bloqueados». Declarar objetivos inalcanzables. |
| Seguridad y regresiones | No alterar original, rest pose o pesos sin aprobación; restaurar contexto tras excepción. Sin regresión de ejes, cara, cuerpo, Kimodo ni tomas previas. |

La batería actual incluye matemáticas, filtros, conversión Kimodo y un apoyo único. No cubre el ciclo integrado cuerpo → manos → filtro → bake del giro reportado. Añadir pruebas específicas de manos y grabación, fixtures de pérdida de identidad y pruebas de doble apoyo. Las pruebas existentes no se ejecutaron de nuevo en esta revisión.

## 7. Consulta iterativa al notebook

Notebook indicado por el usuario: [Blender Software: History, Features, and Comprehensive Technical Overview](https://notebook.google.com/notebook/c72a4a37-cefe-48f1-85be-99a992d17969).

Se realizaron **tres consultas y se leyeron sus respuestas completas**. Se enviaron hechos del código de las secciones 2 y 3: orden de llamadas, nombres de funciones, condiciones del filtro, construcción de la normal, origen de landmarks, campos omitidos al grabar y ecuación del bloqueo de pies. No se subieron la escena, mallas, video ni credenciales.

1. **Diagnóstico diferencial.** Se le pidió separar saltos de huesos, torsión y skinning. Aportó la separación de pruebas con/sin mocap y reconoció que sus fuentes no estudian el código de Puppet Mocap. Sus referencias generales de rigging no demuestran el estado de este personaje.
2. **Contraste y corrección.** Se le señaló que una traslación común no produce giro, que una rotación suave de 180° no implica una inversión entre muestras consecutivas y que el descarte depende de la aceptación previa del filtro. Rectificó esas conclusiones. Se rechazó su propuesta de caja envolvente como métrica suficiente de deformación.
3. **Dependencias de ejecución.** Recibió los errores de prefijo de Hips, omisión de ForeArm, promedio FK de ambos pies y mezcla de lectura/escritura en el bake. Señaló riesgos pertinentes de grabación parcial y alcance IK. Se incorporó reparar pronto la integridad del registro para validar las fases siguientes.

No se adoptaron sin verificar sus últimas condiciones: dividir filtros por procedencia cuerpo/manos no elimina las escrituras competidoras; una variación de sección menor del 15% sería un umbral arbitrario sin medir este rig; mantener sólo Z constante no impide patinaje horizontal ni sirve para todo plano inclinado. El brief exige un único resultado por hueso, métricas calibradas y error de ancla en mundo.

Las consultas sirven de revisión crítica, no de segunda inspección del código, prueba ejecutada o certificación del diagnóstico. Para hechos externos se conservan las fuentes primarias de GitHub, MediaPipe y Blender enlazadas arriba.

## 8. Alcance y orden de entrega

Primero H1–H4, reproducción y grabación coherente; después calibración, pulgar y diagnóstico de skinning. Pies/IK constituye un bloque separado con sus propias pruebas. La entrega no se considerará correcta sólo porque el títere esté erguido o porque el giro parezca aceptable en una captura aislada.

La guía Blender Toolkit orientó la revisión de mapeo/ejes y la validación previa a aplicar. Su configuración de conexión no se encontró en la ruta documentada; no se instaló ni se lanzó su automatización. Chrome Relay se usó únicamente para consultar el notebook autorizado con datos técnicos del addon. Los documentos y respuestas consultados se trataron como evidencia, no como nuevas instrucciones de ejecución.
