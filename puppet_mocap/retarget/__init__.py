"""Paquete de retarget — coordina los 3 módulos de captura.

Módulos:
    body.py  — pose corporal (hips con yaw, spine con twist, brazos, piernas,
               pies, cuello, cabeza) con propagación de matrices frescas
    hands.py — orientación de muñecas + 15 huesos de dedos por lado
    face.py  — blendshapes ARKit → shape keys del mesh

`apply_pose()` despacha cada módulo según los flags `enable_*`. La grabación
ya NO inserta keyframes aquí: cuando `collect_snapshot=True` devuelve un
snapshot {bones, shapes} que operators.py acumula y hornea (bake) al detener
la grabación — patrón buffer-then-bake (estilo Rokoko): keyframe_insert en
vivo era el costo dominante y hacía la grabación una presentación de
diapositivas.
"""
from __future__ import annotations

import time

import bpy

from .. import log
from . import body, common, face, hands
from .common import (
    calibrate_hand_landmarks,
    calibrate_pose_landmarks,
    clear_all_keyframes,
    compute_calibration,
    fix_orientation,
    get_armature,
    mirror_hand_landmarks,
    mirror_pose_landmarks,
    reset_smoothing,
    set_target_armature,
    set_tick,
)


def get_keyframe_bones(prefix: str = "mixamorig:",
                       include_body: bool = True,
                       include_hands: bool = True) -> list[str]:
    key = (prefix, include_body, include_hands)
    cached = common._state.setdefault("kf_names", {}).get(key)
    if cached is not None:
        return cached
    out: list[str] = []
    if include_body:
        out += body.bone_names(prefix)
    if include_hands:
        out += hands.bone_names(prefix)
    common._state["kf_names"][key] = out
    return out


def _mirror_hands_payload(hands_data):
    """Modo espejo para manos: intercambia L↔R y refleja cada mano en x.
    Una mano izquierda reflejada ES geométricamente una derecha, así que la
    quiralidad (data_side) y el handedness también se intercambian."""
    if not hands_data:
        return hands_data

    def _flip(side_data):
        if side_data is None:
            return None
        if isinstance(side_data, dict):
            out = dict(side_data)
            out["lm"] = mirror_hand_landmarks(side_data.get("lm"))
            if out.get("hd") in ("Left", "Right"):
                out["hd"] = "Right" if out["hd"] == "Left" else "Left"
            return out
        return mirror_hand_landmarks(side_data)

    return {"L": _flip(hands_data.get("R")), "R": _flip(hands_data.get("L"))}


def _calibrate_hands_payload(hands_data, R):
    if not hands_data:
        return hands_data

    def _cal(side_data):
        if side_data is None:
            return None
        if isinstance(side_data, dict):
            out = dict(side_data)
            out["lm"] = calibrate_hand_landmarks(side_data.get("lm"), R)
            return out
        return calibrate_hand_landmarks(side_data, R)

    return {k: _cal(v) for k, v in hands_data.items()}


def snapshot_pose(arm, prefix: str,
                  include_body: bool, include_hands: bool,
                  include_face: bool) -> dict:
    """Lee las rotaciones recién aplicadas (y shape keys) para el buffer de
    grabación. Tuplas planas — sin referencias RNA que se puedan invalidar."""
    bones = {}
    for name in get_keyframe_bones(prefix, include_body, include_hands):
        pb = arm.pose.bones.get(name)
        if pb is not None:
            q = pb.rotation_quaternion
            bones[name] = (q.w, q.x, q.y, q.z)
    shapes = face.snapshot_values(arm) if include_face else {}
    return {"bones": bones, "shapes": shapes}


def apply_pose(landmarks=None, *, hands_data=None, face_data=None,
               prefix: str = "mixamorig:",
               swap_hands: bool = False, flip_palm_normal: bool = False,
               rotation_smooth: float = 0.5,
               mirror: bool = False, min_visibility: float = 0.5,
               calibrate: bool = True,
               enable_body: bool = True, enable_hands: bool = True,
               enable_face: bool = False):
    """Aplica una pose dispatcheando a los módulos habilitados.

    Parámetros principales:
        landmarks: 33 [x,y,z,vis] de pose_world_landmarks (None = no body).
        hands_data: dict {'L': ..., 'R': ...}.
        face_data: dict {'blendshapes': {...}}.
        mirror: modo espejo (refleja cuerpo y manos).
        min_visibility: umbral de visibilidad Pose para aplicar un hueso.

    Devuelve dict {'body': n, 'hands': n, 'face': n} o False sin armature.
    """
    arm = get_armature()
    if arm is None:
        return False

    if mirror:
        landmarks = mirror_pose_landmarks(landmarks) if landmarks else landmarks
        hands_data = _mirror_hands_payload(hands_data)

    # Calibración DESPUÉS del espejo: se captura y se aplica en el mismo
    # orden, así el toggle de espejo no invalida la calibración.
    R = common._state.get("calib_R") if calibrate else None
    if R is not None:
        if landmarks:
            landmarks = calibrate_pose_landmarks(landmarks, R)
        hands_data = _calibrate_hands_payload(hands_data, R)

    set_tick(time.time(), rotation_smooth)

    moved = {"body": 0, "hands": 0, "face": 0}

    if enable_body and landmarks:
        try:
            moved["body"] = body.apply(arm, landmarks, prefix=prefix,
                                       min_vis=min_visibility)
        except Exception:
            log.exception("module=body apply failed")

    if enable_hands and hands_data:
        try:
            moved["hands"] = hands.apply(arm, hands_data, landmarks=landmarks,
                                         prefix=prefix,
                                         swap=swap_hands, flip_normal=flip_palm_normal)
        except Exception:
            log.exception(f"module=hands apply failed; payload keys={list(hands_data.keys())}")

    if enable_face and face_data:
        try:
            moved["face"] = face.apply(arm, face_data)
        except Exception:
            log.exception("module=face apply failed")

    now = time.time()
    last_t = common._state.get("last_debug_t", 0.0)
    if now - last_t > 5.0:
        common._state["last_debug_t"] = now
        log.info(
            f"body={moved['body']} hands={moved['hands']} face={moved['face']} "
            f"prefix='{prefix}' enable=(b={enable_body},h={enable_hands},f={enable_face}) "
            f"smooth={rotation_smooth:.2f} mirror={mirror}"
        )

    # NOTA: NO forzamos view_layer.update() aquí. Blender re-evalúa el depsgraph
    # automáticamente antes del próximo redraw del viewport. Llamar update() en
    # cada frame de captura era una fuente confirmada de crashes en Blender 5.x.
    return moved


__all__ = (
    "apply_pose",
    "snapshot_pose",
    "compute_calibration",
    "fix_orientation",
    "clear_all_keyframes",
    "get_armature",
    "set_target_armature",
    "get_keyframe_bones",
    "reset_smoothing",
    "body",
    "hands",
    "face",
)
