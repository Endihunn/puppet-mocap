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
}

# DEDOS: deliberadamente AUSENTES del mapa.
#
# Kimodo-SOMA-RP-v1.1 corre internamente sobre SOMASkeleton30 y expande a 77 en
# la salida rellenando las falanges con una pose relajada ESTÁTICA
# (output_to_SOMASkeleton77 → relaxed_hands_rest_pose). Los joints existen en el
# array pero no llevan movimiento: medido sobre walk_wave.npz, las 30 falanges
# daban 150 keyframes con variación 0.00e+00.
#
# Mapearlos añadía 120 fcurves (18.000 keyframes) de relleno constante que
# PISAN la animación de manos capturada al combinar tomas. Al no emitir canal,
# los dedos quedan como estén y un cuerpo generado se puede combinar con manos
# capturadas — que es el objetivo en una app de titiritero.
#
# Si algún modelo futuro sí anima falanges, esto se revierte añadiendo las
# entradas Left/RightHand{Thumb,Index,Middle,Ring,Pinky}{1,2,3} → mismo nombre.
FINGER_SUFFIXES = ("Thumb", "Index", "Middle", "Ring", "Pinky")


def is_finger_bone(bone_name: str) -> bool:
    """True si el hueso Mixamo es una falange (no se anima desde Kimodo)."""
    return any(s in bone_name for s in FINGER_SUFFIXES)

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


# Joint de Kimodo hacia el que apunta cada hueso. El retarget es POR DIRECCION,
# no por matriz de rotacion: medido sobre walk_wave, apuntar da 0.2 grados de
# error angular medio (max 3.0) mientras que componer las global_rot_mats sobre
# la rest de Mixamo da 14.1 (max 49.2) y usarlas como orientacion absoluta —lo
# que hacia el codigo original— da 95.3 (max 159.1), que es el personaje
# retorcido que se veia.
#
# El motivo del residuo de las matrices: las rest pose de SOMA y de Mixamo no
# tienen los huesos en los mismos angulos, asi que aplicar el delta de SOMA
# sobre la rest de Mixamo arrastra esa diferencia. Apuntar la elimina por
# construccion.
#
# LIMITACION: apuntar deja el ROLL libre (no hay twist). Es la misma renuncia
# que hace body.py con MediaPipe. Si algun dia hace falta twist, hay que
# derivar la rest de SOMA por joint y componer, no volver a las matrices tal
# cual.
SOMA_AIM_CHILD = {
    "Hips": "Spine1", "Spine1": "Spine2", "Spine2": "Chest", "Chest": "Neck1",
    "Neck1": "Head", "Head": "HeadEnd",
    "LeftShoulder": "LeftArm", "LeftArm": "LeftForeArm",
    "LeftForeArm": "LeftHand", "LeftHand": "LeftHandMiddle1",
    "RightShoulder": "RightArm", "RightArm": "RightForeArm",
    "RightForeArm": "RightHand", "RightHand": "RightHandMiddle1",
    "LeftLeg": "LeftShin", "LeftShin": "LeftFoot", "LeftFoot": "LeftToeBase",
    "LeftToeBase": "LeftToeEnd",
    "RightLeg": "RightShin", "RightShin": "RightFoot", "RightFoot": "RightToeBase",
    "RightToeBase": "RightToeEnd",
    # SMPL-X (22 joints) no trae extremos; los huesos hoja quedan en rest.
    "pelvis": "spine1", "spine1": "spine2", "spine2": "spine3", "spine3": "neck",
    "neck": "head",
    "left_collar": "left_shoulder", "left_shoulder": "left_elbow",
    "left_elbow": "left_wrist",
    "right_collar": "right_shoulder", "right_shoulder": "right_elbow",
    "right_elbow": "right_wrist",
    "left_hip": "left_knee", "left_knee": "left_ankle", "left_ankle": "left_foot",
    "right_hip": "right_knee", "right_knee": "right_ankle",
    "right_ankle": "right_foot",
}


def prefixed_mapping(mapping: dict, prefix: str) -> dict:
    """Aplica el prefijo del rig a los huesos Mixamo del mapa.

    `mapping_for()` devuelve nombres SIN prefijo ("Hips"), pero un armature
    Mixamo real los tiene con prefijo ("mixamorig:Hips"). Sin esto, ni un solo
    hueso casaba y la generación no movía nada.
    """
    if not prefix:
        return dict(mapping)
    return {k: f"{prefix}{v}" for k, v in mapping.items()}


def torso_scale(motion, joint_names, mapping, rest_pos, root_bone) -> float:
    """Factor metros (Kimodo) -> unidades del rig, medido sobre el torso.

    Un FBX de Mixamo suele venir en escala centimetrica (~100 unidades de alto)
    mientras Kimodo emite metros. Se deriva del rig en vez de hardcodearse,
    igual que hace body._rig_torso_length para la captura.
    """
    pj = motion.get("posed_joints")
    if pj is None or len(pj) == 0:
        return 1.0
    chest = mapping.get("Chest") or mapping.get("spine3")
    if chest is None or chest not in rest_pos or root_bone not in rest_pos:
        return 1.0
    rig_torso = (rest_pos[chest] - rest_pos[root_bone]).length
    idx = {}
    for j, name in enumerate(joint_names):
        b = mapping.get(name)
        if b in (root_bone, chest):
            idx[b] = j
    if root_bone not in idx or chest not in idx:
        return 1.0
    a, b = pj[0, idx[root_bone]], pj[0, idx[chest]]
    k_torso = float(sum((float(a[i]) - float(b[i])) ** 2 for i in range(3)) ** 0.5)
    if k_torso < 1e-6 or rig_torso < 1e-6:
        return 1.0
    return rig_torso / k_torso


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
                   fps: float = 30.0, apply_root: bool = True,
                   root_scale: float | None = None):
    """Convierte un npz de Kimodo a cuaterniones Mixamo por hueso.

    motion: dict del npz (posed_joints [T,J,3] Y-up, root_positions [T,3]).
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

    b2k = {b: n for n, b in mapping.items()}
    jidx = {n: j for j, n in enumerate(joint_names)}

    # El retarget usa POSICIONES (posed_joints), no global_rot_mats. Ver la
    # nota de SOMA_AIM_CHILD para los numeros que respaldan la decision.
    pj = np.asarray(motion["posed_joints"], dtype=np.float64)     # [T,J,3]
    rp = np.asarray(motion["root_positions"], dtype=np.float64)   # [T,3]
    T = pj.shape[0]
    infps = float(motion.get("fps", fps) or fps)
    # resamplea si el motion va más rápido que fps objetivo
    step = max(1, round(infps / fps)) if infps > fps else 1
    frames = list(range(0, T, step)) or [0]

    # orden padre-antes-de-hijo según la jerarquía Mixamo
    order = _hierarchy_order(set(rest3.keys()), parent_of)

    # Hueso raíz según el mapping (respeta el prefijo del rig).
    root_bone = mapping.get("Hips") or mapping.get("pelvis")
    if root_bone is None:
        raise ValueError("el mapping no define un hueso raíz (Hips/pelvis)")

    if root_scale is None:
        root_scale = torso_scale(motion, joint_names, mapping, rest_pos, root_bone)
    root_ref = None  # posición del primer frame; la traslación es relativa

    rotations: dict = {}
    root: dict = {}
    for f in frames:
        # World 3x3 POR HUESO. Antes era una sola variable arrastrada sobre una
        # lista ordenada por profundidad: al pasar de una rama a otra al mismo
        # nivel (p.ej. Spine → LeftUpLeg, ambos hijos de Hips) el hueso heredaba
        # la matriz de la rama anterior en vez de la de su padre.
        world: dict = {}
        for b in order:
            parent = parent_of.get(b)
            parent_world = world.get(parent) if parent is not None else None
            rest_arm = _rest_arm_3x3(b, rest3, parent, parent_world)
            kn = b2k.get(b)
            child = SOMA_AIM_CHILD.get(kn) if kn else None
            jn = jidx.get(kn)
            jc = jidx.get(child)
            basis3x3 = None
            if jn is not None and jc is not None and f < T:
                d = YUP_TO_ZUP @ Vector((float(pj[f, jc, 0] - pj[f, jn, 0]),
                                         float(pj[f, jc, 1] - pj[f, jn, 1]),
                                         float(pj[f, jc, 2] - pj[f, jn, 2])))
                if d.length > 1e-9:
                    local = rest_arm.inverted() @ d
                    if local.length > 1e-9:
                        local.normalize()
                        q = Vector((0.0, 1.0, 0.0)).rotation_difference(local)
                        q.normalize()
                        basis3x3 = q.to_matrix()
                        rotations.setdefault(b, []).append((f, (q.w, q.x, q.y, q.z)))
            if basis3x3 is None:
                # Hueso del rig sin joint Kimodo (dedos, huesos propios del rig):
                # NO se emite canal. Antes se horneaba identidad, lo que metía
                # fcurves constantes que pisan la animación existente de esos
                # huesos al combinar tomas. Sí participa en la cadena.
                basis3x3 = Matrix.Identity(3)
            world[b] = _chained_world(b, rest3, parent, parent_world, basis3x3)

        if apply_root and f < T:
            posbl = YUP_TO_ZUP @ Vector((rp[f, 0], rp[f, 1], rp[f, 2]))
            if root_ref is None:
                root_ref = posbl.copy()
            # RELATIVO al primer frame, no absoluto: Kimodo canonicaliza su
            # root a XZ=(0,0) y una altura de cadera de ~1 m, que no tiene nada
            # que ver con dónde está el Hips del rig. Restando la posición de
            # reposo, el personaje se hundía 52 unidades bajo el suelo en el
            # frame 1 sobre un rig Mixamo de escala centimétrica.
            #
            # ESCALADO: Kimodo trabaja en metros; el rig puede estar en
            # centímetros (un Mixamo típico mide ~100 unidades). Sin el factor,
            # una caminata de 6.4 m avanzaba 6.4 unidades sobre un cuerpo de
            # 100 — el personaje caminaba en el sitio.
            h3 = rest3.get(root_bone, Matrix.Identity(3))
            loc = h3.inverted() @ ((posbl - root_ref) * root_scale)
            root.setdefault(root_bone, []).append((f, (loc.x, loc.y, loc.z)))

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
