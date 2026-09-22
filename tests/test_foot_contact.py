"""Tests unitarios de contacto, suelo y máquina de estados de pies (FootStateMachine).
"""
import pytest
from mathutils import Vector

from puppet_mocap.retarget.foot_plant import (
    FootContactPatch,
    GroundModel,
    FootStateMachine,
    SWING,
    CANDIDATE,
    LOCKED,
    RELEASING,
)


def _make_patch(mid_z=0.0, vel_z=0.0, sole_up=(0, 0, 1)):
    heel = Vector((-0.05, 0.0, mid_z))
    toe = Vector((0.15, 0.0, mid_z))
    mid = (heel + toe) * 0.5
    normal = Vector(sole_up).normalized()
    ankle = Vector((0.0, 0.0, mid_z + 0.08))
    return FootContactPatch(heel=heel, toe=toe, mid=mid, normal=normal, length=0.20, ankle=ankle)


def test_foot_contact_patch_properties():
    patch = _make_patch(mid_z=0.02)
    assert abs(patch.mid.z - 0.02) < 1e-5
    assert abs(patch.length - 0.20) < 1e-5
    assert patch.normal.z > 0.9


def test_swing_immunity_high_above_ground():
    """Un pie a 8 cm del suelo nunca debe entrar en LOCKED aunque esté quieto."""
    sm = FootStateMachine(candidate_duration=0.10)
    ground = GroundModel(mode="PLANE_Z", ground_z=0.0)

    t = 0.0
    patch = _make_patch(mid_z=0.08) # 8 cm del piso

    for step in range(10):
        t += 0.033
        state = sm.update(patch, ground, t_now=t, visibility=0.9)
        assert state == SWING, f"A 8 cm del suelo debe ser SWING pero fue {state}"
    assert sm.blend_weight == 0.0


def test_stationary_foot_enters_locked_after_duration():
    """Un pie quieto a ras de suelo debe entrar en LOCKED tras el tiempo mínimo (80-120 ms)."""
    sm = FootStateMachine(candidate_duration=0.10)
    ground = GroundModel(mode="PLANE_Z", ground_z=0.0)

    # Frame 1: reposo en z=0.01 (1 cm)
    patch = _make_patch(mid_z=0.01)
    state1 = sm.update(patch, ground, t_now=0.033, visibility=0.9)
    assert state1 == CANDIDATE

    # Tras 66 ms
    state2 = sm.update(patch, ground, t_now=0.066, visibility=0.9)
    assert state2 == CANDIDATE

    # Tras 99 ms
    state3 = sm.update(patch, ground, t_now=0.099, visibility=0.9)
    assert state3 == CANDIDATE

    # Tras 132 ms (supera 100 ms)
    state4 = sm.update(patch, ground, t_now=0.132, visibility=0.9)
    assert state4 == LOCKED
    assert sm.blend_weight == 1.0
    assert sm.anchor_mid is not None


def test_fps_invariance_20_30_60_fps():
    """La misma trayectoria física debe transicionar a LOCKED en el mismo tiempo
    absoluto independientemente de si la tasa es 20, 30 o 60 FPS."""
    durations = {}
    for fps in (20, 30, 60):
        dt = 1.0 / fps
        sm = FootStateMachine(candidate_duration=0.10)
        ground = GroundModel(mode="PLANE_Z", ground_z=0.0)
        patch = _make_patch(mid_z=0.005)

        t = 0.0
        locked_t = None
        for _ in range(30):
            t += dt
            st = sm.update(patch, ground, t_now=t, visibility=0.9)
            if st == LOCKED and locked_t is None:
                locked_t = t
                break
        assert locked_t is not None, f"Fallo al bloquearse a {fps} FPS"
        durations[fps] = locked_t

    # Todos deben consolidar entre 0.10 y 0.15 segundos
    for fps, t_lock in durations.items():
        assert 0.09 <= t_lock <= 0.16, f"A {fps} FPS el bloqueo tardó {t_lock}s"


def test_tracking_loss_grace_period():
    """Una pérdida de visibilidad de 1-3 frames retiene el ancla; una pérdida prolongada libera."""
    sm = FootStateMachine(candidate_duration=0.10, grace_duration=0.12)
    ground = GroundModel(mode="PLANE_Z", ground_z=0.0)
    patch = _make_patch(mid_z=0.005)

    # Entrar a LOCKED
    t = 0.0
    for _ in range(5):
        t += 0.033
        sm.update(patch, ground, t_now=t, visibility=0.9)
    assert sm.state == LOCKED
    assert sm.blend_weight == 1.0

    # Pérdida de tracking por 2 frames (~66 ms < 120 ms)
    t += 0.033
    st_loss1 = sm.update(patch, ground, t_now=t, visibility=0.1)
    assert st_loss1 == LOCKED
    assert sm.blend_weight == 1.0

    t += 0.033
    st_loss2 = sm.update(patch, ground, t_now=t, visibility=0.1)
    assert st_loss2 == LOCKED
    assert sm.blend_weight == 1.0

    # Pérdida prolongada (> 120 ms): debe pasar a RELEASING
    for _ in range(4):
        t += 0.033
        sm.update(patch, ground, t_now=t, visibility=0.1)
    assert sm.state == RELEASING


def test_intentional_liftoff_immediate_release():
    """Un despegue intencional con velocidad vertical positiva y altura creciente
    debe liberar inmediatamente sin reanclarse en el aire."""
    sm = FootStateMachine(candidate_duration=0.10)
    ground = GroundModel(mode="PLANE_Z", ground_z=0.0)

    # Bloquear
    t = 0.0
    for _ in range(5):
        t += 0.033
        sm.update(_make_patch(mid_z=0.005), ground, t_now=t, visibility=0.9)
    assert sm.state == LOCKED

    # Despegue rápido hacia arriba
    t += 0.033
    patch_lift = _make_patch(mid_z=0.06) # salto a 6 cm
    st_lift = sm.update(patch_lift, ground, t_now=t, visibility=0.9)
    assert st_lift in (RELEASING, SWING), f"Despegue debe liberar inmediatamente, no retener LOCKED: {st_lift}"
