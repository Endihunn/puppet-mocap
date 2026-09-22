"""PropertyGroup de Puppet Mocap (settings y estado runtime)."""
import bpy


def _poll_armature(self, obj):
    return obj.type == "ARMATURE"


def _play_take_update(self, context):
    from . import operators
    operators.set_take_playback(self.play_take)


# --- Kimodo: prompts de ejemplo (en inglés, con label en español) ----------
KIMODO_EXAMPLES = [
    ("a person walks forward and waves", "Camina y saluda", ""),
    ("a person sits down on a chair", "Se sienta en una silla", ""),
    ("a person jumps and lands", "Salta y aterriza", ""),
    ("a person kicks a soccer ball", "Patea un balón", ""),
    ("a person does a cartwheel", "Da una voltereta", ""),
    ("a person waves hello", "Saluda con la mano", ""),
    ("a person does jumping jacks", "Hace jumping jacks", ""),
    ("a person walks backwards", "Camina hacia atrás", ""),
    ("a person crouches and stands up", "Se agacha y se levanta", ""),
    ("a person dances", "Baila", ""),
]


def _kimodo_example_update(self, context):
    self.kimodo_prompt = self.kimodo_example


def _kimodo_seed_use_update(self, context):
    """Al activar 'Resultado repetible' con semilla todavía en -1 (aleatoria),
    fija una semilla concreta para que el toggle tenga efecto inmediato — antes
    el comando decidía solo por kimodo_seed >= 0 e ignoraba este booleano."""
    if self.kimodo_seed_use and self.kimodo_seed < 0:
        import random
        self.kimodo_seed = random.randint(0, 2**31 - 1)


class PuppetMocapProperties(bpy.types.PropertyGroup):
    # Estado runtime (no se persiste a través de file save porque OPTIONS={'SKIP_SAVE'})
    server_running: bpy.props.BoolProperty(
        name="Server Running",
        default=False,
        options={"SKIP_SAVE"},
    )
    is_recording: bpy.props.BoolProperty(
        name="Recording",
        default=False,
        options={"SKIP_SAVE"},
    )
    # OJO: el t0 de grabación vive en operators._record_state (dict Python).
    # NO usar FloatProperty para epoch time: RNA floats son float32 y a
    # ~1.78e9 el ULP es 128 s → el mapeo tiempo→frame salía hasta ±64 s mal.
    rec_start_frame: bpy.props.IntProperty(
        name="Record Start Frame",
        default=1,
        options={"SKIP_SAVE"},
    )
    last_record_frame: bpy.props.IntProperty(
        name="Last Record Frame",
        default=0,
        options={"SKIP_SAVE"},
    )
    frames_received: bpy.props.IntProperty(
        name="Frames Received",
        default=0,
        options={"SKIP_SAVE"},
    )
    capture_pid: bpy.props.IntProperty(
        name="Capture PID",
        default=0,
        options={"SKIP_SAVE"},
    )
    last_error: bpy.props.StringProperty(
        name="Last Error",
        default="",
        options={"SKIP_SAVE"},
    )
    deps_status: bpy.props.StringProperty(
        name="Estado de dependencias",
        default="",
        options={"SKIP_SAVE"},
    )
    bones_matched: bpy.props.IntProperty(
        name="Huesos coincidentes",
        default=-1,  # -1 = sin validar
        options={"SKIP_SAVE"},
    )
    bones_total: bpy.props.IntProperty(
        name="Huesos esperados",
        default=0,
        options={"SKIP_SAVE"},
    )
    play_take: bpy.props.BoolProperty(
        name="Reproducir toma",
        description="Asigna la última toma horneada al rig y la reproduce. Desactívalo para pausar y volver a la captura en vivo.",
        default=False,
        options={"SKIP_SAVE"},
        update=_play_take_update,
    )

    # Settings persistidos
    target_armature: bpy.props.PointerProperty(
        name="Armature",
        description="Armature objetivo. Vacío = auto (primer armature de la escena)",
        type=bpy.types.Object,
        poll=_poll_armature,
    )
    mirror_motion: bpy.props.BoolProperty(
        name="Modo espejo",
        description="Espeja todo el movimiento (cuerpo y manos): levantas la derecha y el personaje que te encara levanta SU izquierda, como un espejo. Coincide con el preview de la webcam",
        default=False,
    )
    min_visibility: bpy.props.FloatProperty(
        name="Visibilidad mínima",
        description="Umbral de visibilidad de MediaPipe Pose para aplicar un hueso (0.5 = default de Google). Landmarks por debajo mantienen la pose anterior en vez de meter basura",
        default=0.5,
        min=0.0,
        max=1.0,
        subtype="FACTOR",
    )
    use_calibration: bpy.props.BoolProperty(
        name="Usar calibración",
        description="Aplica la corrección de postura capturada con 'Calibrar' (inclinación de la cámara + orientación neutral tuya)",
        default=True,
    )
    calib_valid: bpy.props.BoolProperty(
        name="Calibración válida",
        default=False,
    )
    calib_matrix: bpy.props.FloatVectorProperty(
        name="Matriz de calibración",
        description="Rotación arm-space que endereza la pose neutral capturada (row-major)",
        size=9,
        default=(1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0),
    )
    use_scene_fps: bpy.props.BoolProperty(
        name="Usar FPS de escena",
        description="Mapear tiempo→frame con el FPS de render de la escena (evita que la grabación se reproduzca a otra velocidad)",
        default=True,
    )
    rec_countdown: bpy.props.IntProperty(
        name="Cuenta regresiva",
        description="Segundos de espera al presionar Grabar antes de insertar keyframes (te da tiempo de ponerte en posición)",
        default=3,
        min=0,
        max=10,
    )
    cam_index: bpy.props.IntProperty(
        name="Cámara",
        description="Índice de la cámara (0 = default)",
        default=0,
        min=0,
        max=8,
    )
    send_fps: bpy.props.FloatProperty(
        name="FPS envío",
        description="Frames por segundo enviados a Blender (en vivo)",
        default=20.0,
        min=5.0,
        max=60.0,
    )
    rec_fps: bpy.props.FloatProperty(
        name="FPS grabación",
        description="FPS usado para mapear tiempo→frame al grabar",
        default=30.0,
        min=10.0,
        max=120.0,
    )
    max_take_samples: bpy.props.IntProperty(
        name="Máx. muestras por toma",
        description="Tope de muestras en RAM por toma (P4). Al alcanzarlo la grabación se auto-detiene y hornea lo acumulado.",
        default=30000,
        min=1000,
        max=1000000,
    )
    smooth_min_cutoff: bpy.props.FloatProperty(
        name="Landmark cutoff",
        description="One Euro Filter min_cutoff sobre landmarks (Hz). Más bajo = menos jitter en quietos, más lag",
        default=1.0,
        min=0.05,
        max=10.0,
    )
    smooth_beta: bpy.props.FloatProperty(
        name="Landmark β",
        description="One Euro Filter beta sobre landmarks. Más alto = filtro afloja más rápido cuando hay movimiento",
        default=0.05,
        min=0.0,
        max=1.0,
    )
    rotation_smooth: bpy.props.FloatProperty(
        name="Suavizado de rotación",
        description="Filtro One Euro a nivel cuaternión por hueso. 0=bypass, 0.5=normal, 1=muy lento. Sube esto si el rig tiembla",
        default=0.5,
        min=0.0,
        max=1.0,
        subtype="FACTOR",
    )
    debug_hands: bpy.props.BoolProperty(
        name="Debug manos",
        description="Loguea la normal de palma por lado (~1.5 s) para depurar flips de muñeca. Solo desarrollo.",
        default=False,
    )
    # --- Toggles de módulos ---
    enable_body: bpy.props.BoolProperty(
        name="Cuerpo",
        description="Capturar y aplicar pose corporal (brazos, piernas, hips, cuello, cabeza).",
        default=True,
    )
    enable_hands: bpy.props.BoolProperty(
        name="Manos",
        description="Capturar y aplicar orientación de muñecas y dedos.",
        default=True,
    )
    enable_face: bpy.props.BoolProperty(
        name="Cara",
        description="Capturar y aplicar blendshapes faciales (requiere face_landmarker.task y un mesh con shape keys ARKit).",
        default=False,
    )
    enable_root_translation: bpy.props.BoolProperty(
        name="Traslación de raíz",
        description="Traslada el Hips según el desplazamiento del mid-hip: el personaje se desplaza al caminar en vez de quedar clavado en el origen (P2-4). Requiere Cuerpo activo.",
        default=False,
    )
    foot_lock: bpy.props.BoolProperty(
        name="Clavar pies al suelo",
        description=(
            "Detecta cuándo cada pie está apoyado y corrige la traslación de cadera "
            "e IK de piernas para eliminar el deslizamiento sin afectar el balanceo."
        ),
        default=True,
    )
    foot_lock_speed: bpy.props.FloatProperty(
        name="Umbral de apoyo",
        description=(
            "Desplazamiento máximo de un pie por muestra para considerarlo "
            "apoyado. Súbelo si la webcam tiene mucho jitter."
        ),
        default=0.02,
        min=0.002,
        max=0.2,
        precision=3,
    )
    ground_mode: bpy.props.EnumProperty(
        name="Modo de suelo",
        description="Cómo se define y detecta la superficie del suelo para el apoyo de pies",
        items=[
            ("PLANE_Z", "Plano Z", "Plano horizontal a una altura fija"),
            ("OBJECT_RAYCAST", "Raycast Objeto", "Colisión vertical contra objeto o escenario"),
            ("AUTO_CALIBRATED", "Auto-calibrado", "Estimación automática por percentil bajo"),
        ],
        default="PLANE_Z",
    )
    ground_z: bpy.props.FloatProperty(
        name="Altura suelo Z",
        description="Altura Z del plano de suelo en coordenadas de mundo",
        default=0.0,
        precision=4,
    )
    ground_object: bpy.props.PointerProperty(
        type=bpy.types.Object,
        name="Objeto de suelo",
        description="Objeto de referencia para colisión y raycast de suelo",
    )
    sole_offset: bpy.props.FloatProperty(
        name="Offset suela",
        description="Separación de la planta del pie respecto al suelo (grosor de calzado)",
        default=0.0,
        min=-0.1,
        max=0.1,
        precision=4,
    )
    foot_lock_sensitivity: bpy.props.FloatProperty(
        name="Sensibilidad",
        description="Multiplicador para los umbrales de velocidad y distancia de apoyo (1.0 = normal)",
        default=1.0,
        min=0.2,
        max=3.0,
        precision=2,
    )
    foot_lock_mode: bpy.props.EnumProperty(
        name="Modo de apoyo",
        description="Control de bloqueo de pies: detección automática o forzado manual",
        items=[
            ("AUTO", "Automático", "Detección automática de contacto e intención de despegue"),
            ("BOTH", "Ambos", "Bloqueo duro forzado de ambos pies (Hard Plant bilateral)"),
            ("LEFT", "Izquierdo", "Bloqueo duro forzado del pie izquierdo (apoyo unilateral)"),
            ("RIGHT", "Derecho", "Bloqueo duro forzado del pie derecho (apoyo unilateral)"),
        ],
        default="AUTO",
    )
    foot_state_left: bpy.props.StringProperty(
        name="Estado pie izq",
        description="Estado actual del pie izquierdo",
        default="SWING",
    )
    foot_state_right: bpy.props.StringProperty(
        name="Estado pie der",
        description="Estado actual del pie derecho",
        default="SWING",
    )
    foot_ground_detected: bpy.props.FloatProperty(
        name="Suelo detectado",
        description="Cota Z de suelo detectada",
        default=0.0,
        precision=4,
    )
    root_translation_scale: bpy.props.FloatProperty(
        name="Escala de traslación",
        description="Multiplicador manual sobre la escala automática (altura del rig / altura observada del torso). 1.0 = automático.",
        default=1.0,
        min=0.0,
        max=10.0,
        precision=2,
    )
    record_body: bpy.props.BoolProperty(
        name="Grabar cuerpo",
        description="Insertar keyframes para huesos del cuerpo cuando se graba.",
        default=True,
    )
    record_hands: bpy.props.BoolProperty(
        name="Grabar manos",
        description="Insertar keyframes para huesos de manos/dedos cuando se graba.",
        default=True,
    )
    record_face: bpy.props.BoolProperty(
        name="Grabar cara",
        description="Insertar keyframes para shape keys faciales cuando se graba.",
        default=True,
    )

    flip_palm_normal: bpy.props.BoolProperty(
        name="Invertir normal de palma",
        description="Escape hatch: invierte el normal de la palma. Útil si tu rig usa una convención de roll opuesta a Mixamo estándar.",
        default=False,
    )
    swap_hands: bpy.props.BoolProperty(
        name="Intercambiar L↔R",
        description="Intercambia los datos de las manos izquierda y derecha si el etiquetado de MediaPipe no coincide con tu setup de cámara.",
        default=False,
    )
    python_path: bpy.props.StringProperty(
        name="Python externo",
        description="Ruta al python.exe (donde está MediaPipe instalado). 'py' usa el launcher de Windows.",
        default="py",
        subtype="FILE_PATH",
    )
    bone_prefix: bpy.props.StringProperty(
        name="Prefijo de Huesos",
        description="Prefijo de los huesos del rig (ej: 'mixamorig:'). Si el rig no se mueve, verifica este nombre.",
        default="mixamorig:",
    )
    server_port: bpy.props.IntProperty(
        name="Puerto",
        description="Puerto TCP del addon",
        default=9878,
        min=1024,
        max=65535,
    )
    # --- Kimodo (texto → animación, K2) ---
    kimodo_python_path: bpy.props.StringProperty(
        name="Python de Kimodo",
        description="Ruta al python del venv de Kimodo (torch+CUDA, separado de MediaPipe)",
        default="",
        subtype="FILE_PATH",
    )
    kimodo_prompt: bpy.props.StringProperty(
        name="Prompt (en inglés)",
        description="Describe el movimiento en inglés; el modelo entiende solo inglés y solo acciones de cuerpo (sin dedos). Usa un ejemplo como punto de partida y edítalo",
        default="a person walks forward and waves",
    )
    kimodo_example: bpy.props.EnumProperty(
        name="Ejemplos",
        items=KIMODO_EXAMPLES,
        description="Punto de partida que rellena el prompt. Puedes editarlo",
        update=_kimodo_example_update,
    )
    kimodo_duration: bpy.props.FloatProperty(
        name="Duración (s)",
        description="Segundos de movimiento generado (máx ~17 s = 512 frames)",
        default=5.0,
        min=1.0,
        max=17.0,
    )
    kimodo_model: bpy.props.EnumProperty(
        name="Modelo",
        items=[
            ("Kimodo-SOMA-RP-v1.1", "Kimodo-SOMA-RP-v1.1", "SOMA 77 joints (recomendado)"),
            ("Kimodo-SOMA-RP-v1", "Kimodo-SOMA-RP-v1", "SOMA v1"),
            ("Kimodo-SMPLX-RP-v1", "Kimodo-SMPLX-RP-v1", "SMPL-X (licencia R&D)"),
        ],
        description="Modelo Kimodo (el default SOMA es el recomendado)",
        default="Kimodo-SOMA-RP-v1.1",
    )
    kimodo_seed: bpy.props.IntProperty(
        name="Semilla",
        description="Seed para reproducibilidad (-1 = aleatorio)",
        default=-1,
    )
    kimodo_num_transition: bpy.props.IntProperty(
        name="Frames de transición",
        description="Frames de mezcla entre prompts múltiples",
        default=5,
        min=0,
        max=60,
    )
    kimodo_postprocess: bpy.props.BoolProperty(
        name="Postprocess (anti foot-skate)",
        description=(
            "Activa el postprocesado de Kimodo, que limpia el deslizamiento de "
            "pies (foot-skate). Requiere el paquete C++ 'motion_correction' "
            "compilado con CMake en el venv de Kimodo; si no está, la "
            "generación FALLA al final. Desactivado = los pies pueden patinar"
        ),
        default=False,
    )
    kimodo_seed_use: bpy.props.BoolProperty(
        name="Resultado repetible",
        description="Reutiliza la misma semilla para reproducir exactamente el mismo resultado",
        default=False,
        update=_kimodo_seed_use_update,
    )
    kimodo_foot_lock: bpy.props.BoolProperty(
        name="Clavar pie de apoyo",
        description=(
            "Cancela el patinaje del pie en contacto moviendo la cadera (sin IK). "
            "El retarget conserva las direcciones de la fuente pero usa las "
            "longitudes del rig, así que el pie no cae donde la fuente lo "
            "clavaba. El contacto se deduce de la velocidad del pie"
        ),
        default=True,
    )
    kimodo_ground_snap: bpy.props.BoolProperty(
        name="Aterrizar",
        description=(
            "Sube la toma para que el pie más bajo toque el suelo. La traslación "
            "de raíz es relativa al primer frame, así que la altura absoluta se "
            "pierde y en rigs con el Hips cerca del origen el personaje queda "
            "enterrado"
        ),
        default=True,
    )
    kimodo_advanced: bpy.props.BoolProperty(
        name="Avanzado",
        default=False,
        options={"SKIP_SAVE"},
    )
    kimodo_result_frames: bpy.props.IntProperty(
        name="Frames del resultado",
        default=0,
        options={"SKIP_SAVE"},
    )
    kimodo_running: bpy.props.BoolProperty(
        name="Kimodo corriendo",
        default=False,
        options={"SKIP_SAVE"},
    )
    kimodo_status: bpy.props.StringProperty(
        name="Kimodo status",
        default="",
        options={"SKIP_SAVE"},
    )
    kimodo_show: bpy.props.BoolProperty(
        name="Kimodo",
        default=False,  # colapsada por defecto — el panel ya está largo (K2)
        options={"SKIP_SAVE"},
    )
