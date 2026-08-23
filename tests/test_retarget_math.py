"""Tests de la lógica pura de retarget/common.py (calibración, espejo,
mp_to_arm y gate de outlier de QuatOneEuro).

Requiere mathutils (pip en Linux, o nativo dentro de Blender). En entornos sin
mathutils se salta (importorskip).
"""
import math
import sys
from pathlib import Path

import pytest

mathutils = pytest.importorskip("mathutils")
from mathutils import Matrix, Quaternion  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from puppet_mocap.retarget import common  # noqa: E402


def _neutral_landmarks():
    """Pose neutral en arm-space: up=+Z (hombros sobre caderas), lateral=+X."""
    lms = [[0.0, 0.0, 0.0, 1.0] for _ in range(33)]
    lms[11] = [0.5, -1.0, 0.0, 1.0]   # L shoulder → arm (0.5, 0, 1)
    lms[12] = [-0.5, -1.0, 0.0, 1.0]  # R shoulder → arm (-0.5, 0, 1)
    lms[23] = [0.5, 0.0, 0.0, 1.0]    # L hip → arm (0.5, 0, 0)
    lms[24] = [-0.5, 0.0, 0.0, 1.0]   # R hip → arm (-0.5, 0, 0)
    return lms


def _assert_close(a, b, tol=1e-5):
    assert abs(a - b) < tol, f"{a} != {b}"


def test_calibration_identity_on_neutral():
    R = common.compute_calibration([_neutral_landmarks()] * 10)
    assert R is not None
    diff = R - Matrix.Identity(3)
    for row in diff:
        for v in row:
            _assert_close(v, 0.0, 1e-4)


def test_mp_to_arm_convention():
    v = common.mp_to_arm([1.0, 2.0, 3.0])
    _assert_close(v.x, 1.0)
    _assert_close(v.y, 3.0)   # arm.y = mp.z
    _assert_close(v.z, -2.0)  # arm.z = -mp.y


def test_calibrate_pose_landmarks_roundtrip_identity():
    lm = [0.3, -0.4, 0.5, 0.9]
    out = common.calibrate_pose_landmarks([lm], Matrix.Identity(3))[0]
    _assert_close(out[0], 0.3)
    _assert_close(out[1], -0.4)
    _assert_close(out[2], 0.5)
    _assert_close(out[3], 0.9)  # visibilidad preservada


def test_double_mirror_pose_identity():
    lms = [[i * 0.1, i * 0.2, i * 0.3, 1.0] for i in range(33)]
    twice = common.mirror_pose_landmarks(common.mirror_pose_landmarks(lms))
    for a, b in zip(lms, twice):
        for x, y in zip(a, b):
            _assert_close(x, y, 1e-9)


def test_double_mirror_hand_identity():
    lms = [[i * 0.5, i * 0.25, i * 0.125] for i in range(21)]
    twice = common.mirror_hand_landmarks(common.mirror_hand_landmarks(lms))
    for a, b in zip(lms, twice):
        for x, y in zip(a, b):
            _assert_close(x, y, 1e-9)


def test_quat_one_euro_outlier_gate():
    f = common.QuatOneEuro()
    q0 = Quaternion((1.0, 0.0, 0.0, 0.0))
    q120 = Quaternion((0.5, math.sqrt(3) / 2, 0.0, 0.0))  # 120° (> 90°)

    assert f(q0, 0.0).dot(q0) > 0.99  # primer sample pasa
    r1 = f(q120, 0.03)  # salto 120° en 30 ms → rechazo
    r2 = f(q120, 0.06)  # 2º rechazo consecutivo
    assert r1.dot(q0) > 0.99
    assert r2.dot(q0) > 0.99
    r3 = f(q120, 0.09)  # 3º consecutivo → acepta
    assert r3.rotation_difference(q0).angle > 0.3  # ya se movió


def test_quat_one_euro_small_jump_not_rejected():
    f = common.QuatOneEuro()
    q0 = Quaternion((1.0, 0.0, 0.0, 0.0))
    f(q0, 0.0)
    q20 = Quaternion((math.cos(0.1), math.sin(0.1), 0.0, 0.0))  # 20°
    out = f(q20, 0.05)
    assert 1e-6 < out.rotation_difference(q0).angle < 0.2  # se mueve, no se atasca
