"""UI Panel en N-panel del View3D."""
import time
from pathlib import Path

import bpy

from . import log, server
from .retarget import face as face_mod

# Cache de checks de filesystem (exists/stat en cada redraw suman en escenas
# pesadas — el panel se redibuja continuamente durante la captura)
_fs_cache = {"t": 0.0, "face_model": False, "pose_model": False,
            "hand_model": False, "log_kb": None}


def _fs_state(ops_mod):
    now = time.monotonic()
    if now - _fs_cache["t"] > 2.0:
        _fs_cache["t"] = now
        _fs_cache["face_model"] = ops_mod._face_model_path().exists()
        _fs_cache["pose_model"] = ops_mod._model_path().exists()
        _fs_cache["hand_model"] = ops_mod._hand_model_path().exists()
        try:
            p = Path(log.get_log_path())
            _fs_cache["log_kb"] = p.stat().st_size / 1024 if p.exists() else None
        except OSError:
            _fs_cache["log_kb"] = None
    return _fs_cache


# Cache de la lista de comprobación de Kimodo (TTL ~5 s): python_ok/kimodo_ok
# requieren un subprocess de import; no escanear por redibujo (U2).
_kimodo_checklist_cache = {"t": 0.0, "data": None}


def _kimodo_checklist(ops_mod, props):
    now = time.monotonic()
    if _kimodo_checklist_cache["data"] is not None and now - _kimodo_checklist_cache["t"] < 5.0:
        return _kimodo_checklist_cache["data"]
    py = props.kimodo_python_path
    # No bloqueante (subprocess.Popen + poll): un check síncrono aquí
    # colgaba Blender hasta 10s en cada redibujo con un venv real
    # (torch+transformers tarda 25-35s en importar). Ver la nota en
    # operators._kimodo_python_check_poll.
    py_result = ops_mod._kimodo_python_check_poll(py) if py else False
    python_ok = bool(py_result)
    python_checking = bool(py) and py_result is None
    rig_ok = False
    arm_name = ""
    from .retarget import get_armature
    arm = get_armature()
    if arm is not None:
        arm_name = arm.name
        ok, _ = ops_mod._validate_rig(props, write=False)
        rig_ok = ok
    data = {"python_ok": python_ok, "python_checking": python_checking,
            "kimodo_ok": python_ok, "rig_ok": rig_ok, "arm_name": arm_name,
            "model_ok": ops_mod._kimodo_encoder_ready()}
    _kimodo_checklist_cache["t"] = now
    _kimodo_checklist_cache["data"] = data
    return data


def _module_box(layout, props, title, icon, enable_attr, record_attr, locked=False):
    """Caja con header (toggle de habilitar) y switch de grabación.

    `locked=True` (captura corriendo) grisa el toggle de habilitar: el
    proceso externo decide qué modelos cargar AL LANZARSE según
    enable_body/hands/face — cambiarlo a media captura no hace nada porque
    ese proceso nunca recibió el modelo correspondiente (P1, "inmutabilidad
    de selección de módulos"). record_body/hands/face sí se puede cambiar
    en vivo: solo decide si el buffer de grabación incluye datos que YA
    están llegando, no qué carga el proceso externo."""
    box = layout.box()
    row = box.row(align=True)
    enable_row = row.row(align=True)
    enable_row.enabled = not locked
    enable_row.prop(props, enable_attr, text="")
    row.label(text=title, icon=icon)
    sub = row.row(align=True)
    sub.active = getattr(props, enable_attr)
    sub.prop(props, record_attr, text="Grabar", toggle=True)
    if locked:
        box.label(
            text="Fijo durante la captura — detén e inicia de nuevo para cambiarlo",
            icon="LOCKED",
        )
    return box


class PUPPET_PT_main(bpy.types.Panel):
    bl_label = "Puppet Mocap"
    bl_idname = "PUPPET_PT_main"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Puppet Mocap"

    def draw(self, context):
        from . import operators as ops_mod
        layout = self.layout
        props = context.scene.puppet_mocap
        running = server.is_running()
        fs = _fs_state(ops_mod)

        # --- Rig objetivo ---
        box = layout.box()
        col = box.column(align=True)
        row = col.row(align=True)
        row.enabled = not running
        row.prop(props, "target_armature", text="Rig")
        row = col.row(align=True)
        row.enabled = not running
        row.prop(props, "bone_prefix", text="")
        row.operator("puppet_mocap.detect_prefix", text="", icon="VIEWZOOM")
        if props.bones_matched >= 0 and props.bones_total > 0:
            if props.bones_matched == props.bones_total:
                col.label(text=f"Huesos: {props.bones_matched}/{props.bones_total}",
                          icon="CHECKMARK")
            elif props.bones_matched == 0:
                col.label(text="Prefijo no coincide con el rig", icon="ERROR")
            else:
                col.label(text=f"Huesos: {props.bones_matched}/{props.bones_total}",
                          icon="QUESTION")

        # --- Estado ---
        box = layout.box()
        col = box.column(align=True)
        connected = server.is_client_connected()
        if running and connected:
            col.label(text="Server: corriendo, cliente conectado", icon="LINKED")
        elif running:
            col.label(text="Server: esperando cliente...", icon="UNLINKED")
        else:
            col.label(text="Server: detenido", icon="REMOVE")
        proc = ops_mod._subprocess_state.get("proc")
        if proc is not None:
            alive = proc.poll() is None
            col.label(
                text=f"Proceso: PID {proc.pid} ({'vivo' if alive else 'muerto'})",
                icon="PLAY" if alive else "CANCEL",
            )
        col.label(text=f"Frames recibidos: {props.frames_received}")
        if props.last_error:
            col.label(text=props.last_error, icon="ERROR")

        # --- Módulos: Cuerpo ---
        box = _module_box(layout, props, "Cuerpo", "ARMATURE_DATA",
                          "enable_body", "record_body", locked=running)
        if props.enable_body:
            sub = box.column(align=True)
            sub.label(text="Pose + spine con twist + pies")
            # P1 (hallazgo #5): foot_lock está activo por default y afecta el
            # retarget en vivo, pero antes no tenía ningún control en el
            # panel — el usuario no podía verlo ni apagarlo sin la consola.
            sub.prop(props, "foot_lock", text="Estabilizar pies")
            sub.prop(props, "enable_root_translation")
            if props.enable_root_translation:
                sub.prop(props, "root_translation_scale")

        # --- Módulos: Manos ---
        box = _module_box(layout, props, "Manos", "VIEW_PAN",
                          "enable_hands", "record_hands", locked=running)
        if props.enable_hands:
            sub = box.column(align=True)
            sub.prop(props, "swap_hands")
            sub.prop(props, "flip_palm_normal")
            sub.operator("puppet_mocap.enable_preserve_volume",
                         text="Preserve Volume (Codo/Muñeca)",
                         icon="MOD_ARMATURE")

        # --- Módulos: Cara ---
        box = _module_box(layout, props, "Cara", "USER",
                          "enable_face", "record_face", locked=running)
        if props.enable_face:
            sub = box.column(align=True)
            if not fs["face_model"]:
                sub.label(text="face_landmarker.task no descargado", icon="ERROR")
                row = sub.row(align=True)
                row.enabled = not props.models_downloading
                row.operator("puppet_mocap.download_models", icon="IMPORT")
            else:
                # usa el cache del retarget en vez de re-escanear la escena;
                # una sola llamada reutilizando el objeto (P1-1)
                from .retarget import get_armature
                arm = get_armature()
                mesh = face_mod.get_cached_mesh(arm)
                if mesh is None:
                    sub.label(text="No hay mesh con shape keys ARKit", icon="ERROR")
                else:
                    n_arkit = len(face_mod.shape_key_names_in_mesh(arm, mesh))
                    sub.label(
                        text=f"Mesh: {mesh.name} ({n_arkit}/52 ARKit)",
                        icon="MESH_DATA",
                    )

        # --- Espejo ---
        layout.prop(props, "mirror_motion", icon="MOD_MIRROR")

        # --- Calibración ---
        box = layout.box()
        row = box.row(align=True)
        row.label(text="Calibración", icon="ORIENTATION_GIMBAL")
        row.prop(props, "use_calibration", text="")
        col = box.column(align=True)
        if ops_mod._calib_state["collecting"]:
            col.label(text="Capturando pose neutral...", icon="TIME")
        elif props.calib_valid:
            col.label(text="Calibrado", icon="CHECKMARK")
        else:
            col.label(text="Sin calibrar (opcional)", icon="DOT")
        row = col.row(align=True)
        sub = row.row(align=True)
        sub.enabled = running and connected
        sub.operator("puppet_mocap.calibrate", icon="ORIENTATION_GIMBAL")
        sub2 = row.row(align=True)
        sub2.enabled = props.calib_valid
        sub2.operator("puppet_mocap.clear_calibration", text="", icon="X")

        # --- Captura ---
        box = layout.box()
        box.label(text="Captura", icon="OUTLINER_OB_CAMERA")
        if running:
            box.operator("puppet_mocap.stop_capture", icon="PAUSE")
        else:
            box.operator("puppet_mocap.start_capture", icon="PLAY")

        # --- Grabación ---
        box = layout.box()
        box.label(text="Grabación", icon="REC")
        rec_mods = []
        if props.enable_body and props.record_body:
            rec_mods.append("cuerpo")
        if props.enable_hands and props.record_hands:
            rec_mods.append("manos")
        if props.enable_face and props.record_face:
            rec_mods.append("cara")
        rec_status = "+".join(rec_mods) if rec_mods else "(ningún módulo)"
        box.label(text=f"Módulos: {rec_status}")

        row = box.row(align=True)
        st = ops_mod._record_state
        if props.is_recording:
            row.alert = True
            if st["pending"]:
                remaining = max(0.0, st["t_pending_end"] - time.time())
                label = f"● REC en {remaining:.0f}s..."
            else:
                label = f"● REC  f={props.last_record_frame}"
            row.operator("puppet_mocap.toggle_record", text=label, icon="PAUSE")
        else:
            can_rec, rec_reason = ops_mod.can_start_recording(props)
            row.enabled = can_rec
            row.operator("puppet_mocap.toggle_record", text="Grabar", icon="REC")
            if not can_rec:
                box.label(text=rec_reason, icon="ERROR")
        col = box.column(align=True)
        col.prop(props, "rec_countdown")
        col.prop(props, "use_scene_fps")
        sub = col.row(align=True)
        sub.active = not props.use_scene_fps
        sub.prop(props, "rec_fps")
        col.prop(props, "max_take_samples")
        # P2-3: si enviamos más muestras/s que frames de grabación, colisionan y
        # gana la última (descarte silencioso). Avisar en vez de callar.
        eff_fps = (context.scene.render.fps / context.scene.render.fps_base
                   if props.use_scene_fps else props.rec_fps)
        if props.send_fps > eff_fps:
            box.label(
                text=f"send_fps ({props.send_fps:.0f}) > fps de grabación "
                     f"({eff_fps:.1f}): se descartarán muestras",
                icon="ERROR",
            )
        row = box.row(align=True)
        row.enabled = (not running) and (
            ops_mod._baked_state.get("body") is not None
            or ops_mod._baked_state.get("face") is not None
        )
        row.prop(props, "play_take", text="Reproducir toma", toggle=True)
        box.operator("puppet_mocap.clear_keyframes", icon="TRASH")

        # --- Rig ---
        box = layout.box()
        box.label(text="Rig", icon="ARMATURE_DATA")
        box.operator("puppet_mocap.reset_rig", icon="LOOP_BACK")

        # --- Diagnóstico ---
        box = layout.box()
        box.label(text="Diagnóstico", icon="CONSOLE")
        col = box.column(align=True)
        row = col.row(align=True)
        row.enabled = not props.deps_checking
        row.operator("puppet_mocap.check_deps", icon="CHECKMARK")
        missing_models = []
        if not fs["pose_model"]:
            missing_models.append("pose")
        if not fs["hand_model"]:
            missing_models.append("manos")
        if not fs["face_model"]:
            missing_models.append("cara")
        if missing_models:
            col.label(text=f"Modelos faltantes: {', '.join(missing_models)}", icon="ERROR")
            row = col.row(align=True)
            row.enabled = not props.models_downloading
            row.operator("puppet_mocap.download_models", icon="IMPORT")
        if props.deps_status:
            icon = "TIME" if props.deps_checking else (
                "CHECKMARK" if props.deps_status.startswith("OK") else "ERROR")
            col.label(text=props.deps_status, icon=icon)
        if props.models_download_status:
            col.label(text=props.models_download_status, icon="IMPORT")
        if fs["log_kb"] is not None:
            col.label(text=f"Log: {fs['log_kb']:.1f} KB")
        else:
            col.label(text="Log: (todavía vacío)")
        col.label(text=log.get_log_path(), icon="FILE_TEXT")
        row = col.row(align=True)
        row.operator("puppet_mocap.open_log", icon="TEXT")
        row.operator("puppet_mocap.clear_log", icon="X")

        # --- Settings ---
        box = layout.box()
        box.label(text="Settings", icon="PREFERENCES")
        col = box.column(align=True)
        col.prop(props, "rotation_smooth")
        col.prop(props, "min_visibility")
        col.prop(props, "debug_hands")
        col.separator()
        # Estos solo se leen al INICIAR captura — editarlos a media sesión no
        # hace nada, así que se bloquean para no confundir
        launch = box.column(align=True)
        launch.enabled = not running
        launch.prop(props, "cam_index")
        launch.prop(props, "send_fps")
        launch.prop(props, "smooth_min_cutoff")
        launch.prop(props, "smooth_beta")
        launch.separator()
        launch.prop(props, "server_port")
        launch.prop(props, "python_path")

        # --- Kimodo (texto → animación, K2) --- Independiente de la captura.
        box = layout.box()
        row = box.row(align=True)
        row.prop(props, "kimodo_show", text="Generar (Kimodo)", toggle=True, icon="ANIM")
        if props.kimodo_show:
            # U1: autodetecta el Python de Kimodo una vez por sesión (cache negativo)
            ops_mod._maybe_autodetect_kimodo(props)
            col = box.column(align=True)
            if props.kimodo_running:
                # U5: progreso
                col.label(text=props.kimodo_status or "Generando…", icon="TIME")
                col.label(text="Cargando el modelo (la primera vez tarda más)", icon="INFO")
                col.operator("puppet_mocap.cancel_generate", icon="CANCEL")
            else:
                ch = _kimodo_checklist(ops_mod, props)
                # U5: resultado (banner) — el prompt queda visible para probar otro
                if ops_mod._KIMODO_STATE.get("done_ok"):
                    frames = props.kimodo_result_frames
                    col.label(text=f"✓ Listo: {frames} frames ({frames / 30.0:.0f} s)",
                              icon="CHECKMARK")
                    col.operator("puppet_mocap.play_kimodo_take", icon="PLAY",
                                 text="Ver la animación")
                # U2: lista de comprobación (solo si algo no está listo)
                need_list = not (ch["python_ok"] and ch["rig_ok"]
                                 and ch["model_ok"])
                if need_list:
                    if not ch["python_ok"]:
                        if ch.get("python_checking"):
                            col.label(text="Verificando Python de Kimodo…", icon="TIME")
                        else:
                            rr = col.row(align=True)
                            rr.label(text="✗ Python de Kimodo", icon="ERROR")
                            sub_btn = rr.row(align=True)
                            sub_btn.enabled = not props.kimodo_python_checking
                            sub_btn.operator("puppet_mocap.autodetect_kimodo_python", text="Detectar")
                        if props.kimodo_python_checking:
                            col.label(text="Buscando Python de Kimodo…", icon="TIME")
                        col.prop(props, "kimodo_python_path", text="")
                    if ch["python_ok"] and not ch["rig_ok"]:
                        col.label(text="✗ Rig no encontrado. Importa un FBX de Mixamo.",
                                  icon="ERROR")
                    if not ch["model_ok"]:
                        col.label(text="Falta descargar el modelo de texto",
                                  icon="QUESTION")
                        col.label(text="Necesita cuenta de Hugging Face")
                        col.operator("puppet_mocap.open_kimodo_license",
                                     text="Cómo obtener acceso")
                # U3: prompt (en inglés) + ejemplo
                col.prop(props, "kimodo_prompt")
                col.prop(props, "kimodo_example")
                col.prop(props, "kimodo_duration")
                if props.kimodo_duration > 10.0:
                    col.label(
                        text=f"Duración {props.kimodo_duration:.0f}s > 10s: el modelo se "
                             f"entrena a máximo 10s; trocea el prompt en segmentos",
                        icon="INFO")
                # U4: Avanzado plegado
                ar = col.row(align=True)
                ar.prop(props, "kimodo_advanced", text="Avanzado", toggle=True,
                        icon="PREFERENCES")
                if props.kimodo_advanced:
                    col.prop(props, "kimodo_foot_lock")
                    col.prop(props, "kimodo_ground_snap")
                    col.prop(props, "kimodo_model")
                    rr = col.row(align=True)
                    rr.prop(props, "kimodo_seed_use", text="Resultado repetible")
                    if props.kimodo_seed_use:
                        rr.prop(props, "kimodo_seed", text="N.º")
                    pp = col.row(align=True)
                    pp.enabled = False
                    pp.prop(props, "kimodo_postprocess")
                    col.label(text="El postprocess necesita un componente no instalado; "
                                   "déjalo apagado", icon="INFO")
                # Generar (deshabilitado si el prompt está vacío)
                gen_row = col.row(align=True)
                gen_row.enabled = bool(props.kimodo_prompt.strip())
                gen_row.operator("puppet_mocap.generate_motion", icon="PLAY")
                if not gen_row.enabled:
                    col.label(text="Escribe un prompt (en inglés) o elige un ejemplo",
                              icon="INFO")
                # U6: estado + acción accionable
                if props.kimodo_status and not ops_mod._KIMODO_STATE.get("done_ok"):
                    col.label(text=props.kimodo_status, icon="ERROR")
                last_rc = ops_mod._KIMODO_STATE.get("last_rc")
                if not ops_mod._KIMODO_STATE.get("done_ok") and last_rc is not None:
                    if ops_mod._KIMODO_STATE.get("gate"):
                        col.operator("puppet_mocap.open_kimodo_license",
                                     text="Cómo obtener acceso al modelo")
                    elif last_rc == 1:
                        row = col.row(align=True)
                        row.enabled = not props.kimodo_deps_checking
                        row.operator("puppet_mocap.check_kimodo_deps",
                                     text="Verificando…" if props.kimodo_deps_checking
                                     else "Comprobar Kimodo")
                    elif last_rc == 2:
                        col.operator("puppet_mocap.open_kimodo_log", text="Ver detalles")


CLASSES = (PUPPET_PT_main,)
