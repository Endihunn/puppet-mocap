"""Retarget facial: blendshapes ARKit → shape keys del mesh.

MediaPipe Face Landmarker (con output_face_blendshapes=True) produce 52 blendshapes
con nombres ARKit estándar (eyeBlinkLeft, mouthSmileRight, jawOpen, ...).
Si el mesh tiene shape keys con esos mismos nombres (o con nombres equivalentes),
las manejamos directamente.
"""
from __future__ import annotations

import time

import bpy

from .. import log
from .common import _state, smooth_scalar

# Nombres ARKit de los 52 blendshapes (se incluye '_neutral' aunque no lo usamos)
ARKIT_SHAPES = (
    "_neutral",
    "browDownLeft", "browDownRight", "browInnerUp",
    "browOuterUpLeft", "browOuterUpRight",
    "cheekPuff", "cheekSquintLeft", "cheekSquintRight",
    "eyeBlinkLeft", "eyeBlinkRight",
    "eyeLookDownLeft", "eyeLookDownRight",
    "eyeLookInLeft", "eyeLookInRight",
    "eyeLookOutLeft", "eyeLookOutRight",
    "eyeLookUpLeft", "eyeLookUpRight",
    "eyeSquintLeft", "eyeSquintRight",
    "eyeWideLeft", "eyeWideRight",
    "jawForward", "jawLeft", "jawOpen", "jawRight",
    "mouthClose", "mouthDimpleLeft", "mouthDimpleRight",
    "mouthFrownLeft", "mouthFrownRight",
    "mouthFunnel",
    "mouthLeft", "mouthRight",
    "mouthLowerDownLeft", "mouthLowerDownRight",
    "mouthPressLeft", "mouthPressRight",
    "mouthPucker",
    "mouthRollLower", "mouthRollUpper",
    "mouthShrugLower", "mouthShrugUpper",
    "mouthSmileLeft", "mouthSmileRight",
    "mouthStretchLeft", "mouthStretchRight",
    "mouthUpperUpLeft", "mouthUpperUpRight",
    "noseSneerLeft", "noseSneerRight",
    "tongueOut",
)

ARKIT_SHAPES_LOWER = frozenset(s.lower() for s in ARKIT_SHAPES)

# TTL del cache NEGATIVO de find_face_mesh (P1-1): sin mesh, el panel
# redibujaba a ~50 Hz y cada redraw era un escaneo completo de la escena.
_FACE_MISS_TTL = 2.0


def _is_arkit_name(name: str) -> bool:
    return name in ARKIT_SHAPES or name.lower() in ARKIT_SHAPES_LOWER


def find_face_mesh(arm):
    """Busca un mesh con shape keys que sea hijo del armature, o que esté
    skinneado al armature. Si no hay hijos directos con shape keys, busca
    cualquier mesh con shape keys que use ARKit names."""
    if arm is None:
        return None
    # 1. Hijos directos (parented o con armature modifier apuntando a `arm`)
    candidates = []
    for obj in bpy.data.objects:
        if obj.type != "MESH" or obj.data.shape_keys is None:
            continue
        # parent armature
        if obj.parent == arm:
            candidates.append(obj)
            continue
        # armature modifier
        for mod in obj.modifiers:
            if mod.type == "ARMATURE" and mod.object == arm:
                candidates.append(obj)
                break

    if not candidates:
        # Fallback: cualquier mesh con shape keys que tenga al menos un nombre ARKit
        for obj in bpy.data.objects:
            if obj.type != "MESH" or obj.data.shape_keys is None:
                continue
            kb = obj.data.shape_keys.key_blocks
            if any(k.name in ARKIT_SHAPES for k in kb):
                return obj
        return None

    # Preferir el que tenga más nombres ARKit
    best = None
    best_score = -1
    for obj in candidates:
        kb = obj.data.shape_keys.key_blocks
        score = sum(1 for k in kb if k.name in ARKIT_SHAPES)
        if score > best_score:
            best_score = score
            best = obj
    return best


def get_cached_mesh(arm):
    """Mesh facial cacheado en _state — find_face_mesh escanea TODOS los
    objetos del archivo, hacerlo por frame costaba ms en escenas grandes.
    Se invalida en reset_smoothing() (cada start/reset). El FALLO también se
    cachea con TTL (P1-1)."""
    name = _state.get("face_mesh_name")
    if name is not None:
        obj = bpy.data.objects.get(name)
        if obj is not None and obj.type == "MESH" and obj.data.shape_keys:
            return obj
        _state["face_mesh_name"] = None
        _state["face_key_map"] = None
    last_miss = _state.get("face_mesh_miss_t", 0.0)
    now = time.monotonic()
    if last_miss and now - last_miss < _FACE_MISS_TTL:
        return None
    mesh = find_face_mesh(arm)
    if mesh is not None:
        _state["face_mesh_name"] = mesh.name
        _state["face_key_map"] = None
        _state["face_mesh_miss_t"] = 0.0
    else:
        _state["face_mesh_miss_t"] = now
    return mesh


def _key_map(mesh) -> dict:
    """{arkit_name_lower: nombre real del shape key} — resuelto una vez."""
    m = _state.get("face_key_map")
    if m is not None:
        return m
    m = {}
    for k in mesh.data.shape_keys.key_blocks:
        low = k.name.lower()
        if low in ARKIT_SHAPES_LOWER:
            m[low] = k.name
    _state["face_key_map"] = m
    return m


def apply(arm, face_data) -> int:
    """face_data: dict con clave 'blendshapes' = {arkit_name: float 0..1}."""
    if not face_data:
        return 0
    blendshapes = face_data.get("blendshapes")
    if not blendshapes:
        return 0

    mesh = get_cached_mesh(arm)
    if mesh is None:
        # No spammear el log: avisar una vez por sesión
        if not _state.get("face_warned"):
            _state["face_warned"] = True
            log.warn("no encontré mesh con shape keys ARKit para captura facial")
        return 0
    kb = mesh.data.shape_keys.key_blocks
    kmap = _key_map(mesh)

    moved = 0
    for name, score in blendshapes.items():
        real = kmap.get(name.lower())
        if real is None:
            continue
        sk = kb.get(real)
        if sk is None:
            continue
        try:
            v = float(score)
        except (TypeError, ValueError):
            continue
        v = max(0.0, min(1.0, v))
        sk.value = smooth_scalar(f"face::{real}", v)
        moved += 1
    return moved


def snapshot_values(arm) -> dict:
    """{nombre_shape_key: valor actual} de los keys ARKit — para el buffer de
    grabación (bake al detener)."""
    mesh = get_cached_mesh(arm)
    if mesh is None:
        return {}
    kb = mesh.data.shape_keys.key_blocks
    out = {}
    for real in _key_map(mesh).values():
        sk = kb.get(real)
        if sk is not None:
            out[real] = sk.value
    return out


def shape_key_names_in_mesh(arm, mesh=None) -> list[str]:
    """Nombres de los shape keys ARKit presentes en el mesh."""
    mesh = mesh if mesh is not None else get_cached_mesh(arm)
    if mesh is None:
        return []
    return list(_key_map(mesh).values())


def zero_values(arm):
    """Regresa los shape keys ARKit a 0 (sin tocar animación)."""
    mesh = get_cached_mesh(arm)
    if mesh is None:
        return
    kb = mesh.data.shape_keys.key_blocks
    for real in _key_map(mesh).values():
        sk = kb.get(real)
        if sk is not None:
            sk.value = 0.0


def clear_face_animation(arm):
    """Borra la action de shape keys y regresa los valores ARKit a 0."""
    mesh = get_cached_mesh(arm)
    if mesh is None:
        return
    sks = mesh.data.shape_keys
    if sks.animation_data and sks.animation_data.action:
        bpy.data.actions.remove(sks.animation_data.action, do_unlink=True)
    zero_values(arm)
