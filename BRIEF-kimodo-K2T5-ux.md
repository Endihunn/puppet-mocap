# Brief de ejecución — K2-T5: UI de generación por texto, apta para no programadores

Repo: `C:\Users\darth\puppet-mocap` · rama `master` · último commit `e8deebf` · árbol limpio.
Lee antes: `BRIEF-kimodo-K2.md` (fase), `BRIEF-kimodo-K2T1.md` (conversor).
Blender **5.2.0 LTS** es la única versión instalada; el addon ya está instalado y habilitado ahí.

**Alcance: solo la interfaz de generación por texto.** No se toca el conversor, ni el runner, ni
nada de captura. Un commit por tarea. Sin push.

---

## Estado: la tubería funciona, la interfaz no es usable

Verificado de punta a punta con un Mixamo real (`Idle.fbx`, 65 huesos, prefijo `mixamorig:`):
prompt → 91 fcurves sobre 22 huesos, avance de 6 alturas de cuerpo, sin dedos. **La generación
ya no es el problema.** El problema es que solo la puede operar quien conozca el código.

El objetivo de esta fase es que alguien que nunca ha visto el addon —un alumno, un titiritero,
tú dentro de seis meses— pase de instalar a tener una animación **sin leer documentación y sin
escribir una sola ruta**.

---

## Los doce puntos de fricción (observados en el código, no supuestos)

| # | Dónde | Problema |
|---|---|---|
| 1 | `properties.py` | `kimodo_python_path` está **vacío** por defecto. Hay que localizar y pegar `...\venv\Scripts\python.exe`. Es el muro principal |
| 2 | `panel.py` | `"Requiere: Python de Kimodo en Settings + acceso al modelo"` se muestra con icono `ERROR` **siempre**, incluso cuando todo está bien. Fatiga de alarma |
| 3 | `operators.py` | Errores que apuntan al log: `"faltan deps o modelo (log)"`, `"rc 2"`. Dicen qué se rompió, no qué hacer |
| 4 | `operators.py` | El mensaje de OOM pide definir `TEXT_ENCODER_DEVICE=cpu`, una variable que la UI no ofrece — y que además ya se fija sola desde `e8deebf`. Mensaje muerto |
| 5 | `properties.py` | `kimodo_model` es un `StringProperty` **libre**: un typo rompe la generación |
| 6 | `properties.py` | `kimodo_seed` con `-1 = aleatorio`. Jerga |
| 7 | `properties.py` | `kimodo_num_transition` ("frames de mezcla entre prompts múltiples") se expone **sin que exista UI de múltiples prompts**. Configuración muerta a la vista |
| 8 | `properties.py` | La descripción de `kimodo_postprocess` dice literalmente *"requiere el paquete C++ 'motion_correction' compilado con CMake"*. Y hoy no está compilado, así que activarlo **rompe** la generación |
| 9 | `panel.py` | El prompt es un campo de texto pelado. Nada indica que **debe ir en inglés** (el encoder es Llama-3) ni qué sabe hacer el modelo |
| 10 | `panel.py` | Durante ~30 s solo se ve `"Generando..."` estático. Parece colgado |
| 11 | `operators.py` | Al terminar, la action se hornea pero **no se asigna al slot** (fix P0-1, correcto). El usuario no ve nada moverse y cree que falló |
| 12 | `panel.py` | El aviso de duración >10 s está partido en dos labels y la frase se corta a media palabra: `"máx. de "` / `"entrenamiento del modelo"` |

---

## Principios de diseño

1. **Nada de rutas a mano.** Si hay que escribir una ruta de sistema, el diseño falló.
2. **Estado antes que fallo.** El panel dice si está listo *antes* de pulsar, no revienta después.
3. **Cada error trae su acción.** Nunca "revisa el log" a secas: qué pasó, qué hacer, y un botón
   que lo haga si es automatizable.
4. **Dos niveles.** Básico visible (prompt, duración, Generar); todo lo demás en *Avanzado*,
   plegado. Un no programador nunca debería tener que abrirlo.
5. **Los términos del dominio, no los del código.** "Semilla", "frames de transición" y
   "postprocess" no significan nada fuera de aquí.

---

## U1 — Autodetección del Python de Kimodo

Elimina el punto de fricción nº 1, que bloquea todo lo demás.

Nuevo operador `PUPPET_OT_autodetect_kimodo_python`, y **llamada automática** la primera vez que
se despliega la caja de Kimodo con la ruta vacía (una sola vez por sesión; que no escanee en
cada redibujo — ver el cache negativo con TTL de `face.py:get_cached_mesh`, mismo patrón).

Orden de búsqueda:
1. Variable de entorno `KIMODO_PYTHON` si existe.
2. `%USERPROFILE%\_kimodo_spike\venv\Scripts\python.exe` (la instalación actual).
3. Venvs hermanos del addon y del repo: `../venv`, `../.venv`, `../kimodo*/venv`.
4. `%USERPROFILE%\` en primer nivel: cualquier `*kimodo*/venv/Scripts/python.exe`.

**Validar antes de aceptar**, no solo comprobar que el archivo existe: lanzar
`python -c "import kimodo"` con timeout corto y `CREATE_NO_WINDOW` (el patrón exacto de
`PUPPET_OT_check_deps`). Un python que existe pero no tiene Kimodo no sirve y confunde más.

Si falla, el panel ofrece un selector de archivo, no un campo de texto.

---

## U2 — Panel de estado con lista de comprobación

Sustituye el label de ERROR permanente (nº 2) por un estado real. Cuando **no** está listo, la
caja muestra qué falta, en orden, con su acción al lado:

```
Generar (Kimodo)
├ ✗ Python de Kimodo          [Detectar]  [Elegir…]
├ ✓ Kimodo instalado
├ ✗ Acceso al modelo           [Cómo obtenerlo]
└ ✓ Rig: Armature (mixamorig:)
```

Cuando **todo** está listo, la lista desaparece por completo y solo queda prompt + duración +
Generar. El estado se cachea (TTL ~5 s) — nada de subprocesos por redibujo.

El item "Acceso al modelo" cubre el gate de Hugging Face. Su botón abre la URL con
`bpy.ops.wm.url_open`; el texto explica en una frase que hace falta cuenta de HF y aceptar la
licencia de Meta Llama-3, **sin jerga de tokens ni CLI**.

---

## U3 — El prompt: ejemplos y el idioma

**Ejemplos desplegables.** Un `EnumProperty` "Ejemplos" que al elegir rellena `kimodo_prompt`.
Ocho o diez, agrupados por lo que el modelo hace bien: locomoción, gestos, acciones,
expresivos. En inglés, con su traducción entre paréntesis en el label del enum:

```
"a person walks forward and waves"      (camina y saluda)
"a person sits down on a chair"          (se sienta)
"a person jumps and lands"               (salta y aterriza)
"a person dances happily"                (baila)
...
```

Elegir un ejemplo **rellena el campo, no lo bloquea**: el usuario puede editarlo después. Es un
punto de partida, no un menú cerrado.

**El idioma.** El campo lleva la etiqueta "Prompt (en inglés)" y el tooltip explica por qué en
una frase. Nada de validadores de idioma: si escribe en español el modelo dará algo raro, pero
eso es asunto del modelo, no un error a bloquear.

**Prompt vacío**: el botón Generar se deshabilita (`col.enabled = False`), no reporta error
tras el clic.

---

## U4 — Avanzado, plegado

Subsección `kimodo_show_advanced` (bool, por defecto `False`) que recoge todo lo que hoy está
suelto:

- **`kimodo_model` → `EnumProperty`** con los modelos conocidos (`Kimodo-SOMA-RP-v1.1` como
  default y único recomendado; `v1` y los G1/SMPL-X si se listan, marcados). Se acabaron los
  typos. Mantén el valor actual como default para no romper archivos guardados.
- **`kimodo_seed` → "Variación"**: un `BoolProperty` "Resultado repetible" más el número, que
  solo aparece si está activo. Con él apagado, aleatorio. La palabra "semilla" desaparece de la
  UI (puede quedarse en el tooltip).
- **`kimodo_num_transition`**: **quitar de la UI**. No hay interfaz de múltiples prompts, así
  que hoy no hace nada visible. La property se queda (el runner la usa); solo deja de mostrarse.
- **`kimodo_postprocess`**: se queda en Avanzado, pero **deshabilitado** (`row.enabled = False`)
  con una línea que diga que necesita un componente extra no instalado. Hoy activarlo rompe la
  generación; ofrecerlo como si funcionara es una trampa. Si algún día se compila
  `motion_correction`, se detecta y se habilita solo.

---

## U5 — Progreso y resultado

**Durante la generación** (nº 10), en lugar de `"Generando..."` fijo:

```
Generando… 12 s        [Cancelar]
Cargando el modelo (la primera vez tarda más)
```

El tiempo transcurrido sale del timer de poll, que ya corre a 0.25 s. El texto de fase puede
inferirse del tiempo (los primeros ~25 s son carga de modelo, después difusión) o leyendo la
cola del log de Kimodo — lo segundo es mejor si sale barato; si no, el tiempo basta.

**Al terminar** (nº 11) — esto es lo más importante de la tarea. Hoy la action se hornea sin
asignarse y el usuario no ve nada:

```
✓ Listo: 120 frames (4 s)
[Ver la animación]        ← activa play_take y pone frame_current al inicio
```

El botón reusa `set_take_playback(True)`, que ya existe. **No cambies el comportamiento de
`_bake_channels`**: que la action no se asigne sola es el fix P0-1 y protege la captura en vivo.
Lo que se arregla es que el usuario sepa que hay algo que ver y cómo verlo en un clic.

---

## U6 — Mensajes de error accionables

Reescribe el mapa de `reasons` (nº 3 y 4). Cada uno: qué pasó, qué hacer, y botón si aplica.

| rc | Ahora | Debe decir |
|---|---|---|
| 1 | "faltan deps o modelo (log)" | "Falta instalar Kimodo en ese Python" + botón *Comprobar* |
| 2 | "falló la generación (log)" | "La generación falló" + botón *Ver detalles* que abre el log |
| 3 | "OOM de VRAM — usa el encoder en CPU (`TEXT_ENCODER_DEVICE=cpu`)" | **Mensaje muerto**: desde `e8deebf` el encoder ya va a CPU. Reescribir como "Sin memoria de vídeo. Cierra otras aplicaciones 3D e inténtalo de nuevo" |

Además, el gate de Hugging Face (403 de Meta Llama-3) hoy cae en el cajón de rc=1 y el usuario
no tiene forma de saberlo. **Detéctalo**: si el log del runner contiene `gated repo`,
`meta-llama` o `401/403`, muestra el mensaje específico con el botón que abre la página del
modelo. Es el fallo de primera vez más probable para alguien nuevo.

Corrige de paso el aviso de duración partido a media palabra (nº 12): una sola frase corta,
icono `INFO` en vez de `ERROR` — no es un error, es una advertencia de calidad.

---

## Orden

| Tarea | Bloquea a | Notas |
|---|---|---|
| **U1** autodetección | U2 | Sin esto, U2 no tiene qué mostrar |
| **U2** lista de comprobación | — | |
| **U3** ejemplos + idioma | — | Paralelizable con U2 |
| **U4** avanzado | — | Paralelizable |
| **U5** progreso y resultado | — | Paralelizable |
| **U6** errores | — | Paralelizable |

---

## Criterio de cierre

El de verdad es uno solo, y no es un test automático:

> Alguien que nunca ha visto el addon abre Blender, importa un FBX de Mixamo, despliega
> "Generar (Kimodo)", y obtiene una animación que puede ver moverse — **sin escribir ninguna
> ruta, sin leer documentación y sin abrir el log**.

Recórrelo tú mismo con Blender abierto y **describe cada pantalla que ves**, en orden. Si en
algún punto hay que salir del panel para averiguar algo, ese punto no está resuelto.

Además:

1. **Estado limpio**: con `kimodo_python_path` vacío en un archivo nuevo, el panel detecta el
   venv solo y muestra todo en verde sin intervención.
2. **Estado roto**: apuntando a un python sin Kimodo, la lista muestra el fallo *antes* de
   pulsar Generar, con su acción al lado.
3. **Sin regresión de rendimiento**: ninguna comprobación lanza subprocesos ni escanea el disco
   por redibujo. Verifícalo — el panel se redibuja continuamente.
4. **Independencia intacta**: todo funciona con MediaPipe sin instalar y sin iniciar captura.
5. `py -m pytest tests/` y `py -m ruff check .` en verde, **con la salida real pegada**.

---

## Reglas de reporte

- **Nada verde sin ejecutar.** Pega la salida real. Ya pasó una vez que se reportó "ruff en
  verde" con 7 findings abiertos.
- Describe la UI **por pantallas**, no por diffs. Es una tarea de interfaz: el diff no dice si
  se entiende.
- Si algo de este brief está mal diagnosticado, dilo y para esa tarea. No improvises un rediseño
  sin avisar.
- Si una decisión de UX tiene dos salidas razonables, **reporta y pregunta**. No la cierres solo.

---

## Lo que NO hay que tocar

Vigente todo lo de los briefs anteriores, y además:

- **`_bake_channels` no asigna la action a propósito** (fix P0-1): sus fcurves pisarían la
  captura en vivo. U5 añade un botón para verla, **no** cambia ese comportamiento.
- **`TEXT_ENCODER_DEVICE=cpu` y `TEXT_ENCODER_MODE=local`** se fijan con `setdefault` en el
  Popen del runner (commit `e8deebf`). Son lo que evita el OOM en una tarjeta de 16 GB. No los
  quites ni los conviertas en obligatorios: el `setdefault` permite que un usuario con más VRAM
  los sobrescriba desde el entorno.
- **La escala de raíz sale de `torso_scale()`**, derivada del rig. No la hardcodees ni la
  expongas como slider "por si acaso".
- **Los dedos no reciben canal**, a propósito. No añadas un toggle para "activarlos": lo que
  emitiría son curvas constantes que pisan las manos capturadas.
- **Nada de tocar `_drain_tick`, la `POSE_QUEUE` ni el puerto 9878.** Son de MediaPipe.
- **La caja de Kimodo sigue plegada por defecto** y no se desactiva cuando la captura está
  parada — es independiente.
