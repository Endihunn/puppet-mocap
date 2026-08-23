"""Tests del conversor Kimodo → Mixamo (K2-T1).

Corren bajo el Python de Blender (mathutils + numpy reales). Fuera de Blender
se saltan con importorskip (mathutils no tiene wheel de Windows).
Reportar POR EJE, nunca por magnitud (lo que enmascaró el bug de T1).
"""
import math
import pathlib
import sys

import pytest

np = pytest.importorskip("numpy")
mathutils = pytest.importorskip("mathutils")
from mathutils import Matrix, Quaternion, Vector  # noqa: E402

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from puppet_mocap.kimodo.convert import (  # noqa: E402
    YUP_TO_ZUP, convert_motion, mapping_for)

ZUP = YUP_TO_ZUP


def _assert_close(a, b, tol=1e-5):
    assert abs(a - b) < tol, f"{a} != {b}"


def test_yup_to_zup_axes():
    # Kimodo → Blender: +Y(up)→+Z, +Z(forward)→−Y, +X(left)→+X
    expect = {
        (1, 0, 0): (1, 0, 0),   # X = left
        (0, 1, 0): (0, 0, 1),   # Y = up
        (0, 0, 1): (0, -1, 0),  # Z = forward
    }
    for k, e in expect.items():
        v = ZUP @ Vector(k)
        assert (v - Vector(e)).length < 1e-6


def test_yup_to_zup_rotation_axis():
    # rot 90° sobre Kimodo +Y (up) → rot 90° sobre Blender +Z (up)
    R_k = Matrix.Rotation(math.pi / 2, 3, "Y")
    R_b = ZUP @ R_k @ ZUP.transposed()
    axis = R_b.to_quaternion().axis
    assert abs(axis.z) > 0.99   # gira sobre +Z (up), no sobre X/Y


def test_identity_kimodo_gives_rest_mixamo():
    # Cadena sintética Hips→LeftArm con rest matrices NO triviales.
    rest3 = {
        "Hips": Matrix.Rotation(math.radians(20), 3, "X"),
        "LeftArm": Matrix.Rotation(math.radians(-30), 3, "Y")
                   @ Matrix.Rotation(math.radians(10), 3, "Z"),
    }
    rest_pos = {"Hips": Vector((0, 0, 0)), "LeftArm": Vector((0, 1, 0))}
    parent_of = {"Hips": None, "LeftArm": "Hips"}
    joint_order = ["Hips", "LeftArm"]
    mapping = {"Hips": "Hips", "LeftArm": "LeftArm"}

    # Kimodo global_rot_mats que reproduzcan la rest Mixamo (en mundo, Z-up):
    #   r_k = YUP^T @ rest3[bone] @ YUP
    T = 3
    g = np.zeros((T, 2, 3, 3))
    rp = np.zeros((T, 3))
    for f in range(T):
        for i, jname in enumerate(joint_order):
            bone = mapping[jname]
            g[f, i] = ZUP.transposed() @ rest3[bone] @ ZUP
    motion = {"global_rot_mats": g, "root_positions": rp, "fps": 30.0}

    rot, _root = convert_motion(motion, joint_order, rest3, rest_pos,
                                parent_of, mapping, fps=30.0)

    # Con el motion correcto, TODOS los huesos deben quedar en rest (basis=identity)
    for bone, pts in rot.items():
        for f, (w, x, y, z) in pts:
            q = Quaternion((w, x, y, z))
            if q.w < 0:
                q = Quaternion((-q.w, -q.x, -q.y, -q.z))
            assert q.angle < 1e-4, f"{bone} frame {f} no quedó en rest (angle={q.angle})"


def test_root_yup_to_zup_and_rest_basis():
    # Hips.location está en el REST BASIS del hueso (bug T1). Con rest3[Hips]=I
    # y head en el origen, un root en Kimodo (0,0.9,0) debe dar location=(0,0,0.9)
    # (Y-up → Z-up) — POR EJE, no magnitud.
    rest3 = {"Hips": Matrix.Identity(3)}
    rest_pos = {"Hips": Vector((0, 0, 0))}
    parent_of = {"Hips": None}
    g = np.eye(3).reshape(1, 1, 3, 3)
    rp = np.array([[0.0, 0.9, 0.0]])
    motion = {"global_rot_mats": g, "root_positions": rp, "fps": 30.0}
    _rot, root = convert_motion(motion, ["Hips"], rest3, rest_pos,
                                parent_of, {"Hips": "Hips"}, fps=30.0)
    loc = root["Hips"][0][1]
    _assert_close(loc[0], 0.0, 1e-5)   # X
    _assert_close(loc[1], 0.0, 1e-5)   # Y (armature)
    _assert_close(loc[2], 0.9, 1e-5)   # Z (up) — Kimodo +Y → Blender +Z


def test_mapping_for_joint_counts():
    assert len(mapping_for(77)) == 77 - 25  # descarta cara/neck2/falanges extra
    # cada bone del mapeo es un hueso Mixamo válido (lista de body+hands)
    assert "Hips" in mapping_for(77).values()
    assert len(mapping_for(22)) == 22
