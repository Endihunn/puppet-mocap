"""Retarget de manos: orientación del wrist + 15 huesos de dedos por lado."""
from __future__ import annotations

import math
import time

from mathutils import Matrix, Quaternion, Vector

from .. import log
from .common import (
    _state,
    _vec_is_finite,
    aim,
    bone_rest_arm_3x3,
    canonical_to_armature_space,
    chained_world_3x3,
    decompose_swing_twist,
    lm_visibility,
    mp_to_arm,
    orient_yz,
    smooth_q,
    smooth_scalar,
    unwrap_angle,
)

# Tolerancia para flips de la palm normal: cuántos frames consecutivos con
# normal invertida toleramos antes de aceptarla como rotación real. A 20 fps,
# 3 frames ≈ 150 ms — suficiente para descartar flips espurios del modelo
# (<2 frames) sin bloquear giros de muñeca legítimos del usuario.
PALM_FLIP_TOLERANCE = 3

# Índices MediaPipe Pose para codo/muñeca por lado anatómico
_BODY_ELBOW_WRIST = {"Left": (13, 15), "Right": (14, 16)}

# --- MediaPipe Hand landmark indices --------------------------------------
HL_WRIST = 0
HL_THUMB_CMC, HL_THUMB_MCP, HL_THUMB_IP, HL_THUMB_TIP = 1, 2, 3, 4
HL_INDEX_MCP, HL_INDEX_PIP, HL_INDEX_DIP, HL_INDEX_TIP = 5, 6, 7, 8
HL_MIDDLE_MCP, HL_MIDDLE_PIP, HL_MIDDLE_DIP, HL_MIDDLE_TIP = 9, 10, 11, 12
HL_RING_MCP, HL_RING_PIP, HL_RING_DIP, HL_RING_TIP = 13, 14, 15, 16
HL_PINKY_MCP, HL_PINKY_PIP, HL_PINKY_DIP, HL_PINKY_TIP = 17, 18, 19, 20

FINGER_VECTORS = [
    ("HandThumb1",  HL_THUMB_CMC,   HL_THUMB_MCP),
    ("HandThumb2",  HL_THUMB_MCP,   HL_THUMB_IP),
    ("HandThumb3",  HL_THUMB_IP,    HL_THUMB_TIP),
    ("HandIndex1",  HL_INDEX_MCP,   HL_INDEX_PIP),
    ("HandIndex2",  HL_INDEX_PIP,   HL_INDEX_DIP),
    ("HandIndex3",  HL_INDEX_DIP,   HL_INDEX_TIP),
    ("HandMiddle1", HL_MIDDLE_MCP,  HL_MIDDLE_PIP),
    ("HandMiddle2", HL_MIDDLE_PIP,  HL_MIDDLE_DIP),
    ("HandMiddle3", HL_MIDDLE_DIP,  HL_MIDDLE_TIP),
    ("HandRing1",   HL_RING_MCP,    HL_RING_PIP),
    ("HandRing2",   HL_RING_PIP,    HL_RING_DIP),
    ("HandRing3",   HL_RING_DIP,    HL_RING_TIP),
    ("HandPinky1",  HL_PINKY_MCP,   HL_PINKY_PIP),
    ("HandPinky2",  HL_PINKY_PIP,   HL_PINKY_DIP),
    ("HandPinky3",  HL_PINKY_DIP,   HL_PINKY_TIP),
]

# Cadenas padre→hijo. Procesamos cada cadena propagando la matriz world 3x3
# manualmente para que los hijos vean la rotación recién asignada del padre,
# no la stale del depsgraph.
FINGER_CHAINS = (
    ("HandThumb1",  "HandThumb2",  "HandThumb3"),
    ("HandIndex1",  "HandIndex2",  "HandIndex3"),
    ("HandMiddle1", "HandMiddle2", "HandMiddle3"),
    ("HandRing1",   "HandRing2",   "HandRing3"),
    ("HandPinky1",  "HandPinky2",  "HandPinky3"),
)
_FINGER_VEC_BY_NAME = {n: (a, b) for n, a, b in FINGER_VECTORS}


def bone_names(prefix: str = "mixamorig:") -> list[str]:
    out = []
    for side in ("Left", "Right"):
        out.append(f"{prefix}{side}Arm")
        out.append(f"{prefix}{side}ForeArm")
        out.append(f"{prefix}{side}Hand")
        for suffix, _, _ in FINGER_VECTORS:
            out.append(f"{prefix}{side}{suffix}")
    return out


def _palm_basis(side: str, pts, flip_normal: bool = False, handedness: str | None = None):
    """Devuelve (palm_forward, palm_normal) en arm-space.
    palm_forward = wrist → middle MCP (= Y local del bone Hand de Mixamo)
    palm_normal  = 'hacia la palma'     (= Z local del bone Hand de Mixamo)

    Geometría palmar anatómica:
    - Escala de mano: tamaño relativo wrist → middle MCP.
    - Vector metacarpiano lateral: index MCP → pinky MCP (pts[17] - pts[5]).
    - Convención por quiralidad (Left vs Right):
      Para LeftHand: fwd.cross(pinky - index) apunta en dirección palmar (+Z local).
      Para RightHand: (pinky - index).cross(fwd) apunta en dirección palmar (+Z local).
    - Continuidad temporal: en giros físicos continuos (0° a 180°), la normal rota
      suavemente (dot > 0 entre frames adyacentes a >=20 fps).
      Si entre frames con dt < 0.25s ocurre un salto con dot < 0 (ambigüedad de profundidad
      de MediaPipe con la mano de canto), se mantiene la orientación previa válida.
    """
    wrist = pts[0]
    idx = pts[5]
    mid = pts[9]
    pinky = pts[17]

    fwd = mid - wrist
    hand_size = fwd.length
    if hand_size < 1e-4:
        return None
    fwd = fwd / hand_size

    # Metacarpiano lateral: de índice a meñique (radial a ulnar)
    v_lateral = pinky - idx
    if v_lateral.length < 0.05 * hand_size:
        return None

    # Normal anatómica palmar
    if side == "Left":
        normal = fwd.cross(v_lateral)
    else:
        normal = v_lateral.cross(fwd)

    normal = normal - normal.dot(fwd) * fwd
    if normal.length < 0.05 * hand_size:
        return None
    normal.normalize()

    # Continuidad temporal frente a ambigüedad y ruido en cantos (histéresis)
    # Evita el bloqueo permanente al no resetear el timer del candidato en cada muestra.
    cache = _state.setdefault("palm_hysteresis", {})
    entry = cache.setdefault(side, {
        "accepted": None,
        "t_accepted": 0.0,
        "candidate": None,
        "t_candidate": 0.0,
    })
    t_now = _state.get("tick_t", 0.0)

    accepted = entry["accepted"]
    if accepted is None:
        entry["accepted"] = normal.copy()
        entry["t_accepted"] = t_now
        entry["candidate"] = None
        entry["t_candidate"] = t_now
    else:
        dt_accepted = t_now - entry["t_accepted"]
        dot_accepted = normal.dot(accepted)

        if dot_accepted >= 0.0:
            # Continuo: ángulo <= 90° con la normal aceptada
            entry["accepted"] = normal.copy()
            entry["t_accepted"] = t_now
            entry["candidate"] = None
            entry["t_candidate"] = t_now
        elif dt_accepted > 0.5:
            # Salto temporal grande (pausa o pérdida prolongada de tracking): aceptar
            entry["accepted"] = normal.copy()
            entry["t_accepted"] = t_now
            entry["candidate"] = None
            entry["t_candidate"] = t_now
        else:
            # Salto > 90°: evaluar persistencia temporal del candidato
            cand = entry["candidate"]
            if (cand is not None and
                    cand.length_squared > 1e-6 and
                    normal.length_squared > 1e-6 and
                    normal.dot(cand) > 0.5):
                # El candidato es consistente consigo mismo
                dt_cand = t_now - entry["t_candidate"]
                if dt_cand >= 0.20:
                    # Sostenido más de 200 ms: confirmamos el giro intencional
                    entry["accepted"] = normal.copy()
                    entry["t_accepted"] = t_now
                    entry["candidate"] = None
                    entry["t_candidate"] = t_now
                else:
                    # En ventana de observación (< 200 ms): retener normal aceptada
                    normal = accepted.copy()
            else:
                # Nuevo candidato o inconsistente: iniciar observación
                entry["candidate"] = normal.copy()
                entry["t_candidate"] = t_now
                normal = accepted.copy()

    if flip_normal:
        normal = -normal

    return fwd, normal


def _orient_forearm_from_palm(arm, bone_side: str, body_landmarks,
                              palm_normal_arm, prefix: str,
                              min_vis: float = 0.5):
    """Restringe el roll del forearm orientándolo con orient_yz(elbow→wrist,
    palm_normal). Resuelve la indeterminación del aim() de body.apply, que
    deja el roll libre y multiplica el efecto de los flips de la palma.

    Devuelve el world 3x3 recién asignado al forearm para que el Hand hijo
    lo use como `parent_world_3x3` y no lea la matriz stale del depsgraph.
    Retorna None si no pudo orientar (debe caerse al fallback de body.apply).
    """
    fa = arm.pose.bones.get(f"{prefix}{bone_side}ForeArm")
    if fa is None:
        return None
    elbow_idx, wrist_idx = _BODY_ELBOW_WRIST[bone_side]
    if len(body_landmarks) <= max(elbow_idx, wrist_idx):
        return None
    from .common import lm_visibility
    if lm_visibility(body_landmarks[elbow_idx]) < min_vis or lm_visibility(body_landmarks[wrist_idx]) < min_vis:
        return None
    elbow = canonical_to_armature_space(arm, mp_to_arm(body_landmarks[elbow_idx]))
    wrist = canonical_to_armature_space(arm, mp_to_arm(body_landmarks[wrist_idx]))
    y = wrist - elbow
    if y.length < 1e-4:
        return None
    y.normalize()
    # Matriz fresca del brazo (la acaba de calcular body.apply este mismo
    # frame) — sin ella el ForeArm se orienta contra el depsgraph stale.
    arm_world = _state.get("arm_world_3x3", {}).get(bone_side)
    q_fa = orient_yz(fa, y, palm_normal_arm, parent_world_3x3=arm_world)
    if q_fa is None:
        return None
    _state.setdefault("forearm_oriented", set()).add(bone_side)
    return chained_world_3x3(fa, q_fa, parent_world_3x3=arm_world)


def was_arm_chain_oriented(side: str) -> bool:
    """True si la cadena del brazo (UpperArm, ForeArm, Hand) fue orientada por manos en este frame."""
    return side in _state.get("arm_chain_oriented", set())


def was_forearm_oriented(side: str) -> bool:
    """True si el antebrazo o la cadena del brazo fue orientada en este frame."""
    return side in _state.get("forearm_oriented", set()) or side in _state.get("arm_chain_oriented", set())


def solve_coordinated_arm_chain(
    arm, bone_side: str, body_landmarks,
    palm_fwd: Vector, palm_normal: Vector, prefix: str,
    min_vis: float = 0.5,
    twist_budget: tuple[float, float, float] = (0.25, 0.50, 0.25)
) -> tuple[Matrix | None, int]:
    """Resuelve la cadena completa UpperArm -> ForeArm -> Hand distribuyendo el twist de la palma.

    1. UpperArm: swing hacia shoulder->elbow + twist (w_u * dynamic_twist).
    2. ForeArm: swing hacia elbow->wrist con matriz fresca del padre + twist (w_f * dynamic_twist).
    3. Hand: cierre exacto hacia la base objetivo de la palma (palm_forward, palm_normal).
    4. Continuidad temporal: desenrollado de fase en +-pi y filtrado OneEuro escalar.

    Devuelve (hand_world_3x3, moved_bones_count) o (None, 0) si no se pudo resolver.
    """
    pb_u = arm.pose.bones.get(f"{prefix}{bone_side}Arm")
    pb_fa = arm.pose.bones.get(f"{prefix}{bone_side}ForeArm")
    pb_h = arm.pose.bones.get(f"{prefix}{bone_side}Hand")
    if pb_u is None or pb_fa is None or pb_h is None:
        return None, 0

    arm_parent = _state.get("arm_parent_world_3x3", {}).get(bone_side)
    if arm_parent is None:
        sh = arm.pose.bones.get(f"{prefix}{bone_side}Shoulder")
        arm_parent = sh.matrix.to_3x3() if (sh is not None and hasattr(sh, "matrix")) else Matrix.Identity(3)

    dirs = _state.get("arm_dirs", {}).get(bone_side, (None, None))
    dir_u, dir_l = dirs[0], dirs[1]

    if (dir_u is None or dir_l is None) and body_landmarks and len(body_landmarks) >= 17:
        elbow_idx, wrist_idx = _BODY_ELBOW_WRIST[bone_side]
        sh_idx = 11 if bone_side == "Left" else 12
        if (lm_visibility(body_landmarks[sh_idx]) < min_vis or
                lm_visibility(body_landmarks[elbow_idx]) < min_vis or
                lm_visibility(body_landmarks[wrist_idx]) < min_vis):
            return None, 0
        sh_pt = canonical_to_armature_space(arm, mp_to_arm(body_landmarks[sh_idx]))
        el_pt = canonical_to_armature_space(arm, mp_to_arm(body_landmarks[elbow_idx]))
        wr_pt = canonical_to_armature_space(arm, mp_to_arm(body_landmarks[wrist_idx]))
        v_u = el_pt - sh_pt
        v_l = wr_pt - el_pt
        if v_u.length < 1e-4 or v_l.length < 1e-4:
            return None, 0
        dir_u = v_u.normalized()
        dir_l = v_l.normalized()

    if dir_u is None or dir_l is None:
        return None, 0

    def _aim_pure(pb, target_dir: Vector, parent_3x3: Matrix) -> Quaternion | None:
        if not _vec_is_finite(target_dir) or target_dir.length < 1e-6:
            return None
        rest = bone_rest_arm_3x3(pb, parent_world_3x3=parent_3x3)
        try:
            inv = rest.inverted_safe()
        except Exception:
            return None
        tgt_local = inv @ target_dir
        if not _vec_is_finite(tgt_local) or tgt_local.length < 1e-6:
            return None
        return Vector((0.0, 1.0, 0.0)).rotation_difference(tgt_local.normalized())

    # Base objetivo de Hand en arm space
    y_h = palm_fwd.normalized()
    z_h = palm_normal - palm_normal.dot(y_h) * y_h
    if z_h.length < 1e-4:
        z_h = Vector((0.0, 0.0, 1.0))
    z_h.normalize()
    x_h = y_h.cross(z_h).normalized()
    target_hand_3x3 = Matrix(((x_h.x, y_h.x, z_h.x),
                              (x_h.y, y_h.y, z_h.y),
                              (x_h.z, y_h.z, z_h.z)))

    # Medición base con cero twist en UpperArm y ForeArm
    q_u_swing0 = _aim_pure(pb_u, dir_u, arm_parent)
    if q_u_swing0 is None:
        return None, 0
    u_w0 = chained_world_3x3(pb_u, q_u_swing0, arm_parent)

    q_f_swing0 = _aim_pure(pb_fa, dir_l, u_w0)
    if q_f_swing0 is None:
        return None, 0
    fa_w0 = chained_world_3x3(pb_fa, q_f_swing0, u_w0)

    hand_rest_0 = bone_rest_arm_3x3(pb_h, parent_world_3x3=fa_w0)
    try:
        q_h_base = (hand_rest_0.inverted_safe() @ target_hand_3x3).to_quaternion()
    except Exception:
        return None, 0

    _, _, raw_twist = decompose_swing_twist(q_h_base, Vector((0.0, 1.0, 0.0)))

    # Desenrollado temporal continuo
    unwrapped_cache = _state.setdefault("arm_twist_unwrapped", {})
    valid_cache = _state.setdefault("arm_twist_prev_valid", {})
    neutral_cache = _state.setdefault("arm_twist_neutral", {})

    prev_unwrapped = unwrapped_cache.get(bone_side, 0.0)
    has_prev = valid_cache.get(bone_side, False)

    if not has_prev:
        unwrapped = raw_twist
        neutral_cache[bone_side] = raw_twist
        valid_cache[bone_side] = True
    else:
        unwrapped = unwrap_angle(raw_twist, prev_unwrapped)

    unwrapped_cache[bone_side] = unwrapped

    # Filtrado OneEuro escalar del twist desenrollado
    filtered_twist = smooth_scalar(f"arm_twist_{bone_side}", unwrapped)

    theta_neutral = neutral_cache.get(bone_side, 0.0)
    dynamic_twist = filtered_twist - theta_neutral

    w_u, w_f, _ = twist_budget
    delta_u = w_u * dynamic_twist
    delta_f = w_f * dynamic_twist

    # 1. UpperArm: swing0 + twist_u
    q_u = (q_u_swing0 @ Quaternion(Vector((0.0, 1.0, 0.0)), delta_u)).normalized()
    pb_u.rotation_mode = "QUATERNION"
    pb_u.rotation_quaternion = q_u
    u_world = chained_world_3x3(pb_u, q_u, arm_parent)
    _state.setdefault("arm_world_3x3", {})[bone_side] = u_world

    # 2. ForeArm: swing hacia dir_l usando la matriz fresca del padre + twist_f
    q_f_swing = _aim_pure(pb_fa, dir_l, u_world)
    if q_f_swing is None:
        q_f_swing = q_f_swing0
    q_fa = (q_f_swing @ Quaternion(Vector((0.0, 1.0, 0.0)), delta_f)).normalized()
    pb_fa.rotation_mode = "QUATERNION"
    pb_fa.rotation_quaternion = q_fa
    fa_world = chained_world_3x3(pb_fa, q_fa, u_world)

    # 3. Hand: cierre exacto hacia target_hand_3x3
    hand_rest_fresh = bone_rest_arm_3x3(pb_h, parent_world_3x3=fa_world)
    try:
        q_h = (hand_rest_fresh.inverted_safe() @ target_hand_3x3).to_quaternion().normalized()
    except Exception:
        return None, 0
    pb_h.rotation_mode = "QUATERNION"
    pb_h.rotation_quaternion = q_h
    hand_world = chained_world_3x3(pb_h, q_h, fa_world)

    _state.setdefault("forearm_oriented", set()).add(bone_side)
    _state.setdefault("arm_chain_oriented", set()).add(bone_side)

    return hand_world, 3


def _hand_matches_body_side(arm, lms, body_landmarks, bone_side) -> bool:
    """True si la muñeca de la mano está más cerca de la muñeca del cuerpo de
    `bone_side` que de la opuesta — i.e. los landmarks del cuerpo corresponden
    al mismo brazo que esta mano. Sustituye al viejo `data_side == bone_side`,
    que se apagaba en silencio tras el auto-swap correctivo (P1-5)."""
    wrist_idx = _BODY_ELBOW_WRIST[bone_side][1]  # 15 Left / 16 Right
    other_idx = 16 if wrist_idx == 15 else 15
    if not lms or len(body_landmarks) <= other_idx:
        return False
    hand_wrist = canonical_to_armature_space(arm, mp_to_arm(lms[0]))
    b_wrist = canonical_to_armature_space(arm, mp_to_arm(body_landmarks[wrist_idx]))
    b_other = canonical_to_armature_space(arm, mp_to_arm(body_landmarks[other_idx]))
    return (hand_wrist - b_wrist).length < (hand_wrist - b_other).length


def _dump_hand_axes_once(arm, prefix: str):
    """Loguea los ejes locales del Hand al primer apply para debug."""
    if _state.get("last_axis_dump"):
        return
    _state["last_axis_dump"] = True
    for side in ("Left", "Right"):
        pb = arm.pose.bones.get(f"{prefix}{side}Hand")
        if pb is None:
            log.warn(f"hueso {prefix}{side}Hand no encontrado en el rig")
            continue
        rest = pb.bone.matrix_local.to_3x3()
        cols = [rest.col[i] for i in range(3)]
        log.info(
            f"{side}Hand rest axes: "
            f"X=({cols[0][0]:+.2f},{cols[0][1]:+.2f},{cols[0][2]:+.2f}) "
            f"Y=({cols[1][0]:+.2f},{cols[1][1]:+.2f},{cols[1][2]:+.2f}) "
            f"Z=({cols[2][0]:+.2f},{cols[2][1]:+.2f},{cols[2][2]:+.2f})"
        )


def _apply_one_hand(arm, data_side: str, bone_side: str, lms,
                    prefix: str, flip_normal: bool,
                    body_landmarks=None, handedness: str | None = None,
                    min_vis: float = 0.5) -> int:
    """Aplica una mano al rig.

    `data_side` = clave del cache de histéresis por lado en `_palm_basis` (el
        signo palma/dorso lo decide `handedness`, no este parámetro). Debe
        seguir a la entrada para no mezclar el estado de histéresis entre manos.
    `bone_side` = a qué huesos del rig se aplica (LeftHand* o RightHand*).
        Es el que cambia con swap.
    `body_landmarks` = lms 33 del pose corporal (opcional). Si está dado y la
        muñeca de la mano corresponde al mismo lado del cuerpo que `bone_side`
        (por proximidad, no por data_side), resuelve coordinadamente
        UpperArm -> ForeArm -> Hand distribuyendo el twist para eliminar nudos de globo.
    """
    if not lms or len(lms) < 21:
        return 0

    pts = [canonical_to_armature_space(arm, mp_to_arm(lm)) for lm in lms]
    moved = 0

    pb_hand = arm.pose.bones.get(f"{prefix}{bone_side}Hand")
    if pb_hand is None:
        return 0

    basis = _palm_basis(data_side, pts, flip_normal=flip_normal, handedness=handedness)
    if basis is None:
        # Palma degenerada: resolver de inmediato el brazo completo con fallback FK
        if body_landmarks and len(body_landmarks) >= 17:
            from . import body
            if body.orient_arm_fallback(arm, body_landmarks, prefix=prefix,
                                       side=bone_side, min_vis=min_vis):
                _state.setdefault("forearm_oriented", set()).add(bone_side)
        return 0
    fwd, normal = basis

    # Resolver la cadena coordinada UpperArm -> ForeArm -> Hand ANTES de orientar dedos.
    hand_world_3x3 = None
    matches = _hand_matches_body_side(arm, lms, body_landmarks, bone_side) if body_landmarks and len(body_landmarks) >= 17 else False
    if matches:
        hand_world_3x3, arm_moved = solve_coordinated_arm_chain(
            arm, bone_side, body_landmarks, fwd, normal, prefix, min_vis=min_vis
        )
        if hand_world_3x3 is not None:
            moved += arm_moved

    # Si la cadena coordinada no pudo resolverse (swap manual, visibilidad, etc.),
    # ejecutar fallback FK de cuerpo ANTES de calcular Hand y dedos.
    if hand_world_3x3 is None and body_landmarks and len(body_landmarks) >= 17:
        from . import body
        if body.orient_arm_fallback(arm, body_landmarks, prefix=prefix,
                                    side=bone_side, min_vis=min_vis):
            moved += 2
            _state.setdefault("forearm_oriented", set()).add(bone_side)
            fa = arm.pose.bones.get(f"{prefix}{bone_side}ForeArm")
            arm_world = _state.get("arm_world_3x3", {}).get(bone_side)
            forearm_world_3x3 = chained_world_3x3(fa, fa.rotation_quaternion, parent_world_3x3=arm_world) if fa else None
            q_hand = orient_yz(pb_hand, fwd, normal, parent_world_3x3=forearm_world_3x3)
            if q_hand is not None:
                moved += 1
                hand_world_3x3 = chained_world_3x3(pb_hand, q_hand, parent_world_3x3=forearm_world_3x3)

    if hand_world_3x3 is None:
        fa = arm.pose.bones.get(f"{prefix}{bone_side}ForeArm")
        arm_world = _state.get("arm_world_3x3", {}).get(bone_side)
        forearm_world_3x3 = chained_world_3x3(fa, fa.rotation_quaternion, parent_world_3x3=arm_world) if fa else None
        q_hand = orient_yz(pb_hand, fwd, normal, parent_world_3x3=forearm_world_3x3)
        if q_hand is None:
            return 0
        moved += 1
        hand_world_3x3 = chained_world_3x3(pb_hand, q_hand, parent_world_3x3=forearm_world_3x3)

    # World 3x3 del Hand con su rotación recién asignada — clave para que los
    # dedos no hereden una matriz stale del depsgraph. Sin esto, el thumb
    # termina apuntando a `R_hand · target` en lugar de `target` (R_hand ≈ 90°
    # cuando la palma rota al frente), lo que se ve como "el pulgar va al revés".
    hand_world_3x3 = chained_world_3x3(pb_hand, q_hand,
                                        parent_world_3x3=forearm_world_3x3)

    for chain in FINGER_CHAINS:
        parent_3x3 = hand_world_3x3
        for suffix in chain:
            pb = arm.pose.bones.get(f"{prefix}{bone_side}{suffix}")
            if pb is None:
                break
            idx = _FINGER_VEC_BY_NAME.get(suffix)
            if idx is None:
                continue
            p_idx, c_idx = idx
            v = pts[c_idx] - pts[p_idx]
            q_assigned = None
            if v.length > 1e-5:
                q_assigned = aim(pb, v.normalized(), parent_world_3x3=parent_3x3)
                if q_assigned is not None:
                    moved += 1
            # Propagar al siguiente hijo de la cadena. Si aim falló, usamos la
            # rotación actual del bone (no la matrix stale) para mantener la
            # consistencia.
            q_for_chain = q_assigned if q_assigned is not None else pb.rotation_quaternion
            parent_3x3 = chained_world_3x3(pb, q_for_chain, parent_world_3x3=parent_3x3)

    return moved


def _hand_landmarks(side_data):
    """Acepta lista cruda de 21 lms o {'lm': [...]} del nuevo formato."""
    if side_data is None:
        return None
    if isinstance(side_data, dict):
        return side_data.get("lm")
    return side_data


def _hand_field(side_data, key):
    """Extrae un campo del dict de una mano (p.ej. 'hd'=handedness). None si
    el payload es la lista cruda vieja o el campo no existe."""
    if isinstance(side_data, dict):
        return side_data.get(key)
    return None


def apply(arm, hands, landmarks=None, prefix: str = "mixamorig:",
          swap: bool = False, flip_normal: bool = False,
          min_vis: float = 0.5) -> int:
    """Aplica orientación de manos + dedos a partir del payload `hands`."""
    _state["forearm_oriented"] = set()
    _state["arm_chain_oriented"] = set()
    if not hands:
        return 0
    l_lms = _hand_landmarks(hands.get("L"))
    r_lms = _hand_landmarks(hands.get("R"))
    # handedness de MediaPipe por lado (viaja con el dato; se usa para el signo
    # palma/dorso). Sigue al dato en el auto-swap de abajo.
    l_hd = _hand_field(hands.get("L"), "hd")
    r_hd = _hand_field(hands.get("R"), "hd")

    # data_side = chiralidad anatómica del lado físico que produjo el dato.
    # Lo trackeamos por entrada para que tanto el auto-swap como el manual
    # actualicen consistentemente; pasarle la data_side equivocada a
    # `_palm_basis` invierte la normal de la palma → muñeca rota 180°.
    l_data_side = "Left"
    r_data_side = "Right"

    # Red de seguridad: el capture_runner ya empareja L/R por proximidad
    # image-space; esto ataja casos raros (mano fuera del frame al momento
    # de la pose, swap por handedness en versiones viejas, etc). Margen del
    # 20% para no oscilar cuando las muñecas se cruzan al aplaudir.
    if landmarks and len(landmarks) >= 17 and l_lms and r_lms:
        try:
            body_l_wrist = canonical_to_armature_space(arm, mp_to_arm(landmarks[15]))
            body_r_wrist = canonical_to_armature_space(arm, mp_to_arm(landmarks[16]))
            hand_l_wrist = canonical_to_armature_space(arm, mp_to_arm(l_lms[0]))
            hand_r_wrist = canonical_to_armature_space(arm, mp_to_arm(r_lms[0]))

            dist_keep = ((body_l_wrist - hand_l_wrist).length
                         + (body_r_wrist - hand_r_wrist).length)
            dist_swap = ((body_l_wrist - hand_r_wrist).length
                         + (body_r_wrist - hand_l_wrist).length)

            did_swap = dist_swap < dist_keep * 0.8
            if did_swap:
                l_lms, r_lms = r_lms, l_lms
                # Tras intercambiar las listas, l_lms contiene datos del lado
                # físico contrario; data_side y handedness deben seguirlas.
                l_data_side, r_data_side = r_data_side, l_data_side
                l_hd, r_hd = r_hd, l_hd
            # Log solo en TRANSICIÓN — antes esto disparaba un log (con
            # flush a disco) por cada frame mientras la condición persistía.
            if did_swap != _state.get("auto_swap_active", False):
                _state["auto_swap_active"] = did_swap
                if did_swap:
                    log.info("hands: auto-swap correctivo activado (capture asignó al revés)")
                else:
                    log.info("hands: auto-swap correctivo desactivado")
        except Exception as e:
            log.warn(f"Error en auto-swap de manos: {e}")

    # Loguear el primer frame con manos para diagnóstico (qué nos llega exactamente)
    if not _state.get("first_hand_logged"):
        _state["first_hand_logged"] = True
        sample = l_lms or r_lms
        if sample and len(sample) >= 21:
            wrist = sample[0]
            mid = sample[9]
            log.info(
                f"primer frame de manos: L={'sí' if l_lms else 'no'} R={'sí' if r_lms else 'no'} "
                f"swap={swap} wrist={wrist} mid_mcp={mid}"
            )

    # `bone_side` = a qué huesos del rig va. swap solo cambia el bone_side;
    # data_side se mantiene atado a la chiralidad física del dato para que
    # `_palm_basis` calcule la normal hacia el dorso correctamente.
    moved = 0
    if l_lms:
        moved += _apply_one_hand(
            arm, data_side=l_data_side,
            bone_side=("Right" if swap else "Left"),
            lms=l_lms, prefix=prefix, flip_normal=flip_normal,
            body_landmarks=landmarks, handedness=l_hd,
            min_vis=min_vis,
        )
    if r_lms:
        moved += _apply_one_hand(
            arm, data_side=r_data_side,
            bone_side=("Left" if swap else "Right"),
            lms=r_lms, prefix=prefix, flip_normal=flip_normal,
            body_landmarks=landmarks, handedness=r_hd,
            min_vis=min_vis,
        )

    _dump_hand_axes_once(arm, prefix)
    return moved
