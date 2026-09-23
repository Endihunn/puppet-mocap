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
    """Caja con un selector por módulo (brief Fase 3: "un selector por
    módulo en modo básico. Grabar los módulos seleccionados"). En modo
    básico el único toggle decide captura Y grabación juntas (ver
    _enable_*_update en properties.py); en Avanzado (props.modulos_advanced)
    se ve también el control independiente de grabar, para superponer
    manos/cara sobre una animación de cuerpo ya grabada.

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
    if props.modulos_advanced:
        sub = row.row(align=True)
        sub.active = getattr(props, enable_attr)
        sub.prop(props, record_attr, text="Grabar", toggle=True)
    if locked:
        box.label(
            text="Fijo durante la captura — detén e inicia de nuevo para cambiarlo",
            icon="LOCKED",
        )
    return box


class PUPPET_MT_take_list(bpy.types.Menu):
    bl_idname = "PUPPET_MT_take_list"
    bl_label = "Tomas"

    def draw(self, context):
        from . import operators as ops_mod
        layout = self.layout
        props = context.scene.puppet_mocap
        arm = ops_mod.retarget.get_armature()
        if arm is None:
            layout.label(text="Sin armature", icon="ERROR")
            return
        takes = ops_mod.list_takes(arm.name)
        if not takes:
            layout.label(text="No hay tomas todavía")
            return
        for take in takes:
            label = take["label"]
            if take["take_id"] == props.selected_take_id:
                label = f"● {label}"
            op = layout.operator("puppet_mocap.select_take", text=label)
            op.take_id = take["take_id"]


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

        # --- Personaje y cámara ---
        box = layout.box()
        box.label(text="Personaje y cámara", icon="ARMATURE_DATA")
        col = box.column(align=True)
        row = col.row(align=True)
        row.enabled = not running
        row.prop(props, "target_armature", text="Rig")
        if running:
            col.label(text="Detén la cámara para cambiar el rig", icon="INFO")

        if props.target_armature is None:
            col.label(text="Elige un personaje para continuar", icon="ERROR")
        elif props.bones_matched < 0 or props.bones_total == 0:
            col.label(text="Verificando compatibilidad…", icon="TIME")
        elif props.bones_matched == props.bones_total:
            col.label(text="Personaje compatible", icon="CHECKMARK")
        elif props.bones_matched == 0:
            col.label(text="Prefijo no coincide con el rig — revisa Avanzado", icon="ERROR")
        else:
            col.label(
                text=f"Personaje parcialmente compatible ({props.bones_matched}/"
                     f"{props.bones_total} huesos)",
                icon="ERROR",
            )

        col.label(text=ops_mod.camera_display_label(props.cam_index), icon="CAMERA_DATA")

        adv = box.column(align=True)
        adv.prop(props, "personaje_advanced", text="Avanzado", toggle=True, icon="PREFERENCES")
        if props.personaje_advanced:
            row = adv.row(align=True)
            row.enabled = not running
            row.prop(props, "bone_prefix", text="")
            row.operator("puppet_mocap.detect_prefix", text="", icon="VIEWZOOM")
            if props.bones_matched >= 0 and props.bones_total > 0:
                adv.label(text=f"Huesos: {props.bones_matched}/{props.bones_total}")
            cam_row = adv.row(align=True)
            cam_row.enabled = not running
            cam_row.prop(props, "cam_index")
            if running:
                adv.label(text="Detén la cámara para cambiar el índice", icon="INFO")

        connected = server.is_client_connected()

        # --- Módulos ---
        mrow = layout.row(align=True)
        mrow.prop(props, "modulos_advanced", text="Grabar canales por separado (Avanzado)",
                  toggle=True, icon="PREFERENCES")

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
                if props.models_downloading:
                    sub.label(text=props.models_download_status or "Descargando…", icon="TIME")
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
        layout.prop(props, "mirror_motion", text="Espejo (verte de frente como en un espejo)",
                    icon="MOD_MIRROR")

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
        if running and not connected:
            col.label(text="Esperando a que la cámara se conecte", icon="INFO")
        elif not running:
            col.label(text="Inicia la vista previa para calibrar", icon="INFO")

        # --- Captura --- (estado resumido; detalle técnico va a Diagnóstico)
        box = layout.box()
        box.label(text="Captura", icon="OUTLINER_OB_CAMERA")
        if props.is_recording:
            box.label(text="● Grabando", icon="REC")
        elif running and connected:
            box.label(text="Listo — cámara conectada", icon="CHECKMARK")
        elif running:
            box.label(text="Sin señal — esperando la cámara", icon="TIME")
        else:
            box.label(text="Preparando", icon="DOT")
        if props.last_error:
            box.label(text=props.last_error, icon="ERROR")

        if running:
            if props.is_recording:
                box.label(text="Esto también finalizará la grabación en curso", icon="INFO")
            box.operator("puppet_mocap.stop_capture", text="Detener cámara", icon="PAUSE")
        else:
            can_prev, prev_reason = ops_mod.can_start_preview(props)
            prow = box.row(align=True)
            prow.enabled = can_prev
            prow.operator("puppet_mocap.start_capture", text="Iniciar vista previa", icon="PLAY")
            if not can_prev:
                box.label(text=prev_reason, icon="ERROR")

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
                row.operator("puppet_mocap.toggle_record",
                             text=f"Cancelar cuenta regresiva ({remaining:.0f} s)", icon="X")
            else:
                label = f"Finalizar toma — f={props.last_record_frame}"
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
        # --- Última toma ---
        box = layout.box()
        box.label(text="Última toma", icon="ANIM_DATA")
        arm = ops_mod.retarget.get_armature()
        takes = ops_mod.list_takes(arm.name) if arm is not None else []
        take = ops_mod.find_take(arm.name, props.selected_take_id) if arm is not None else None

        if not takes:
            box.label(text="Todavía no hay ninguna toma.", icon="INFO")
        elif take is None:
            # selected_take_id vacío o apunta a una toma ya eliminada.
            box.label(text=f"{len(takes)} toma(s) disponible(s) — elige una", icon="INFO")
            box.menu("PUPPET_MT_take_list", text="Elegir toma")
        else:
            f0, f1 = take["frame_range"]
            fps = context.scene.render.fps / context.scene.render.fps_base
            dur = (f1 - f0) / fps if fps else 0.0
            chans = []
            if take["body_action"]:
                chans.append("cuerpo")
            if take["face_action"]:
                chans.append("cara")
            chan_txt = " + ".join(chans) if chans else "(sin canales)"
            src_txt = {"kimodo": " · Kimodo", "correction": " · corregida"}.get(take["source"], "")
            box.label(text=take["label"])
            box.label(text=f"{dur:.1f} s ({f1 - f0} frames) · {chan_txt}{src_txt}")
            if take["corrects"]:
                box.label(text="Conserva la toma original sin corregir", icon="INFO")

            row = box.row(align=True)
            row.enabled = not running
            row.prop(props, "play_take",
                     text="Pausar" if props.play_take else "Reproducir",
                     icon="PAUSE" if props.play_take else "PLAY_SOUND", toggle=True)
            sub = row.row(align=True)
            sub.enabled = not running
            sub.operator("puppet_mocap.new_take", text="Nueva toma", icon="ADD")
            if running:
                box.label(text="Detén la cámara para reproducir una toma", icon="INFO")

            opt = box.column(align=True)
            opt.label(text="Opciones de la toma:")
            if len(takes) > 1:
                opt.menu("PUPPET_MT_take_list", text="Elegir otra toma…")
            corr_row = opt.row(align=True)
            corr_row.enabled = (not running) and (not props.is_recording) and bool(take["body_action"])
            corr_row.operator("puppet_mocap.lock_current_take", text="Corregir pies", icon="MOD_DYNAMICPAINT")
            if not corr_row.enabled:
                if running or props.is_recording:
                    opt.label(text="Detén la captura para corregir pies", icon="ERROR")
                elif not take["body_action"]:
                    opt.label(text="Esta toma no tiene canal de cuerpo", icon="ERROR")
            del_row = opt.row(align=True)
            del_row.operator("puppet_mocap.clear_keyframes", text="Eliminar esta toma", icon="TRASH")

        # --- Rig ---
        box = layout.box()
        box.label(text="Rig", icon="ARMATURE_DATA")
        box.operator("puppet_mocap.reset_rig", icon="LOOP_BACK")

        # --- Diagnóstico ---
        box = layout.box()
        box.label(text="Diagnóstico", icon="CONSOLE")
        col = box.column(align=True)
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
        col.separator()
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
        if running:
            box.label(text="Fijo durante la captura — detén e inicia de nuevo para cambiarlo",
                      icon="LOCKED")
        launch = box.column(align=True)
        launch.enabled = not running
        # cam_index vive en Personaje y cámara > Avanzado (junto al nombre
        # detectado, donde tiene contexto); aquí solo los params de envío.
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


CLASSES = (PUPPET_MT_take_list, PUPPET_PT_main)
