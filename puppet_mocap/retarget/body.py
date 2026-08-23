"""Retarget del cuerpo: hips (con yaw), spine con twist, brazos, piernas,
pies, cuello y cabeza.

Diseño v0.3:
  * Propagación de matrices world 3x3 FRESCAS por toda la jerarquía
    (hips → spine → cuello/cabeza y hips → piernas → pies, spine2 → brazos).
    Antes los hijos leían la matriz stale del depsgraph (el mismo bug del
    pulgar que hands.py ya había arreglado) y las extremidades hacían
    "rubber-band" en movimientos rápidos.
  * Hips con orient_yx (torso + línea de caderas): el personaje ahora GIRA
    cuando tú giras — aim() solo inclinaba, el yaw se perdía por completo.
  * Twist de torso distribuido en Spine/Spine1/Spine2 interpolando la línea
    de caderas hacia la línea de hombros (técnica estilo BlendArMocap de
    separar rotación de cadera y pecho).
  * Gating por visibilidad de MediaPipe (default 0.5, el umbral que usa el
    propio Google): un landmark ocluido mantiene la pose anterior en vez de
    meter basura al rig. Solo Pose trae visibility (manos/cara la reportan 0).
"""
from __future__ import annotations

from mathutils import Vector

from .common import (
    _state,
    aim,
    chained_world_3x3,
    lm_visibility,
    mp_to_arm,
    orient_yx,
)

# --- MediaPipe Pose landmark indices --------------------------------------
LM_NOSE = 0
LM_L_EAR, LM_R_EAR = 7, 8
LM_L_SHOULDER, LM_R_SHOULDER = 11, 12
LM_L_ELBOW, LM_R_ELBOW = 13, 14
LM_L_WRIST, LM_R_WRIST = 15, 16
LM_L_HIP, LM_R_HIP = 23, 24
LM_L_KNEE, LM_R_KNEE = 25, 26
LM_L_ANKLE, LM_R_ANKLE = 27, 28
LM_L_FOOT, LM_R_FOOT = 31, 32  # foot_index (punta del pie)

SPINE_SUFFIXES = ["Spine", "Spine1", "Spine2"]
# Fracción de twist cadera→hombros que recibe cada hueso de spine
SPINE_TWIST_FRACTIONS = [0.33, 0.66, 1.0]

# Cadenas de extremidades: (padre_bone, hijo_bone, lm_a, lm_b, lm_c, foot?)
#   padre apunta lm_a→lm_b, hijo apunta lm_b→lm_c
LIMB_CHAINS = [
    # (shoulder_link, bones..., landmarks...)
    ("Left",  "LeftArm",  "LeftForeArm",  LM_L_SHOULDER, LM_L_ELBOW, LM_L_WRIST),
    ("Right", "RightArm", "RightForeArm", LM_R_SHOULDER, LM_R_ELBOW, LM_R_WRIST),
]
LEG_CHAINS = [
    ("LeftUpLeg",  "LeftLeg",  "LeftFoot",  LM_L_HIP, LM_L_KNEE, LM_L_ANKLE, LM_L_FOOT),
    ("RightUpLeg", "RightLeg", "RightFoot", LM_R_HIP, LM_R_KNEE, LM_R_ANKLE, LM_R_FOOT),
]


def bone_names(prefix: str = "mixamorig:") -> list[str]:
    """Huesos que toca el módulo body (para keyframing)."""
    out = [f"{prefix}Hips"]
    out += [f"{prefix}{s}" for s in SPINE_SUFFIXES]
    for _side, a, b, *_ in LIMB_CHAINS:
        out += [f"{prefix}{a}", f"{prefix}{b}"]
    for a, b, c, *_ in LEG_CHAINS:
        out += [f"{prefix}{a}", f"{prefix}{b}", f"{prefix}{c}"]
    out += [f"{prefix}Neck", f"{prefix}Head"]
    return out


def _slerp_dir(a: Vector, b: Vector, t: float) -> Vector:
    """Interpola direcciones normalizadas; cae a lerp si son ~opuestas."""
    try:
        return a.slerp(b, t)
    except ValueError:
        v = a.lerp(b, t)
        if v.length < 1e-6:
            return a
        return v.normalized()


def _advance(pb, q, parent_world):
    """World 3x3 del bone con la rotación q (o la actual si q es None)."""
    if pb is None:
        return parent_world
    q_eff = q if q is not None else pb.rotation_quaternion
    return chained_world_3x3(pb, q_eff, parent_world_3x3=parent_world)


def apply(arm, landmarks, prefix: str = "mixamorig:",
          min_vis: float = 0.5) -> int:
    """Aplica pose corporal con propagación de matrices frescas.
    Devuelve cuántos huesos movió."""
    if not landmarks or len(landmarks) < 33:
        return 0

    pts = [mp_to_arm(lm) for lm in landmarks]
    vis = [lm_visibility(lm) for lm in landmarks]

    def ok(*idxs) -> bool:
        return all(vis[i] >= min_vis for i in idxs)

    mid_hip      = (pts[LM_L_HIP] + pts[LM_R_HIP]) * 0.5
    mid_shoulder = (pts[LM_L_SHOULDER] + pts[LM_R_SHOULDER]) * 0.5
    torso = mid_shoulder - mid_hip

    hip_line = pts[LM_L_HIP] - pts[LM_R_HIP]
    shoulder_line = pts[LM_L_SHOULDER] - pts[LM_R_SHOULDER]

    torso_ok = (ok(LM_L_HIP, LM_R_HIP, LM_L_SHOULDER, LM_R_SHOULDER)
                and torso.length > 1e-4
                and hip_line.length > 1e-4
                and shoulder_line.length > 1e-4)
    if torso_ok:
        torso = torso.normalized()
        hip_line = hip_line.normalized()
        shoulder_line = shoulder_line.normalized()

    moved = 0
    bones = arm.pose.bones

    # --- Hips: orientación completa (inclinación + YAW + roll) ------------
    hips = bones.get(f"{prefix}Hips")
    hips_world = None
    if hips is not None:
        q = None
        if torso_ok:
            q = orient_yx(hips, torso, hip_line)
            if q is not None:
                moved += 1
        hips_world = _advance(hips, q, None)

    # --- Spine: twist distribuido cadera→hombros --------------------------
    parent_world = hips_world
    for suffix, frac in zip(SPINE_SUFFIXES, SPINE_TWIST_FRACTIONS):
        pb = bones.get(f"{prefix}{suffix}")
        if pb is None:
            continue
        q = None
        if torso_ok:
            lateral = _slerp_dir(hip_line, shoulder_line, frac)
            q = orient_yx(pb, torso, lateral, parent_world_3x3=parent_world)
            if q is not None:
                moved += 1
        parent_world = _advance(pb, q, parent_world)
    spine_top_world = parent_world

    # --- Cuello y cabeza ---------------------------------------------------
    mid_ear = (pts[LM_L_EAR] + pts[LM_R_EAR]) * 0.5
    head_up = mid_ear - mid_shoulder
    neck = bones.get(f"{prefix}Neck")
    neck_world = spine_top_world
    if neck is not None:
        q = None
        if ok(LM_L_EAR, LM_R_EAR, LM_L_SHOULDER, LM_R_SHOULDER) and head_up.length > 1e-4:
            q = aim(neck, head_up.normalized(), parent_world_3x3=spine_top_world)
            if q is not None:
                moved += 1
        neck_world = _advance(neck, q, spine_top_world)

    head = bones.get(f"{prefix}Head")
    if head is not None:
        lateral_head = pts[LM_L_EAR] - pts[LM_R_EAR]
        if (ok(LM_L_EAR, LM_R_EAR) and head_up.length > 1e-4
                and lateral_head.length > 1e-4):
            if orient_yx(head, head_up.normalized(), lateral_head.normalized(),
                         parent_world_3x3=neck_world) is not None:
                moved += 1

    # --- Brazos (via Shoulder link, sin animarlo) --------------------------
    _state["arm_world_3x3"] = {}
    for side, upper, lower, i_a, i_b, i_c in LIMB_CHAINS:
        sh = bones.get(f"{prefix}{side}Shoulder")
        # el clavicular no se anima, pero SÍ propaga la rotación fresca del torso
        arm_parent = _advance(sh, None, spine_top_world) if sh else spine_top_world

        pb_u = bones.get(f"{prefix}{upper}")
        if pb_u is None:
            continue
        q_u = None
        v = pts[i_b] - pts[i_a]
        if ok(i_a, i_b) and v.length > 1e-4:
            q_u = aim(pb_u, v.normalized(), parent_world_3x3=arm_parent)
            if q_u is not None:
                moved += 1
        upper_world = _advance(pb_u, q_u, arm_parent)
        # hands.py usa esta matriz para orientar el ForeArm con la normal de
        # la palma sin leer el depsgraph stale
        _state["arm_world_3x3"][side] = upper_world

        pb_l = bones.get(f"{prefix}{lower}")
        if pb_l is None:
            continue
        v = pts[i_c] - pts[i_b]
        if ok(i_b, i_c) and v.length > 1e-4:
            if aim(pb_l, v.normalized(), parent_world_3x3=upper_world) is not None:
                moved += 1

    # --- Piernas + pies -----------------------------------------------------
    for upper, lower, foot, i_hip, i_knee, i_ankle, i_foot in LEG_CHAINS:
        pb_u = bones.get(f"{prefix}{upper}")
        if pb_u is None:
            continue
        q_u = None
        v = pts[i_knee] - pts[i_hip]
        if ok(i_hip, i_knee) and v.length > 1e-4:
            q_u = aim(pb_u, v.normalized(), parent_world_3x3=hips_world)
            if q_u is not None:
                moved += 1
        upper_world = _advance(pb_u, q_u, hips_world)

        pb_l = bones.get(f"{prefix}{lower}")
        if pb_l is None:
            continue
        q_l = None
        v = pts[i_ankle] - pts[i_knee]
        if ok(i_knee, i_ankle) and v.length > 1e-4:
            q_l = aim(pb_l, v.normalized(), parent_world_3x3=upper_world)
            if q_l is not None:
                moved += 1
        lower_world = _advance(pb_l, q_l, upper_world)

        pb_f = bones.get(f"{prefix}{foot}")
        if pb_f is None:
            continue
        v = pts[i_foot] - pts[i_ankle]
        if ok(i_ankle, i_foot) and v.length > 1e-4:
            if aim(pb_f, v.normalized(), parent_world_3x3=lower_world) is not None:
                moved += 1

    return moved
