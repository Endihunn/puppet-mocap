"""Postproceso de una toma ya horneada: aterrizar y clavar el pie de apoyo.

Portado de gvhmr-mocap, donde se midió cada defecto antes y después:
  - aterrizar : pie más bajo -0.449 -> +0.065 (la fuente daba +0.061)
  - clavar pie: patinaje 0.192 -> 0.034 u (la fuente 0.039)

Ambos operan sobre las keys de `Hips.location` con la action asignada, para
que Blender haga la FK. Necesita bpy.
"""
from __future__ import annotations

import bpy
from mathutils import Vector

FOOT_SUFFIXES = ("LeftToeBase", "RightToeBase", "LeftFoot", "RightFoot")


def _root_fcurves(action, arm, root_bone: str):
    """fcurves de `root_bone.location` de la action (API de slotted actions)."""
    esc = bpy.utils.escape_identifier(root_bone)
    path = f'pose.bones["{esc}"].location'
    slot = None
    ad = arm.animation_data
    if ad is not None and ad.action is action:
        slot = ad.action_slot
    out = []
    for layer in action.layers:
        for strip in layer.strips:
            bags = getattr(strip, "channelbags", None)
            if bags is None:
                bag = strip.channelbag(slot) if slot is not None else None
                bags = [bag] if bag is not None else []
            for cb in bags:
                for fc in cb.fcurves:
                    if fc.data_path == path and fc.array_index in (0, 1, 2):
                        out.append(fc)
    return out


def _shift_keys(fc, delta_by_frame, frame_start, n):
    """Suma un delta (constante o por frame) a las keys de una fcurve."""
    i_ax = fc.array_index
    for kp in fc.keyframe_points:
        if callable(delta_by_frame):
            i = int(round(kp.co[0])) - frame_start
            if not (0 <= i < n):
                continue
            d = delta_by_frame(i)[i_ax]
        else:
            d = delta_by_frame[i_ax]
        kp.co[1] += d
        kp.handle_left[1] += d
        kp.handle_right[1] += d
    fc.update()


# --- aterrizar -------------------------------------------------------------

def ground_offset(arm, prefix: str, frame_start: int, n_frames: int,
                  samples: int = 40) -> float:
    """Z (espacio de armature) a SUMAR para que el pie más bajo toque el suelo.

    La traslación de raíz es relativa al primer frame, así que se descarta la
    altura absoluta de la fuente y el personaje se queda a la altura de rest de
    su Hips. En rigs con el Hips casi en el origen eso lo deja enterrado.
    Requiere la action ya asignada (Blender hace la FK).
    """
    bones = [arm.pose.bones.get(f"{prefix}{s}") for s in FOOT_SUFFIXES]
    bones = [b for b in bones if b is not None]
    if not bones:
        return 0.0
    scene = bpy.context.scene
    prev = scene.frame_current
    step = max(1, n_frames // max(1, samples))
    lowest = None
    try:
        for f in range(frame_start, frame_start + n_frames, step):
            scene.frame_set(f)
            for pb in bones:
                for pt in (pb.head, pb.tail):
                    if lowest is None or pt.z < lowest:
                        lowest = pt.z
    finally:
        scene.frame_set(prev)
    return 0.0 if lowest is None else -lowest


def apply_ground_offset(action, arm, root_bone: str, rest3_root, offset_z: float) -> bool:
    if abs(offset_z) < 1e-6:
        return False
    try:
        delta = rest3_root.inverted() @ Vector((0.0, 0.0, offset_z))
    except ValueError:
        return False
    fcurves = _root_fcurves(action, arm, root_bone)
    for fc in fcurves:
        _shift_keys(fc, delta, 0, 0)
    return bool(fcurves)


# --- clavar el pie de apoyo ------------------------------------------------

def contacts_from_source(posed_joints, joint_names, foot_joints, pct: float = 30.0):
    """Deriva el contacto por VELOCIDAD del pie en la fuente.

    Kimodo trae un `foot_contacts` booleano, pero sus 6 canales no están
    documentados (dos son constantes en el fixture), así que no se usa: la
    velocidad es independiente del formato y es lo mismo que hace
    `get_static_joint_mask` de GVHMR (velocidad < umbral).

    Devuelve {nombre_joint: array bool (T,)}.
    """
    import numpy as np
    idx = {n: i for i, n in enumerate(joint_names)}
    out = {}
    for jn in foot_joints:
        j = idx.get(jn)
        if j is None:
            continue
        p = np.asarray(posed_joints[:, j], dtype=np.float64)
        vel = np.linalg.norm(np.diff(p, axis=0), axis=-1)
        vel = np.concatenate([vel[:1], vel])           # (T,)
        thr = float(np.percentile(vel, pct))
        out[jn] = vel <= max(thr, 1e-6)
    return out


def foot_lock(action, arm, prefix: str, root_bone: str, rest3_root,
              contacts: dict, bone_of_joint: dict,
              frame_start: int, n_frames: int) -> float:
    """Cancela el patinaje del pie en contacto moviendo la cadera (FK puro).

    El retarget conserva las DIRECCIONES de la fuente pero usa las LONGITUDES
    del rig, así que el pie no cae donde la fuente lo clavaba. Mientras un pie
    está en contacto se exige pos[i] == pos[i-1] y la corrección se acumula
    sobre `Hips.location`; como mover la cadera traslada el cuerpo rígidamente,
    basta una pasada de medición.

    contacts: {joint_name: array bool (T,)}
    bone_of_joint: {joint_name: nombre del hueso Mixamo SIN prefijo}
    """
    pairs = []
    for jn, mask in contacts.items():
        bn = bone_of_joint.get(jn)
        pb = arm.pose.bones.get(f"{prefix}{bn}") if bn else None
        if pb is not None:
            pairs.append((jn, mask, pb))
    if not pairs:
        return 0.0

    scene = bpy.context.scene
    prev = scene.frame_current
    n = n_frames
    pos = []
    try:
        for i in range(n):
            scene.frame_set(frame_start + i)
            pos.append({jn: pb.head.copy() for jn, _, pb in pairs})
    finally:
        scene.frame_set(prev)

    corr = [Vector((0.0, 0.0, 0.0)) for _ in range(n)]
    for i in range(1, n):
        deltas = [pos[i][jn] - pos[i - 1][jn]
                  for jn, mask, _ in pairs
                  if i < len(mask) and mask[i] and mask[i - 1]]
        c = corr[i - 1].copy()
        if deltas:
            avg = sum(deltas, Vector()) / len(deltas)
            c -= Vector((avg.x, avg.y, 0.0))   # la altura la maneja ground_offset
        corr[i] = c

    if max(v.length for v in corr) < 1e-6:
        return 0.0
    try:
        inv = rest3_root.inverted()
    except ValueError:
        return 0.0
    local = [inv @ c for c in corr]
    for fc in _root_fcurves(action, arm, root_bone):
        _shift_keys(fc, lambda i: local[i], frame_start, n)
    return float(corr[-1].length)
