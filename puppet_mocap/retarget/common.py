"""Helpers compartidos por los módulos de retarget (body, hands, face).

Convención arm-space:
    arm.x =  mp.x   (+X = lado IZQUIERDO anatómico del personaje)
    arm.y =  mp.z   (+Y = atrás del personaje, lejos de la cámara)
    arm.z = -mp.y   (+Z = arriba)
"""
from __future__ import annotations

import math
import time

import bpy
from mathutils import Matrix, Quaternion, Vector

from .. import log

PUPPET_INIT_FLAG = "puppet_mocap_initialized"

# Estado global compartido por los módulos de retarget. Vive a nivel de proceso
# Blender — una sola sesión de mocap a la vez.
_state: dict = {
    "arm": None,
    "target_arm_name": None,  # nombre elegido por el usuario (PointerProperty)
    "last_debug_t": 0.0,
    "last_axis_dump": False,
    "quat_filters": {},   # bone_name → QuatOneEuro
    "scalar_filters": {}, # key → ScalarOneEuro (para shape keys de cara)
    "smooth_params": None, # (min_cutoff, beta) | None = bypass
    "tick_t": 0.0,
    "kf_names": {},       # (prefix, body, hands) → list[str]
    "arm_world_3x3": {},  # "Left"/"Right" → Matrix 3x3 fresca del brazo (body→hands)
}


# --- Armature management --------------------------------------------------

def set_target_armature(name: str | None):
    """Fija el armature objetivo por nombre (None = auto). Invalida el cache."""
    if _state.get("target_arm_name") != name:
        _state["target_arm_name"] = name
        _state["arm"] = None
        _state["kf_names"] = {}


def _cache_arm(arm):
    for pb in arm.pose.bones:
        pb.rotation_mode = "QUATERNION"
    _state["arm"] = arm
    return arm


def get_armature():
    """Prioridad: armature elegido por el usuario → cache → escena actual →
    cualquier armature del archivo (compat)."""
    name = _state.get("target_arm_name")
    if name:
        obj = bpy.data.objects.get(name)
        if obj is not None and obj.type == "ARMATURE":
            arm = _state.get("arm")
            try:
                if arm is not None and arm.name == name:
                    return arm
            except ReferenceError:
                pass
            return _cache_arm(obj)
        # objetivo desapareció → cae al auto
    arm = _state.get("arm")
    if arm is not None:
        try:
            if arm.name in bpy.data.objects:
                return arm
        except ReferenceError:
            pass
        _state["arm"] = None
    try:
        scene_objs = bpy.context.scene.objects
    except AttributeError:
        scene_objs = bpy.data.objects
    arm = next((o for o in scene_objs if o.type == "ARMATURE"), None)
    if arm is None:
        arm = next((o for o in bpy.data.objects if o.type == "ARMATURE"), None)
    if arm is None:
        return None
    return _cache_arm(arm)


def fix_orientation(force: bool = False, prefix: str = "mixamorig:") -> bool:
    """Prepara el rig para capturar. NO borra actions (eso era pérdida de
    datos silenciosa) y solo resetea la rotación del OBJETO con force=True
    (botón Reset Rig) — un start normal ya no pisa la colocación del usuario."""
    arm = get_armature()
    if arm is None:
        return False
    reset_smoothing()
    needs_init = force or not arm.get(PUPPET_INIT_FLAG)
    if needs_init:
        log.info(f"inicializando armature '{arm.name}' prefix='{prefix}' force={force}")
        if force:
            arm.rotation_mode = "XYZ"
            arm.rotation_euler = (0.0, 0.0, 0.0)
            arm.rotation_quaternion = (1.0, 0.0, 0.0, 0.0)
        for pb in arm.pose.bones:
            pb.rotation_mode = "QUATERNION"
            pb.rotation_quaternion = (1.0, 0.0, 0.0, 0.0)
            pb.location = (0.0, 0.0, 0.0)
            pb.scale = (1.0, 1.0, 1.0)
        arm[PUPPET_INIT_FLAG] = True
        _state["last_axis_dump"] = False
    bpy.context.view_layer.update()
    return True


def clear_all_keyframes() -> bool:
    """Borra la action del armature Y la animación facial (shape keys)."""
    arm = get_armature()
    if arm is None:
        return False
    if arm.animation_data and arm.animation_data.action:
        bpy.data.actions.remove(arm.animation_data.action, do_unlink=True)
    # Animación de shape keys vive en un datablock aparte (Key) — sin esto,
    # la actuación facial vieja sobrevivía a "Borrar Keyframes".
    from . import face as _face
    _face.clear_face_animation(arm)
    bpy.context.view_layer.update()
    return True


# --- Coordinate conversion ------------------------------------------------

def mp_to_arm(lm) -> Vector:
    """MediaPipe pose_world_landmarks → arm space.
    Devuelve un vector con ceros si los landmarks tienen NaN/Inf — más vale un
    landmark en el origen que propagar NaN a una matriz de rotación.
    """
    x, y, z = float(lm[0]), float(lm[1]), float(lm[2])
    if not (math.isfinite(x) and math.isfinite(y) and math.isfinite(z)):
        return Vector((0.0, 0.0, 0.0))
    return Vector((x, z, -y))


def lm_visibility(lm) -> float:
    """Visibilidad del landmark de Pose (4to componente). Manos/cara no la
    traen (MediaPipe la deja en 0 para esas tasks) → default 1.0."""
    try:
        return float(lm[3])
    except (IndexError, TypeError, ValueError):
        return 1.0


# Pares L/R de MediaPipe Pose para el modo espejo
_MIRROR_PAIRS = ((1, 4), (2, 5), (3, 6), (7, 8), (9, 10), (11, 12), (13, 14),
                 (15, 16), (17, 18), (19, 20), (21, 22), (23, 24), (25, 26),
                 (27, 28), (29, 30), (31, 32))
_MIRROR_IDX = list(range(33))
for _a, _b in _MIRROR_PAIRS:
    _MIRROR_IDX[_a], _MIRROR_IDX[_b] = _b, _a


def mirror_pose_landmarks(landmarks):
    """Modo espejo: intercambia landmarks L↔R y niega x. El resultado es una
    pose geométricamente válida (la imagen especular), así que todo el
    pipeline downstream funciona sin tocar nada."""
    if not landmarks or len(landmarks) < 33:
        return landmarks
    out = []
    for i in range(len(landmarks)):
        src = landmarks[_MIRROR_IDX[i]] if i < 33 else landmarks[i]
        m = list(src)
        m[0] = -m[0]
        out.append(m)
    return out


def mirror_hand_landmarks(lms):
    """Niega x de los 21 landmarks de una mano (la vuelve su quiral opuesta)."""
    if not lms:
        return lms
    out = []
    for lm in lms:
        m = list(lm)
        m[0] = -m[0]
        out.append(m)
    return out


# --- Calibración de postura -------------------------------------------------
# Una webcam inclinada (o un usuario que no encara la cámara) mete un offset
# constante a TODA la captura: el personaje queda eternamente echado para
# atrás o volteado. La calibración captura ~1.5 s de pose neutral (parado
# derecho, de frente) y calcula la rotación arm-space que endereza esa pose:
#   up observado (mid_hip→mid_shoulder)  → +Z
#   lateral observado (cadera R→L)       → +X
# Después cada landmark se pre-rota con esa matriz (técnica equivalente al
# offset de T-pose de Rokoko Studio Live, reducida a un solo cuadro global).

def compute_calibration(samples: list) -> Matrix | None:
    """samples: lista de listas de 33 landmarks [x,y,z,(vis)] MediaPipe.
    Devuelve la Matrix 3x3 arm-space correctora, o None si no hay señal."""
    if len(samples) < 5:
        return None
    up_acc = Vector((0.0, 0.0, 0.0))
    lat_acc = Vector((0.0, 0.0, 0.0))
    n = 0
    for lms in samples:
        if not lms or len(lms) < 33:
            continue
        l_sh, r_sh = mp_to_arm(lms[11]), mp_to_arm(lms[12])
        l_hip, r_hip = mp_to_arm(lms[23]), mp_to_arm(lms[24])
        up = (l_sh + r_sh) * 0.5 - (l_hip + r_hip) * 0.5
        lat = l_hip - r_hip
        if up.length < 1e-4 or lat.length < 1e-4:
            continue
        up_acc += up.normalized()
        lat_acc += lat.normalized()
        n += 1
    if n < 5 or up_acc.length < 1e-4 or lat_acc.length < 1e-4:
        return None
    up = up_acc.normalized()
    lat = lat_acc - lat_acc.dot(up) * up
    if lat.length < 1e-4:
        return None
    lat = lat.normalized()
    fwd = up.cross(lat)  # = +Y arm ideal (atrás del personaje) si Z×X
    if fwd.length < 1e-6:
        return None
    # Base observada por columnas (X=lat, Y=Z×X, Z=up); como es ortonormal,
    # la correctora es su transpuesta: R @ up_obs = +Z, R @ lat_obs = +X.
    m_obs = Matrix(((lat.x, fwd.x, up.x),
                    (lat.y, fwd.y, up.y),
                    (lat.z, fwd.z, up.z)))
    return m_obs.transposed()


def calibrate_pose_landmarks(landmarks, R: Matrix):
    """Pre-rota landmarks crudos MediaPipe con la R arm-space. Conversión:
    arm=(mx,mz,-my) ⇒ de regreso mp=(ax,-az,ay). Preserva visibilidad."""
    if not landmarks:
        return landmarks
    out = []
    for lm in landmarks:
        p = R @ mp_to_arm(lm)
        m = list(lm)
        m[0], m[1], m[2] = p.x, -p.z, p.y
        out.append(m)
    return out


def calibrate_hand_landmarks(hand_lms, R: Matrix):
    return calibrate_pose_landmarks(hand_lms, R)


def bone_rest_arm_3x3(pose_bone, parent_world_3x3: Matrix | None = None) -> Matrix:
    """3x3 del hueso en arm space asumiendo rotation=identity en este hueso
    pero la pose actual del padre.

    Si `parent_world_3x3` está dado, se usa en lugar de `pose_bone.parent.matrix`.
    Necesario cuando recién acabamos de asignar la rotación del padre y aún no
    hemos llamado a `view_layer.update()` — el depsgraph todavía no la evaluó,
    así que `parent.matrix` está stale. Pasarle el 3x3 calculado manualmente
    rompe esa cadena de stale-ness y evita el bug del pulgar (Hand rota 90°
    pero el rest_3x3 del thumb seguía pensando que Hand=identity).
    """
    if pose_bone.parent is not None:
        rel_3x3 = (pose_bone.parent.bone.matrix_local.inverted_safe()
                   @ pose_bone.bone.matrix_local).to_3x3()
        if parent_world_3x3 is None:
            parent_3x3 = pose_bone.parent.matrix.to_3x3()
        else:
            parent_3x3 = parent_world_3x3
        return parent_3x3 @ rel_3x3
    return pose_bone.bone.matrix_local.to_3x3()


def chained_world_3x3(pose_bone, q: Quaternion,
                      parent_world_3x3: Matrix | None = None) -> Matrix:
    """World 3x3 del bone asumiendo `rotation_quaternion = q`. Útil para
    propagar el override a los children dentro del mismo frame, sin esperar
    al depsgraph eval."""
    q_3x3 = q.to_matrix()
    if pose_bone.parent is None:
        return pose_bone.bone.matrix_local.to_3x3() @ q_3x3
    rel_3x3 = (pose_bone.parent.bone.matrix_local.inverted_safe()
               @ pose_bone.bone.matrix_local).to_3x3()
    if parent_world_3x3 is None:
        parent_3x3 = pose_bone.parent.matrix.to_3x3()
    else:
        parent_3x3 = parent_world_3x3
    return parent_3x3 @ rel_3x3 @ q_3x3


# --- One Euro filter (cuaternión y escalar) -------------------------------

def _alpha_oe(cutoff_hz: float, dt: float) -> float:
    tau = 1.0 / (2.0 * math.pi * cutoff_hz)
    return 1.0 / (1.0 + tau / max(dt, 1e-4))


class QuatOneEuro:
    """One Euro Filter sobre cuaterniones vía slerp."""

    # Gate de outlier: si el sample salta más de este ángulo en menos de
    # OUTLIER_DT_MAX segundos, lo tratamos como un flip espurio del modelo
    # (típico en MediaPipe HandLandmarker cuando la palma está casi de canto).
    # Tras OUTLIER_MAX rechazos consecutivos asumimos que es un cambio real y
    # aceptamos, para no quedarnos colgados.
    OUTLIER_ANGLE_RAD = math.pi / 2.0   # 90°
    OUTLIER_DT_MAX = 0.1                # 100 ms
    OUTLIER_MAX = 2

    def __init__(self, min_cutoff=2.0, beta=0.5, d_cutoff=1.0):
        self.min_cutoff = min_cutoff
        self.beta = beta
        self.d_cutoff = d_cutoff
        self.q = None
        self.dq_smoothed = 0.0
        self.last_t = None
        self.outlier_count = 0

    def reset(self):
        self.q = None
        self.dq_smoothed = 0.0
        self.last_t = None
        self.outlier_count = 0

    def __call__(self, q_new: Quaternion, t: float) -> Quaternion:
        # Si el estado interno se corrompió por un input previo malo, reset
        if self.q is not None and not all(math.isfinite(c)
                                          for c in (self.q.w, self.q.x, self.q.y, self.q.z)):
            self.reset()
        if self.q is None or self.last_t is None:
            self.q = q_new.copy()
            self.last_t = t
            return q_new
        dt = t - self.last_t
        if dt <= 1e-4:
            return self.q
        if self.q.dot(q_new) < 0.0:
            q_new = Quaternion((-q_new.w, -q_new.x, -q_new.y, -q_new.z))
        try:
            d_angle = self.q.rotation_difference(q_new).angle
        except ValueError:
            self.last_t = t
            self.q = q_new.copy()
            return self.q
        if not math.isfinite(d_angle):
            self.last_t = t
            self.q = q_new.copy()
            return self.q
        # Outlier gate: salto grande y rápido → probable flip espurio. Rechaza
        # sin actualizar last_t para que el siguiente sample compare contra el
        # mismo instante (preserva la métrica de discontinuidad). Tras N rechazos
        # consecutivos asumimos que es un cambio real y dejamos pasar.
        if d_angle > self.OUTLIER_ANGLE_RAD and dt < self.OUTLIER_DT_MAX:
            if self.outlier_count < self.OUTLIER_MAX:
                self.outlier_count += 1
                return self.q
            self.outlier_count = 0
        else:
            self.outlier_count = 0
        self.last_t = t
        speed = d_angle / dt
        ad = _alpha_oe(self.d_cutoff, dt)
        self.dq_smoothed = ad * speed + (1.0 - ad) * self.dq_smoothed
        cutoff = self.min_cutoff + self.beta * self.dq_smoothed
        a = _alpha_oe(cutoff, dt)
        # Clamp por si acaso
        if not math.isfinite(a):
            a = 0.5
        a = max(0.0, min(1.0, a))
        self.q = self.q.slerp(q_new, a)
        return self.q


class ScalarOneEuro:
    """One Euro Filter para un valor escalar (shape keys de cara)."""

    def __init__(self, min_cutoff=2.0, beta=0.5, d_cutoff=1.0):
        self.min_cutoff = min_cutoff
        self.beta = beta
        self.d_cutoff = d_cutoff
        self.x = None
        self.dx_smoothed = 0.0
        self.last_t = None

    def reset(self):
        self.x = None
        self.dx_smoothed = 0.0
        self.last_t = None

    def __call__(self, x_new: float, t: float) -> float:
        if self.x is None or self.last_t is None:
            self.x = x_new
            self.last_t = t
            return x_new
        dt = t - self.last_t
        if dt <= 1e-4:
            return self.x
        self.last_t = t
        speed = abs(x_new - self.x) / dt
        ad = _alpha_oe(self.d_cutoff, dt)
        self.dx_smoothed = ad * speed + (1.0 - ad) * self.dx_smoothed
        cutoff = self.min_cutoff + self.beta * self.dx_smoothed
        a = _alpha_oe(cutoff, dt)
        self.x = a * x_new + (1.0 - a) * self.x
        return self.x


def reset_smoothing():
    _state["quat_filters"] = {}
    _state["scalar_filters"] = {}
    _state["last_axis_dump"] = False
    _state["first_hand_logged"] = False
    _state["face_warned"] = False
    _state["palm_normal_prev"] = {}
    _state["palm_diag_Left"] = False
    _state["palm_diag_Right"] = False
    _state["face_mesh_name"] = None
    _state["face_key_map"] = None
    _state["kf_names"] = {}
    _state["arm_world_3x3"] = {}
    _state["auto_swap_active"] = False


def set_tick(t: float, rotation_smooth: float):
    _state["tick_t"] = t
    _state["smooth_params"] = _smooth_strength_to_params(rotation_smooth)


def _smooth_strength_to_params(strength: float):
    """0..1 → (min_cutoff_hz, beta) | None = bypass.
       0 → bypass; 0.5 → ~2 Hz; 1 → ~0.3 Hz."""
    if strength <= 0.0:
        return None
    min_cutoff = 12.0 * (0.025 ** strength)
    return (min_cutoff, 0.4)


def _is_finite_q(q: Quaternion) -> bool:
    """True si todos los componentes son finitos (no NaN ni Inf)."""
    return all(math.isfinite(c) for c in (q.w, q.x, q.y, q.z))


def _safe_quaternion(q: Quaternion, fallback: Quaternion = None) -> Quaternion:
    """Devuelve q normalizado y validado. Si q tiene NaN/Inf o es ~0, devuelve
    fallback (default = identity). Pasarle quaterniones inválidos a Blender
    causa segfaults silenciosos."""
    if fallback is None:
        fallback = Quaternion((1.0, 0.0, 0.0, 0.0))
    if not _is_finite_q(q):
        return fallback
    n = (q.w * q.w + q.x * q.x + q.y * q.y + q.z * q.z) ** 0.5
    if n < 1e-8:
        return fallback
    return Quaternion((q.w / n, q.x / n, q.y / n, q.z / n))


def smooth_q(name: str, q: Quaternion) -> Quaternion:
    q = _safe_quaternion(q)
    params = _state.get("smooth_params")
    if params is None:
        return q
    f = _state["quat_filters"].get(name)
    if f is None:
        f = QuatOneEuro(min_cutoff=params[0], beta=params[1])
        _state["quat_filters"][name] = f
    else:
        f.min_cutoff = params[0]
        f.beta = params[1]
    out = f(q, _state["tick_t"])
    return _safe_quaternion(out, fallback=q)


def smooth_scalar(key: str, value: float) -> float:
    params = _state.get("smooth_params")
    if params is None:
        return value
    f = _state["scalar_filters"].get(key)
    if f is None:
        f = ScalarOneEuro(min_cutoff=params[0], beta=params[1])
        _state["scalar_filters"][key] = f
    else:
        f.min_cutoff = params[0]
        f.beta = params[1]
    return f(value, _state["tick_t"])


# --- Aim primitivas -------------------------------------------------------

def _vec_is_finite(v: Vector) -> bool:
    return math.isfinite(v.x) and math.isfinite(v.y) and math.isfinite(v.z)


def aim(pose_bone, target_arm_dir: Vector,
        parent_world_3x3: Matrix | None = None):
    """Rota pose_bone para que su Y local apunte hacia target_arm_dir.
    Retorna el quaternion suavizado asignado, o None si no se pudo aplicar."""
    if not _vec_is_finite(target_arm_dir) or target_arm_dir.length < 1e-6:
        return None
    rest_3x3 = bone_rest_arm_3x3(pose_bone, parent_world_3x3=parent_world_3x3)
    try:
        inv = rest_3x3.inverted_safe()
    except Exception:
        return None
    target_local = inv @ target_arm_dir
    if not _vec_is_finite(target_local) or target_local.length < 1e-6:
        return None
    target_local.normalize()
    q = Vector((0.0, 1.0, 0.0)).rotation_difference(target_local)
    q_s = smooth_q(pose_bone.name, q)
    pose_bone.rotation_quaternion = q_s
    return q_s


def orient_yz(pose_bone, y_arm: Vector, z_arm: Vector,
              parent_world_3x3: Matrix | None = None):
    """Y local = y_arm; Z local = z_arm (ortogonalizado).
    Retorna el quaternion suavizado asignado, o None si no se pudo aplicar."""
    if not (_vec_is_finite(y_arm) and _vec_is_finite(z_arm)):
        return None
    rest_3x3 = bone_rest_arm_3x3(pose_bone, parent_world_3x3=parent_world_3x3)
    try:
        inv = rest_3x3.inverted_safe()
    except Exception:
        return None
    y = inv @ y_arm
    z = inv @ z_arm
    if not (_vec_is_finite(y) and _vec_is_finite(z)):
        return None
    if y.length < 1e-6 or z.length < 1e-6:
        return None
    y.normalize()
    z = z - z.dot(y) * y
    if z.length < 1e-4:
        return None
    z.normalize()
    x = y.cross(z)
    if x.length < 1e-6:
        return None
    x.normalize()
    m = Matrix(((x.x, y.x, z.x),
                (x.y, y.y, z.y),
                (x.z, y.z, z.z)))
    q_s = smooth_q(pose_bone.name, m.to_quaternion())
    pose_bone.rotation_quaternion = q_s
    return q_s


def orient_yx(pose_bone, y_arm: Vector, x_arm: Vector,
              parent_world_3x3: Matrix | None = None):
    """Y local = y_arm; X local = x_arm (ortogonalizado).
    Retorna el quaternion suavizado asignado, o None si no se pudo aplicar."""
    if not (_vec_is_finite(y_arm) and _vec_is_finite(x_arm)):
        return None
    rest_3x3 = bone_rest_arm_3x3(pose_bone, parent_world_3x3=parent_world_3x3)
    try:
        inv = rest_3x3.inverted_safe()
    except Exception:
        return None
    y = inv @ y_arm
    x = inv @ x_arm
    if not (_vec_is_finite(y) and _vec_is_finite(x)):
        return None
    if y.length < 1e-6 or x.length < 1e-6:
        return None
    y.normalize()
    x = x - x.dot(y) * y
    if x.length < 1e-4:
        return None
    x.normalize()
    z = x.cross(y)
    if z.length < 1e-6:
        return None
    z.normalize()
    m = Matrix(((x.x, y.x, z.x),
                (x.y, y.y, z.y),
                (x.z, y.z, z.z)))
    q_s = smooth_q(pose_bone.name, m.to_quaternion())
    pose_bone.rotation_quaternion = q_s
    return q_s
