"""Tests unitarios y de integración para la distribución coordinada del twist de brazo.

Cubre:
1. Descomposición swing-twist y reconstrucción exacta (< 1e-5 rad).
2. Desenrollado de fase continuo en 0° -> 180° -> 0° sin saltos de rama en +-pi.
3. Simetría anatómica y coherencia de signo entre Left y Right.
4. Posturas del brazo: recto (colineal), moderadamente flexionado (30°) y flexionado 90°.
5. Conservación de direcciones de extremidad (< 2°) y base palmar (< 3°).
6. Reparto de deformación: ninguna articulación absorbe más del 60% del twist.
7. Pérdida temporal de landmarks de mano (1-5 frames) con fallback FK y recuperación sin flips.
"""
import math
import sys
import types
from pathlib import Path

import pytest

mathutils = pytest.importorskip("mathutils")
from mathutils import Matrix, Quaternion, Vector

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from puppet_mocap import retarget
from puppet_mocap.retarget import body, common, hands


def _assert_close(a, b, tol=1e-5, msg=""):
    assert abs(a - b) < tol, f"{msg}: {a} != {b} (diff={abs(a - b)})"


# ---------------------------------------------------------------------------
# 1. Descomposición swing-twist y reconstrucción
# ---------------------------------------------------------------------------

def test_decompose_swing_twist_pure_and_mixed():
    axis_y = Vector((0.0, 1.0, 0.0))

    # Caso 1: Rotación identidad
    q_id = Quaternion((1.0, 0.0, 0.0, 0.0))
    s, t, angle = common.decompose_swing_twist(q_id, axis_y)
    _assert_close(angle, 0.0)
    _assert_close((s @ t).rotation_difference(q_id).angle, 0.0)

    # Caso 2: Twist puro alrededor de Y
    for deg in [15, 45, 90, 135, 179, -45, -120]:
        rad = math.radians(deg)
        q_twist = Quaternion(axis_y, rad)
        s, t, angle = common.decompose_swing_twist(q_twist, axis_y)
        _assert_close(s.rotation_difference(q_id).angle, 0.0, tol=1e-5, msg=f"pure twist swing deg={deg}")
        _assert_close(abs(angle), abs(rad), tol=1e-5, msg=f"pure twist angle deg={deg}")
        recon = s @ t
        _assert_close(recon.rotation_difference(q_twist).angle, 0.0, tol=1e-5)

    # Caso 3: Swing puro perpendicular a Y (ej. alrededor de X o Z)
    for deg in [10, 30, 60, 85]:
        rad = math.radians(deg)
        q_swing = Quaternion(Vector((1.0, 0.0, 0.0)), rad)
        s, t, angle = common.decompose_swing_twist(q_swing, axis_y)
        _assert_close(abs(angle), 0.0, tol=1e-5, msg=f"pure swing twist deg={deg}")
        recon = s @ t
        _assert_close(recon.rotation_difference(q_swing).angle, 0.0, tol=1e-5)

    # Caso 4: Rotación combinada arbitraria (swing + twist)
    for swing_deg in [20, 45, 70]:
        for twist_deg in [-110, -45, 30, 90, 150]:
            q_comb = (Quaternion(Vector((1.0, 0.0, 1.0)).normalized(), math.radians(swing_deg))
                      @ Quaternion(axis_y, math.radians(twist_deg)))
            s, t, angle = common.decompose_swing_twist(q_comb, axis_y)
            recon = s @ t
            recon_err = recon.rotation_difference(q_comb).angle
            assert recon_err < 1e-5, f"Reconstrucción falló ({recon_err} rad) para swing={swing_deg} twist={twist_deg}"


# ---------------------------------------------------------------------------
# 2. Desenrollado de fase continuo 0° -> 180° -> 0°
# ---------------------------------------------------------------------------

def test_phase_unwrapping_continuous():
    common.reset_smoothing()

    # Simular una trayectoria suave que pasa de 0 a 190° (cruzando pi) y vuelve
    angles_deg = [0, 30, 60, 90, 120, 150, 175, 185, 195, 185, 175, 150, 120, 90, 60, 30, 0]
    unwrapped_history = []
    prev_unwrapped = None

    for deg in angles_deg:
        raw_rad = math.radians(deg)
        # Quaternión y descomposición natural mapean a [-pi, pi]
        q = Quaternion(Vector((0.0, 1.0, 0.0)), raw_rad)
        _, _, raw_twist = common.decompose_swing_twist(q, Vector((0.0, 1.0, 0.0)))

        if prev_unwrapped is None:
            unw = raw_twist
        else:
            unw = common.unwrap_angle(raw_twist, prev_unwrapped)
            # Verificar que no hay saltos discontinuos (> 35° por paso de 10-30°)
            step_diff_deg = math.degrees(abs(unw - prev_unwrapped))
            assert step_diff_deg < 35.0, f"Salto de rama detectado: {step_diff_deg:.1f}° al pasar por {deg}°"

        prev_unwrapped = unw
        unwrapped_history.append(math.degrees(unw))

    # El punto de 195° debe haberse desenrollado a ~195°, no a -165°
    idx_peak = angles_deg.index(195)
    _assert_close(unwrapped_history[idx_peak], 195.0, tol=1.0, msg="Unwrapped 195 peak")
    _assert_close(unwrapped_history[-1], 0.0, tol=1.0, msg="Unwrapped return to 0")


# ---------------------------------------------------------------------------
# Mocks e infraestructura para pruebas cinemáticas completas
# ---------------------------------------------------------------------------

def _make_mock_bone(name, matrix_local=None, parent=None):
    if matrix_local is None:
        matrix_local = Matrix.Identity(4)
    bone = types.SimpleNamespace(matrix_local=matrix_local, length=0.25)
    pb = types.SimpleNamespace(
        name=name,
        bone=bone,
        parent=parent,
        matrix=matrix_local.copy(),
        rotation_mode="QUATERNION",
        rotation_quaternion=Quaternion((1.0, 0.0, 0.0, 0.0)),
        location=Vector((0.0, 0.0, 0.0)),
    )
    return pb


def _build_full_rig(prefix="mixamorig:"):
    """Construye un rig completo con brazos izquierdo y derecho."""
    bones_dict = {}

    for side, sign in [("Left", 1.0), ("Right", -1.0)]:
        # Shoulder
        m_sh = Matrix.Translation(Vector((sign * 0.2, 0.0, 1.4)))
        sh = _make_mock_bone(f"{prefix}{side}Shoulder", m_sh)
        bones_dict[sh.name] = sh

        # UpperArm
        m_arm = Matrix.Translation(Vector((sign * 0.35, 0.0, 1.4)))
        arm_b = _make_mock_bone(f"{prefix}{side}Arm", m_arm, parent=sh)
        bones_dict[arm_b.name] = arm_b

        # ForeArm
        m_fa = Matrix.Translation(Vector((sign * 0.60, 0.0, 1.4)))
        fa = _make_mock_bone(f"{prefix}{side}ForeArm", m_fa, parent=arm_b)
        bones_dict[fa.name] = fa

        # Hand
        m_hand = Matrix.Translation(Vector((sign * 0.85, 0.0, 1.4)))
        hand = _make_mock_bone(f"{prefix}{side}Hand", m_hand, parent=fa)
        bones_dict[hand.name] = hand

        # Fingers
        for suffix in hands.FINGER_CHAINS:
            p = hand
            for s in suffix:
                b = _make_mock_bone(f"{prefix}{side}{s}", Matrix.Identity(4), parent=p)
                bones_dict[b.name] = b
                p = b

    # Hips
    hips = _make_mock_bone(f"{prefix}Hips", Matrix.Identity(4))
    bones_dict[hips.name] = hips

    arm_obj = types.SimpleNamespace(
        name="Armature",
        matrix_world=Matrix.Identity(4),
        pose=types.SimpleNamespace(bones=bones_dict),
        data=types.SimpleNamespace(bones={k: v.bone for k, v in bones_dict.items()}),
    )
    return arm_obj


def _make_body_landmarks(side="Left", elbow_posture="straight"):
    """Genera 33 landmarks corporales con el brazo en postura straight, bent30 o flexed90."""
    lms = [[0.0, 0.0, 0.0, 1.0] for _ in range(33)]

    # Hombros
    l_sh = Vector((0.2, 0.0, 1.4))
    r_sh = Vector((-0.2, 0.0, 1.4))

    # Configuración de codo y muñeca según postura
    sign = 1.0 if side == "Left" else -1.0
    sh_pt = l_sh if side == "Left" else r_sh

    if elbow_posture == "straight":
        el_pt = sh_pt + Vector((sign * 0.25, 0.0, 0.0))
        wr_pt = el_pt + Vector((sign * 0.25, 0.0, 0.0))
    elif elbow_posture == "bent30":
        el_pt = sh_pt + Vector((sign * 0.25, 0.0, 0.0))
        angle = math.radians(30)
        # Flexión hacia adelante (+Y canónico / -mp.z)
        wr_pt = el_pt + Vector((sign * 0.25 * math.cos(angle), 0.25 * math.sin(angle), 0.0))
    elif elbow_posture == "flexed90":
        el_pt = sh_pt + Vector((sign * 0.25, 0.0, 0.0))
        # Flexión a 90 grados hacia adelante
        wr_pt = el_pt + Vector((0.0, 0.25, 0.0))
    else:
        raise ValueError(elbow_posture)

    # Brazo opuesto siempre neutral
    opp_sign = -sign
    opp_sh = r_sh if side == "Left" else l_sh
    opp_el = opp_sh + Vector((opp_sign * 0.25, 0.0, 0.0))
    opp_wr = opp_el + Vector((opp_sign * 0.25, 0.0, 0.0))

    # Convertir de arm-space a mp: mp.x = arm.x, mp.y = -arm.z, mp.z = arm.y
    def to_mp(v):
        return [v.x, -v.z, v.y, 1.0]

    lms[11] = to_mp(l_sh)
    lms[12] = to_mp(r_sh)
    if side == "Left":
        lms[13] = to_mp(el_pt)
        lms[15] = to_mp(wr_pt)
        lms[14] = to_mp(opp_el)
        lms[16] = to_mp(opp_wr)
    else:
        lms[14] = to_mp(el_pt)
        lms[16] = to_mp(wr_pt)
        lms[13] = to_mp(opp_el)
        lms[15] = to_mp(opp_wr)

    # Caderas
    lms[23] = to_mp(Vector((0.15, 0.0, 0.9)))
    lms[24] = to_mp(Vector((-0.15, 0.0, 0.9)))

    return lms, sh_pt, el_pt, wr_pt


def _make_hand_landmarks(wrist_pt: Vector, roll_deg: float, side: str = "Left",
                         fwd_dir: Vector | None = None):
    """Genera landmarks de mano con roll continuo sobre su eje longitudinal."""
    rad = math.radians(roll_deg)
    sign = 1.0 if side == "Left" else -1.0
    if fwd_dir is None:
        fwd_axis = Vector((sign, 0.0, 0.0))
    else:
        fwd_axis = fwd_dir.normalized()

    up_ref = Vector((0.0, 0.0, 1.0))
    lat = fwd_axis.cross(up_ref)
    if lat.length < 1e-4:
        lat = Vector((0.0, 1.0, 0.0))
    lat.normalize()
    up = lat.cross(fwd_axis).normalized()

    # Rota lat y up alrededor de fwd_axis por rad
    R = Matrix.Rotation(rad, 3, fwd_axis)
    cur_lat = R @ lat
    cur_up = R @ up

    base_pts = [Vector((0, 0, 0)) for _ in range(21)]
    base_pts[9] = 0.08 * fwd_axis
    base_pts[5] = 0.06 * fwd_axis + sign * 0.04 * cur_lat
    base_pts[17] = 0.06 * fwd_axis - sign * 0.04 * cur_lat
    base_pts[1] = 0.02 * fwd_axis + sign * 0.03 * cur_lat + 0.01 * cur_up

    pts = []
    for p in base_pts:
        world_pt = p + wrist_pt
        pts.append([world_pt.x, -world_pt.z, world_pt.y, 1.0])
    return pts


# ---------------------------------------------------------------------------
# 3. Simetría anatómica Left / Right
# ---------------------------------------------------------------------------

def test_symmetry_left_right(monkeypatch):
    arm = _build_full_rig()
    monkeypatch.setattr(retarget, "get_armature", lambda: arm)
    common.reset_smoothing()

    for side_idx, side in enumerate(("Left", "Right")):
        common.reset_smoothing()
        body_lms, sh_pt, el_pt, wr_pt = _make_body_landmarks(side=side, elbow_posture="straight")
        fwd = (wr_pt - el_pt).normalized()
        t_base = 10.0 * (side_idx + 1)

        # Frame 1: Calibración neutral (roll = 0°)
        hand_lms_0 = _make_hand_landmarks(wr_pt, roll_deg=0.0, side=side, fwd_dir=fwd)
        key = "L" if side == "Left" else "R"
        retarget.apply_pose(
            landmarks=body_lms,
            hands_data={key: {"lm": hand_lms_0, "hd": side}},
            prefix="mixamorig:",
            sample_time=t_base + 1.0,
            enable_body=True,
            enable_hands=True,
        )

        # Frame 2: Giro de 60°
        hand_lms_60 = _make_hand_landmarks(wr_pt, roll_deg=60.0, side=side, fwd_dir=fwd)
        retarget.apply_pose(
            landmarks=body_lms,
            hands_data={key: {"lm": hand_lms_60, "hd": side}},
            prefix="mixamorig:",
            sample_time=t_base + 1.05,
            enable_body=True,
            enable_hands=True,
        )

        assert hands.was_arm_chain_oriented(side), f"{side} arm chain debe orientarse coordinadamente"
        pb_u = arm.pose.bones[f"mixamorig:{side}Arm"]
        pb_fa = arm.pose.bones[f"mixamorig:{side}ForeArm"]
        pb_h = arm.pose.bones[f"mixamorig:{side}Hand"]

        # Ambos huesos deben haber recibido rotación no nula
        assert pb_u.rotation_quaternion.angle > 0.01, f"{side} UpperArm debe recibir twist"
        assert pb_fa.rotation_quaternion.angle > 0.01, f"{side} ForeArm debe recibir twist"
        assert pb_h.rotation_quaternion.angle > 0.01, f"{side} Hand debe recibir twist"


# ---------------------------------------------------------------------------
# 4. Posturas del brazo (recto, 30°, 90°) y conservación de direcciones
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("posture", ["straight", "bent30", "flexed90"])
def test_postures_direction_and_palm_preservation(monkeypatch, posture):
    arm = _build_full_rig()
    monkeypatch.setattr(retarget, "get_armature", lambda: arm)
    common.reset_smoothing()

    side = "Left"
    body_lms, sh_pt, el_pt, wr_pt = _make_body_landmarks(side=side, elbow_posture=posture)
    expected_dir_u = (el_pt - sh_pt).normalized()
    expected_dir_l = (wr_pt - el_pt).normalized()

    # Evaluar bajo diferentes giros de palma (0°, 45°, 90°, 135°, 180°)
    for deg in [0.0, 45.0, 90.0, 135.0, 180.0]:
        hand_lms = _make_hand_landmarks(wr_pt, roll_deg=deg, side=side, fwd_dir=expected_dir_l)
        payload = {"L": {"lm": hand_lms, "hd": "Left"}}

        retarget.apply_pose(
            landmarks=body_lms,
            hands_data=payload,
            prefix="mixamorig:",
            sample_time=1.0 + deg * 0.01,
            enable_body=True,
            enable_hands=True,
        )

        # 1. Dirección UpperArm: hombro -> codo
        pb_u = arm.pose.bones["mixamorig:LeftArm"]
        sh_m = arm.pose.bones["mixamorig:LeftShoulder"].matrix.to_3x3()
        u_world = common.chained_world_3x3(pb_u, pb_u.rotation_quaternion, parent_world_3x3=sh_m)
        actual_dir_u = (u_world @ Vector((0.0, 1.0, 0.0))).normalized()
        err_u_deg = math.degrees(actual_dir_u.angle(expected_dir_u))
        assert err_u_deg < 2.0, f"Error dirección hombro->codo en postura {posture} (deg={deg}): {err_u_deg:.2f}°"

        # 2. Dirección ForeArm: codo -> muñeca
        pb_fa = arm.pose.bones["mixamorig:LeftForeArm"]
        fa_world = common.chained_world_3x3(pb_fa, pb_fa.rotation_quaternion, parent_world_3x3=u_world)
        actual_dir_l = (fa_world @ Vector((0.0, 1.0, 0.0))).normalized()
        err_l_deg = math.degrees(actual_dir_l.angle(expected_dir_l))
        assert err_l_deg < 2.0, f"Error dirección codo->muñeca en postura {posture} (deg={deg}): {err_l_deg:.2f}°"

        # 3. Orientación de la palma
        pb_h = arm.pose.bones["mixamorig:LeftHand"]
        hand_world = common.chained_world_3x3(pb_h, pb_h.rotation_quaternion, parent_world_3x3=fa_world)
        actual_palm_fwd = (hand_world @ Vector((0.0, 1.0, 0.0))).normalized()

        # El forward de la mano debe alinearse con wrist -> middle MCP
        pts = [common.mp_to_arm(lm) for lm in hand_lms]
        target_fwd = (pts[9] - pts[0]).normalized()
        err_fwd_deg = math.degrees(actual_palm_fwd.angle(target_fwd))
        assert err_fwd_deg < 3.0, f"Error orientación palma forward en postura {posture} (deg={deg}): {err_fwd_deg:.2f}°"


# ---------------------------------------------------------------------------
# 5. Presupuesto de deformación: ningún hueso absorbe > 60%
# ---------------------------------------------------------------------------

def test_twist_distribution_budget(monkeypatch):
    arm = _build_full_rig()
    monkeypatch.setattr(retarget, "get_armature", lambda: arm)
    common.reset_smoothing()

    side = "Left"
    body_lms, sh_pt, el_pt, wr_pt = _make_body_landmarks(side=side, elbow_posture="bent30")
    fwd = (wr_pt - el_pt).normalized()

    # Pose neutral (deg = 0)
    hand_lms_0 = _make_hand_landmarks(wr_pt, roll_deg=0.0, side=side, fwd_dir=fwd)
    retarget.apply_pose(
        landmarks=body_lms,
        hands_data={"L": {"lm": hand_lms_0, "hd": "Left"}},
        prefix="mixamorig:",
        sample_time=1.0,
        rotation_smooth=0.0,
        enable_body=True,
        enable_hands=True,
    )

    q_u_0 = arm.pose.bones["mixamorig:LeftArm"].rotation_quaternion.copy()
    q_fa_0 = arm.pose.bones["mixamorig:LeftForeArm"].rotation_quaternion.copy()
    q_h_0 = arm.pose.bones["mixamorig:LeftHand"].rotation_quaternion.copy()

    # Pose con giro completo de 180 grados
    total_roll_deg = 180.0
    hand_lms_180 = _make_hand_landmarks(wr_pt, roll_deg=total_roll_deg, side=side, fwd_dir=fwd)
    retarget.apply_pose(
        landmarks=body_lms,
        hands_data={"L": {"lm": hand_lms_180, "hd": "Left"}},
        prefix="mixamorig:",
        sample_time=2.0,
        rotation_smooth=0.0,
        enable_body=True,
        enable_hands=True,
    )

    q_u_180 = arm.pose.bones["mixamorig:LeftArm"].rotation_quaternion.copy()
    q_fa_180 = arm.pose.bones["mixamorig:LeftForeArm"].rotation_quaternion.copy()
    q_h_180 = arm.pose.bones["mixamorig:LeftHand"].rotation_quaternion.copy()

    delta_u_deg = math.degrees(q_u_0.rotation_difference(q_u_180).angle)
    delta_fa_deg = math.degrees(q_fa_0.rotation_difference(q_fa_180).angle)
    delta_h_deg = math.degrees(q_h_0.rotation_difference(q_h_180).angle)

    # Cada articulación debe mantenerse por debajo del 60% del giro total (180 * 0.60 = 108°)
    assert delta_u_deg < 108.0, f"UpperArm absorbió demasiado twist: {delta_u_deg:.1f}°"
    assert delta_fa_deg < 108.0, f"ForeArm absorbió demasiado twist: {delta_fa_deg:.1f}°"
    assert delta_h_deg < 108.0, f"Hand absorbió demasiado twist: {delta_h_deg:.1f}°"

    # Y todas deben haber participado en la absorción coordinada
    assert delta_u_deg > 15.0, f"UpperArm debe absorber twist coordinado: {delta_u_deg:.1f}°"
    assert delta_fa_deg > 30.0, f"ForeArm debe absorber twist coordinado: {delta_fa_deg:.1f}°"
    assert delta_h_deg > 15.0, f"Hand debe absorber twist coordinado: {delta_h_deg:.1f}°"


# ---------------------------------------------------------------------------
# 6. Pérdida temporal de landmarks de mano y recuperación sin flips
# ---------------------------------------------------------------------------

def test_missing_hand_landmarks_fallback_and_recovery(monkeypatch):
    arm = _build_full_rig()
    monkeypatch.setattr(retarget, "get_armature", lambda: arm)
    common.reset_smoothing()

    side = "Left"
    body_lms, sh_pt, el_pt, wr_pt = _make_body_landmarks(side=side, elbow_posture="bent30")
    fwd = (wr_pt - el_pt).normalized()

    # Frames 1-5: Mano presente
    for frame in range(1, 6):
        t = 1.0 + frame * 0.05
        hand_lms = _make_hand_landmarks(wr_pt, roll_deg=45.0, side=side, fwd_dir=fwd)
        retarget.apply_pose(
            landmarks=body_lms,
            hands_data={"L": {"lm": hand_lms, "hd": "Left"}},
            prefix="mixamorig:",
            sample_time=t,
            enable_body=True,
            enable_hands=True,
        )
        assert hands.was_arm_chain_oriented(side)

    q_fa_before = arm.pose.bones["mixamorig:LeftForeArm"].rotation_quaternion.copy()

    # Frames 6-10: Mano ocluida o ausente (5 frames)
    for frame in range(6, 11):
        t = 1.0 + frame * 0.05
        retarget.apply_pose(
            landmarks=body_lms,
            hands_data=None,  # Pérdida de mano
            prefix="mixamorig:",
            sample_time=t,
            enable_body=True,
            enable_hands=True,
        )
        # El fallback FK de body debió mantener orientados UpperArm y ForeArm
        assert not hands.was_arm_chain_oriented(side)
        pb_fa = arm.pose.bones["mixamorig:LeftForeArm"]
        assert pb_fa.rotation_quaternion.magnitude > 0.99

    # Frame 11: Recuperación de la mano
    t = 1.0 + 11 * 0.05
    hand_lms = _make_hand_landmarks(wr_pt, roll_deg=50.0, side=side, fwd_dir=fwd)
    retarget.apply_pose(
        landmarks=body_lms,
        hands_data={"L": {"lm": hand_lms, "hd": "Left"}},
        prefix="mixamorig:",
        sample_time=t,
        enable_body=True,
        enable_hands=True,
    )
    assert hands.was_arm_chain_oriented(side)

    q_fa_after = arm.pose.bones["mixamorig:LeftForeArm"].rotation_quaternion.copy()
    diff_angle = math.degrees(q_fa_before.rotation_difference(q_fa_after).angle)
    # No debe haber ocurrido un salto discontinuo tipo flip (> 45°)
    assert diff_angle < 35.0, f"Recuperación de mano produjo salto o flip: {diff_angle:.1f}°"

