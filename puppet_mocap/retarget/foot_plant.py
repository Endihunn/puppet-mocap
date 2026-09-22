"""Núcleo compartido de contacto y cinemática inversa de piernas (Foot Plant & Leg IK).

Proporciona:
1. `FootContactPatch`: representación de parche de contacto talón-punta y normal de suela.
2. `GroundModel`: abstracción de superficie de suelo (plano Z, raycast con escena o calibrado).
3. `FootStateMachine`: máquina de estados temporal multivariable e independiente de FPS:
   SWING → CANDIDATE → LOCKED → RELEASING → SWING.
4. `solve_leg_ik`: solucionador analítico puro de 2 huesos con estabilidad de polo y orientación de suela.
5. `solve_pelvis_assist`: cálculo de compensación acotada de cadera para apoyo uni/bilateral.

Desacoplado de filtros OneEuro y variables globales de retargeting en vivo.
"""
from __future__ import annotations

import math
from mathutils import Matrix, Quaternion, Vector


# Estados posibles de la máquina de contacto
SWING = "SWING"
CANDIDATE = "CANDIDATE"
LOCKED = "LOCKED"
RELEASING = "RELEASING"


class FootContactPatch:
    """Representación del parche de contacto del pie en coordenadas de mundo."""

    def __init__(self, heel: Vector, toe: Vector, mid: Vector,
                 normal: Vector, length: float, ankle: Vector):
        self.heel = heel.copy()
        self.toe = toe.copy()
        self.mid = mid.copy()
        self.normal = normal.copy()
        self.length = float(length)
        self.ankle = ankle.copy()

    @classmethod
    def from_bones(cls, arm, pb_foot, pb_toe=None, sole_offset: float = 0.0) -> FootContactPatch:
        """Deriva el parche de contacto a partir de los huesos del rig y matriz de mundo."""
        M = getattr(arm, "matrix_world", Matrix.Identity(4)) if arm is not None else Matrix.Identity(4)
        M_rot = M.to_3x3()

        head = getattr(pb_foot, "head", Vector((0.0, 0.0, 0.0)))
        ankle_world = M @ head

        # Dirección fwd y longitud
        if pb_toe is not None and hasattr(pb_toe, "head") and hasattr(pb_toe, "tail"):
            foot_vec = pb_toe.head - head
            fwd_local = foot_vec.normalized() if foot_vec.length > 1e-4 else Vector((0.0, 1.0, 0.0))
            length = (pb_toe.tail - head).length
            toe_local = pb_toe.tail.copy()
            heel_local = head - fwd_local * (length * 0.25)
        elif hasattr(pb_foot, "tail"):
            foot_vec = pb_foot.tail - head
            fwd_local = foot_vec.normalized() if foot_vec.length > 1e-4 else Vector((0.0, 1.0, 0.0))
            length = pb_foot.length if hasattr(pb_foot, "length") else foot_vec.length
            toe_local = pb_foot.tail.copy()
            heel_local = head - fwd_local * (length * 0.25)
        else:
            fwd_local = Vector((0.0, 1.0, 0.0))
            length = 0.15
            toe_local = head + fwd_local * length
            heel_local = head - fwd_local * (length * 0.25)

        # Normal de la suela en espacio del bone / rig
        if hasattr(pb_foot, "matrix"):
            foot_3x3 = pb_foot.matrix.to_3x3()
            up_world = (M_rot @ foot_3x3.col[2]).normalized()
        else:
            up_world = Vector((0.0, 0.0, 1.0))
        down_world = -up_world

        # Puntos de suela con sole_offset
        heel_world = (M @ heel_local) + down_world * sole_offset
        toe_world = (M @ toe_local) + down_world * sole_offset
        mid_world = (heel_world + toe_world) * 0.5
        sole_normal = up_world

        return cls(heel=heel_world, toe=toe_world, mid=mid_world,
                   normal=sole_normal, length=max(length, 0.05), ankle=ankle_world)


class GroundModel:
    """Modelo abstracto de consulta de suelo y orientación de superficie."""

    def __init__(self, mode: str = "PLANE_Z", ground_z: float = 0.0,
                 ground_object=None, fallback_z: float = 0.0):
        self.mode = mode
        self.ground_z = float(ground_z)
        self.ground_object = ground_object
        self.fallback_z = float(fallback_z)

    def query(self, x: float, y: float, depsgraph=None, scene=None) -> tuple[float, Vector]:
        """Devuelve (z_altura, normal_mundo) para el punto (x, y)."""
        if self.mode == "PLANE_Z" or (depsgraph is None and scene is None and self.ground_object is None):
            return self.ground_z, Vector((0.0, 0.0, 1.0))

        if self.mode == "OBJECT_RAYCAST":
            if self.ground_object is not None and hasattr(self.ground_object, "ray_cast"):
                # Raycast local directo sobre el objeto de suelo especificado
                inv_mat = self.ground_object.matrix_world.inverted_safe()
                origin_world = Vector((x, y, self.fallback_z + 2.0))
                dir_world = Vector((0.0, 0.0, -1.0))
                origin_local = inv_mat @ origin_world
                dir_local = (inv_mat.to_3x3() @ dir_world).normalized()
                res = self.ground_object.ray_cast(origin_local, dir_local, distance=5.0)
                if res[0]:
                    loc_world = self.ground_object.matrix_world @ res[1]
                    norm_world = (self.ground_object.matrix_world.to_3x3() @ res[2]).normalized()
                    return loc_world.z, norm_world
            elif scene is not None and depsgraph is not None:
                origin = Vector((x, y, self.fallback_z + 2.0))
                direction = Vector((0.0, 0.0, -1.0))
                hit, loc, normal, idx, obj, matrix = scene.ray_cast(
                    depsgraph, origin, direction, distance=5.0
                )
                if hit:
                    return loc.z, normal.normalized()

            return self.ground_z, Vector((0.0, 0.0, 1.0))

        # AUTO_CALIBRATED o default
        return self.ground_z, Vector((0.0, 0.0, 1.0))


class FootStateMachine:
    """Máquina de estados independiente de FPS para detección y bloqueo de apoyo."""

    def __init__(self, candidate_duration: float = 0.10,
                 grace_duration: float = 0.12,
                 blend_duration: float = 0.10,
                 release_multiplier: float = 2.5):
        self.candidate_duration = candidate_duration
        self.grace_duration = grace_duration
        self.blend_duration = blend_duration
        self.release_multiplier = release_multiplier

        self.state = SWING
        self.anchor_heel: Vector | None = None
        self.anchor_toe: Vector | None = None
        self.anchor_mid: Vector | None = None
        self.anchor_ankle: Vector | None = None
        self.anchor_normal: Vector = Vector((0.0, 0.0, 1.0))
        self.snapshot: StanceSnapshot | None = None

        self.candidate_timer = 0.0
        self.loss_timer = 0.0
        self.release_timer = 0.0
        self.blend_weight = 0.0

        self.prev_mid: Vector | None = None
        self.prev_time: float | None = None
        self.confidence = 1.0

    def force_lock(self):
        """Fuerza la entrada inmediata en estado LOCKED."""
        self.state = LOCKED
        self.blend_weight = 1.0
        self.candidate_timer = self.candidate_duration
        self.loss_timer = 0.0

    def force_release(self):
        """Fuerza la liberación inmediata a SWING."""
        self.state = SWING
        self.blend_weight = 0.0
        self.snapshot = None
        self.anchor_mid = None
        self.anchor_ankle = None
        self.candidate_timer = 0.0
        self.loss_timer = 0.0

    def reset(self):
        self.state = SWING
        self.anchor_heel = None
        self.anchor_toe = None
        self.anchor_mid = None
        self.anchor_ankle = None
        self.snapshot = None
        self.candidate_timer = 0.0
        self.loss_timer = 0.0
        self.release_timer = 0.0
        self.blend_weight = 0.0
        self.prev_mid = None
        self.prev_time = None
        self.confidence = 1.0

    def update(self, patch: FootContactPatch, ground: GroundModel,
               t_now: float, visibility: float = 1.0,
               sensitivity: float = 1.0, leg_scale: float = 1.0,
               depsgraph=None, scene=None,
               side: str = "L", override_mode: str = "AUTO") -> str:
        """Actualiza el estado con el parche actual y tiempo monotónico."""
        # Consulta de suelo
        ground_z, ground_normal = ground.query(patch.mid.x, patch.mid.y, depsgraph, scene)

        # Sobrescrituras manuales explícitas (AUTO / BOTH / LEFT / RIGHT)
        if override_mode == "BOTH" or (override_mode == "LEFT" and side == "L") or (override_mode == "RIGHT" and side == "R"):
            self.state = LOCKED
            self.blend_weight = 1.0
            if self.anchor_mid is None:
                anc_mid = patch.mid.copy()
                anc_mid.z = ground_z
                self.anchor_mid = anc_mid
                self.anchor_ankle = patch.ankle.copy()
                self.anchor_ankle.z += (ground_z - patch.mid.z)
                self.anchor_normal = ground_normal.copy()
            return self.state

        if (override_mode == "LEFT" and side == "R") or (override_mode == "RIGHT" and side == "L"):
            self.state = SWING
            self.blend_weight = 0.0
            self.snapshot = None
            self.anchor_mid = None
            self.anchor_ankle = None
            return self.state

        self.confidence = float(visibility)
        dt = (t_now - self.prev_time) if self.prev_time is not None else 0.0333
        if dt <= 1e-4:
            dt = 0.0333
        self.prev_time = t_now

        # Velocidades espaciales divididas por dt real
        if self.prev_mid is not None:
            vel = (patch.mid - self.prev_mid) / dt
            horiz_speed = Vector((vel.x, vel.y)).length
            vert_speed = vel.z
        else:
            horiz_speed = 0.0
            vert_speed = 0.0
        self.prev_mid = patch.mid.copy()

        # Distancia y alineación con el suelo
        sole_dist = patch.mid.z - ground_z
        alignment = patch.normal.dot(ground_normal)

        # Umbrales escalados
        base_h_speed = 0.18 * max(sensitivity, 0.2)
        base_v_speed = 0.18 * max(sensitivity, 0.2)
        base_height = 0.045 * max(leg_scale, 0.5)
        min_align = 0.65

        is_candidate = (
            self.confidence >= 0.5
            and horiz_speed <= base_h_speed
            and abs(vert_speed) <= base_v_speed
            and abs(sole_dist) <= base_height
            and alignment >= min_align
        )

        # Transiciones de estado
        if self.state == SWING:
            self.blend_weight = 0.0
            if is_candidate:
                self.state = CANDIDATE
                self.candidate_timer = dt
            else:
                self.candidate_timer = 0.0

        elif self.state == CANDIDATE:
            self.blend_weight = 0.0
            if is_candidate:
                self.candidate_timer += dt
                if self.candidate_timer >= self.candidate_duration:
                    # Entrar en bloqueo (LOCKED) y registrar anclas
                    self.state = LOCKED
                    anc_mid = patch.mid.copy()
                    anc_mid.z = ground_z
                    self.anchor_mid = anc_mid
                    self.anchor_ankle = patch.ankle.copy()
                    self.anchor_ankle.z += (ground_z - patch.mid.z)
                    self.anchor_normal = ground_normal.copy()
                    self.blend_weight = 1.0
                    self.loss_timer = 0.0
            else:
                # No consolidó el apoyo: volver a swing
                self.state = SWING
                self.candidate_timer = 0.0

        elif self.state == LOCKED:
            # Detección de intención clara de levantar (despegue)
            rel_h_limit = base_h_speed * self.release_multiplier
            rel_v_limit = base_v_speed * self.release_multiplier
            rel_height = base_height * 1.8

            lift_intent = (sole_dist > base_height * 0.8 and vert_speed > base_v_speed * 0.7)

            if self.confidence < 0.5:
                # Pérdida de tracking: mantener periodo de gracia antes de liberar
                self.loss_timer += dt
                if self.loss_timer > self.grace_duration:
                    self.state = RELEASING
                    self.release_timer = 0.0
                else:
                    self.blend_weight = 1.0
            elif lift_intent or horiz_speed > rel_h_limit or sole_dist > rel_height:
                # Intención de despegue voluntario o movimiento rápido: liberar
                self.state = RELEASING
                self.release_timer = 0.0
                self.loss_timer = 0.0
            else:
                self.loss_timer = 0.0
                self.blend_weight = 1.0

        elif self.state == RELEASING:
            self.release_timer += dt
            self.blend_weight = max(0.0, 1.0 - (self.release_timer / max(self.blend_duration, 1e-4)))
            if self.blend_weight <= 0.0:
                self.state = SWING
                self.anchor_mid = None
                self.anchor_ankle = None
                self.snapshot = None
                self.candidate_timer = 0.0

        return self.state


class StanceSnapshot:
    """Snapshot inmutable de la cadena de pierna y cadera al momento de plantar."""

    def __init__(self, hips_world: Matrix | None = None,
                 up_leg_local: Quaternion | None = None,
                 leg_local: Quaternion | None = None,
                 foot_local: Quaternion | None = None,
                 toe_local: Quaternion | None = None,
                 contact_world: Vector | None = None,
                 ground_object=None,
                 heel_world: Vector | None = None,
                 toe_world: Vector | None = None,
                 normal_world: Vector | None = None,
                 hips_loc: Vector | None = None,
                 hips_rot: Quaternion | None = None):
        self.hips_world = hips_world.copy() if hips_world is not None else Matrix.Identity(4)
        self.up_leg_local = up_leg_local.copy() if up_leg_local is not None else Quaternion((1.0, 0.0, 0.0, 0.0))
        self.leg_local = leg_local.copy() if leg_local is not None else Quaternion((1.0, 0.0, 0.0, 0.0))
        self.foot_local = foot_local.copy() if foot_local is not None else Quaternion((1.0, 0.0, 0.0, 0.0))
        self.toe_local = toe_local.copy() if toe_local is not None else None
        self.contact_world = contact_world.copy() if contact_world is not None else Vector((0.0, 0.0, 0.0))
        self.ground_object = ground_object
        self.heel_world = heel_world.copy() if heel_world is not None else None
        self.toe_world = toe_world.copy() if toe_world is not None else None
        self.normal_world = normal_world.copy() if normal_world is not None else Vector((0.0, 0.0, 1.0))
        self.hips_loc = hips_loc.copy() if hips_loc is not None else Vector((0.0, 0.0, 0.0))
        self.hips_rot = hips_rot.copy() if hips_rot is not None else Quaternion((1.0, 0.0, 0.0, 0.0))

    @classmethod
    def capture(cls, arm, hips_pb, upleg_pb, leg_pb, foot_pb, toe_pb=None,
                patch: FootContactPatch | None = None, ground_object=None) -> StanceSnapshot:
        """Captura un snapshot instantáneo de Hips y la cadena de pierna."""
        M = arm.matrix_world if arm is not None and hasattr(arm, "matrix_world") else Matrix.Identity(4)
        hips_w = (M @ hips_pb.matrix) if hips_pb is not None and hasattr(hips_pb, "matrix") else Matrix.Identity(4)

        q_u = Quaternion(upleg_pb.rotation_quaternion) if upleg_pb is not None and hasattr(upleg_pb, "rotation_quaternion") else Quaternion((1.0, 0.0, 0.0, 0.0))
        q_l = Quaternion(leg_pb.rotation_quaternion) if leg_pb is not None and hasattr(leg_pb, "rotation_quaternion") else Quaternion((1.0, 0.0, 0.0, 0.0))
        q_f = Quaternion(foot_pb.rotation_quaternion) if foot_pb is not None and hasattr(foot_pb, "rotation_quaternion") else Quaternion((1.0, 0.0, 0.0, 0.0))
        q_t = Quaternion(toe_pb.rotation_quaternion) if (toe_pb is not None and hasattr(toe_pb, "rotation_quaternion")) else None

        loc_h = Vector(hips_pb.location).copy() if hips_pb is not None and hasattr(hips_pb, "location") else Vector((0.0, 0.0, 0.0))
        rot_h = Quaternion(hips_pb.rotation_quaternion).copy() if hips_pb is not None and hasattr(hips_pb, "rotation_quaternion") else Quaternion((1.0, 0.0, 0.0, 0.0))

        if patch is not None:
            contact_w = patch.mid.copy()
            heel_w = patch.heel.copy()
            toe_w = patch.toe.copy()
            normal_w = patch.normal.copy()
        elif foot_pb is not None and hasattr(foot_pb, "head"):
            contact_w = M @ foot_pb.head
            heel_w = contact_w.copy()
            toe_w = (M @ foot_pb.tail) if hasattr(foot_pb, "tail") else contact_w.copy()
            normal_w = Vector((0.0, 0.0, 1.0))
        else:
            contact_w = Vector((0.0, 0.0, 0.0))
            heel_w = None
            toe_w = None
            normal_w = Vector((0.0, 0.0, 1.0))

        return cls(
            hips_world=hips_w,
            up_leg_local=q_u,
            leg_local=q_l,
            foot_local=q_f,
            toe_local=q_t,
            contact_world=contact_w,
            ground_object=ground_object,
            heel_world=heel_w,
            toe_world=toe_w,
            normal_world=normal_w,
            hips_loc=loc_h,
            hips_rot=rot_h,
        )

    def restore(self, hips_pb=None, upleg_pb=None, leg_pb=None, foot_pb=None,
                toe_pb=None, restore_hips: bool = True):
        """Restaura la pose de reposo clavada en los pose bones."""
        if restore_hips and hips_pb is not None:
            if hasattr(hips_pb, "location") and self.hips_loc is not None:
                hips_pb.location = self.hips_loc.copy()
            if hasattr(hips_pb, "rotation_quaternion") and self.hips_rot is not None:
                if hasattr(hips_pb, "rotation_mode"):
                    hips_pb.rotation_mode = "QUATERNION"
                hips_pb.rotation_quaternion = self.hips_rot.copy()

        if upleg_pb is not None and hasattr(upleg_pb, "rotation_quaternion"):
            if hasattr(upleg_pb, "rotation_mode"):
                upleg_pb.rotation_mode = "QUATERNION"
            upleg_pb.rotation_quaternion = self.up_leg_local.copy()

        if leg_pb is not None and hasattr(leg_pb, "rotation_quaternion"):
            if hasattr(leg_pb, "rotation_mode"):
                leg_pb.rotation_mode = "QUATERNION"
            leg_pb.rotation_quaternion = self.leg_local.copy()

        if foot_pb is not None and hasattr(foot_pb, "rotation_quaternion"):
            if hasattr(foot_pb, "rotation_mode"):
                foot_pb.rotation_mode = "QUATERNION"
            foot_pb.rotation_quaternion = self.foot_local.copy()

        if toe_pb is not None and self.toe_local is not None and hasattr(toe_pb, "rotation_quaternion"):
            if hasattr(toe_pb, "rotation_mode"):
                toe_pb.rotation_mode = "QUATERNION"
            toe_pb.rotation_quaternion = self.toe_local.copy()


def remap_hips_delta_to_spine(arm, prefix: str, delta_rot: Quaternion | None,
                             delta_loc: Vector | None,
                             max_trans: float = 0.06,
                             weights: tuple[float, float, float] = (0.33, 0.33, 0.34)) -> int:
    """Distribuye la rotación y traslación de cadera no aplicadas hacia Spine, Spine1 y Spine2.

    Evita que Hips traslade/rote las piernas de apoyo clavadas, permitiendo que
    el torso exprese la inclinación, balanceo y giro capturados por el mocap.
    """
    if arm is None or not hasattr(arm, "pose"):
        return 0

    bones = arm.pose.bones
    spine_names = [f"{prefix}{suffix}" for suffix in ("Spine", "Spine1", "Spine2")]
    spine_pbs = [bones.get(name) for name in spine_names]
    active_pbs = [pb for pb in spine_pbs if pb is not None]
    if not active_pbs:
        return 0

    moved = 0
    # 1. Distribuir delta de rotación
    if delta_rot is not None:
        q_norm = delta_rot.copy()
        if abs(q_norm.magnitude - 1.0) > 1e-4 and q_norm.magnitude > 1e-4:
            q_norm.normalize()
        # Si hay rotación apreciable (> 0.01 grados)
        if abs(q_norm.w) < 0.999999:
            q_id = Quaternion((1.0, 0.0, 0.0, 0.0))
            for pb, w in zip(spine_pbs, weights):
                if pb is None or not hasattr(pb, "rotation_quaternion"):
                    continue
                q_part = q_id.slerp(q_norm, w)
                if hasattr(pb, "rotation_mode"):
                    pb.rotation_mode = "QUATERNION"
                pb.rotation_quaternion = (q_part @ pb.rotation_quaternion).normalized()
                moved += 1

    # 2. Distribuir delta de traslación hacia el primer hueso de la columna
    if delta_loc is not None and delta_loc.length > 1e-5:
        pb_base = spine_pbs[0] or active_pbs[0]
        if hasattr(pb_base, "location"):
            arm_3x3 = arm.matrix_world.to_3x3() if hasattr(arm, "matrix_world") else Matrix.Identity(3)
            try:
                if hasattr(pb_base, "bone") and hasattr(pb_base.bone, "matrix_local"):
                    rest3 = pb_base.bone.matrix_local.to_3x3()
                    bone_local_delta = rest3.inverted_safe() @ (arm_3x3.inverted_safe() @ delta_loc)
                else:
                    bone_local_delta = delta_loc.copy()
            except Exception:
                bone_local_delta = delta_loc.copy()

            if bone_local_delta.length > max_trans:
                bone_local_delta = bone_local_delta.normalized() * max_trans

            pb_base.location = pb_base.location + bone_local_delta
            moved += 1

    return moved


class LegSolution:
    """Resultado del solucionador IK para una pierna."""

    def __init__(self, q_upleg: Quaternion, q_leg: Quaternion, q_foot: Quaternion,
                 achieved_ankle: Vector, residual: float, reachable: bool):
        self.q_upleg = q_upleg
        self.q_leg = q_leg
        self.q_foot = q_foot
        self.achieved_ankle = achieved_ankle
        self.residual = float(residual)
        self.reachable = bool(reachable)


def solve_leg_ik(hip_world: Vector, knee_world: Vector, ankle_world: Vector,
                 target_ankle_world: Vector, pole_world: Vector | None,
                 l1: float, l2: float,
                 hips_world_3x3: Matrix,
                 upleg_rest_3x3: Matrix, leg_rest_3x3: Matrix, foot_rest_3x3: Matrix,
                 foot_orig_dir_world: Vector | None = None,
                 ground_normal_world: Vector | None = None) -> LegSolution:
    """Solucionador analítico puro de 2 huesos para pierna (UpLeg + Leg + Foot).

    Calcula las rotaciones exactas para que el tobillo alcance target_ankle_world
    con estabilidad de plano de rodilla y preservación de apoyo.
    """
    v_T = target_ankle_world - hip_world
    d = v_T.length

    d_min = max(0.001, abs(l1 - l2) + 1e-4)
    d_max = (l1 + l2) - 1e-4
    d_clamped = max(d_min, min(d_max, d))

    reachable = abs(d - d_clamped) < 1e-4
    residual = abs(d - d_clamped)

    dir_T = v_T.normalized() if d > 1e-6 else Vector((0.0, 0.0, -1.0))
    target_pos = hip_world + dir_T * d_clamped

    # Vector de polo de rodilla proyectado en el plano perpendicular a hip->target
    if pole_world is not None:
        v_pole = pole_world - hip_world
    else:
        v_pole = knee_world - hip_world

    p_proj = v_pole - v_pole.dot(dir_T) * dir_T
    if p_proj.length < 1e-4:
        # Si la rodilla está perfectamente colineal, recurrir a la pose de flexión previa
        v_orig_knee = knee_world - hip_world
        p_proj = v_orig_knee - v_orig_knee.dot(dir_T) * dir_T
        if p_proj.length < 1e-4:
            p_proj = Vector((0.0, 1.0, 0.0)) - Vector((0.0, 1.0, 0.0)).dot(dir_T) * dir_T
            if p_proj.length < 1e-4:
                p_proj = Vector((1.0, 0.0, 0.0))
    p_proj.normalize()

    # Ley del coseno para ángulos del triángulo cadera-rodilla-tobillo
    cos_alpha = max(-1.0, min(1.0, (l1 * l1 + d_clamped * d_clamped - l2 * l2) / (2.0 * l1 * d_clamped)))
    alpha = math.acos(cos_alpha)

    # Dirección del muslo hacia la nueva rodilla
    v1_new = (math.cos(alpha) * dir_T + math.sin(alpha) * p_proj).normalized()
    new_knee = hip_world + v1_new * l1

    # Dirección de la espinilla
    v2_new = (target_pos - new_knee).normalized()

    # Conversión a cuaterniones relativos en el espacio del rig
    # 1. UpLeg relativo a Hips
    inv_hips = hips_world_3x3.inverted_safe()
    dir_upleg_in_hips = (inv_hips @ v1_new).normalized()
    # Dirección de reposo de UpLeg en espacio Hips
    rest_upleg_dir = (inv_hips @ upleg_rest_3x3.col[1].to_3d()).normalized()
    q_upleg = rest_upleg_dir.rotation_difference(dir_upleg_in_hips)

    # Orientación 3x3 fresca de UpLeg
    upleg_world_3x3 = hips_world_3x3 @ upleg_rest_3x3.to_3x3() @ q_upleg.to_matrix()

    # 2. Leg relativo a UpLeg
    inv_upleg = upleg_world_3x3.inverted_safe()
    dir_leg_in_upleg = (inv_upleg @ v2_new).normalized()
    rest_leg_dir = (inv_upleg @ leg_rest_3x3.col[1].to_3d()).normalized()
    q_leg = rest_leg_dir.rotation_difference(dir_leg_in_upleg)

    # Orientación 3x3 fresca de Leg
    leg_world_3x3 = upleg_world_3x3 @ leg_rest_3x3.to_3x3() @ q_leg.to_matrix()

    # 3. Foot relativo a Leg (contrarrotación para mantener orientación de planta)
    inv_leg = leg_world_3x3.inverted_safe()
    if foot_orig_dir_world is not None:
        target_fwd = foot_orig_dir_world.normalized()
    else:
        target_fwd = foot_rest_3x3.col[1].to_3d().normalized()

    if ground_normal_world is not None:
        # Alinear con normal de suelo manteniendo dirección horizontal
        gn = ground_normal_world.normalized()
        target_fwd = target_fwd - target_fwd.dot(gn) * gn
        if target_fwd.length > 1e-4:
            target_fwd.normalize()

    dir_foot_in_leg = (inv_leg @ target_fwd).normalized()
    rest_foot_dir = (inv_leg @ foot_rest_3x3.col[1].to_3d()).normalized()
    q_foot = rest_foot_dir.rotation_difference(dir_foot_in_leg)

    return LegSolution(
        q_upleg=q_upleg,
        q_leg=q_leg,
        q_foot=q_foot,
        achieved_ankle=target_pos,
        residual=residual,
        reachable=reachable,
    )


def solve_pelvis_assist(left_state: FootStateMachine, right_state: FootStateMachine,
                        left_patch: FootContactPatch, right_patch: FootContactPatch,
                        max_assist: float = 0.08) -> Vector:
    """Calcula una compensación de cadera acotada para apoyar las piernas en contacto."""
    active_deltas = []
    if left_state.state in (LOCKED, RELEASING) and left_state.anchor_ankle is not None:
        delta_l = left_state.anchor_ankle - left_patch.ankle
        active_deltas.append(delta_l * left_state.blend_weight)

    if right_state.state in (LOCKED, RELEASING) and right_state.anchor_ankle is not None:
        delta_r = right_state.anchor_ankle - right_patch.ankle
        active_deltas.append(delta_r * right_state.blend_weight)

    if not active_deltas:
        return Vector((0.0, 0.0, 0.0))

    if len(active_deltas) == 2:
        # Apoyo bilateral: promedio horizontal para balancear centro de masas
        # y cota superior en Z para no hundir el cuerpo
        delta = Vector((
            (active_deltas[0].x + active_deltas[1].x) * 0.5,
            (active_deltas[0].y + active_deltas[1].y) * 0.5,
            max(active_deltas[0].z, active_deltas[1].z),
        ))
    else:
        # Apoyo unilateral: desplaza la cadera hacia el ancla para aliviar extensión
        delta = active_deltas[0]

    # Acotar para no deformar excesivamente la silueta del personaje
    if delta.length > max_assist:
        delta = delta.normalized() * max_assist

    return delta


def aim_bone(pb, target_dir_arm: Vector, parent_world_3x3: Matrix | None = None) -> Quaternion:
    """Calcula la rotación local pura para que el hueso apunte hacia target_dir_arm (sin OneEuro)."""
    from .common import bone_rest_arm_3x3
    if not math.isfinite(target_dir_arm.x) or target_dir_arm.length < 1e-6:
        return Quaternion((1.0, 0.0, 0.0, 0.0))
    rest_3x3 = bone_rest_arm_3x3(pb, parent_world_3x3=parent_world_3x3)
    try:
        inv = rest_3x3.inverted_safe()
    except Exception:
        return Quaternion((1.0, 0.0, 0.0, 0.0))
    target_local = inv @ target_dir_arm
    if not math.isfinite(target_local.x) or target_local.length < 1e-6:
        return Quaternion((1.0, 0.0, 0.0, 0.0))
    target_local.normalize()
    return Vector((0.0, 1.0, 0.0)).rotation_difference(target_local)


def solve_leg_ik_bones(arm, pb_u, pb_l, pb_f, target_ankle_world: Vector,
                       hips_world_3x3: Matrix | None = None,
                       pole_world: Vector | None = None,
                       ground_normal_world: Vector | None = None) -> tuple[Quaternion, Quaternion, Quaternion, float, bool]:
    """Resuelve la cinemática inversa directamente sobre los pose bones de Blender.

    Devuelve (q_u, q_l, q_f, residual, reachable).
    """
    from .common import chained_world_3x3

    arm_3x3 = arm.matrix_world.to_3x3()
    arm_3x3_inv = arm_3x3.inverted_safe()

    # Trabajar en espacio armature para consistencia de matrices rest
    target_arm = arm.matrix_world.inverted_safe() @ target_ankle_world

    H = pb_u.head.copy()
    K = pb_l.head.copy()
    A = pb_f.head.copy()

    L1 = (K - H).length
    L2 = (A - K).length

    v_T = target_arm - H
    d = v_T.length
    d_min = max(0.001, abs(L1 - L2) + 1e-4)
    d_max = (L1 + L2) - 1e-4
    d_clamped = max(d_min, min(d_max, d))

    reachable = abs(d - d_clamped) < 1e-4
    residual = abs(d - d_clamped)

    dir_T = v_T.normalized() if d > 1e-6 else Vector((0.0, 0.0, -1.0))
    target_pos = H + dir_T * d_clamped

    if pole_world is not None:
        pole_arm = arm.matrix_world.inverted_safe() @ pole_world
        v_pole = pole_arm - H
    else:
        v_pole = K - H

    p_proj = v_pole - v_pole.dot(dir_T) * dir_T
    if p_proj.length < 1e-4:
        v_orig = K - H
        p_proj = v_orig - v_orig.dot(dir_T) * dir_T
        if p_proj.length < 1e-4:
            p_proj = Vector((0.0, 1.0, 0.0)) - Vector((0.0, 1.0, 0.0)).dot(dir_T) * dir_T
            if p_proj.length < 1e-4:
                p_proj = Vector((1.0, 0.0, 0.0))
    p_proj.normalize()

    cos_alpha = max(-1.0, min(1.0, (L1 * L1 + d_clamped * d_clamped - L2 * L2) / (2.0 * L1 * d_clamped)))
    alpha = math.acos(cos_alpha)

    v1_new = (math.cos(alpha) * dir_T + math.sin(alpha) * p_proj).normalized()
    new_knee = H + v1_new * L1
    v2_new = (target_pos - new_knee).normalized()

    if hips_world_3x3 is None:
        if pb_u.parent is not None:
            hips_world_3x3 = pb_u.parent.matrix.to_3x3()
        else:
            hips_world_3x3 = Matrix.Identity(3)

    q_u = aim_bone(pb_u, v1_new, parent_world_3x3=hips_world_3x3)
    u_world = chained_world_3x3(pb_u, q_u, parent_world_3x3=hips_world_3x3)

    q_l = aim_bone(pb_l, v2_new, parent_world_3x3=u_world)
    l_world = chained_world_3x3(pb_l, q_l, parent_world_3x3=u_world)

    f_orig_dir = (pb_f.tail - pb_f.head).normalized()
    if ground_normal_world is not None:
        gn_arm = (arm_3x3_inv @ ground_normal_world).normalized()
        f_aligned = f_orig_dir - f_orig_dir.dot(gn_arm) * gn_arm
        if f_aligned.length > 1e-4:
            f_orig_dir = f_aligned.normalized()

    q_f = aim_bone(pb_f, f_orig_dir, parent_world_3x3=l_world)

    return q_u, q_l, q_f, residual, reachable

