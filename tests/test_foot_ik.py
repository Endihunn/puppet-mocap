"""Tests unitarios del solucionador analítico puro de 2 huesos para piernas (solve_leg_ik).
"""
import math
import pytest
from mathutils import Matrix, Quaternion, Vector

from puppet_mocap.retarget.foot_plant import solve_leg_ik


def _setup_neutral_leg():
    # Pierna Mixamo estándar Z-up:
    # Hip en (0.1, 0, 1.0)
    # Knee en (0.1, 0, 0.5)  -> L1 = 0.5
    # Ankle en (0.1, 0, 0.1) -> L2 = 0.4
    H = Vector((0.1, 0.0, 1.0))
    K = Vector((0.1, 0.0, 0.5))
    A = Vector((0.1, 0.0, 0.1))
    L1 = 0.5
    L2 = 0.4
    hips_world = Matrix.Identity(3)
    # En rest pose Mixamo, el hueso apunta a lo largo de -Z en espacio armature
    # Representado por rest_3x3 ortonormal
    upleg_rest = Matrix.Identity(3)
    leg_rest = Matrix.Identity(3)
    foot_rest = Matrix.Identity(3)
    return H, K, A, L1, L2, hips_world, upleg_rest, leg_rest, foot_rest


def test_ik_reaches_target_in_plane():
    """Verifica que el solver alcanza un objetivo desplazado en el plano de flexión."""
    H, K, A, L1, L2, hips_world, u_rest, l_rest, f_rest = _setup_neutral_leg()
    target = A + Vector((0.0, 0.05, 0.02)) # Flexión hacia adelante

    sol = solve_leg_ik(
        hip_world=H,
        knee_world=K,
        ankle_world=A,
        target_ankle_world=target,
        pole_world=Vector((0.1, 0.5, 0.5)),
        l1=L1,
        l2=L2,
        hips_world_3x3=hips_world,
        upleg_rest_3x3=u_rest,
        leg_rest_3x3=l_rest,
        foot_rest_3x3=f_rest,
    )
    assert sol.reachable is True
    assert sol.residual < 1e-4
    err = (sol.achieved_ankle - target).length
    assert err < 1e-4, f"Error residual de IK: {err}"


def test_ik_reaches_target_out_of_plane():
    """Verifica que el solver alcanza un objetivo con desplazamiento lateral (abducción/aducción)."""
    H, K, A, L1, L2, hips_world, u_rest, l_rest, f_rest = _setup_neutral_leg()
    # Objetivo dentro del rango de alcance L1 + L2 = 0.90 m (elevado en Z para flexión)
    target = A + Vector((0.04, 0.03, 0.05))

    sol = solve_leg_ik(
        hip_world=H,
        knee_world=K,
        ankle_world=A,
        target_ankle_world=target,
        pole_world=Vector((0.1, 0.5, 0.5)),
        l1=L1,
        l2=L2,
        hips_world_3x3=hips_world,
        upleg_rest_3x3=u_rest,
        leg_rest_3x3=l_rest,
        foot_rest_3x3=f_rest,
    )
    assert sol.reachable is True
    err = (sol.achieved_ankle - target).length
    assert err < 1e-4, f"Error lateral de IK: {err}"


def test_ik_full_extension_straight_knee():
    """Verifica que cerca de la extensión completa (d ≈ L1 + L2) no hay singularidades ni NaN."""
    H, K, A, L1, L2, hips_world, u_rest, l_rest, f_rest = _setup_neutral_leg()
    target = H + Vector((0.0, 0.0, -(L1 + L2 - 0.001))) # Casi extensión completa (0.899 m)

    sol = solve_leg_ik(
        hip_world=H,
        knee_world=K,
        ankle_world=A,
        target_ankle_world=target,
        pole_world=Vector((0.1, 0.5, 0.5)),
        l1=L1,
        l2=L2,
        hips_world_3x3=hips_world,
        upleg_rest_3x3=u_rest,
        leg_rest_3x3=l_rest,
        foot_rest_3x3=f_rest,
    )
    assert sol.reachable is True
    assert math.isfinite(sol.q_upleg.w)
    assert math.isfinite(sol.q_leg.w)
    err = (sol.achieved_ankle - target).length
    assert err < 1e-3


def test_ik_unreachable_target_clamping():
    """Verifica que un objetivo fuera del alcance físico se acota suavemente sin producir NaN."""
    H, K, A, L1, L2, hips_world, u_rest, l_rest, f_rest = _setup_neutral_leg()
    target = H + Vector((0.0, 0.0, -1.5)) # 1.5 m supera L1 + L2 = 0.9 m

    sol = solve_leg_ik(
        hip_world=H,
        knee_world=K,
        ankle_world=A,
        target_ankle_world=target,
        pole_world=Vector((0.1, 0.5, 0.5)),
        l1=L1,
        l2=L2,
        hips_world_3x3=hips_world,
        upleg_rest_3x3=u_rest,
        leg_rest_3x3=l_rest,
        foot_rest_3x3=f_rest,
    )
    assert sol.reachable is False
    assert sol.residual > 0.5
    assert math.isfinite(sol.q_upleg.w)
    assert math.isfinite(sol.q_leg.w)
    # El tobillo alcanzado debe estar a d_max de la cadera
    achieved_dist = (sol.achieved_ankle - H).length
    assert abs(achieved_dist - (L1 + L2)) < 0.01
