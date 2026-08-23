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


def _module_box(layout, props, title, icon, enable_attr, record_attr):
    """Caja con header (toggle de habilitar) y switch de grabación."""
    box = layout.box()
    row = box.row(align=True)
    row.prop(props, enable_attr, text="")
    row.label(text=title, icon=icon)
    sub = row.row(align=True)
    sub.active = getattr(props, enable_attr)
    sub.prop(props, record_attr, text="Grabar", toggle=True)
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
                          "enable_body", "record_body")
        if props.enable_body:
            sub = box.column(align=True)
            sub.label(text="Pose + spine con twist + pies")
            sub.prop(props, "enable_root_translation")
            if props.enable_root_translation:
                sub.prop(props, "root_translation_scale")

        # --- Módulos: Manos ---
        box = _module_box(layout, props, "Manos", "VIEW_PAN",
                          "enable_hands", "record_hands")
        if props.enable_hands:
            sub = box.column(align=True)
            sub.prop(props, "swap_hands")
            sub.prop(props, "flip_palm_normal")

        # --- Módulos: Cara ---
        box = _module_box(layout, props, "Cara", "USER",
                          "enable_face", "record_face")
        if props.enable_face:
            sub = box.column(align=True)
            if not fs["face_model"]:
                sub.label(text="face_landmarker.task no descargado", icon="ERROR")
                sub.operator("puppet_mocap.download_models", icon="IMPORT")
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
            row.operator("puppet_mocap.toggle_record", text="Grabar", icon="REC")
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
        col.operator("puppet_mocap.check_deps", icon="CHECKMARK")
        missing_models = []
        if not fs["pose_model"]:
            missing_models.append("pose")
        if not fs["hand_model"]:
            missing_models.append("manos")
        if not fs["face_model"]:
            missing_models.append("cara")
        if missing_models:
            col.label(text=f"Modelos faltantes: {', '.join(missing_models)}", icon="ERROR")
            col.operator("puppet_mocap.download_models", icon="IMPORT")
        if props.deps_status:
            icon = "CHECKMARK" if props.deps_status.startswith("OK") else "ERROR"
            col.label(text=props.deps_status, icon=icon)
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


CLASSES = (PUPPET_PT_main,)
