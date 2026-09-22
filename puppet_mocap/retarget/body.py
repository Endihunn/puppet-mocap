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

import time
from mathutils import Matrix, Quaternion, Vector

from .common import (
    _state,
    aim,
    canonical_to_armature_space,
    chained_world_3x3,
    lm_visibility,
    mp_to_arm,
    orient_yx,
    reset_foot_lock,
    smooth_scalar,
)
from .foot_plant import (
    FootContactPatch,
    GroundModel,
    FootStateMachine,
    StanceSnapshot,
    remap_hips_delta_to_spine,
    LOCKED,
    SWING,
)
from .foot_lock import _solve_two_bone_ik

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

# El umbral se mide en metros de MediaPipe por muestra. La histéresis evita
# que un pie quieto entre/salga de contacto por el jitter de la webcam.
FOOT_LOCK_RELEASE_MULTIPLIER = 2.5


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


def _rig_torso_length(arm, prefix: str) -> float:
    """Altura del torso del rig en rest pose (Hips→Spine2), en unidades locales."""
    hips = arm.data.bones.get(f"{prefix}Hips")
    spine2 = arm.data.bones.get(f"{prefix}Spine2")
    if hips is None or spine2 is None:
        return 0.0
    return (spine2.head - hips.head).length


def _shift_root_in_arm_space(hips, delta: Vector):
    """Suma a Hips una traslación expresada en arm-space."""
    if hips is None or delta.length < 1e-8:
        return
    rest3 = hips.bone.matrix_local.to_3x3()
    hips.location = Vector(hips.location) + rest3.inverted_safe() @ delta


def _fresh_root_head(hips) -> Vector:
    """Head del Hips en arm-space sin depender del depsgraph stale."""
    rest = hips.bone.matrix_local
    return rest.translation + rest.to_3x3() @ Vector(hips.location)


def _fresh_child_head(parent, parent_head: Vector, parent_world,
                      child) -> Vector:
    """Head de un hijo usando la cadena de matrices recién calculada."""
    rel = parent.bone.matrix_local.inverted_safe() @ child.bone.matrix_local
    return parent_head + parent_world @ rel.translation


def _apply_foot_lock(arm, hips, pts, vis, foot_heads, prefix: str,
                     min_vis: float, speed_limit: float,
                     root_translation: bool, mid_hip: Vector,
                     ground_mode: str = "PLANE_Z", ground_z: float = 0.0,
                     ground_object=None, sole_offset: float = 0.0,
                     sensitivity: float = 1.0,
                     sample_time: float | None = None,
                     foot_lock_mode: str = "AUTO") -> int:
    """Mantiene el apoyo de los pies con arquitectura Hard Plant y remapeo de torso.

    En modo Hard Plant:
    - Una pierna en LOCKED captura y congela su StanceSnapshot (UpLeg, Leg, Foot, ToeBase).
    - Hips se estabiliza al transform de apoyo y no articula las piernas clavadas.
    - El movimiento solicitado por el mocap para Hips (rotación y traslación) se remapea
      hacia Spine, Spine1 y Spine2.
    - La pierna en SWING permanece libre en FK mocap puro.
    """
    t_now = sample_time if sample_time is not None else time.time()
    sms = _state.setdefault("foot_sms", {})
    snapshots = _state.setdefault("foot_snapshots", {})

    force_lock = _state.pop("foot_force_lock", False)
    force_release = _state.pop("foot_force_release", False)

    ground = GroundModel(mode=ground_mode, ground_z=ground_z,
                         ground_object=ground_object, fallback_z=ground_z)

    feet_def = (
        ("L", LM_L_ANKLE, LM_L_FOOT, "LeftFoot", "Left"),
        ("R", LM_R_ANKLE, LM_R_FOOT, "RightFoot", "Right"),
    )

    bones = arm.pose.bones if hasattr(arm, "pose") else {}
    has_full_rig = hasattr(arm, "pose") and any(f"{prefix}{side}UpLeg" in bones for side in ("Left", "Right"))

    st = _state.setdefault("foot_lock", {})
    st.setdefault("prev", {})
    st.setdefault("contact", {})
    st.setdefault("anchor", {})
    st.setdefault("bias", Vector((0.0, 0.0, 0.0)))

    if not has_full_rig:
        feet = (
            ("L", LM_L_ANKLE, LM_L_FOOT, "LeftFoot"),
            ("R", LM_R_ANKLE, LM_R_FOOT, "RightFoot"),
        )
        active = []
        limit = max(float(speed_limit), 1e-4) * max(sensitivity, 0.2)
        for side, i_ankle, i_toe, suffix in feet:
            pb = arm.pose.bones.get(f"{prefix}{suffix}") if hasattr(arm, "pose") else None
            current_head = foot_heads.get(side)
            valid = (pb is not None and i_ankle < len(vis) and i_toe < len(vis)
                     and vis[i_ankle] >= min_vis and vis[i_toe] >= min_vis
                     and current_head is not None)
            if not valid:
                st["prev"].pop(side, None)
                st["contact"][side] = False
                st["anchor"].pop(side, None)
                continue

            source = (pts[i_ankle] + pts[i_toe]) * 0.5
            previous = st["prev"].get(side)
            st["prev"][side] = source.copy()
            if previous is None:
                st["contact"][side] = False
                st["anchor"].pop(side, None)
                continue

            speed = (source - previous).length
            was_contact = bool(st["contact"].get(side, False))
            threshold = limit * (FOOT_LOCK_RELEASE_MULTIPLIER if was_contact else 1.0)
            in_contact = speed <= threshold
            st["contact"][side] = in_contact
            if in_contact and not was_contact:
                st["anchor"][side] = current_head.copy()
            elif not in_contact:
                st["anchor"].pop(side, None)

            anchor = st["anchor"].get(side)
            if in_contact and anchor is not None:
                active.append((side, anchor - current_head, anchor))

        _state["foot_telemetry"] = {
            "state_l": "LOCKED" if st["contact"].get("L", False) else "SWING",
            "state_r": "LOCKED" if st["contact"].get("R", False) else "SWING",
            "weight_l": 1.0 if st["contact"].get("L", False) else 0.0,
            "weight_r": 1.0 if st["contact"].get("R", False) else 0.0,
            "ground_z": ground_z,
        }

        if not active:
            bias = Vector(st.get("bias", (0.0, 0.0, 0.0)))
            if root_translation and bias.length > 1e-6 and hips is not None and hasattr(hips, "location"):
                _state["root_hip_ref"] = mid_hip.copy()
                _state["root_loc_ref"] = Vector(hips.location)
                st["bias"] = Vector((0.0, 0.0, 0.0))
            return 0

        deltas = [item[1] for item in active]
        delta = sum(deltas, Vector((0.0, 0.0, 0.0))) / len(deltas)
        if delta.length < 1e-6 or hips is None:
            return 0
        if root_translation:
            st["bias"] = Vector(st.get("bias", (0.0, 0.0, 0.0))) + delta
        else:
            _shift_root_in_arm_space(hips, delta)

        return len(active)

    active_deltas = []
    locked_sides = []

    for side, i_ankle, i_toe, foot_suffix, side_name in feet_def:
        sm = sms.setdefault(side, FootStateMachine())
        if force_release:
            sm.force_release()
            snapshots.pop(side, None)
        elif force_lock:
            sm.force_lock()

        pb_f = bones.get(f"{prefix}{foot_suffix}")
        pb_toe = bones.get(f"{prefix}{side_name}ToeBase")
        pb_u = bones.get(f"{prefix}{side_name}UpLeg")
        pb_l = bones.get(f"{prefix}{side_name}Leg")

        current_head = foot_heads.get(side)
        valid = (i_ankle < len(vis) and i_toe < len(vis) and vis[i_ankle] >= min_vis and vis[i_toe] >= min_vis)

        if pb_f is not None:
            patch = FootContactPatch.from_bones(arm, pb_f, pb_toe, sole_offset=sole_offset)
        elif current_head is not None:
            patch = FootContactPatch(heel=current_head, toe=current_head, mid=current_head,
                                     normal=Vector((0.0, 0.0, 1.0)), length=0.15, ankle=current_head)
        else:
            pt = pts[i_ankle] if i_ankle < len(pts) else Vector()
            patch = FootContactPatch(heel=pt, toe=pt, mid=pt,
                                     normal=Vector((0.0, 0.0, 1.0)), length=0.15, ankle=pt)

        # Actualizar máquina de estados con override manual
        state = sm.update(
            patch, ground, t_now,
            visibility=vis[i_ankle] if valid else 0.0,
            sensitivity=sensitivity,
            side=side,
            override_mode=foot_lock_mode,
        )

        st["contact"][side] = (state == LOCKED)

        if state == LOCKED:
            locked_sides.append((side, side_name, pb_u, pb_l, pb_f, pb_toe, patch))
            if current_head is not None and sm.anchor_ankle is not None:
                delta_w = sm.anchor_ankle - current_head
                active_deltas.append(delta_w)
        else:
            snapshots.pop(side, None)

    # Telemetría en tiempo real
    state_l = sms["L"].state if "L" in sms else "SWING"
    state_r = sms["R"].state if "R" in sms else "SWING"
    _state["foot_telemetry"] = {
        "state_l": state_l,
        "state_r": state_r,
        "weight_l": sms["L"].blend_weight if "L" in sms else 0.0,
        "weight_r": sms["R"].blend_weight if "R" in sms else 0.0,
        "ground_z": ground_z,
        "torso_remapped": len(locked_sides) > 0,
    }

    if not locked_sides:
        bias = Vector(st.get("bias", (0.0, 0.0, 0.0)))
        if root_translation and bias.length > 1e-6 and hips is not None and hasattr(hips, "location"):
            _state["root_hip_ref"] = mid_hip.copy()
            _state["root_loc_ref"] = Vector(hips.location)
            st["bias"] = Vector((0.0, 0.0, 0.0))
        return 0

    # ---------------------------------------------------------------------
    # HARD PLANT: estabilizar Hips, congelar piernas de apoyo y remapear torso
    # ---------------------------------------------------------------------
    if has_full_rig and hips is not None:
        mocap_hips_loc = Vector(hips.location).copy() if hasattr(hips, "location") else Vector()
        mocap_hips_rot = Quaternion(hips.rotation_quaternion).copy() if hasattr(hips, "rotation_quaternion") else Quaternion()

        # Capturar snapshots para lados recién bloqueados
        for side, side_name, pb_u, pb_l, pb_f, pb_toe, patch in locked_sides:
            if side not in snapshots or snapshots[side] is None:
                snapshots[side] = StanceSnapshot.capture(
                    arm, hips, pb_u, pb_l, pb_f, pb_toe, patch=patch
                )

        # Usar el snapshot del primer apoyo bloqueado como ancla de Hips
        ref_snapshot = snapshots[locked_sides[0][0]]
        delta_rot = mocap_hips_rot @ ref_snapshot.hips_rot.inverted()
        delta_loc = mocap_hips_loc - ref_snapshot.hips_loc

        # 1. Estabilizar Hips al snapshot duro
        if hasattr(hips, "location") and ref_snapshot.hips_loc is not None:
            hips.location = ref_snapshot.hips_loc.copy()
        if hasattr(hips, "rotation_quaternion") and ref_snapshot.hips_rot is not None:
            hips.rotation_mode = "QUATERNION"
            hips.rotation_quaternion = ref_snapshot.hips_rot.copy()

        # 2. Restaurar pose snapshot en cada pierna de apoyo (LOCKED)
        for side, side_name, pb_u, pb_l, pb_f, pb_toe, _ in locked_sides:
            snap = snapshots.get(side)
            if snap is not None:
                snap.restore(hips_pb=None, upleg_pb=pb_u, leg_pb=pb_l,
                             foot_pb=pb_f, toe_pb=pb_toe, restore_hips=False)

        # 3. Transferir el movimiento capturado de cadera exclusivamente al torso
        remap_hips_delta_to_spine(arm, prefix, delta_rot, delta_loc)

        return len(locked_sides)

    # Fallback para mocks de prueba sin jerarquía completa de huesos
    if active_deltas and hips is not None:
        delta = sum(active_deltas, Vector((0.0, 0.0, 0.0))) / len(active_deltas)
        if delta.length > 1e-6:
            if root_translation:
                st["bias"] = Vector(st.get("bias", (0.0, 0.0, 0.0))) + delta
            else:
                _shift_root_in_arm_space(hips, delta)

    return len(locked_sides)


def get_foot_telemetry() -> dict:
    """Devuelve el estado de apoyo actual para la UI."""
    return _state.get("foot_telemetry", {
        "state_l": "SWING",
        "state_r": "SWING",
        "weight_l": 0.0,
        "weight_r": 0.0,
        "ground_z": 0.0,
    })


def _apply_root_translation(arm, hips, mid_hip, observed_torso_len, torso_ok,
                            prefix, root_scale, foot_lock: bool = False):
    """P2-4: traslada el Hips según el desplazamiento del mid-hip en arm-space.

    La referencia se captura en la primera frame con la feature activa, para que
    el rig se mueva RELATIVO a su posición actual y no salte a la posición
    absoluta de la persona. Escala: automática (altura_rig_torso /
    altura_observada_torso) × multiplicador manual `root_scale`.

    El delta de arm-space se convierte al REST BASIS del hueso (pose_bone.location
    NO está en arm-space). Hips no tiene padre, así que matrix_local es la
    conversión completa; si algún día se traslada un hueso con padre, haría
    falta la cadena de matrices."""
    if hips is None:
        return
    ref = _state.get("root_hip_ref")
    if ref is None:
        _state["root_hip_ref"] = mid_hip.copy()
        _state["root_loc_ref"] = Vector(hips.location)
        return
    root_loc_ref = _state.get("root_loc_ref")
    if root_loc_ref is None:
        root_loc_ref = Vector(hips.location)
        _state["root_loc_ref"] = root_loc_ref

    scale = float(root_scale)
    if torso_ok and observed_torso_len > 1e-3:
        rig_torso = _rig_torso_length(arm, prefix)
        if rig_torso > 1e-3:
            scale *= rig_torso / observed_torso_len

    d = mid_hip - ref
    # pose_bone.location está en el REST BASIS del hueso, no en arm-space. El
    # Hips de Mixamo apunta hacia arriba, así que matrix_local mapea
    # Y_local→+Z_arm y Z_local→−Y_arm; sumar el delta sin convertir lo rotaba
    # 90° sobre X (agacharse empujaba hacia atrás).
    M = hips.bone.matrix_local.to_3x3()
    arm_delta = Vector((d.x, d.y, d.z)) * scale
    if foot_lock:
        arm_delta += Vector(_state.get("foot_lock", {}).get(
            "bias", (0.0, 0.0, 0.0)))
    loc = root_loc_ref + M.inverted_safe() @ arm_delta
    hips.location = (
        smooth_scalar("root_loc_x", loc.x),
        smooth_scalar("root_loc_y", loc.y),
        smooth_scalar("root_loc_z", loc.z),
    )


def apply(arm, landmarks, prefix: str = "mixamorig:",
          min_vis: float = 0.5, root_translation: bool = False,
          root_scale: float = 1.0, foot_lock: bool = False,
          foot_lock_speed: float = 0.02,
          ground_mode: str = "PLANE_Z", ground_z: float = 0.0,
          ground_object=None, sole_offset: float = 0.0,
          foot_lock_sensitivity: float = 1.0,
          sample_time: float | None = None,
          skip_forearms: set[str] | None = None,
          skip_arms: set[str] | None = None,
          foot_lock_mode: str = "AUTO") -> int:
    """Aplica pose corporal con propagación de matrices frescas.
    Devuelve cuántos huesos movió."""
    if not landmarks or len(landmarks) < 33:
        return 0
    if not foot_lock:
        reset_foot_lock()

    # El espacio canónico de MediaPipe es Z-up, pero el objeto Armature puede
    # estar rotado para colocarlo en la escena. El solver trabaja en los ejes
    # locales del armature, que es también donde viven las rest matrices.
    pts = [canonical_to_armature_space(arm, mp_to_arm(lm))
           for lm in landmarks]
    vis = [lm_visibility(lm) for lm in landmarks]

    def ok(*idxs) -> bool:
        return all(vis[i] >= min_vis for i in idxs)

    mid_hip      = (pts[LM_L_HIP] + pts[LM_R_HIP]) * 0.5
    mid_shoulder = (pts[LM_L_SHOULDER] + pts[LM_R_SHOULDER]) * 0.5
    torso = mid_shoulder - mid_hip
    torso_len = torso.length

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
        if root_translation:
            _apply_root_translation(arm, hips, mid_hip, torso_len, torso_ok,
                                    prefix, root_scale, foot_lock=foot_lock)

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
    _state["arm_parent_world_3x3"] = {}
    _state["arm_dirs"] = {}
    for side, upper, lower, i_a, i_b, i_c in LIMB_CHAINS:
        sh = bones.get(f"{prefix}{side}Shoulder")
        # el clavicular no se anima, pero SÍ propaga la rotación fresca del torso
        arm_parent = _advance(sh, None, spine_top_world) if sh else spine_top_world
        _state["arm_parent_world_3x3"][side] = arm_parent

        pb_u = bones.get(f"{prefix}{upper}")
        if pb_u is None:
            continue

        v_u = pts[i_b] - pts[i_a]
        v_l = pts[i_c] - pts[i_b]
        dir_u = v_u.normalized() if (ok(i_a, i_b) and v_u.length > 1e-4) else None
        dir_l = v_l.normalized() if (ok(i_b, i_c) and v_l.length > 1e-4) else None
        _state["arm_dirs"][side] = (dir_u, dir_l)

        if skip_arms and side in skip_arms:
            # Manos resolverá UpperArm, ForeArm y Hand de forma coordinada
            continue

        q_u = None
        if dir_u is not None:
            q_u = aim(pb_u, dir_u, parent_world_3x3=arm_parent)
            if q_u is not None:
                moved += 1
        upper_world = _advance(pb_u, q_u, arm_parent)
        _state["arm_world_3x3"][side] = upper_world

        pb_l = bones.get(f"{prefix}{lower}")
        if pb_l is None:
            continue
        if skip_forearms and side in skip_forearms:
            continue
        if dir_l is not None:
            if aim(pb_l, dir_l, parent_world_3x3=upper_world) is not None:
                moved += 1

    # --- Piernas + pies -----------------------------------------------------
    foot_heads = {}
    hips_head = _fresh_root_head(hips) if hips is not None else None
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
        if hips is None or hips_world is None or hips_head is None:
            continue
        upper_head = _fresh_child_head(hips, hips_head, hips_world, pb_u)

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
        lower_head = _fresh_child_head(pb_u, upper_head, upper_world, pb_l)

        pb_f = bones.get(f"{prefix}{foot}")
        if pb_f is None:
            continue
        v = pts[i_foot] - pts[i_ankle]
        if ok(i_ankle, i_foot) and v.length > 1e-4:
            if aim(pb_f, v.normalized(), parent_world_3x3=lower_world) is not None:
                moved += 1
        foot_head = _fresh_child_head(pb_l, lower_head, lower_world, pb_f)
        foot_heads["L" if upper.startswith("Left") else "R"] = foot_head

    if foot_lock:
        _apply_foot_lock(arm, hips, pts, vis, foot_heads, prefix, min_vis,
                         foot_lock_speed, root_translation, mid_hip,
                         ground_mode=ground_mode, ground_z=ground_z,
                         ground_object=ground_object, sole_offset=sole_offset,
                         sensitivity=foot_lock_sensitivity, sample_time=sample_time,
                         foot_lock_mode=foot_lock_mode)

    return moved


def orient_forearm_fallback(arm, landmarks, prefix: str = "mixamorig:",
                            side: str = "Left", min_vis: float = 0.5) -> bool:
    """Orientación de rescate del antebrazo con aim() cuando hands no pudo resolverlo."""
    if not landmarks or len(landmarks) < 33:
        return False
    bones = arm.pose.bones
    pb_fa = bones.get(f"{prefix}{side}ForeArm")
    if pb_fa is None:
        return False
    elbow_idx, wrist_idx = (LM_L_ELBOW, LM_L_WRIST) if side == "Left" else (LM_R_ELBOW, LM_R_WRIST)
    if lm_visibility(landmarks[elbow_idx]) < min_vis or lm_visibility(landmarks[wrist_idx]) < min_vis:
        return False
    elbow = canonical_to_armature_space(arm, mp_to_arm(landmarks[elbow_idx]))
    wrist = canonical_to_armature_space(arm, mp_to_arm(landmarks[wrist_idx]))
    v = wrist - elbow
    if v.length < 1e-4:
        return False
    upper_world = _state.get("arm_world_3x3", {}).get(side)
    if upper_world is None:
        pb_u = bones.get(f"{prefix}{side}Arm")
        if pb_u is not None:
            upper_world = pb_u.matrix.to_3x3()
    q = aim(pb_fa, v.normalized(), parent_world_3x3=upper_world)
    return q is not None


def orient_arm_fallback(arm, landmarks, prefix: str = "mixamorig:",
                        side: str = "Left", min_vis: float = 0.5) -> bool:
    """Orientación de rescate del brazo completo (UpperArm + ForeArm) con aim() cuando hands no pudo resolverlo."""
    if not landmarks or len(landmarks) < 33:
        return False
    bones = arm.pose.bones
    for side_chain, upper, lower, i_a, i_b, i_c in LIMB_CHAINS:
        if side_chain == side:
            break
    else:
        return False

    sh = bones.get(f"{prefix}{side}Shoulder")
    arm_parent = _state.get("arm_parent_world_3x3", {}).get(side)
    if arm_parent is None:
        if sh is not None and hasattr(sh, "matrix"):
            arm_parent = sh.matrix.to_3x3()
        else:
            arm_parent = Matrix.Identity(3)

    pb_u = bones.get(f"{prefix}{upper}")
    pb_l = bones.get(f"{prefix}{lower}")
    if pb_u is None or pb_l is None:
        return False

    dirs = _state.get("arm_dirs", {}).get(side, (None, None))
    v_u, v_l = dirs[0], dirs[1]

    if v_u is None:
        if lm_visibility(landmarks[i_a]) < min_vis or lm_visibility(landmarks[i_b]) < min_vis:
            return False
        sh_pt = canonical_to_armature_space(arm, mp_to_arm(landmarks[i_a]))
        el_pt = canonical_to_armature_space(arm, mp_to_arm(landmarks[i_b]))
        v = el_pt - sh_pt
        v_u = v.normalized() if v.length > 1e-4 else None

    q_u = None
    if v_u is not None:
        q_u = aim(pb_u, v_u, parent_world_3x3=arm_parent)
    upper_world = _advance(pb_u, q_u, arm_parent)
    _state.setdefault("arm_world_3x3", {})[side] = upper_world

    if v_l is None:
        if lm_visibility(landmarks[i_b]) < min_vis or lm_visibility(landmarks[i_c]) < min_vis:
            return q_u is not None
        el_pt = canonical_to_armature_space(arm, mp_to_arm(landmarks[i_b]))
        wr_pt = canonical_to_armature_space(arm, mp_to_arm(landmarks[i_c]))
        v = wr_pt - el_pt
        v_l = v.normalized() if v.length > 1e-4 else None

    if v_l is not None:
        aim(pb_l, v_l, parent_world_3x3=upper_world)
    return True

