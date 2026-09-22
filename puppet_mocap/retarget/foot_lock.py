"""Foot-lock para una action ya horneada.

La captura en vivo corrige el Hips sin esperar al depsgraph. Este módulo se
usa fuera del timer, así que puede evaluar la escena frame a frame y escribir
una action nueva sin destruir la original.

Patrón inmutable de 3 pasos:
1. Mide la fuente original completa sin modificarla (posiciones, parches y suelo).
2. Simula apoyos mediante FootStateMachine y resuelve IK/pelvis en memoria.
3. Duplica la action y escribe los keyframes corregidos (Hips y piernas IK).
"""
from __future__ import annotations

import math
import bpy
from mathutils import Vector, Quaternion, Matrix

from .common import aim, chained_world_3x3
from .foot_plant import (
    FootContactPatch,
    GroundModel,
    FootStateMachine,
    StanceSnapshot,
    remap_hips_delta_to_spine,
    LOCKED,
    RELEASING,
    SWING,
    CANDIDATE,
    solve_pelvis_assist,
    aim_bone,
)


FOOT_BONES = (("L", "LeftFoot"), ("R", "RightFoot"))
RELEASE_MULTIPLIER = 2.5


class FootLockStats(dict):
    """Estadísticas detalladas del proceso de bloqueo de pies."""

    def __init__(self, total_locked_frames: int = 0, **kwargs):
        super().__init__(total_locked_frames=total_locked_frames, **kwargs)
        self.total_locked_frames = int(total_locked_frames)

    def __int__(self):
        return self.total_locked_frames

    def __index__(self):
        return self.total_locked_frames

    def __gt__(self, other):
        return self.total_locked_frames > int(other)

    def __ge__(self, other):
        return self.total_locked_frames >= int(other)

    def __lt__(self, other):
        return self.total_locked_frames < int(other)

    def __le__(self, other):
        return self.total_locked_frames <= int(other)

    def __eq__(self, other):
        if isinstance(other, (int, float)):
            return self.total_locked_frames == other
        return super().__eq__(other)

    def __str__(self):
        return str(self.total_locked_frames)


def _solve_two_bone_ik(arm, pb_u, pb_l, pb_f, res_w: Vector, hips_world):
    """Calcula las rotaciones de UpLeg y Leg para desplazar el pie en res_w (mundo)."""
    arm_3x3_inv = arm.matrix_world.to_3x3().inverted_safe()
    delta_arm = arm_3x3_inv @ res_w
    target_arm = pb_f.head.copy() + delta_arm

    H = pb_u.head.copy()
    K = pb_l.head.copy()
    A = pb_f.head.copy()

    L1 = (K - H).length
    L2 = (A - K).length
    v_T = target_arm - H
    d = max(0.001, min(L1 + L2 - 1e-4, v_T.length))
    dir_T = v_T.normalized()

    n = (K - H).cross(A - K)
    if n.length < 1e-4:
        n = Vector((1.0, 0.0, 0.0))
    else:
        n.normalize()

    cos_alpha = max(-1.0, min(1.0, (L1 * L1 + d * d - L2 * L2) / (2.0 * L1 * d)))
    alpha = math.acos(cos_alpha)

    R_alpha = Quaternion(n, alpha)
    v1_cand1 = R_alpha @ dir_T
    v1_cand2 = R_alpha.inverted() @ dir_T
    v1_orig = (K - H).normalized()
    v1_new = v1_cand1 if (v1_cand1 - v1_orig).length < (v1_cand2 - v1_orig).length else v1_cand2

    K_new = H + v1_new * L1
    v2_new = (target_arm - K_new).normalized()

    q_u = aim_bone(pb_u, v1_new, parent_world_3x3=hips_world)
    if q_u is None:
        return None, None, None
    u_world = chained_world_3x3(pb_u, q_u, parent_world_3x3=hips_world)

    q_l = aim_bone(pb_l, v2_new, parent_world_3x3=u_world)
    if q_l is None:
        return None, None, None
    l_world = chained_world_3x3(pb_l, q_l, parent_world_3x3=u_world)

    f_orig_dir = (pb_f.tail - pb_f.head).normalized()
    q_f = aim_bone(pb_f, f_orig_dir, parent_world_3x3=l_world)

    return q_u, q_l, q_f


def _foot_point_world(arm, pb) -> Vector:
    """Punto de contacto aproximado en coordenadas de mundo."""
    local_mid = (pb.head + pb.tail) * 0.5
    if arm is not None:
        try:
            return arm.matrix_world @ local_mid
        except (AttributeError, TypeError, ValueError):
            pass
    return local_mid


def _shift_root_from_world_delta(arm, hips, world_delta: Vector,
                                 base_location: Vector | None = None) -> Vector:
    """Calcula el nuevo location del hueso Hips aplicando un desplazamiento en mundo."""
    if hips is None:
        return Vector()
    base = Vector(base_location) if base_location is not None else Vector(hips.location)
    if world_delta.length < 1e-8:
        return base
    # Mundo → local del armature
    arm_3x3 = arm.matrix_world.to_3x3()
    try:
        local_delta = arm_3x3.inverted_safe() @ world_delta
    except (AttributeError, TypeError, ValueError):
        local_delta = world_delta
    # Local del armature → espacio rest del hueso Hips
    rest3 = hips.bone.matrix_local.to_3x3()
    bone_delta = rest3.inverted_safe() @ local_delta
    return base + bone_delta


def _percentile(values, pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(float(v) for v in values)
    i = int(round((len(ordered) - 1) * pct / 100.0))
    return ordered[max(0, min(i, len(ordered) - 1))]


def _speed_threshold(arm, prefix: str, velocities: list[float]) -> float:
    """Umbral automático en unidades de mundo."""
    leg = [arm.data.bones.get(f"{prefix}{name}")
           for name in ("LeftUpLeg", "LeftLeg")]
    leg_len = sum((b.length for b in leg if b is not None), 0.0)
    scale_y = arm.scale.y if hasattr(arm, "scale") else 1.0
    leg_len *= scale_y
    rig_floor = max(0.02, leg_len * 0.003)
    return max(rig_floor, _percentile(velocities, 30.0))


def lock_action(arm, action, prefix: str = "mixamorig:",
                ground_mode: str = "PLANE_Z",
                ground_z: float | None = None,
                ground_object=None,
                sole_offset: float = 0.0,
                sensitivity: float = 1.0) -> tuple[bpy.types.Action | None, FootLockStats]:
    """Duplica `action`, corrige apoyos y devuelve la nueva action y estadísticas.

    Devuelve `(new_action, stats)` o `(None, FootLockStats(0))` si no hay Hips o
    pies reconocibles. La action original permanece estrictamente intacta.
    """
    if arm is None or action is None:
        return None, FootLockStats(0)

    hips = arm.pose.bones.get(f"{prefix}Hips")
    feet = {
        side: arm.pose.bones.get(f"{prefix}{suffix}")
        for side, suffix in FOOT_BONES
    }
    feet = {side: pb for side, pb in feet.items() if pb is not None}
    if hips is None or not feet:
        return None, FootLockStats(0)

    legs = {}
    for side in ("L", "R"):
        side_name = "Left" if side == "L" else "Right"
        pb_u = arm.pose.bones.get(f"{prefix}{side_name}UpLeg")
        pb_l = arm.pose.bones.get(f"{prefix}{side_name}Leg")
        pb_f = feet.get(side)
        pb_toe = arm.pose.bones.get(f"{prefix}{side_name}ToeBase")
        if pb_u and pb_l and pb_f:
            legs[side] = (pb_u, pb_l, pb_f, pb_toe)

    spines = [arm.pose.bones.get(f"{prefix}{suffix}") for suffix in ("Spine", "Spine1", "Spine2")]
    spines = [pb for pb in spines if pb is not None]

    start = int(round(action.frame_range[0]))
    end = int(round(action.frame_range[1]))
    if end < start:
        end = start
    frames = list(range(start, end + 1))

    scene = bpy.context.scene
    depsgraph = bpy.context.evaluated_depsgraph_get()
    previous_frame = scene.frame_current

    ad = arm.animation_data
    if ad is None:
        ad = arm.animation_data_create()
    original_action = ad.action

    fps = scene.render.fps / scene.render.fps_base if scene.render.fps_base > 0 else 30.0
    dt = 1.0 / max(fps, 1.0)

    new_action = None
    try:
        ad.action = action

        # ---------------------------------------------------------------------
        # Paso 1: Medir la toma original sin modificarla
        # ---------------------------------------------------------------------
        hips_original_locs = {}
        hips_original_rots = {}
        spines_original_rots = {i: {} for i in range(len(spines))}
        spines_original_locs = {i: {} for i in range(len(spines))}
        legs_original_rots = {side: {} for side in legs}
        points = {side: {} for side in feet}
        patches = {side: {} for side in feet}
        auto_floor_candidates = []

        for frame in frames:
            scene.frame_set(frame)
            bpy.context.view_layer.update()
            hips_original_locs[frame] = Vector(hips.location).copy()
            hips_original_rots[frame] = Quaternion(hips.rotation_quaternion).copy() if hasattr(hips, "rotation_quaternion") else Quaternion()
            for i, sp in enumerate(spines):
                spines_original_rots[i][frame] = Quaternion(sp.rotation_quaternion).copy() if hasattr(sp, "rotation_quaternion") else Quaternion()
                spines_original_locs[i][frame] = Vector(sp.location).copy() if hasattr(sp, "location") else Vector()

            for side in legs:
                pb_u, pb_l, pb_f, pb_toe = legs[side]
                legs_original_rots[side][frame] = (
                    Quaternion(pb_u.rotation_quaternion).copy(),
                    Quaternion(pb_l.rotation_quaternion).copy(),
                    Quaternion(pb_f.rotation_quaternion).copy(),
                    Quaternion(pb_toe.rotation_quaternion).copy() if pb_toe else None,
                )
                pt = _foot_point_world(arm, pb_f).copy()
                points[side][frame] = pt
                patch = FootContactPatch.from_bones(arm, pb_f, pb_toe, sole_offset=sole_offset)
                patches[side][frame] = patch
                auto_floor_candidates.append(pt.z)

        # Determinar modelo de suelo
        if ground_mode == "AUTO_CALIBRATED" or ground_z is None:
            floor_z = _percentile(auto_floor_candidates, 5.0) if auto_floor_candidates else 0.0
        else:
            floor_z = float(ground_z)

        ground = GroundModel(mode=ground_mode, ground_z=floor_z,
                             ground_object=ground_object, fallback_z=floor_z)

        # ---------------------------------------------------------------------
        # Paso 2: Resolver apoyos con Hard Plant y desacoplamiento de torso
        # ---------------------------------------------------------------------
        candidate_dur = min(0.06, dt * 1.5)
        sms = {side: FootStateMachine(candidate_duration=candidate_dur) for side in feet}
        snapshots = {}

        target_hips_locs = {}
        target_hips_rots = {}
        target_spines_rots = {i: {} for i in range(len(spines))}
        target_spines_locs = {i: {} for i in range(len(spines))}
        target_legs_rots = {side: {} for side in legs}

        locked_frames = 0
        locked_frames_left = 0
        locked_frames_right = 0
        bilateral_count = 0
        residuals = []

        for frame in frames:
            t_now = frame * dt
            for side in feet:
                sms[side].update(
                    patches[side][frame], ground, t_now,
                    visibility=1.0, sensitivity=sensitivity,
                    side=side, override_mode="AUTO"
                )

            locked_sides = [side for side in legs if sms[side].state == LOCKED]

            if locked_sides:
                locked_frames += 1
                if "L" in locked_sides:
                    locked_frames_left += 1
                if "R" in locked_sides:
                    locked_frames_right += 1
                if len(locked_sides) == 2:
                    bilateral_count += 1

                for side in locked_sides:
                    if side not in snapshots or snapshots[side] is None:
                        patch = patches[side][frame]
                        snap = StanceSnapshot(
                            hips_world=None,
                            up_leg_local=legs_original_rots[side][frame][0],
                            leg_local=legs_original_rots[side][frame][1],
                            foot_local=legs_original_rots[side][frame][2],
                            toe_local=legs_original_rots[side][frame][3],
                            contact_world=patch.mid,
                            heel_world=patch.heel,
                            toe_world=patch.toe,
                            normal_world=patch.normal,
                            hips_loc=hips_original_locs[frame],
                            hips_rot=hips_original_rots[frame],
                        )
                        snapshots[side] = snap

                ref_snap = snapshots[locked_sides[0]]
                delta_rot = hips_original_rots[frame] @ ref_snap.hips_rot.inverted()
                delta_loc = hips_original_locs[frame] - ref_snap.hips_loc

                # 1. Hips estabilizado al snapshot duro
                target_hips_locs[frame] = ref_snap.hips_loc.copy()
                target_hips_rots[frame] = ref_snap.hips_rot.copy()

                # 2. Piernas en LOCKED congeladas a su snapshot
                for side in locked_sides:
                    snap = snapshots[side]
                    target_legs_rots[side][frame] = (
                        snap.up_leg_local.copy(),
                        snap.leg_local.copy(),
                        snap.foot_local.copy(),
                        snap.toe_local.copy() if snap.toe_local else None,
                    )
                    residuals.append(0.0)

                # Piernas en SWING conservan FK original
                for side in legs:
                    if side not in locked_sides:
                        snapshots.pop(side, None)
                        target_legs_rots[side][frame] = legs_original_rots[side][frame]

                # 3. Torso absorbe delta_hips
                if spines:
                    q_id = Quaternion((1.0, 0.0, 0.0, 0.0))
                    weights = (0.33, 0.33, 0.34)
                    q_norm = delta_rot.copy()
                    if abs(q_norm.magnitude - 1.0) > 1e-4 and q_norm.magnitude > 1e-4:
                        q_norm.normalize()
                    for i, sp in enumerate(spines):
                        w = weights[i] if i < len(weights) else 0.33
                        q_part = q_id.slerp(q_norm, w)
                        orig_q = spines_original_rots[i][frame]
                        target_spines_rots[i][frame] = (q_part @ orig_q).normalized()

                    pb_base = spines[0]
                    arm_3x3 = arm.matrix_world.to_3x3()
                    try:
                        rest3 = pb_base.bone.matrix_local.to_3x3()
                        bone_delta = rest3.inverted_safe() @ (arm_3x3.inverted_safe() @ delta_loc)
                    except Exception:
                        bone_delta = delta_loc.copy()
                    if bone_delta.length > 0.06:
                        bone_delta = bone_delta.normalized() * 0.06
                    target_spines_locs[0][frame] = spines_original_locs[0][frame] + bone_delta
            else:
                for side in legs:
                    snapshots.pop(side, None)
                    target_legs_rots[side][frame] = legs_original_rots[side][frame]
                target_hips_locs[frame] = hips_original_locs[frame]
                target_hips_rots[frame] = hips_original_rots[frame]
                for i in range(len(spines)):
                    target_spines_rots[i][frame] = spines_original_rots[i][frame]
                    target_spines_locs[i][frame] = spines_original_locs[i][frame]

        # ---------------------------------------------------------------------
        # Paso 3: Duplicar action y escribir keyframes
        # ---------------------------------------------------------------------
        new_action = action.copy()
        new_action.name = f"{action.name}_FootLocked"
        new_action.use_fake_user = True
        ad.action = new_action

        hips.rotation_mode = "QUATERNION"
        for side in legs:
            pb_u, pb_l, pb_f, pb_toe = legs[side]
            pb_u.rotation_mode = "QUATERNION"
            pb_l.rotation_mode = "QUATERNION"
            pb_f.rotation_mode = "QUATERNION"
            if pb_toe:
                pb_toe.rotation_mode = "QUATERNION"
        for sp in spines:
            sp.rotation_mode = "QUATERNION"

        for frame in frames:
            hips.location = target_hips_locs[frame]
            hips.rotation_quaternion = target_hips_rots[frame]
            hips.keyframe_insert(data_path="location", frame=frame)
            hips.keyframe_insert(data_path="rotation_quaternion", frame=frame)

            for side in legs:
                pb_u, pb_l, pb_f, pb_toe = legs[side]
                q_u, q_l, q_f, q_t = target_legs_rots[side][frame]
                pb_u.rotation_quaternion = q_u
                pb_l.rotation_quaternion = q_l
                pb_f.rotation_quaternion = q_f
                pb_u.keyframe_insert(data_path="rotation_quaternion", frame=frame)
                pb_l.keyframe_insert(data_path="rotation_quaternion", frame=frame)
                pb_f.keyframe_insert(data_path="rotation_quaternion", frame=frame)
                if pb_toe and q_t is not None:
                    pb_toe.rotation_quaternion = q_t
                    pb_toe.keyframe_insert(data_path="rotation_quaternion", frame=frame)

            for i, sp in enumerate(spines):
                if i in target_spines_rots and frame in target_spines_rots[i]:
                    sp.rotation_quaternion = target_spines_rots[i][frame]
                    sp.keyframe_insert(data_path="rotation_quaternion", frame=frame)
                if i == 0 and 0 in target_spines_locs and frame in target_spines_locs[0]:
                    sp.location = target_spines_locs[0][frame]
                    sp.keyframe_insert(data_path="location", frame=frame)

        rms_res = math.sqrt(sum(r * r for r in residuals) / len(residuals)) if residuals else 0.0
        max_res = max(residuals) if residuals else 0.0

        stats = FootLockStats(
            total_locked_frames=locked_frames,
            locked_frames_left=locked_frames_left,
            locked_frames_right=locked_frames_right,
            bilateral_frames=bilateral_count,
            rms_residual=rms_res,
            max_residual=max_res,
            ground_mode=ground_mode,
            ground_z=floor_z,
        )

        return new_action, stats
    except Exception:
        if original_action is not None and ad is not None:
            ad.action = original_action
        raise
    finally:
        scene.frame_set(previous_frame)
        bpy.context.view_layer.update()
