"""Conversor Kimodo → Mixamo (una sola dirección).

bpy-free en el CORE para que sea testeable: usa mathutils (disponible fuera de
Blender). El addon (K2-T3) extrae las rest matrices reales del armature y las
pasa aquí; el resultado son cuaterniones por hueso + Hips.location listos para
_bake_channels.

Trampa conocida (fix T1 de este repo): pose_bone.location y rotation_quaternion
NO están en espacio de armature — están en el REST BASIS del hueso. Toda
conversión pasa por la rest matrix del hueso.
"""
from __future__ import annotations

import numpy as np
from mathutils import Matrix, Vector

# Y-up (Kimodo) → Z-up (Blender/Mixamo):
#   Blender_x = Kimodo_x (+X = izquierda anatómica)
#   Blender_y = -Kimodo_z (+Z = forward del personaje → Blender -Y)
#   Blender_z = Kimodo_y  (+Y = up → Blender +Z)
YUP_TO_ZUP = Matrix((
    (1.0, 0.0, 0.0),
    (0.0, 0.0, -1.0),
    (0.0, 1.0, 0.0),
))

# Kimodo joint name (sin prefijo) → Mixamo bone (sin prefijo). Solo los que se
# animan y existen en ambos. Los joints Kimodo sin equivalente (cara, neck2,
# falanges 4/End, toe-end) se descartan.
SOMA77_TO_MIXAMO = {
    "Hips": "Hips",
    "Spine1": "Spine", "Spine2": "Spine1", "Chest": "Spine2",
    "Neck1": "Neck", "Head": "Head",
    "LeftShoulder": "LeftShoulder", "LeftArm": "LeftArm",
    "LeftForeArm": "LeftForeArm", "LeftHand": "LeftHand",
    "RightShoulder": "RightShoulder", "RightArm": "RightArm",
    "RightForeArm": "RightForeArm", "RightHand": "RightHand",
    "LeftLeg": "LeftUpLeg", "LeftShin": "LeftLeg", "LeftFoot": "LeftFoot",
    "LeftToeBase": "LeftToeBase",
    "RightLeg": "RightUpLeg", "RightShin": "RightLeg", "RightFoot": "RightFoot",
    "RightToeBase": "RightToeBase",
    **{f"LeftHand{s}": f"LeftHand{s}" for s in (
        "Thumb1", "Thumb2", "Thumb3", "Index1", "Index2", "Index3",
        "Middle1", "Middle2", "Middle3", "Ring1", "Ring2", "Ring3",
        "Pinky1", "Pinky2", "Pinky3")},
    **{f"RightHand{s}": f"RightHand{s}" for s in (
        "Thumb1", "Thumb2", "Thumb3", "Index1", "Index2", "Index3",
        "Middle1", "Middle2", "Middle3", "Ring1", "Ring2", "Ring3",
        "Pinky1", "Pinky2", "Pinky3")},
}

SMPLX22_TO_MIXAMO = {
    "pelvis": "Hips",
    "spine1": "Spine", "spine2": "Spine1", "spine3": "Spine2",
    "neck": "Neck", "head": "Head",
    "left_collar": "LeftShoulder", "left_shoulder": "LeftArm",
    "left_elbow": "LeftForeArm", "left_wrist": "LeftHand",
    "right_collar": "RightShoulder", "right_shoulder": "RightArm",
    "right_elbow": "RightForeArm", "right_wrist": "RightHand",
    "left_hip": "LeftUpLeg", "left_knee": "LeftLeg", "left_ankle": "LeftFoot",
    "left_foot": "LeftToeBase",
    "right_hip": "RightUpLeg", "right_knee": "RightLeg", "right_ankle": "RightFoot",
    "right_foot": "RightToeBase",
}

_SOMA30_NAMES = {
    "Hips", "Spine1", "Spine2", "Chest", "Neck1", "Neck2", "Head", "Jaw",
    "LeftEye", "RightEye", "LeftShoulder", "LeftArm", "LeftForeArm", "LeftHand",
    "LeftHandThumbEnd", "LeftHandMiddleEnd", "RightShoulder", "RightArm",
    "RightForeArm", "RightHand", "RightHandThumbEnd", "RightHandMiddleEnd",
    "LeftLeg", "LeftShin", "LeftFoot", "LeftToeBase", "RightLeg", "RightShin",
    "RightFoot", "RightToeBase",
}


def mapping_for(joint_count: int) -> dict:
    """Mapa joint_name→mixamo_bone según el número de joints del npz."""
    if joint_count == 77:
        return dict(SOMA77_TO_MIXAMO)
    if joint_count == 22:
        return dict(SMPLX22_TO_MIXAMO)
    if joint_count == 30:
        return {k: v for k, v in SOMA77_TO_MIXAMO.items() if k in _SOMA30_NAMES}
    raise ValueError(f"unsupported Kimodo joint count: {joint_count}")


def _hierarchy_order(bones: set, parent_of: dict) -> list:
    """Orden topológico: padres antes que hijos."""
    def _depth(b):
        d, p = 0, parent_of.get(b)
        while p is not None:
            d += 1
            p = parent_of.get(p)
        return d
    return sorted(bones, key=_depth)


def _rest_arm_3x3(bone, rest3, parent, parent_world):
    """Equivalente bpy-free de retarget.common.bone_rest_arm_3x3 (rotación)."""
    if parent is not None:
        rel = rest3[parent].inverted() @ rest3[bone]
        pw = parent_world if parent_world is not None else rest3[parent]
        return pw @ rel
    return rest3[bone]


def _chained_world(bone, rest3, parent, parent_world, basis3x3):
    """Equivalente bpy-free de retarget.common.chained_world_3x3 (rotación)."""
    if parent is None:
        return rest3[bone] @ basis3x3
    rel = rest3[parent].inverted() @ rest3[bone]
    pw = parent_world if parent_world is not None else rest3[parent]
    return pw @ rel @ basis3x3


def convert_motion(motion, joint_names, rest3, rest_pos, parent_of, mapping,
                   fps: float = 30.0, apply_root: bool = True):
    """Convierte un npz de Kimodo a cuaterniones Mixamo por hueso.

    motion: dict del npz (global_rot_mats [T,J,3,3] Y-up, root_positions [T,3]).
    joint_names: lista de nombres de joints Kimodo (orden del npz).
    rest3: {mixamo_bone: Matrix 3x3 (rotación de la rest matrix)}.
    rest_pos: {mixamo_bone: Vector — posición (head) del hueso en rest}.
    parent_of: {mixamo_bone: bone_padre | None}.
    mapping: {joint_name: mixamo_bone}.
    fps: fps objetivo (resamplea si el del motion difiere).

    Devuelve (rotations, root):
        rotations: {mixamo_bone: [(frame, (w,x,y,z))]} — quaternion en rest basis.
        root: {Hips: [(frame, (x,y,z))]} — Hips.location en rest basis.
    """
    b2j: dict = {}
    for j, name in enumerate(joint_names):
        b = mapping.get(name)
        if b is not None:
            b2j[b] = j

    g = np.asarray(motion["global_rot_mats"], dtype=np.float64)  # [T,J,3,3]
    rp = np.asarray(motion["root_positions"], dtype=np.float64)  # [T,3]
    T = g.shape[0]
    infps = float(motion.get("fps", fps) or fps)
    # resamplea si el motion va más rápido que fps objetivo
    step = max(1, round(infps / fps)) if infps > fps else 1
    frames = list(range(0, T, step)) or [0]

    # orden padre-antes-de-hijo según la jerarquía Mixamo
    order = _hierarchy_order(set(rest3.keys()), parent_of)

    rotations: dict = {}
    root: dict = {}
    for f in frames:
        parent_world = None
        for b in order:
            parent = parent_of.get(b)
            rest_arm = _rest_arm_3x3(b, rest3, parent, parent_world)
            j = b2j.get(b)
            if j is not None and f < T:
                row = g[f, j]
                r_k = Matrix((tuple(row[0]), tuple(row[1]), tuple(row[2])))
                target_world = YUP_TO_ZUP @ r_k @ YUP_TO_ZUP.transposed()
                basis3x3 = rest_arm.inverted() @ target_world
            else:
                basis3x3 = Matrix.Identity(3)
            q = basis3x3.to_quaternion()
            q.normalize()
            rotations.setdefault(b, []).append((f, (q.w, q.x, q.y, q.z)))
            parent_world = _chained_world(b, rest3, parent, parent_world, basis3x3)

        if apply_root and f < T:
            posbl = YUP_TO_ZUP @ Vector((rp[f, 0], rp[f, 1], rp[f, 2]))
            h3 = rest3.get("Hips", Matrix.Identity(3))
            head = rest_pos.get("Hips", Vector((0.0, 0.0, 0.0)))
            loc = h3.inverted() @ (posbl - head)
            root.setdefault("Hips", []).append((f, (loc.x, loc.y, loc.z)))

    return rotations, root


# --- Orden de joints del npz (Kimodo), sin prefijo -------------------------
# De kimodo/skeleton/definitions.py bone_order_names_with_parents.
_SOMA_BODY = ["Hips", "Spine1", "Spine2", "Chest", "Neck1", "Neck2", "Head",
              "HeadEnd", "Jaw", "LeftEye", "RightEye"]
_SOMA_ARM = ["Shoulder", "Arm", "ForeArm", "Hand"]
_SOMA_FINGERS = ["Thumb1", "Thumb2", "Thumb3", "ThumbEnd",
                 "Index1", "Index2", "Index3", "Index4", "IndexEnd",
                 "Middle1", "Middle2", "Middle3", "Middle4", "MiddleEnd",
                 "Ring1", "Ring2", "Ring3", "Ring4", "RingEnd",
                 "Pinky1", "Pinky2", "Pinky3", "Pinky4", "PinkyEnd"]
_SOMA_LEG = ["Leg", "Shin", "Foot", "ToeBase", "ToeEnd"]


def _soma77_order() -> list:
    order = list(_SOMA_BODY)
    for side in ("Left", "Right"):
        order += [side + a for a in _SOMA_ARM]
        order += [side + "Hand" + f for f in _SOMA_FINGERS]
    order += ["Left" + s for s in _SOMA_LEG]
    order += ["Right" + s for s in _SOMA_LEG]
    return order


SOMA77_JOINT_ORDER = _soma77_order()

SMPLX22_JOINT_ORDER = [
    "pelvis", "left_hip", "right_hip", "spine1", "left_knee", "right_knee",
    "spine2", "left_ankle", "right_ankle", "spine3", "left_foot",
    "right_foot", "neck", "left_collar", "right_collar", "head",
    "left_shoulder", "right_shoulder", "left_elbow", "right_elbow",
    "left_wrist", "right_wrist",
]


def joint_order(joint_count: int) -> list:
    """Lista de nombres de joints Kimodo según el número de joints del npz."""
    if joint_count == 77:
        return list(SOMA77_JOINT_ORDER)
    if joint_count == 22:
        return list(SMPLX22_JOINT_ORDER)
    if joint_count == 30:
        return [n for n in SOMA77_JOINT_ORDER if n in _SOMA30_NAMES]
    raise ValueError(f"unsupported Kimodo joint count: {joint_count}")
