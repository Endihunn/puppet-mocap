"""Operators de Puppet Mocap."""
from __future__ import annotations

import math
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import bpy
from bpy.app.handlers import persistent

from . import log, retarget, server


# Subprocess + log file handle (no son serializables, no caben en PropertyGroup)
_subprocess_state = {"proc": None, "log_fh": None}

# Nombre de la última toma horneada (cuerpo/cara). Se desasigna del slot en
# vivo al hornear (P0-1) y "Reproducir toma" la re-asigna por nombre.
_baked_state = {"body": None, "face": None}

# Estado de grabación. NO vive en PropertyGroup: los RNA floats son float32 y
# un epoch (~1.78e9) pierde ±64 s de precisión — la grabación entera salía
# corrida de frames. Aquí son floats de Python de 64 bits.
_record_state = {
    "pending": False,      # cuenta regresiva corriendo
    "active": False,       # insertando samples
    "t_pending_end": 0.0,
    "t0": 0.0,
    "fps": 30.0,
    "start_frame": 1,
    "samples": [],         # list[(elapsed, {bone:(w,x,y,z)}, {shape:val}, {bone:(x,y,z)})]
}

# Escena dueña de la captura (el timer NO debe leer bpy.context.scene: cambiar
# de escena a media captura intercambiaba todos los settings en silencio)
_capture_state = {"scene": None}

# Rate-limit de errores de apply_pose (cada uno loguea traceback a disco)
_err_state = {"n": 0, "t": 0.0}

# Pulso lento (~10 Hz) para contadores RNA + redraw de UI (P1-2)
_ui_pulse_state = {"last": 0.0, "pending_frames": 0, "pending_last_frame": None}

# Recolección de calibración de postura (el drain junta ~1.5 s de landmarks)
_calib_state = {"collecting": False, "until": 0.0, "buf": []}


def _addon_dir() -> Path:
    return Path(__file__).resolve().parent


def _capture_runner_path() -> Path:
    return _addon_dir() / "capture" / "capture_runner.py"


def _model_path() -> Path:
    return _addon_dir() / "models" / "pose_landmarker_lite.task"


def _hand_model_path() -> Path:
    return _addon_dir() / "models" / "hand_landmarker.task"


def _face_model_path() -> Path:
    return _addon_dir() / "models" / "face_landmarker.task"


def _download_model(filename: str) -> tuple[bool, str]:
    """Descarga un .task a models/ con timeout + .tmp + validación de tamaño +
    os.replace atómico. Devuelve (ok, msg)."""
    target = _addon_dir() / "models" / filename
    if target.exists():
        return True, "ya existe"
    url = MODEL_URLS[filename]
    tmp = target.with_suffix(".task.tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        # timeout + descarga a .tmp: sin esto una conexión colgada congelaba
        # Blender indefinidamente y un corte dejaba un .task truncado que
        # pasaba el check de exists().
        with urllib.request.urlopen(url, timeout=20) as resp, open(tmp, "wb") as fh:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                fh.write(chunk)
        size = tmp.stat().st_size
        if size < 1024 * 1024:
            tmp.unlink(missing_ok=True)
            return False, f"descarga truncada ({size} bytes)"
        os.replace(tmp, target)
    except Exception as e:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False, str(e)
    return True, f"{target.stat().st_size / (1024 * 1024):.1f} MB"


# Modelos MediaPipe descargables on demand (P3-2). URLs oficiales; los
# tamaños coinciden con los .task que antes iban commiteados en models/.
MODEL_URLS = {
    "pose_landmarker_lite.task": (
        "https://storage.googleapis.com/mediapipe-models/pose_landmarker/"
        "pose_landmarker_lite/float16/1/pose_landmarker_lite.task"
    ),
    "hand_landmarker.task": (
        "https://storage.googleapis.com/mediapipe-models/hand_landmarker/"
        "hand_landmarker/float16/1/hand_landmarker.task"
    ),
    "face_landmarker.task": (
        "https://storage.googleapis.com/mediapipe-models/face_landmarker/"
        "face_landmarker/float16/1/face_landmarker.task"
    ),
}



def _sync_target(props):
    """Propaga el armature elegido en la UI al estado del retarget."""
    obj = props.target_armature
    retarget.set_target_armature(obj.name if obj is not None else None)


def _assign_action(id_data, kind: str, play: bool):
    """Asigna/desasigna la última toma horneada (por nombre) a `id_data`."""
    name = _baked_state.get(kind)
    if play:
        if not name:
            return
        action = bpy.data.actions.get(name)
        if action is None:
            return
        ad = id_data.animation_data
        if ad is None:
            ad = id_data.animation_data_create()
        ad.action = action  # auto-asigna el slot (verificado en Blender 5.1)
    else:
        ad = id_data.animation_data
        if ad is not None and ad.action is not None:
            ad.action.use_fake_user = True
            ad.action = None


def set_take_playback(play: bool):
    """Toggle "Reproducir toma": re-asigna la última toma (cuerpo + cara) al
    armature objetivo Y reproduce la animación (o pausa + desasigna para
    volver a la captura en vivo). Antes solo asignaba — el usuario tenía que
    darle Play a mano en la línea de tiempo (P1, hallazgo #3)."""
    arm = retarget.get_armature()
    if arm is not None:
        _assign_action(arm, "body", play)
        mesh = retarget.face.get_cached_mesh(arm)
        if mesh is not None and mesh.data.shape_keys is not None:
            _assign_action(mesh.data.shape_keys, "face", play)
    screen = bpy.context.screen
    if screen is None:
        return
    # animation_play() es un TOGGLE — llamarlo sin guardia mientras ya
    # reproduce lo pausaría en vez de mantenerlo reproduciendo.
    if play:
        if not screen.is_animation_playing:
            bpy.ops.screen.animation_play()
    elif screen.is_animation_playing:
        bpy.ops.screen.animation_cancel(restore_frame=False)


def _load_calibration(props):
    """props (float32, persistente) → common._state (Matrix)."""
    from mathutils import Matrix
    if props.calib_valid:
        v = props.calib_matrix
        retarget.common._state["calib_R"] = Matrix((
            (v[0], v[1], v[2]), (v[3], v[4], v[5]), (v[6], v[7], v[8])))
    else:
        retarget.common._state["calib_R"] = None


def _store_calibration(props, R):
    if R is None:
        props.calib_valid = False
    else:
        props.calib_matrix = (R[0][0], R[0][1], R[0][2],
                              R[1][0], R[1][1], R[1][2],
                              R[2][0], R[2][1], R[2][2])
        props.calib_valid = True
    retarget.common._state["calib_R"] = R


def _close_capture_log():
    fh = _subprocess_state.get("log_fh")
    if fh is not None:
        try:
            fh.flush()
            fh.close()
        except Exception:
            log.exception("cerrando log handle del subprocess")
    _subprocess_state["log_fh"] = None


def _terminate_subprocess():
    proc = _subprocess_state.get("proc")
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
            try:
                rc = proc.wait(timeout=2.0)
                log.info(f"subprocess terminó con rc={rc}")
            except subprocess.TimeoutExpired:
                log.warn("subprocess no respondió a terminate; killing")
                proc.kill()
        except Exception:
            log.exception("cerrando subprocess de captura")
    _subprocess_state["proc"] = None
    _close_capture_log()


def _reset_runtime_props(props):
    props.server_running = False
    props.capture_pid = 0


def _cleanup_capture(props=None, *, unregister_timer: bool = True):
    """Apaga todo: subprocess, server, timer. Compartido por stop_capture,
    el watchdog del timer, unregister() y el handler de load_pre."""
    _terminate_subprocess()
    if not server.stop():
        log.warn("server.stop() dejó el thread vivo (is_running seguirá True)")
    _record_state.update(pending=False, active=False, samples=[])
    _calib_state.update(collecting=False, buf=[])
    _capture_state["scene"] = None
    if props is not None:
        _reset_runtime_props(props)
    if unregister_timer and bpy.app.timers.is_registered(_drain_timer):
        try:
            bpy.app.timers.unregister(_drain_timer)
        except Exception:
            pass


@persistent
def _on_load_pre(*_args):
    """File > Open a media captura: sin esto, el timer persistente seguía
    retargeteando la webcam sobre el primer armature del archivo NUEVO. Además
    invalida el cache del armature (P0-4: puntero stale tras File>Open)."""
    try:
        log.info("load_pre: apagando captura")
        _cleanup_capture(None)
        retarget.common._state["arm"] = None
        retarget.reset_smoothing()
        _baked_state["body"] = None
        _baked_state["face"] = None
    except Exception:
        pass


def register_handlers():
    if _on_load_pre not in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.append(_on_load_pre)


def unregister_handlers():
    if _on_load_pre in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.remove(_on_load_pre)


def _tag_redraw_ui():
    """El N-panel no se redibuja solo desde un timer: sin esto, los contadores
    parecían congelados mientras el usuario actuaba lejos del mouse."""
    wm = bpy.context.window_manager
    if wm is None:
        return
    for window in wm.windows:
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                for region in area.regions:
                    if region.type == "UI":
                        region.tag_redraw()


def _armature_transform_warning(arm) -> str:
    """Detecta transformaciones de objeto que el retarget no puede resolver.

    La rotación del objeto Armature sí está soportada: el retarget convierte
    el espacio canónico a los ejes locales del rig antes de orientar los
    huesos. Solo una escala no uniforme (o degenerada) queda como advertencia.
    """
    if arm is None:
        return ""
    scale = arm.matrix_world.to_scale()
    magnitudes = [abs(float(v)) for v in scale]
    if min(magnitudes) <= 1e-8:
        return "El armature tiene una escala degenerada; aplica Scale antes de capturar."
    if max(magnitudes) / min(magnitudes) <= 1.01:
        return ""
    return ("El armature tiene escala no uniforme; aplica Object > Apply > Scale "
            "para que los ejes y las longitudes del retarget sean consistentes.")


def _validate_rig(props, include_body: bool = True, include_hands: bool = True,
                  write: bool = True):
    """Valida armature + prefijo de huesos. Compartido por start_capture y
    generate_motion (K2). Devuelve (ok, error_msg).

    `write=False` para llamarlo desde un Panel.draw(): Blender prohíbe escribir
    en datos ID (aquí, props del Scene) durante el dibujado y la excepción
    aborta el draw() entero.
    """
    arm = retarget.get_armature()
    if arm is None:
        return False, "No hay un armature en la escena. Importa un FBX Mixamo primero."
    expected = retarget.get_keyframe_bones(
        props.bone_prefix, include_body=include_body, include_hands=include_hands)
    if expected:
        matched = sum(1 for n in expected if n in arm.pose.bones)
        if write:
            props.bones_matched = matched
            props.bones_total = len(expected)
        if matched == 0:
            hint = ""
            for bone in arm.data.bones:
                if bone.name.endswith("Hips"):
                    hint = f" ¿Prefijo correcto: '{bone.name[:-4]}'? Usa 'Detectar prefijo'."
                    break
            return False, f"Ningún hueso coincide con el prefijo '{props.bone_prefix}'.{hint}"
    return True, ""


# --- Kimodo (texto → animación, K2) ----------------------------------------

_KIMODO_STATE = {"proc": None, "log_fh": None, "out_path": None, "scene": None,
                 "t0": 0.0, "done_ok": False, "last_name": None,
                 "last_start": 1, "last_rc": None, "gate": False}
# Repo gated del text encoder (LLM2Vec va sobre Llama-3). Su presencia en la
# caché de HuggingFace es la prueba REAL de que el acceso está concedido y de
# que la generación puede correr sin red.
LLAMA_REPO_DIR = "models--meta-llama--Meta-Llama-3-8B-Instruct"
_KIMODO_ENCODER = {"t": 0.0, "ok": False}
_KIMODO_ENCODER_TTL = 30.0


def _hf_hub_dir() -> Path:
    """Raíz de la caché del hub, respetando HF_HUB_CACHE / HF_HOME."""
    hub = os.environ.get("HF_HUB_CACHE")
    if hub:
        return Path(hub)
    home = os.environ.get("HF_HOME")
    if home:
        return Path(home) / "hub"
    return Path.home() / ".cache" / "huggingface" / "hub"


def _kimodo_encoder_ready() -> bool:
    """True si los pesos del text encoder ya están descargados.

    Antes el panel usaba "¿hubo una generación exitosa en ESTA sesión?" como
    sustituto, así que al reabrir Blender pedía acceso a Hugging Face a un
    usuario que ya lo tenía. Esto es un check de disco, persistente y barato
    (con TTL), y además responde a lo que de verdad importa: si los pesos están,
    la generación funciona incluso sin red.
    """
    now = time.monotonic()
    if now - _KIMODO_ENCODER["t"] < _KIMODO_ENCODER_TTL:
        return _KIMODO_ENCODER["ok"]
    ok = False
    try:
        snaps = _hf_hub_dir() / LLAMA_REPO_DIR / "snapshots"
        if snaps.is_dir():
            ok = any(any(d.glob("*.safetensors")) for d in snaps.iterdir() if d.is_dir())
    except OSError:
        ok = False
    _KIMODO_ENCODER.update(t=now, ok=ok)
    return ok


_KIMODO_AUTODETECT = {"t": 0.0, "done": False}
_KIMODO_AUTODETECT_TTL = 5.0


def _kimodo_runner_path() -> Path:
    return _addon_dir() / "kimodo" / "kimodo_runner.py"


def _kimodo_log_tail(chars: int = 4000) -> str:
    """Últimos chars del log del subprocess de Kimodo (para diagnóstico de errores)."""
    try:
        p = log.get_kimodo_log_path()
        if not Path(p).exists():
            return ""
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            return f.read()[-chars:]
    except OSError:
        return ""


def _kimodo_log_tail_lower() -> str:
    return _kimodo_log_tail().lower()


def _kimodo_python_valid(path) -> bool:
    """True si `path` es un python con kimodo importable (no solo que exista).
    BLOQUEANTE (subprocess.run) -- solo llamar desde un operator (click
    explícito del usuario), nunca desde Panel.draw(). Un venv real con
    torch+transformers puede tardar 25-35s en el primer import; ese costo es
    aceptable como reacción a un botón, no como bloqueo silencioso del
    redibujado. Ver `_kimodo_python_check_poll` para el equivalente no
    bloqueante que sí es seguro en draw()."""
    if not path or not Path(path).exists():
        return False
    flags = 0x08000000 if sys.platform == "win32" else 0
    try:
        r = subprocess.run([str(path), "-c", "import kimodo"],
                           capture_output=True, timeout=60, creationflags=flags)
        return r.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


# Estado del check NO bloqueante de `import kimodo`. Un solo Popen en vuelo
# por sesión; Panel.draw() lo lanza y sondea sin nunca esperar a que termine.
_KIMODO_PYCHECK = {"path": None, "proc": None, "ok": None}


def _kimodo_python_check_poll(path) -> bool | None:
    """Equivalente no bloqueante de `_kimodo_python_valid`, seguro para
    Panel.draw(). Devuelve True/False si ya se sabe, o None mientras el
    subprocess sigue corriendo (o no se ha lanzado aún para este `path` --
    lo lanza y devuelve None en la misma llamada).

    Por qué existe: `_kimodo_checklist` (panel.py) llamaba a
    `_kimodo_python_valid` -- subprocess.run síncrono -- en cada redibujo del
    panel mientras la sección Kimodo estuviera desplegada. Con un venv rápido
    eso no se notaba; con torch+transformers reales (25-35s de import) cada
    redibujo colgaba Blender hasta 10s, repitiéndose cada vez que expiraba el
    caché de 5s del checklist -- cualquier interacción con un menú redibuja
    el panel y dispara el cuelgue.
    """
    if not path or not Path(path).exists():
        return False
    st = _KIMODO_PYCHECK
    if st["path"] != path:
        flags = 0x08000000 if sys.platform == "win32" else 0
        try:
            proc = subprocess.Popen([str(path), "-c", "import kimodo"],
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL,
                                    creationflags=flags)
        except OSError:
            proc = None
        st.update(path=path, proc=proc, ok=(False if proc is None else None))
    proc = st["proc"]
    if proc is not None:
        rc = proc.poll()
        if rc is None:
            return st["ok"]  # sigue corriendo -- último resultado conocido
        st["ok"] = (rc == 0)
        st["proc"] = None
    return st["ok"]


def _autodetect_kimodo_python(props) -> str:
    """Busca y valida un python de Kimodo. Devuelve el path (o '' si no hay)."""
    def _try(p) -> str | None:
        p = str(p)
        return p if _kimodo_python_valid(p) else None
    env = os.environ.get("KIMODO_PYTHON")
    if env:
        r = _try(env)
        if r:
            return r
    r = _try(os.path.expanduser("~\\_kimodo_spike\\venv\\Scripts\\python.exe"))
    if r:
        return r
    base = _addon_dir().parent
    for cand in (base / "venv", base / ".venv"):
        r = _try(cand / "Scripts" / "python.exe")
        if r:
            return r
    import glob
    for d in sorted(glob.glob(os.path.expanduser("~\\*kimodo*\\venv\\Scripts\\python.exe"))):
        r = _try(d)
        if r:
            return r
    return ""


def _maybe_autodetect_kimodo(props):
    """Autodetecta UNA vez por sesión la primera vez que la caja se despliega
    con la ruta vacía (cache negativo con TTL; nada de escanear por redibujo)."""
    if props.kimodo_python_path or _KIMODO_AUTODETECT["done"]:
        return
    now = time.monotonic()
    if now - _KIMODO_AUTODETECT["t"] < _KIMODO_AUTODETECT_TTL:
        return
    _KIMODO_AUTODETECT["t"] = now
    _KIMODO_AUTODETECT["done"] = True
    found = _autodetect_kimodo_python(props)
    if found:
        props.kimodo_python_path = found
        props.kimodo_status = "Python de Kimodo detectado automáticamente"


def _kimodo_error_message(props, rc) -> str:
    """Traduce el returncode del runner a un mensaje ACCIONABLE (U6). Detecta el
    gate de Llama-3 leyendo el log."""
    low = _kimodo_log_tail_lower()
    gate_markers = ("gated repo", "meta-llama", "401", "403",
                    "you are not in the authorized", "cannot access gated")
    if any(m in low for m in gate_markers):
        _KIMODO_STATE["gate"] = True
        return ("El modelo requiere acceso: necesitas cuenta en Hugging Face y "
                "aceptar la licencia de Meta Llama-3")
    _KIMODO_STATE["gate"] = False
    if rc == 1:
        return "Falta instalar Kimodo en ese Python"
    if rc == 2:
        return "La generación falló"
    if rc == 3:
        return "Sin memoria de vídeo. Cierra otras aplicaciones 3D"
    return f"La generación terminó con un error (código {rc})"


def _close_kimodo_log():
    fh = _KIMODO_STATE.get("log_fh")
    if fh is not None:
        try:
            fh.flush()
            fh.close()
        except Exception:
            log.exception("cerrando log del subprocess de Kimodo")
    _KIMODO_STATE["log_fh"] = None


def _kill_kimodo_proc():
    proc = _KIMODO_STATE.get("proc")
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                proc.kill()
        except Exception:
            log.exception("cerrando subprocess de Kimodo")
    _KIMODO_STATE["proc"] = None
    _close_kimodo_log()


def _kimodo_get_props():
    scene_name = _KIMODO_STATE.get("scene")
    if scene_name:
        scene = bpy.data.scenes.get(scene_name)
        if scene is not None:
            return scene.puppet_mocap
    return bpy.context.scene.puppet_mocap


def _kimodo_bake(props, out_path) -> str:
    """Lee el .npz, convierte Kimodo→Mixamo y hornea a una action 'PuppetTake'.
    Devuelve el nombre de la action."""
    import numpy as np
    from .kimodo import convert as kconv

    arm = retarget.get_armature()
    if arm is None:
        raise RuntimeError("sin armature al hornear toma de Kimodo")
    with np.load(out_path, allow_pickle=False) as z:
        motion = {k: np.asarray(z[k]) for k in z.files}
    j = int(motion["posed_joints"].shape[1])

    rest3, rest_pos, parent_of = {}, {}, {}
    for b in arm.data.bones:
        rest3[b.name] = b.matrix_local.to_3x3()
        rest_pos[b.name] = b.matrix_local.to_translation()
        parent_of[b.name] = b.parent.name if b.parent else None

    joint_names = kconv.joint_order(j)
    mapping = kconv.prefixed_mapping(kconv.mapping_for(j), props.bone_prefix)
    rotations, root = kconv.convert_motion(
        motion, joint_names, rest3, rest_pos, parent_of, mapping, fps=30.0,
        input_to_armature=retarget.common.canonical_to_armature_matrix(arm),
    )

    start = max(1, bpy.context.scene.frame_current)
    channels = {}
    for bone, pts in rotations.items():
        esc = bpy.utils.escape_identifier(bone)
        path = f'pose.bones["{esc}"].rotation_quaternion'
        for i in range(4):
            channels[(path, i, bone)] = [(start + f, q[i]) for f, q in pts]
    for bone, pts in root.items():
        esc = bpy.utils.escape_identifier(bone)
        path = f'pose.bones["{esc}"].location'
        for i in range(3):
            channels[(path, i, bone)] = [(start + f, v[i]) for f, v in pts]
    action = _bake_channels(arm, "OBJECT", "PuppetTake", channels, owner=arm.name)
    _baked_state["body"] = action.name   # el postproceso la re-asigna por nombre
    n_frames = int(motion["posed_joints"].shape[0])

    # Postproceso de la toma: clavar el pie de apoyo y aterrizar. Ambos mueven
    # la cadera con la action asignada (Blender hace la FK). Medido en
    # gvhmr-mocap: patinaje 0.192 -> 0.034 u; pie más bajo -0.449 -> +0.065.
    root_bone = mapping.get("Hips") or mapping.get("pelvis")
    rest3_root = rest3.get(root_bone)
    if root_bone and rest3_root is not None and (props.kimodo_foot_lock
                                                 or props.kimodo_ground_snap):
        from .kimodo import postbake
        _assign_action(arm, "body", True)
        try:
            if props.kimodo_foot_lock:
                # el nombre de joint del pie depende del esqueleto (SOMA/SMPL-X)
                feet = [n for n in ("LeftToeBase", "RightToeBase",
                                    "left_foot", "right_foot")
                        if n in joint_names]
                bone_of = {n: mapping[n][len(props.bone_prefix):]
                           for n in feet if n in mapping}
                if bone_of:
                    contacts = postbake.contacts_from_source(
                        motion["posed_joints"], joint_names, list(bone_of))
                    moved = postbake.foot_lock(
                        action, arm, props.bone_prefix, root_bone, rest3_root,
                        contacts, bone_of, start, n_frames)
                    if moved:
                        log.info(f"foot lock: corrección acumulada {moved:.3f}")
            if props.kimodo_ground_snap:
                dz = postbake.ground_offset(arm, props.bone_prefix, start, n_frames)
                if postbake.apply_ground_offset(action, arm, root_bone, rest3_root, dz):
                    log.info(f"ground snap: {dz:+.3f} (espacio de armature)")
        except Exception:
            log.exception("postproceso de la toma de Kimodo")
        finally:
            _assign_action(arm, "body", False)

    _baked_state["body"] = action.name
    _KIMODO_STATE["last_start"] = start
    props.kimodo_result_frames = n_frames
    return action.name


def _kimodo_poll_timer():
    """Timer (~0.25s) que vigila el subprocess de Kimodo. SEPARADO de
    _drain_timer: el server TCP es de MediaPipe, no de Kimodo."""
    props = _kimodo_get_props()
    proc = _KIMODO_STATE.get("proc")
    if proc is None:
        return None  # unregister
    t0 = _KIMODO_STATE.get("t0", 0.0)
    rc = proc.poll()
    if rc is None:
        elapsed = (time.time() - t0) if t0 else 0.0
        props.kimodo_status = f"Generando… {elapsed:.0f} s"
        return 0.25  # sigue corriendo
    out = _KIMODO_STATE.get("out_path")
    _KIMODO_STATE["proc"] = None
    _close_kimodo_log()
    _KIMODO_STATE["done_ok"] = False
    _KIMODO_STATE["last_rc"] = rc
    if rc == 0 and out and Path(out).exists():
        try:
            _kimodo_bake(props, out)
            frames = int(getattr(props, "kimodo_result_frames", 0) or 0)
            props.kimodo_status = f"Listo: {frames} frames ({frames / 30.0:.0f} s)"
            log.info(f"kimodo ok: {props.kimodo_status}")
            _KIMODO_STATE["done_ok"] = True
        except Exception:
            log.exception("kimodo bake")
            props.kimodo_status = "Error al hornear la toma generada"
    else:
        props.kimodo_status = _kimodo_error_message(props, rc)
        log.warn(f"kimodo subprocess rc={rc}")
    props.kimodo_running = False
    _tag_redraw_ui()
    return None


# --- Bake (buffer → fcurves) -----------------------------------------------

def _bake_channels(id_data, id_type: str, name: str, channels: dict, owner: str = ""):
    """channels: {(data_path, index, group|None): [(frame, value), ...]}.
    API de slotted actions (Blender 4.4+; en 5.x action.fcurves ya no existe).
    foreach_set es órdenes de magnitud más rápido que keyframe_insert.
    `owner` marca la action con el nombre del armature dueño (T3) para que
    "Borrar Keyframes" no destruya las tomas de OTROS rigs del archivo."""
    action = bpy.data.actions.new(name)
    ad = id_data.animation_data
    if ad is None:
        ad = id_data.animation_data_create()
    if ad.action is not None:
        # Nunca destruir la toma anterior — fake_user la preserva en el archivo
        ad.action.use_fake_user = True
    ad.action = action
    slot = action.slots.new(id_type=id_type, name=getattr(id_data, "name", "Slot"))
    ad.action_slot = slot
    layer = action.layers.new("Base")
    strip = layer.strips.new(type="KEYFRAME")
    cb = strip.channelbag(slot, ensure=True)
    groups = {}
    for (data_path, index, group), pts in channels.items():
        if not pts:
            continue
        fc = cb.fcurves.new(data_path, index=index)
        if group:
            g = groups.get(group)
            if g is None:
                g = cb.groups.new(group)
                groups[group] = g
            fc.group = g
        n = len(pts)
        fc.keyframe_points.add(n)
        flat = []
        for f, v in pts:
            flat.append(float(f))
            flat.append(float(v))
        fc.keyframe_points.foreach_set("co", flat)
        # 1 = LINEAR. P2-5: interpolar componentes de cuaternión no es slerp;
        # con keys densos (20-30/s) el error es invisible, pero revisitarlo
        # si se añade decimado de keys.
        fc.keyframe_points.foreach_set("interpolation", [1] * n)
        fc.update()
    # P0-1: la action horneada NO debe quedar asignada al slot en vivo — sus
    # fcurves re-evaluarían en cada cambio de frame y pisarían la captura en
    # vivo (el rig "rebota" a la pose horneada). Se conserva con fake_user y un
    # marcador; el panel la re-asigna con "Reproducir toma".
    action["puppet_mocap_take"] = True
    action["puppet_mocap_owner"] = owner
    action.use_fake_user = True
    ad.action = None
    return action


def _bake_take(arm, samples, start_frame: int, fps: float):
    """Hornea el buffer de grabación a una action nueva ('PuppetTake').
    Devuelve (nombre_action, (frame_ini, frame_fin)) o None si no hubo datos."""
    frame_map = {}
    for elapsed, bones, shapes, locs in samples:
        f = start_frame + int(round(elapsed * fps))
        frame_map[f] = (bones, shapes, locs)  # colisión → gana el último sample
    if not frame_map:
        return None
    frames = sorted(frame_map)

    bone_tracks: dict = {}
    shape_tracks: dict = {}
    loc_tracks: dict = {}
    for f in frames:
        bones, shapes, locs = frame_map[f]
        for name, q in bones.items():
            bone_tracks.setdefault(name, []).append((f, q))
        for name, v in shapes.items():
            shape_tracks.setdefault(name, []).append((f, v))
        for name, v in locs.items():
            loc_tracks.setdefault(name, []).append((f, v))

    # Continuidad de hemisferio: q y -q son la misma rotación, pero si la
    # curva salta de signo la interpolación LINEAR da vueltas locas.
    for name, pts in bone_tracks.items():
        prev = None
        fixed = []
        for f, q in pts:
            if prev is not None and sum(a * b for a, b in zip(prev, q)) < 0.0:
                q = tuple(-c for c in q)
            fixed.append((f, q))
            prev = q
        bone_tracks[name] = fixed

    body_action = None
    face_action = None
    if bone_tracks or loc_tracks:
        channels = {}
        for name, pts in bone_tracks.items():
            esc = bpy.utils.escape_identifier(name)
            path = f'pose.bones["{esc}"].rotation_quaternion'
            for i in range(4):
                channels[(path, i, name)] = [(f, q[i]) for f, q in pts]
        for name, pts in loc_tracks.items():
            esc = bpy.utils.escape_identifier(name)
            path = f'pose.bones["{esc}"].location'
            for i in range(3):
                channels[(path, i, name)] = [(f, v[i]) for f, v in pts]
        body_action = _bake_channels(arm, "OBJECT", "PuppetTake", channels, owner=arm.name)

    if shape_tracks:
        mesh = retarget.face.get_cached_mesh(arm)
        if mesh is not None:
            channels = {
                (f'key_blocks["{bpy.utils.escape_identifier(name)}"].value', 0, None): pts
                for name, pts in shape_tracks.items()
            }
            face_action = _bake_channels(
                mesh.data.shape_keys, "KEY", "PuppetTake_cara", channels,
                owner=arm.name)

    _baked_state["body"] = body_action.name if body_action is not None else None
    _baked_state["face"] = face_action.name if face_action is not None else None
    name = _baked_state["body"] or _baked_state["face"]
    return name, (frames[0], frames[-1])


def _finish_recording(scene, props):
    """Cierra la grabación (si había) y hornea el buffer. Seguro de llamar
    aunque no se estuviera grabando."""
    st = _record_state
    was_active = st["active"] and st["samples"]
    props.is_recording = False
    result = None
    if was_active:
        arm = retarget.get_armature()
        if arm is not None:
            try:
                result = _bake_take(arm, st["samples"], st["start_frame"], st["fps"])
            except Exception:
                log.exception("bake de la toma")
        if result is not None:
            _name, (f0, f1) = result
            scene.frame_end = max(scene.frame_end, f1)
            scene.frame_current = st["start_frame"]
            log.info(f"toma horneada: frames {f0}-{f1} "
                     f"({len(st['samples'])} samples @ {st['fps']:.3g} fps)")
    props.play_take = False
    st.update(pending=False, active=False, samples=[])
    return result


# --- Timer de drenado -------------------------------------------------------

def _drain_timer():
    """Llamado por bpy.app.timers para drenar la queue de poses."""
    try:
        return _drain_tick()
    except Exception:
        # Un tick que truena no debe matar el timer (el subprocess sigue
        # mandando); loguea y reintenta.
        log.exception("drain timer")
        return 0.1


def _flush_ui_pulse(props):
    """Vuelca el contador acumulado de frames y last_record_frame a la RNA, y
    redibuja SOLO si había algo pendiente. Sin time-gate: los caminos de salida
    de _drain_tick (early return, watchdog) lo llaman para no dejar el contador
    corto (T4), sin reintroducir el redraw a 50 Hz."""
    dirty = False
    if _ui_pulse_state["pending_frames"]:
        props.frames_received += _ui_pulse_state["pending_frames"]
        _ui_pulse_state["pending_frames"] = 0
        dirty = True
    if _ui_pulse_state["pending_last_frame"] is not None:
        props.last_record_frame = _ui_pulse_state["pending_last_frame"]
        _ui_pulse_state["pending_last_frame"] = None
        dirty = True
    if dirty:
        _tag_redraw_ui()


def _drain_tick():
    if not server.is_running():
        return None  # unregister
    scene = None
    if _capture_state["scene"]:
        scene = bpy.data.scenes.get(_capture_state["scene"])
    if scene is None:
        scene = bpy.context.scene
    if scene is None:
        return 0.1
    props = scene.puppet_mocap
    now = time.time()

    # Watchdog: el subprocess murió (deps faltantes, webcam ocupada, usuario
    # cerró la ventana). Antes esto era un "esperando cliente..." eterno.
    proc = _subprocess_state.get("proc")
    if proc is not None and proc.poll() is not None:
        rc = proc.returncode
        reasons = {
            0: "ventana de captura cerrada",
            1: "faltan dependencias o modelo (revisa el log: botón Abrir Log)",
            2: "webcam ocupada o inexistente",
        }
        props.last_error = f"Captura terminó: {reasons.get(rc, f'código {rc}')}"
        log.warn(f"subprocess murió rc={rc}; apagando captura")
        _finish_recording(scene, props)
        _cleanup_capture(props, unregister_timer=False)
        _flush_ui_pulse(props)  # T4: volcar lo pendiente antes de apagar
        return None

    st = _record_state
    if props.is_recording and st["pending"] and now >= st["t_pending_end"]:
        st.update(pending=False, active=True, t0=now, samples=[])
        log.info(f"REC activo @ frame {st['start_frame']}")
    recording = props.is_recording and st["active"]

    msgs = []
    while len(msgs) < 60:
        try:
            msgs.append(server.POSE_QUEUE.get_nowait())
        except Exception:
            break
    pose_msgs = [m for m in msgs if m.get("t") == "pose"]

    # Recolección de calibración de postura. El timeout se evalúa AUNQUE no
    # lleguen mensajes — si el stream se corta a media recolección, el estado
    # "Capturando..." no debe quedarse pegado.
    cal = _calib_state
    if cal["collecting"]:
        for m in pose_msgs:
            if m.get("lm"):
                cal["buf"].append(m["lm"])
        if now >= cal["until"]:
            cal["collecting"] = False
            R = retarget.compute_calibration(cal["buf"])
            cal["buf"] = []
            if R is None:
                props.last_error = "Calibración falló: no vi tu pose (¿cuerpo completo en cámara?)"
                log.warn("calibración sin datos suficientes")
            else:
                _store_calibration(props, R)
                props.last_error = ""
                log.info(f"calibración capturada: {R!r}")
            _tag_redraw_ui()

    if not pose_msgs:
        # T4: volcar lo pendiente antes de salir — el contador no debe quedar
        # corto cuando el stream se corta.
        _flush_ui_pulse(props)
        return 0.02

    # En vivo solo importa la pose más reciente — aplicar todo el backlog
    # tras un hiccup del viewport era una espiral de stutter. Grabando sí se
    # aplican todas (cada una cae en un frame distinto).
    apply_list = pose_msgs if recording else pose_msgs[-1:]

    include_body = props.enable_body and props.record_body
    include_hands = props.enable_hands and props.record_hands
    include_face = props.enable_face and props.record_face
    last_frame = None
    arm = retarget.get_armature()

    for m in apply_list:
        try:
            retarget.apply_pose(
                landmarks=m.get("lm"),
                hands_data=m.get("hands"),
                face_data=m.get("face"),
                prefix=props.bone_prefix,
                swap_hands=props.swap_hands,
                flip_palm_normal=props.flip_palm_normal,
                rotation_smooth=props.rotation_smooth,
                mirror=props.mirror_motion,
                min_visibility=props.min_visibility,
                calibrate=props.use_calibration,
                enable_body=props.enable_body,
                enable_hands=props.enable_hands,
                enable_face=props.enable_face,
                root_translation=props.enable_root_translation,
                root_scale=props.root_translation_scale,
                foot_lock=props.foot_lock,
                foot_lock_speed=props.foot_lock_speed,
                ground_mode=props.ground_mode,
                ground_z=props.ground_z,
                ground_object=props.ground_object,
                sole_offset=props.sole_offset,
                foot_lock_sensitivity=props.foot_lock_sensitivity,
                debug_hands=props.debug_hands,
                sample_time=m.get("ts", m.get("_rx", now)),
                foot_lock_mode=props.foot_lock_mode,
            )
            telem = retarget.body.get_foot_telemetry()
            if telem:
                props.foot_state_left = telem.get("state_l", "SWING")
                props.foot_state_right = telem.get("state_r", "SWING")
                props.foot_ground_detected = telem.get("ground_z", 0.0)
        except Exception as e:
            # Mensaje corto para la UI; traceback al log con rate limit
            props.last_error = f"apply_pose: {e}"
            _err_state["n"] += 1
            if _err_state["n"] <= 3 or now - _err_state["t"] > 5.0:
                _err_state["t"] = now
                log.exception(f"apply_pose falló (#{_err_state['n']})")
            continue

        if recording and arm is not None:
            elapsed = m.get("_rx", now) - st["t0"]
            if elapsed < 0.0:
                continue  # llegó durante la cuenta regresiva
            if len(st["samples"]) >= props.max_take_samples:
                # P4: tope de muestras — no crecer sin límite en RAM.
                log.warn(f"tope de muestras ({props.max_take_samples}); grabación auto-detenida")
                props.last_error = f"Tope de muestras ({props.max_take_samples}); grabación detenida"
                _finish_recording(scene, props)
                _tag_redraw_ui()
                return 0.02
            snap = retarget.snapshot_pose(
                arm, props.bone_prefix, include_body, include_hands, include_face,
                include_root=props.enable_root_translation or props.foot_lock)
            st["samples"].append((elapsed, snap["bones"], snap["shapes"], snap["locs"]))
            last_frame = st["start_frame"] + int(round(elapsed * st["fps"]))

    # P1-2: aplicar poses a 50 Hz, pero escribir contadores RNA + tag_redraw a
    # ~10 Hz (cada escritura de IntProperty taggea el depsgraph y el redraw por
    # tick era trabajo puro desperdiciado). `scene.frame_current` se mantiene
    # por tick durante la grabación (disparador del scrub, no un contador).
    _ui_pulse_state["pending_frames"] += len(pose_msgs)
    if last_frame is not None:
        _ui_pulse_state["pending_last_frame"] = last_frame
        # frame_current directo NO fuerza la evaluación completa del depsgraph
        # (frame_set sí, y costaba 5-30 ms por mensaje — la grabación era una
        # presentación de diapositivas)
        scene.frame_current = last_frame
    if now - _ui_pulse_state["last"] >= 0.1:
        _ui_pulse_state["last"] = now
        _flush_ui_pulse(props)
    return 0.02


# --- Operators ---------------------------------------------------------------

class PUPPET_OT_start_capture(bpy.types.Operator):
    bl_idname = "puppet_mocap.start_capture"
    bl_label = "Iniciar Captura"
    bl_description = "Arranca el servidor TCP y lanza el proceso externo de webcam + MediaPipe"

    def execute(self, context):
        props = context.scene.puppet_mocap
        if server.is_running():
            self.report({"WARNING"}, "El server ya está corriendo")
            return {"CANCELLED"}

        log.banner(f"START CAPTURE port={props.server_port} cam={props.cam_index}")

        if not (props.enable_body or props.enable_hands or props.enable_face):
            self.report({"ERROR"}, "Activa al menos un módulo (Cuerpo/Manos/Cara)")
            return {"CANCELLED"}

        _sync_target(props)
        _load_calibration(props)
        # No dejar una toma reproduciéndose: sus fcurves pisarían la captura
        # en vivo al cambiar de frame (P0-1).
        props.play_take = False
        set_take_playback(False)
        arm = retarget.get_armature()
        if arm is None:
            log.error("Sin armature al iniciar captura")
            self.report({"ERROR"}, "No hay un armature en la escena. Importa un FBX Mixamo primero.")
            return {"CANCELLED"}
        ok, msg = _validate_rig(props, include_body=props.enable_body, include_hands=props.enable_hands)
        if not ok:
            log.error(msg)
            self.report({"ERROR"}, msg)
            return {"CANCELLED"}
        if props.bones_matched < props.bones_total:
            self.report({"WARNING"},
                        f"Solo {props.bones_matched}/{props.bones_total} huesos coinciden con el prefijo")

        retarget.fix_orientation(prefix=props.bone_prefix)

        runner = _capture_runner_path()
        if not runner.exists():
            log.error(f"capture_runner no encontrado: {runner}")
            self.report({"ERROR"}, f"capture_runner no encontrado: {runner}")
            return {"CANCELLED"}

        cmd = [
            props.python_path,
            str(runner),
            "--addon-port", str(props.server_port),
            "--cam", str(props.cam_index),
            "--fps", str(props.send_fps),
            "--smooth-min-cutoff", str(props.smooth_min_cutoff),
            "--smooth-beta", str(props.smooth_beta),
        ]

        # El pose model también ancla las muñecas para las manos: manos sin
        # cuerpo antes enviaba exactamente CERO datos, en silencio.
        if props.enable_body or props.enable_hands:
            model = _model_path()
            if not model.exists():
                log.error(f"modelo de pose no encontrado: {model}")
                self.report({"ERROR"}, "pose_landmarker_lite.task no encontrado. Usa 'Descargar modelos'.")
                return {"CANCELLED"}
            cmd.extend(["--model", str(model)])

        if props.enable_hands:
            hand_model = _hand_model_path()
            if hand_model.exists():
                cmd.extend(["--hand-model", str(hand_model)])
            elif props.enable_body or props.enable_face:
                log.warn(f"hand_landmarker.task no encontrado: {hand_model}")
                self.report({"WARNING"}, "hand_landmarker.task no encontrado, captura sin manos. Usa 'Descargar modelos'.")
            else:
                self.report({"ERROR"}, "hand_landmarker.task no encontrado y Manos es el único módulo. Usa 'Descargar modelos'.")
                return {"CANCELLED"}

        if props.enable_face:
            face_model = _face_model_path()
            if face_model.exists():
                cmd.extend(["--face-model", str(face_model)])
            elif props.enable_body or props.enable_hands:
                log.warn(f"face_landmarker.task no encontrado: {face_model}")
                self.report(
                    {"WARNING"},
                    "face_landmarker.task no encontrado. Usa el botón 'Descargar modelos'.",
                )
            else:
                self.report({"ERROR"},
                            "face_landmarker.task no encontrado. Usa 'Descargar modelos'.")
                return {"CANCELLED"}

        # Un proceso previo vivo (start fallido anterior) se quedaría huérfano
        # con la webcam tomada
        _terminate_subprocess()

        ok, err = server.start(props.server_port)
        if not ok:
            log.error(f"Server.start falló: {err}")
            self.report({"ERROR"}, f"No se pudo iniciar el server: {err}")
            return {"CANCELLED"}

        log.info(f"subprocess cmd: {' '.join(cmd)}")

        # Redirigir stdout/stderr del subprocess a un log SEPARADO (P1-3): dos
        # handles sobre el mismo archivo entrelazaban líneas a media línea.
        try:
            log_fh = open(log.get_capture_log_path(), "ab")
            _subprocess_state["log_fh"] = log_fh
        except OSError:
            log.exception("no pude abrir log del subprocess; continuando sin captura de stdout")
            log_fh = None

        env = dict(os.environ)
        # Callar el spam por-frame de absl/TF en el log del subprocess
        env.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")
        env.setdefault("GLOG_minloglevel", "2")

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=log_fh if log_fh is not None else subprocess.DEVNULL,
                stderr=subprocess.STDOUT if log_fh is not None else subprocess.DEVNULL,
                cwd=str(_addon_dir()),
                env=env,
            )
        except FileNotFoundError:
            server.stop()
            _close_capture_log()
            log.error(f"Python externo no encontrado: {props.python_path}")
            self.report({"ERROR"}, f"Python no encontrado: {props.python_path}")
            return {"CANCELLED"}
        except Exception as e:
            server.stop()
            _close_capture_log()
            log.exception("subprocess.Popen falló")
            self.report({"ERROR"}, f"No se pudo lanzar capture: {e}")
            return {"CANCELLED"}

        _subprocess_state["proc"] = proc
        _capture_state["scene"] = context.scene.name
        props.capture_pid = proc.pid
        props.server_running = True
        props.frames_received = 0
        props.last_error = ""
        _ui_pulse_state.update(last=0.0, pending_frames=0, pending_last_frame=None)
        log.info(f"subprocess lanzado PID={proc.pid} "
                 f"prefix='{props.bone_prefix}' smooth={props.rotation_smooth} "
                 f"mirror={props.mirror_motion}")

        if not bpy.app.timers.is_registered(_drain_timer):
            bpy.app.timers.register(_drain_timer, first_interval=0.1, persistent=True)

        self.report({"INFO"}, f"Captura iniciada (PID {proc.pid})")
        return {"FINISHED"}


class PUPPET_OT_stop_capture(bpy.types.Operator):
    bl_idname = "puppet_mocap.stop_capture"
    bl_label = "Detener Captura"
    bl_description = "Detiene el server y el proceso externo de captura (hornea la grabación pendiente)"

    def execute(self, context):
        props = context.scene.puppet_mocap
        log.info("stop_capture")
        result = _finish_recording(context.scene, props)
        _cleanup_capture(props)
        log.banner("END CAPTURE")
        if result is not None:
            _name, (f0, f1) = result
            self.report({"INFO"}, f"Captura detenida; toma horneada ({f0}-{f1})")
        else:
            self.report({"INFO"}, "Captura detenida")
        return {"FINISHED"}


class PUPPET_OT_toggle_record(bpy.types.Operator):
    bl_idname = "puppet_mocap.toggle_record"
    bl_label = "Toggle Record"
    bl_description = "Empieza o detiene la grabación (con cuenta regresiva; hornea al detener)"

    def execute(self, context):
        props = context.scene.puppet_mocap
        scene = context.scene
        if props.is_recording:
            result = _finish_recording(scene, props)
            if result is not None:
                _name, (f0, f1) = result
                log.info(f"REC stop; toma {f0}-{f1}")
                self.report({"INFO"}, f"Toma grabada: frames {f0}-{f1} (action '{_name}')")
            else:
                self.report({"INFO"}, "Grabación cancelada (sin datos)")
            return {"FINISHED"}

        if not server.is_running():
            self.report({"ERROR"}, "Inicia la captura antes de grabar")
            return {"CANCELLED"}

        if props.use_scene_fps:
            fps = scene.render.fps / scene.render.fps_base
        else:
            fps = props.rec_fps
        now = time.time()
        countdown = float(props.rec_countdown)
        _record_state.update(
            pending=countdown > 0.0,
            active=countdown <= 0.0,
            t_pending_end=now + countdown,
            t0=now + countdown,
            fps=fps,
            start_frame=max(1, scene.frame_current),
            samples=[],
        )
        props.is_recording = True
        props.rec_start_frame = _record_state["start_frame"]
        props.last_record_frame = _record_state["start_frame"]
        log.info(f"REC armado @ frame {_record_state['start_frame']} "
                 f"fps={fps:.3g} countdown={countdown:.0f}s")
        if countdown > 0:
            self.report({"INFO"}, f"Grabando en {props.rec_countdown} s...")
        else:
            self.report({"INFO"}, f"Grabación iniciada @ frame {_record_state['start_frame']}")
        return {"FINISHED"}


class PUPPET_OT_clear_keyframes(bpy.types.Operator):
    bl_idname = "puppet_mocap.clear_keyframes"
    bl_label = "Eliminar esta toma"
    bl_description = (
        "Elimina la toma de Puppet Mocap del armature objetivo (cuerpo + cara). "
        "Solo borra actions marcadas como propias; una animación ajena que hayas "
        "asignado a mano se conserva"
    )
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        _sync_target(context.scene.puppet_mocap)
        if retarget.clear_all_keyframes():
            log.info("toma eliminada (cuerpo + cara)")
            self.report({"INFO"}, "Toma eliminada")
            return {"FINISHED"}
        log.warn("clear_keyframes: sin armature")
        self.report({"ERROR"}, "No hay armature en la escena")
        return {"CANCELLED"}


class PUPPET_OT_reset_rig(bpy.types.Operator):
    bl_idname = "puppet_mocap.reset_rig"
    bl_label = "Reset Rig"
    bl_description = "Resetea el armature a rest pose y los shape keys a 0 (no borra actions)"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        props = context.scene.puppet_mocap
        _sync_target(props)
        if retarget.fix_orientation(force=True, prefix=props.bone_prefix):
            retarget.face.zero_values(retarget.get_armature())
            log.info(f"rig reseteado prefix='{props.bone_prefix}'")
            self.report({"INFO"}, "Rig reseteado a rest pose")
            return {"FINISHED"}
        log.warn("reset_rig: sin armature")
        self.report({"ERROR"}, "No hay armature en la escena")
        return {"CANCELLED"}


class PUPPET_OT_calibrate_ground(bpy.types.Operator):
    bl_idname = "puppet_mocap.calibrate_ground"
    bl_label = "Calibrar suelo desde pose"
    bl_description = "Establece la altura Z del suelo a partir del pie más bajo en la pose actual"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        props = context.scene.puppet_mocap
        _sync_target(props)
        arm = retarget.get_armature()
        if arm is None:
            self.report({"ERROR"}, "No hay armature en la escena")
            return {"CANCELLED"}
        prefix = props.bone_prefix
        feet = [
            arm.pose.bones.get(f"{prefix}LeftFoot"),
            arm.pose.bones.get(f"{prefix}RightFoot"),
        ]
        feet = [pb for pb in feet if pb is not None]
        if not feet:
            self.report({"ERROR"}, "No se encontraron huesos Foot en el armature")
            return {"CANCELLED"}
        min_z = None
        for pb in feet:
            z_w = (arm.matrix_world @ pb.head).z
            if min_z is None or z_w < min_z:
                min_z = z_w
        if min_z is not None:
            props.ground_z = min_z
            self.report({"INFO"}, f"Suelo calibrado a Z = {min_z:.4f} m")
            return {"FINISHED"}
        return {"CANCELLED"}


class PUPPET_OT_lock_current_take(bpy.types.Operator):
    bl_idname = "puppet_mocap.lock_current_take"
    bl_label = "Clavar pies de la toma"
    bl_description = (
        "Duplica la action activa y hornea la corrección del Hips e IK de piernas "
        "para que los pies de apoyo no patinen"
    )
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        props = context.scene.puppet_mocap
        if server.is_running() or props.is_recording:
            self.report({"ERROR"}, "Detén la captura antes de procesar la toma")
            return {"CANCELLED"}
        _sync_target(props)
        arm = retarget.get_armature()
        action = (arm.animation_data.action
                  if arm is not None and arm.animation_data is not None else None)
        if arm is None:
            self.report({"ERROR"}, "No hay armature en la escena")
            return {"CANCELLED"}
        if action is None:
            self.report({"ERROR"}, "El armature no tiene una action activa")
            return {"CANCELLED"}

        from .retarget import foot_lock as foot_lock_mod
        new_action, stats = foot_lock_mod.lock_action(
            arm,
            action,
            prefix=props.bone_prefix,
            ground_mode=props.ground_mode,
            ground_z=props.ground_z if props.ground_mode == "PLANE_Z" else None,
            ground_object=props.ground_object if props.ground_mode == "OBJECT_RAYCAST" else None,
            sole_offset=props.sole_offset,
            sensitivity=props.foot_lock_sensitivity,
        )
        if new_action is None:
            self.report({"ERROR"}, "No encontré Hips y al menos un hueso Foot")
            return {"CANCELLED"}
        new_action["puppet_mocap_take"] = True
        new_action["puppet_mocap_owner"] = arm.name
        _baked_state["body"] = new_action.name
        props.play_take = True

        locked_frames = int(stats)
        left_frames = stats.get("locked_frames_left", 0) if isinstance(stats, dict) else 0
        right_frames = stats.get("locked_frames_right", 0) if isinstance(stats, dict) else 0
        rms = stats.get("rms_residual", 0.0) if isinstance(stats, dict) else 0.0
        max_res = stats.get("max_residual", 0.0) if isinstance(stats, dict) else 0.0
        g_mode = stats.get("ground_mode", props.ground_mode) if isinstance(stats, dict) else props.ground_mode
        unreachable = stats.get("unreachable_frames", 0) if isinstance(stats, dict) else 0

        log.info(
            f"foot lock de toma: action='{new_action.name}' frames={locked_frames} "
            f"(L:{left_frames} R:{right_frames}) RMS={rms:.4f} Max={max_res:.4f} "
            f"suelo={g_mode} fuera_alcance={unreachable}"
        )
        if max_res > 0.005:
            self.report(
                {"WARNING"},
                f"Toma procesada con residual ({rms * 1000:.1f} mm RMS, {max_res * 1000:.1f} mm max; suelo {g_mode})",
            )
        else:
            self.report(
                {"INFO"},
                f"Toma duplicada y pies clavados ({locked_frames} frames: L:{left_frames}, R:{right_frames}; suelo {g_mode}; RMS {rms * 1000:.1f} mm)",
            )
        return {"FINISHED"}


class PUPPET_OT_foot_lock_now(bpy.types.Operator):
    bl_idname = "puppet_mocap.foot_lock_now"
    bl_label = "Clavar ahora"
    bl_description = "Fuerza el bloqueo duro (LOCKED) de los pies según el modo seleccionado"

    def execute(self, context):
        from .retarget import common
        common._state["foot_force_lock"] = True
        log.info("foot lock: forzar bloqueo duro manual (LOCKED)")
        self.report({"INFO"}, "Bloqueo duro forzado (LOCKED)")
        return {"FINISHED"}


class PUPPET_OT_foot_release(bpy.types.Operator):
    bl_idname = "puppet_mocap.foot_release"
    bl_label = "Soltar"
    bl_description = "Libera inmediatamente el bloqueo de ambos pies (SWING libre)"

    def execute(self, context):
        from .retarget import common
        common._state["foot_force_release"] = True
        log.info("foot lock: liberación manual inmediata (SWING)")
        self.report({"INFO"}, "Pies liberados a balanceo (SWING)")
        return {"FINISHED"}


class PUPPET_OT_calibrate(bpy.types.Operator):
    bl_idname = "puppet_mocap.calibrate"
    bl_label = "Calibrar postura"
    bl_description = ("Párate derecho, de frente a la cámara, y presiona: captura ~1.5 s "
                      "de tu pose neutral y corrige la inclinación de la cámara y tu orientación")

    def execute(self, context):
        if not server.is_running() or not server.is_client_connected():
            self.report({"ERROR"}, "Inicia la captura (y espera al cliente) antes de calibrar")
            return {"CANCELLED"}
        _calib_state.update(collecting=True, until=time.time() + 1.5, buf=[])
        log.info("calibración: recolectando 1.5 s de pose neutral")
        self.report({"INFO"}, "Calibrando... quédate quieto 2 segundos")
        return {"FINISHED"}


class PUPPET_OT_clear_calibration(bpy.types.Operator):
    bl_idname = "puppet_mocap.clear_calibration"
    bl_label = "Borrar calibración"
    bl_description = "Descarta la corrección de postura capturada"

    def execute(self, context):
        props = context.scene.puppet_mocap
        _store_calibration(props, None)
        _calib_state.update(collecting=False, buf=[])
        log.info("calibración borrada")
        self.report({"INFO"}, "Calibración borrada")
        return {"FINISHED"}


class PUPPET_OT_detect_prefix(bpy.types.Operator):
    bl_idname = "puppet_mocap.detect_prefix"
    bl_label = "Detectar prefijo"
    bl_description = "Deduce el prefijo de huesos del armature (busca el hueso *Hips)"

    def execute(self, context):
        props = context.scene.puppet_mocap
        _sync_target(props)
        arm = retarget.get_armature()
        if arm is None:
            self.report({"ERROR"}, "No hay armature en la escena")
            return {"CANCELLED"}
        for bone in arm.data.bones:
            if bone.name.endswith("Hips"):
                prefix = bone.name[: -len("Hips")]
                props.bone_prefix = prefix
                expected = retarget.get_keyframe_bones(
                    prefix,
                    include_body=props.enable_body,
                    include_hands=props.enable_hands,
                )
                if expected:
                    matched = sum(1 for n in expected if n in arm.pose.bones)
                    props.bones_matched = matched
                    props.bones_total = len(expected)
                    msg = f"Prefijo: '{prefix}' ({matched}/{len(expected)} huesos)"
                else:
                    msg = f"Prefijo: '{prefix}'"
                warn = _armature_transform_warning(arm)
                if warn:
                    self.report({"WARNING"}, f"{msg} — {warn}")
                else:
                    self.report({"INFO"}, msg)
                return {"FINISHED"}
        self.report({"ERROR"}, "No encontré un hueso que termine en 'Hips'")
        return {"CANCELLED"}


# Mínimo conocido-bueno de MediaPipe: la Tasks API (PoseLandmarker/
# HandLandmarker/FaceLandmarker con *_world_landmarks y output_face_blendshapes)
# existe desde 0.10.0; versiones anteriores no traen esos campos (P3-5).
MIN_MEDIAPIPE = (0, 10, 0)


def _version_tuple(v: str) -> tuple:
    """'0.10.14' / '0.10.14.dev0' → (0, 10, 14)."""
    return tuple(int(n) for n in re.findall(r"\d+", v)[:3])


class PUPPET_OT_check_deps(bpy.types.Operator):
    bl_idname = "puppet_mocap.check_deps"
    bl_label = "Verificar dependencias"
    bl_description = "Comprueba que el Python externo tenga mediapipe + opencv + numpy"

    def execute(self, context):
        props = context.scene.puppet_mocap
        cmd = [
            props.python_path, "-c",
            "import mediapipe, cv2, numpy; "
            "print(mediapipe.__version__ + '|' + cv2.__version__ + '|' + numpy.__version__)",
        ]
        flags = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=30, creationflags=flags)
        except FileNotFoundError:
            props.deps_status = f"Python no encontrado: {props.python_path}"
            self.report({"ERROR"}, props.deps_status)
            return {"CANCELLED"}
        except subprocess.TimeoutExpired:
            props.deps_status = "Timeout verificando (¿antivirus bloqueando?)"
            self.report({"ERROR"}, props.deps_status)
            return {"CANCELLED"}
        if r.returncode == 0 and "|" in r.stdout:
            mp_v, cv_v, np_v = r.stdout.strip().splitlines()[-1].split("|")
            if _version_tuple(mp_v) < MIN_MEDIAPIPE:
                props.deps_status = (
                    f"mediapipe {mp_v} DEMASIADO viejo (mínimo "
                    f"{'.'.join(map(str, MIN_MEDIAPIPE))}). Actualiza: "
                    f"py -m pip install -U mediapipe"
                )
                log.error(f"deps check: {props.deps_status}")
                self.report({"ERROR"}, props.deps_status)
                return {"CANCELLED"}
            props.deps_status = f"OK — mediapipe {mp_v}, cv2 {cv_v}, numpy {np_v}"
            log.info(f"deps: {props.deps_status}")
            self.report({"INFO"}, props.deps_status)
            return {"FINISHED"}
        err_line = (r.stderr or "").strip().splitlines()
        detail = err_line[-1] if err_line else f"código {r.returncode}"
        props.deps_status = f"FALTAN dependencias: {detail}"
        log.error(f"deps check: {detail}")
        self.report({"ERROR"}, props.deps_status)
        return {"CANCELLED"}


class PUPPET_OT_open_log(bpy.types.Operator):
    bl_idname = "puppet_mocap.open_log"
    bl_label = "Abrir Log"
    bl_description = "Abre el archivo de log de Puppet Mocap con la app por defecto del sistema"

    def execute(self, context):
        # P1-3: abrir tanto el log del addon como el del subprocess.
        paths = [log.get_log_path(), log.get_capture_log_path()]
        existing = [p for p in paths if Path(p).exists()]
        if not existing:
            self.report({"WARNING"}, "Log aún no existe")
            return {"CANCELLED"}
        try:
            for path in existing:
                if sys.platform == "win32":
                    os.startfile(path)
                elif sys.platform == "darwin":
                    subprocess.Popen(["open", path])
                else:
                    subprocess.Popen(["xdg-open", path])
        except Exception as e:
            log.exception("open_log")
            self.report({"ERROR"}, f"No pude abrir el log: {e}")
            return {"CANCELLED"}
        self.report({"INFO"}, f"Abriendo {len(existing)} log(s)")
        return {"FINISHED"}


class PUPPET_OT_download_models(bpy.types.Operator):
    bl_idname = "puppet_mocap.download_models"
    bl_label = "Descargar modelos"
    bl_description = "Descarga los modelos MediaPipe que falten (pose + manos + cara) a la carpeta models/ del addon"

    def execute(self, context):
        missing = [f for f in MODEL_URLS if not (_addon_dir() / "models" / f).exists()]
        if not missing:
            self.report({"INFO"}, "Todos los modelos ya están descargados")
            return {"CANCELLED"}
        results = []
        for filename in missing:
            log.info(f"descargando {filename} ...")
            ok, msg = _download_model(filename)
            if ok:
                log.info(f"{filename} descargado ({msg})")
                results.append(f"{filename} ✓")
            else:
                log.error(f"{filename} falló: {msg}")
                results.append(f"{filename} ✗ ({msg})")
        self.report({"INFO"}, "Modelos: " + "; ".join(results))
        return {"FINISHED"}


class PUPPET_OT_clear_log(bpy.types.Operator):
    bl_idname = "puppet_mocap.clear_log"
    bl_label = "Limpiar Log"
    bl_description = "Trunca el archivo de log para empezar limpio"

    def execute(self, context):
        # unlink() fallaba SIEMPRE en Windows (el FileHandler tiene el archivo
        # abierto) — truncar en sitio sí funciona
        if not log.clear():
            self.report({"ERROR"}, "No pude truncar el log")
            return {"CANCELLED"}
        log.info("log limpiado por el usuario")
        self.report({"INFO"}, "Log limpiado")
        return {"FINISHED"}


def build_kimodo_cmd(props, py: str, runner: str, stem: str) -> list[str]:
    """Construye el comando de kimodo_runner a partir de las props actuales.
    Extraído de generate_motion() para poder probar el gate de --seed sin
    lanzar el subprocess real (P1: 'Resultado repetible' debe cambiar el
    comando efectivo, no solo mostrar/ocultar el campo)."""
    cmd = [py, runner, "--prompt", props.kimodo_prompt,
           "--duration", str(props.kimodo_duration),
           "--model", props.kimodo_model,
           "--out", stem,
           "--num_transition_frames", str(props.kimodo_num_transition)]
    if props.kimodo_seed_use:
        cmd += ["--seed", str(props.kimodo_seed)]
    if props.kimodo_postprocess:
        cmd += ["--postprocess"]
    return cmd


class PUPPET_OT_generate_motion(bpy.types.Operator):
    bl_idname = "puppet_mocap.generate_motion"
    bl_label = "Generar movimiento"
    bl_description = "Genera una animación por texto (Kimodo) y la hornea a una action"

    def execute(self, context):
        props = context.scene.puppet_mocap
        if props.kimodo_running:
            self.report({"WARNING"}, "Ya hay una generación de Kimodo en curso")
            return {"CANCELLED"}
        # Independiente del estado de captura: NO consulta server.is_running().
        _sync_target(props)
        ok, msg = _validate_rig(props, include_body=True, include_hands=True)
        if not ok:
            self.report({"ERROR"}, msg)
            return {"CANCELLED"}
        warn = _armature_transform_warning(retarget.get_armature())
        if warn:
            self.report({"WARNING"}, warn)
            log.warn(f"kimodo: {warn}")
        py = props.kimodo_python_path
        runner = _kimodo_runner_path()
        if not py:
            self.report({"ERROR"}, "Configura 'Python de Kimodo' en Settings")
            return {"CANCELLED"}
        if not runner.exists():
            self.report({"ERROR"}, f"kimodo_runner no encontrado: {runner}")
            return {"CANCELLED"}
        out_dir = Path(tempfile.gettempdir()) / "puppet_mocap_kimodo"
        out_dir.mkdir(parents=True, exist_ok=True)
        stem = str(out_dir / f"take_{int(time.time())}")
        cmd = build_kimodo_cmd(props, py, str(runner), stem)
        _kill_kimodo_proc()
        try:
            log_fh = open(log.get_kimodo_log_path(), "ab")
            _KIMODO_STATE["log_fh"] = log_fh
        except OSError:
            log.exception("no pude abrir log de kimodo")
            log_fh = None
        kenv = dict(os.environ)
        # Con el text encoder en GPU, Kimodo pide ~17 GB de VRAM y no cabe en
        # una tarjeta de 16 GB; en CPU baja a <3 GB. Sin esto, generar desde la
        # UI reventaba con OOM y el mensaje de error pedía al usuario que
        # definiera una variable de entorno que la UI no ofrece.
        kenv.setdefault("TEXT_ENCODER_DEVICE", "cpu")
        # 'auto' sondea primero un servicio en 127.0.0.1:9550 y espera al
        # timeout antes de caer al encoder local. 'local' se lo salta.
        kenv.setdefault("TEXT_ENCODER_MODE", "local")
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=log_fh if log_fh is not None else subprocess.DEVNULL,
                stderr=subprocess.STDOUT if log_fh is not None else subprocess.DEVNULL,
                cwd=str(_addon_dir()),
                env=kenv,
            )
        except FileNotFoundError:
            _close_kimodo_log()
            self.report({"ERROR"}, f"Python de Kimodo no encontrado: {py}")
            return {"CANCELLED"}
        _KIMODO_STATE.update(proc=proc, out_path=stem + ".npz", scene=context.scene.name,
                              t0=time.time(), done_ok=False)
        props.kimodo_running = True
        props.kimodo_status = "Generando… 0 s"
        log.banner(f"GENERATE MOTION prompt='{props.kimodo_prompt}' model={props.kimodo_model}")
        if not bpy.app.timers.is_registered(_kimodo_poll_timer):
            bpy.app.timers.register(_kimodo_poll_timer, first_interval=0.25, persistent=True)
        self.report({"INFO"}, "Generación de Kimodo iniciada")
        return {"FINISHED"}


class PUPPET_OT_cancel_generate(bpy.types.Operator):
    bl_idname = "puppet_mocap.cancel_generate"
    bl_label = "Cancelar"
    bl_description = "Mata el subprocess de Kimodo"

    def execute(self, context):
        props = context.scene.puppet_mocap
        _kill_kimodo_proc()
        props.kimodo_running = False
        props.kimodo_status = "Cancelado"
        self.report({"INFO"}, "Generación de Kimodo cancelada")
        return {"FINISHED"}


class PUPPET_OT_check_kimodo_deps(bpy.types.Operator):
    bl_idname = "puppet_mocap.check_kimodo_deps"
    bl_label = "Verificar deps de Kimodo"
    bl_description = "Comprueba que el Python de Kimodo tenga torch + transformers + kimodo"

    def execute(self, context):
        props = context.scene.puppet_mocap
        py = props.kimodo_python_path
        if not py:
            props.kimodo_status = "Configura el Python de Kimodo"
            self.report({"ERROR"}, props.kimodo_status)
            return {"CANCELLED"}
        cmd = [py, "-c", "import torch, transformers, kimodo; print(torch.__version__)"]
        flags = 0x08000000 if sys.platform == "win32" else 0
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=30, creationflags=flags)
        except FileNotFoundError:
            props.kimodo_status = f"Python de Kimodo no encontrado: {py}"
            self.report({"ERROR"}, props.kimodo_status)
            return {"CANCELLED"}
        except subprocess.TimeoutExpired:
            props.kimodo_status = "Timeout verificando (¿antivirus?)"
            self.report({"ERROR"}, props.kimodo_status)
            return {"CANCELLED"}
        if r.returncode == 0:
            props.kimodo_status = f"OK — torch {r.stdout.strip()}"
            log.info(f"kimodo deps: {props.kimodo_status}")
            self.report({"INFO"}, props.kimodo_status)
            return {"FINISHED"}
        detail = (r.stderr or "").strip().splitlines()
        props.kimodo_status = "FALTAN deps de Kimodo: " + (detail[-1] if detail else f"rc {r.returncode}")
        log.error(f"kimodo deps: {props.kimodo_status}")
        self.report({"ERROR"}, props.kimodo_status)
        return {"CANCELLED"}


class PUPPET_OT_autodetect_kimodo_python(bpy.types.Operator):
    bl_idname = "puppet_mocap.autodetect_kimodo_python"
    bl_label = "Detectar"
    bl_description = "Busca automáticamente el Python de Kimodo (y valida que tenga Kimodo)"

    def execute(self, context):
        props = context.scene.puppet_mocap
        found = _autodetect_kimodo_python(props)
        if found:
            props.kimodo_python_path = found
            props.kimodo_status = "Python de Kimodo detectado"
            self.report({"INFO"}, f"Detectado: {found}")
        else:
            props.kimodo_status = "No encontré el Python de Kimodo. Usa 'Elegir…'."
            self.report({"WARNING"}, "No se encontró el Python de Kimodo")
        return {"FINISHED"}


class PUPPET_OT_play_kimodo_take(bpy.types.Operator):
    bl_idname = "puppet_mocap.play_kimodo_take"
    bl_label = "Ver la animación"
    bl_description = "Activa la toma generada, salta al inicio y reproduce"

    def execute(self, context):
        props = context.scene.puppet_mocap
        context.scene.frame_current = int(_KIMODO_STATE.get("last_start", 1) or 1)
        set_take_playback(True)
        props.play_take = True
        return {"FINISHED"}


class PUPPET_OT_open_kimodo_license(bpy.types.Operator):
    bl_idname = "puppet_mocap.open_kimodo_license"
    bl_label = "Cómo obtenerlo"
    bl_description = "Abre la página para solicitar acceso al modelo (cuenta de Hugging Face + aceptar la licencia de Meta Llama-3)"

    def execute(self, context):
        bpy.ops.wm.url_open(url="https://huggingface.co/meta-llama/Meta-Llama-3-8B-Instruct")
        return {"FINISHED"}


class PUPPET_OT_open_kimodo_log(bpy.types.Operator):
    bl_idname = "puppet_mocap.open_kimodo_log"
    bl_label = "Ver detalles"
    bl_description = "Abre el log del subprocess de Kimodo"

    def execute(self, context):
        p = log.get_kimodo_log_path()
        if sys.platform == "win32":
            try:
                os.startfile(p)
            except OSError:
                log.exception("open kimodo log")
        return {"FINISHED"}


class PUPPET_OT_enable_preserve_volume(bpy.types.Operator):
    bl_idname = "puppet_mocap.enable_preserve_volume"
    bl_label = "Activar Preserve Volume"
    bl_description = "Activa Preserve Volume en los modificadores Armature de las mallas vinculadas al rig para prevenir pérdida de sección en codo y muñeca"

    def execute(self, context):
        arm = retarget.get_armature()
        if arm is None:
            self.report({"WARNING"}, "No hay un rig activo seleccionado")
            return {"CANCELLED"}
        updated = 0
        for obj in bpy.data.objects:
            if obj.type == "MESH":
                for m in obj.modifiers:
                    if m.type == "ARMATURE" and m.object == arm:
                        if not m.use_deform_preserve_volume:
                            m.use_deform_preserve_volume = True
                            updated += 1
        if updated > 0:
            self.report({"INFO"}, f"Preserve Volume activado en {updated} objeto(s)")
        else:
            self.report({"INFO"}, "Preserve Volume ya estaba activo o no se encontraron mallas dependientes")
        return {"FINISHED"}


CLASSES = (
    PUPPET_OT_start_capture,
    PUPPET_OT_stop_capture,
    PUPPET_OT_toggle_record,
    PUPPET_OT_clear_keyframes,
    PUPPET_OT_reset_rig,
    PUPPET_OT_lock_current_take,
    PUPPET_OT_foot_lock_now,
    PUPPET_OT_foot_release,
    PUPPET_OT_calibrate_ground,
    PUPPET_OT_calibrate,
    PUPPET_OT_clear_calibration,
    PUPPET_OT_detect_prefix,
    PUPPET_OT_check_deps,
    PUPPET_OT_open_log,
    PUPPET_OT_clear_log,
    PUPPET_OT_download_models,
    PUPPET_OT_generate_motion,
    PUPPET_OT_cancel_generate,
    PUPPET_OT_check_kimodo_deps,
    PUPPET_OT_autodetect_kimodo_python,
    PUPPET_OT_play_kimodo_take,
    PUPPET_OT_open_kimodo_license,
    PUPPET_OT_open_kimodo_log,
    PUPPET_OT_enable_preserve_volume,
)
