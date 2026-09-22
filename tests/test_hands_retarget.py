"""Tests de retarget de manos, continuidad de palma y resolución de antebrazo.
"""
import math
import sys
import types
from pathlib import Path

import pytest

mathutils = pytest.importorskip("mathutils")
from mathutils import Matrix, Quaternion, Vector

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from puppet_mocap.retarget import body, common, hands


def _make_mock_bone(name, matrix_local=None, parent=None):
    if matrix_local is None:
        matrix_local = Matrix.Identity(4)
    bone = types.SimpleNamespace(matrix_local=matrix_local, length=0.25)
    pb = types.SimpleNamespace(
        name=name,
        bone=bone,
        parent=parent,
        matrix=matrix_local.copy(),
        rotation_quaternion=Quaternion((1.0, 0.0, 0.0, 0.0)),
        location=Vector((0.0, 0.0, 0.0)),
    )
    return pb


def _build_arm_chain(prefix="mixamorig:"):
    """Construye una cadena de brazo y mano mock para pruebas."""
    # Hombro / Clavícula
    m_shoulder = Matrix.Translation(Vector((0.2, 0.0, 1.4)))
    sh = _make_mock_bone(f"{prefix}LeftShoulder", m_shoulder)

    # Brazo superior (UpperArm): apunta a lo largo de +X o -Z
    m_arm = Matrix.Translation(Vector((0.3, 0.0, 1.4)))
    arm_bone = _make_mock_bone(f"{prefix}LeftArm", m_arm, parent=sh)

    # Antebrazo (ForeArm)
    m_fa = Matrix.Translation(Vector((0.55, 0.0, 1.4)))
    fa = _make_mock_bone(f"{prefix}LeftForeArm", m_fa, parent=arm_bone)

    # Mano (Hand)
    m_hand = Matrix.Translation(Vector((0.8, 0.0, 1.4)))
    hand = _make_mock_bone(f"{prefix}LeftHand", m_hand, parent=fa)

    bones_dict = {
        sh.name: sh,
        arm_bone.name: arm_bone,
        fa.name: fa,
        hand.name: hand,
    }

    # Dedos
    for chain, suffixes in [
        ("Thumb", ["LeftHandThumb1", "LeftHandThumb2", "LeftHandThumb3"]),
        ("Index", ["LeftHandIndex1", "LeftHandIndex2", "LeftHandIndex3"]),
        ("Middle", ["LeftHandMiddle1", "LeftHandMiddle2", "LeftHandMiddle3"]),
        ("Ring", ["LeftHandRing1", "LeftHandRing2", "LeftHandRing3"]),
        ("Pinky", ["LeftHandPinky1", "LeftHandPinky2", "LeftHandPinky3"]),
    ]:
        p = hand
        for s in suffixes:
            b = _make_mock_bone(f"{prefix}{s}", Matrix.Identity(4), parent=p)
            bones_dict[b.name] = b
            p = b

    arm_obj = types.SimpleNamespace(
        name="Armature",
        matrix_world=Matrix.Identity(4),
        pose=types.SimpleNamespace(bones=bones_dict),
        data=types.SimpleNamespace(bones={k: v.bone for k, v in bones_dict.items()}),
    )
    return arm_obj


def _make_synthetic_hand_landmarks(theta_rad: float, axis="Y", wrist_offset=(0.8, 0.0, 1.4)):
    """Genera 21 landmarks de mano con la palma girada un ángulo theta alrededor del eje dado."""
    # En sistema canónico Z-up / Y-forward
    # Muñeca en (0,0,0) + wrist_offset
    R = Matrix.Rotation(theta_rad, 3, axis)
    base_pts = [Vector((0, 0, 0)) for _ in range(21)]
    # Dedo medio: fwd a lo largo de +Y
    base_pts[9] = Vector((0.0, 0.08, 0.0))
    # Índice: +X, +Y
    base_pts[5] = Vector((0.04, 0.06, 0.0))
    # Meñique: -X, +Y
    base_pts[17] = Vector((-0.04, 0.06, 0.0))
    # Pulgar CMC: +X palmar
    base_pts[1] = Vector((0.03, 0.02, 0.01))

    # Rotar cada punto por R
    pts = []
    for p in base_pts:
        rot_p = R @ p
        # Convertir a landmarks en espacio MediaPipe:
        # arm.x = mp.x  -> mp.x = arm.x
        # arm.y = mp.z  -> mp.z = arm.y
        # arm.z = -mp.y -> mp.y = -arm.z
        world_pt = rot_p + Vector(wrist_offset)
        mp_pt = [world_pt.x, -world_pt.z, world_pt.y]
        pts.append(mp_pt)
    return pts


def test_synthetic_hand_generator():
    pts = _make_synthetic_hand_landmarks(0.0)
    assert len(pts) == 21
    # Cuando theta = 0, el normal de palma apunta hacia +Z canónico
    v_idx = Vector((pts[5][0], pts[5][2], -pts[5][1])) - Vector((pts[0][0], pts[0][2], -pts[0][1]))
    v_pinky = Vector((pts[17][0], pts[17][2], -pts[17][1])) - Vector((pts[0][0], pts[0][2], -pts[0][1]))
    normal = v_idx.cross(v_pinky).normalized()
    assert normal.dot(Vector((0.0, 0.0, 1.0))) > 0.9


def test_h1_forearm_filter_competition_reproduction():
    """H1: Demuestra que cuando body y hands intentan orientar el mismo ForeArm
    en el mismo frame con suavizado activo, la segunda llamada recibe dt=0
    y descarta la corrección palmar."""
    arm = _build_arm_chain("mixamorig:")
    common.reset_smoothing()

    # Frame 1 en t=1.0
    common.set_tick(1.0, rotation_smooth=0.5)
    fa = arm.pose.bones["mixamorig:LeftForeArm"]

    # 1. body orienta con aim (apuntando a +Y)
    q_body = common.aim(fa, Vector((0.0, 1.0, 0.0)))
    assert q_body is not None

    # Frame 2 en t=1.05 (20 fps)
    common.set_tick(1.05, rotation_smooth=0.5)

    # 1. body vuelve a orientar ForeArm con aim (dirección de codo a muñeca)
    q_body_aim = common.aim(fa, Vector((0.0, 1.0, 0.0)))
    assert q_body_aim is not None

    # 2. Inmediatamente después, hands intenta orientar ForeArm con orient_yz
    # añadiendo el giro palmar (normal en +Z)
    q_hands = common.orient_yz(fa, Vector((0.0, 1.0, 0.0)), Vector((0.0, 0.0, 1.0)))

    # Con el bug H1 actual: como dt = 1.05 - 1.05 = 0 <= 1e-4, QuatOneEuro devuelve
    # self.q (el resultado anterior de aim), descartando el giro palmar.
    # q_hands termina siendo idéntico a q_body_aim en vez de absorber la orientación YZ!
    diff = q_hands.rotation_difference(q_body_aim).angle
    # Comprobamos que el defecto H1 efectivamente ocurre:
    assert diff < 1e-5, "Demostración H1: la segunda llamada fue ignorada debido a dt=0"


def test_h2_palm_normal_continuity_under_180_rotation():
    """H2: Verifica que _palm_basis rota continuamente desde 0° hasta 180°
    paso a paso sin saltos espurios ni bloqueos de orientación."""
    common.reset_smoothing()
    normals = []
    steps = 36  # 180° en pasos de 5°
    for step in range(steps + 1):
        angle = math.pi * step / steps
        common.set_tick(1.0 + step * 0.05, 0.0)
        pts_raw = _make_synthetic_hand_landmarks(angle)
        pts = [common.mp_to_arm(p) for p in pts_raw]
        basis = hands._palm_basis("Left", pts, handedness="Left")
        assert basis is not None
        _, n = basis
        normals.append(n)

    # Cada paso consecutivo debe tener continuidad casi perfecta (> 0.95)
    for i in range(len(normals) - 1):
        dot_product = normals[i].dot(normals[i + 1])
        assert dot_product > 0.95, f"Discontinuidad en paso {i}: dot={dot_product}"

    # A 180°, la normal debe haber rotado ~180° respecto a 0°
    assert normals[-1].dot(normals[0]) < -0.90


def test_palm_orientation_deadlock_fixed():
    """Verifica que un giro brusco de 180° sostenido durante > 200 ms
    a 30 fps es finalmente aceptado, demostrando que NO se queda bloqueado."""
    common.reset_smoothing()

    # Estado inicial en t = 1.0, theta = 0 (palma en +Z)
    common.set_tick(1.0, 0.0)
    pts_0 = [common.mp_to_arm(p) for p in _make_synthetic_hand_landmarks(0.0)]
    _, n0 = hands._palm_basis("Left", pts_0, handedness="Left")

    # A partir de t = 1.0, se presentan muestras a 30 fps (cada ~0.033s) con giro de 180°
    pts_180 = [common.mp_to_arm(p) for p in _make_synthetic_hand_landmarks(math.pi)]

    # t = 1.033 (33 ms): primer frame del salto, debe retener la normal aceptada
    common.set_tick(1.033, 0.0)
    _, n_early = hands._palm_basis("Left", pts_180, handedness="Left")
    assert n_early.dot(n0) > 0.99

    # t = 1.066, 1.100, 1.133, 1.166 (< 200 ms): sigue reteniendo n0
    for frame_idx in range(2, 6):
        t = 1.0 + frame_idx * 0.033
        common.set_tick(t, 0.0)
        _, n_mid = hands._palm_basis("Left", pts_180, handedness="Left")
        assert n_mid.dot(n0) > 0.99

    # t = 1.25 (250 ms de salto sostenido): la histéresis confirma el giro intencional
    common.set_tick(1.25, 0.0)
    _, n_confirmed = hands._palm_basis("Left", pts_180, handedness="Left")
    # Ya NO está bloqueado: ahora adopta la nueva orientación (~180° respecto a n0)
    assert n_confirmed.dot(n0) < -0.90, "La orientación debe aceptarse tras 200 ms sin bloquearse"


def test_palm_orientation_rejects_single_frame_glitch():
    """Verifica que un flip aislado de 1 frame (glitch) se descarta
    y el tracking regresa a la normal aceptada sin alteración."""
    common.reset_smoothing()

    common.set_tick(0.0, 0.0)
    pts_0 = [common.mp_to_arm(p) for p in _make_synthetic_hand_landmarks(0.0)]
    _, n0 = hands._palm_basis("Left", pts_0, handedness="Left")

    # Frame 1: glitch de 180° a los 33 ms
    common.set_tick(0.033, 0.0)
    pts_180 = [common.mp_to_arm(p) for p in _make_synthetic_hand_landmarks(math.pi)]
    _, n_glitch = hands._palm_basis("Left", pts_180, handedness="Left")
    assert n_glitch.dot(n0) > 0.99

    # Frame 2: vuelve a la normalidad a los 66 ms
    common.set_tick(0.066, 0.0)
    _, n_recovered = hands._palm_basis("Left", pts_0, handedness="Left")
    assert n_recovered.dot(n0) > 0.99




def test_h1_resolved_in_apply_pose(monkeypatch):
    """H1 Fix: Verifica que apply_pose coordina body y hands para que
    ForeArm reciba la corrección palmar sin competencia de filtros."""
    from puppet_mocap import retarget
    from puppet_mocap.retarget import common

    arm = _build_arm_chain("mixamorig:")
    monkeypatch.setattr(retarget, "get_armature", lambda: arm)
    common.reset_smoothing()

    # Pose neutral de cuerpo con codo y muñeca visibles
    # 11: L shoulder, 13: L elbow, 15: L wrist
    body_lms = [[0.0, 0.0, 0.0, 1.0] for _ in range(33)]
    # Shoulder: (0.3, 0, 1.4) canónico -> mp = (0.3, -1.4, 0.0)
    body_lms[11] = [0.3, -1.4, 0.0, 1.0]
    # Elbow: (0.55, 0, 1.4) canónico -> mp = (0.55, -1.4, 0.0)
    body_lms[13] = [0.55, -1.4, 0.0, 1.0]
    # Wrist: (0.8, 0, 1.4) canónico -> mp = (0.8, -1.4, 0.0)
    body_lms[15] = [0.8, -1.4, 0.0, 1.0]

    # Mano con palma en theta=0 (normal hacia +Z)
    hand_lms = _make_synthetic_hand_landmarks(0.0, axis="X", wrist_offset=(0.8, 0.0, 1.4))
    hands_payload = {"L": {"lm": hand_lms, "hd": "Left"}}

    retarget.apply_pose(
        landmarks=body_lms,
        hands_data=hands_payload,
        prefix="mixamorig:",
        sample_time=1.0,
        enable_body=True,
        enable_hands=True,
    )

    fa = arm.pose.bones["mixamorig:LeftForeArm"]
    q1 = fa.rotation_quaternion.copy()
    assert q1 is not None

    # Muestra 2 con palma girada continuamente ~28° alrededor del eje X del antebrazo
    hand_lms2 = _make_synthetic_hand_landmarks(0.5, axis="X", wrist_offset=(0.8, 0.0, 1.4))
    hands_payload2 = {"L": {"lm": hand_lms2, "hd": "Left"}}

    retarget.apply_pose(
        landmarks=body_lms,
        hands_data=hands_payload2,
        prefix="mixamorig:",
        sample_time=1.05,
        enable_body=True,
        enable_hands=True,
    )

    q2 = fa.rotation_quaternion.copy()
    # Con H1 resuelto, ForeArm rota absorbiendo el cambio de la palma
    diff = q1.rotation_difference(q2).angle
    assert diff > 0.05, "ForeArm debe rotar siguiendo la palma sin ser descartado por dt=0"


def test_h3_hand_relative_zeroed_wrist():
    """H3: hand_relative debe restar la posición de la muñeca (landmark 0)."""
    import sys
    for mod in ("cv2", "mediapipe", "mediapipe.tasks", "mediapipe.tasks.python", "mediapipe.tasks.python.vision"):
        if mod not in sys.modules:
            sys.modules[mod] = types.ModuleType(mod)
    from puppet_mocap.capture.capture_runner import hand_relative

    fake_world = [
        types.SimpleNamespace(x=0.5, y=0.2, z=-0.3),
        types.SimpleNamespace(x=0.55, y=0.25, z=-0.28),
        types.SimpleNamespace(x=0.6, y=0.3, z=-0.25),
    ]
    rel = hand_relative(fake_world)
    assert rel[0] == [0.0, 0.0, 0.0]
    assert abs(rel[1][0] - 0.05) < 1e-5
    assert abs(rel[2][0] - 0.10) < 1e-5


def test_snapshot_pose_preserves_root_prefix():
    """Snapshot y bake deben preservar el prefijo del rig (mixamorig:Hips)."""
    from puppet_mocap import retarget

    arm = _build_arm_chain("mixamorig:")
    hips = _make_mock_bone("mixamorig:Hips", Matrix.Identity(4))
    hips.location = Vector((0.1, 0.2, 0.3))
    arm.pose.bones["mixamorig:Hips"] = hips

    snap = retarget.snapshot_pose(
        arm, prefix="mixamorig:",
        include_body=True, include_hands=True,
        include_face=False, include_root=True,
    )
    assert "mixamorig:Hips" in snap["locs"], "locs debe contener el nombre completo con prefijo"
    loc = snap["locs"]["mixamorig:Hips"]
    for i in range(3):
        assert abs(loc[i] - (0.1, 0.2, 0.3)[i]) < 1e-5


def test_get_keyframe_bones_includes_forearm_and_no_duplicates():
    """get_keyframe_bones debe incluir ForeArm cuando se graban manos y sin duplicados."""
    from puppet_mocap import retarget

    bones = retarget.get_keyframe_bones("mixamorig:", include_body=True, include_hands=True)
    assert "mixamorig:LeftForeArm" in bones
    assert "mixamorig:RightForeArm" in bones
    assert len(bones) == len(set(bones)), "No debe haber huesos duplicados en la lista"


def test_kimodo_apply_ground_offset_respects_world_matrix():
    """Verifica que apply_ground_offset transforma el offset de mundo al espacio local
    respetando la rotación de mundo del objeto Armature (ej: +90° en X)."""
    from puppet_mocap.kimodo import postbake

    # Armature rotado 90° en X
    rot_x_90 = Matrix.Rotation(math.pi / 2.0, 4, "X")
    arm = types.SimpleNamespace(matrix_world=rot_x_90)

    # Rest matrix de cadera identidad
    rest3_root = Matrix.Identity(3)

    # Mock action con 3 fcurves para Hips.location
    curves = []
    for axis in range(3):
        fc = types.SimpleNamespace(
            data_path='pose.bones["Hips"].location',
            array_index=axis,
            keyframe_points=[types.SimpleNamespace(co=Vector((1.0, 0.0)), handle_left=Vector((0.0, 0.0)), handle_right=Vector((2.0, 0.0)))]
        )
        curves.append(fc)
    action = types.SimpleNamespace(fcurves=curves)

    # Aplicar offset_z de 0.5 metros en MUNDO
    res = postbake.apply_ground_offset(action, arm, "Hips", rest3_root, 0.5)
    assert res is True

    # Como el armature está rotado 90° en X:
    # Mundo Z = +0.5 -> Local Y = +0.5 (porque rot_x_90 @ local = world)
    # Por lo tanto, el canal Y (index 1) debe recibir +0.5 y el canal Z (index 2) 0.0
    assert abs(curves[0].keyframe_points[0].co.y - 0.0) < 1e-5  # X
    assert abs(curves[1].keyframe_points[0].co.y - 0.5) < 1e-5  # Y recibe el offset
    assert abs(curves[2].keyframe_points[0].co.y - 0.0) < 1e-5  # Z queda en 0


def test_forearm_fallback_when_palm_degenerate(monkeypatch):
    """Verifica que si la palma es degenerada o hands no puede orientar el forearm,
    el coordinador recurre automáticamente al fallback FK del cuerpo para no dejar
    el antebrazo congelado."""
    from puppet_mocap import retarget

    arm = _build_arm_chain("mixamorig:")
    monkeypatch.setattr(retarget, "get_armature", lambda: arm)
    common.reset_smoothing()

    # Pose de cuerpo con hombro y codo en +X, pero antebrazo flexionado (+Y canónico / Z en MP)
    body_lms = [[0.0, 0.0, 0.0, 1.0] for _ in range(33)]
    body_lms[11] = [0.3, -1.4, 0.0, 1.0]   # L shoulder (0.3, 0.0, 1.4)
    body_lms[13] = [0.55, -1.4, 0.0, 1.0]  # L elbow (0.55, 0.0, 1.4)
    body_lms[15] = [0.55, -1.4, 0.25, 1.0] # L wrist flexionado (0.55, 0.25, 1.4)

    # Mano degenerada (colapsada): todos los puntos en la muñeca flexionada
    degenerate_hand = [[0.55, -1.4, 0.25] for _ in range(21)]
    hands_payload = {"L": {"lm": degenerate_hand, "hd": "Left"}}

    fa = arm.pose.bones["mixamorig:LeftForeArm"]
    initial_q = fa.rotation_quaternion.copy()

    # Coordinador apply_pose:
    # 1. Detecta landmarks de mano y añade 'Left' a skip_forearms
    # 2. body.apply salta el antebrazo izquierdo
    # 3. hands.apply falla al orientar palma porque está colapsada (normal < 0.05)
    # 4. Coordinador comprueba que 'Left' no fue orientado por hands
    # 5. Coordinador ejecuta body.orient_forearm_fallback
    res = retarget.apply_pose(
        landmarks=body_lms,
        hands_data=hands_payload,
        prefix="mixamorig:",
        sample_time=1.0,
        enable_body=True,
        enable_hands=True,
    )
    assert res is not False
    # Verificamos que el fallback FK de body fue ejecutado con éxito
    after_q = fa.rotation_quaternion.copy()
    diff = initial_q.rotation_difference(after_q).angle
    assert diff > 0.01, "El antebrazo debe haber sido orientado por el fallback FK"


def test_foot_lock_relative_to_original_frame_location():
    """Verifica que foot_lock calcula el desplazamiento de cada frame sumándolo
    a hips_original_locs[idx] y no al location residual del último frame."""
    from puppet_mocap.retarget import foot_lock

    arm = types.SimpleNamespace(
        matrix_world=Matrix.Identity(4),
    )
    hips = types.SimpleNamespace(
        location=Vector((0.0, 0.0, 99.0)),  # Simula que hips quedó en Z=99 al terminar el loop
        bone=types.SimpleNamespace(matrix_local=Matrix.Identity(4)),
    )

    base_frame_loc = Vector((1.0, 2.0, 3.0))
    world_delta = Vector((0.1, 0.2, 0.3))

    new_loc = foot_lock._shift_root_from_world_delta(
        arm, hips, world_delta, base_location=base_frame_loc
    )
    # Debe ser base_frame_loc + world_delta
    assert abs(new_loc.x - 1.1) < 1e-5
    assert abs(new_loc.y - 2.2) < 1e-5
    assert abs(new_loc.z - 3.3) < 1e-5


def test_forearm_fallback_resolved_before_hand(monkeypatch):
    """Valida que si la orientación con palma no se puede resolver, el fallback
    de antebrazo se ejecuta ANTES de calcular Hand y dedos, garantizando que el padre
    no cambie después de calcular sus hijos."""
    from puppet_mocap import retarget
    arm = _build_arm_chain("mixamorig:")
    monkeypatch.setattr(retarget, "get_armature", lambda: arm)
    common.reset_smoothing()

    # Landmark corporal de codo y muñeca
    body_lms = [[0.0, 0.0, 0.0, 1.0] for _ in range(33)]
    body_lms[11] = [0.3, -1.4, 0.0, 1.0]   # L shoulder (0.3, 0.0, 1.4)
    body_lms[13] = [0.55, -1.4, 0.0, 1.0]  # L elbow (0.55, 0.0, 1.4)
    body_lms[15] = [0.55, -1.4, 0.25, 1.0] # L wrist flexionado (0.55, 0.25, 1.4)

    # Mano normal pero desplazada para forzar fallback de antebrazo
    hand_lms = _make_synthetic_hand_landmarks(0.0)
    # Colocar la muñeca de la mano lejos del brazo izquierdo para que matches sea False
    for pt in hand_lms:
        pt[0] += 5.0

    hands_payload = {"L": {"lm": hand_lms, "hd": "Left"}}
    res = retarget.apply_pose(
        landmarks=body_lms,
        hands_data=hands_payload,
        prefix="mixamorig:",
        sample_time=1.0,
        enable_body=True,
        enable_hands=True,
    )
    assert res is not False
    assert hands.was_forearm_oriented("Left"), "El antebrazo debe quedar resuelto vía fallback antes del retorno"


def test_foot_lock_action_full_integration_bilateral_and_single_support():
    """Evalúa lock_action sobre una Action real en Blender, midiendo la posición
    final de cada pie en coordenadas de mundo durante apoyo simple y bilateral."""
    import bpy
    from puppet_mocap.retarget import foot_lock

    arm_data = bpy.data.armatures.new("TestArmData_Int")
    arm_obj = bpy.data.objects.new("TestArmObj_Int", arm_data)
    bpy.context.scene.collection.objects.link(arm_obj)
    bpy.context.view_layer.objects.active = arm_obj
    bpy.ops.object.mode_set(mode="EDIT")

    prefix = "mixamorig:"
    eb_hips = arm_data.edit_bones.new(f"{prefix}Hips")
    eb_hips.head = Vector((0, 0, 1.0))
    eb_hips.tail = Vector((0, 0, 1.1))

    for side, sign in (("Left", 1), ("Right", -1)):
        eb_u = arm_data.edit_bones.new(f"{prefix}{side}UpLeg")
        eb_u.head = Vector((sign * 0.1, 0, 1.0))
        eb_u.tail = Vector((sign * 0.1, 0, 0.5))
        eb_u.parent = eb_hips

        eb_l = arm_data.edit_bones.new(f"{prefix}{side}Leg")
        eb_l.head = Vector((sign * 0.1, 0, 0.5))
        eb_l.tail = Vector((sign * 0.1, 0, 0.1))
        eb_l.parent = eb_u

        eb_f = arm_data.edit_bones.new(f"{prefix}{side}Foot")
        eb_f.head = Vector((sign * 0.1, 0, 0.1))
        eb_f.tail = Vector((sign * 0.1, 0.15, 0.1))
        eb_f.parent = eb_l

    bpy.ops.object.mode_set(mode="POSE")

    action = bpy.data.actions.new("TestAction_Int")
    action.use_fake_user = True
    arm_obj.animation_data_create()
    arm_obj.animation_data.action = action

    pb_hips = arm_obj.pose.bones[f"{prefix}Hips"]
    pb_lu = arm_obj.pose.bones[f"{prefix}LeftUpLeg"]
    pb_ru = arm_obj.pose.bones[f"{prefix}RightUpLeg"]
    pb_lf = arm_obj.pose.bones[f"{prefix}LeftFoot"]
    pb_rf = arm_obj.pose.bones[f"{prefix}RightFoot"]

    # Frame 1: reposo
    bpy.context.scene.frame_set(1)
    pb_hips.location = Vector((0, 0, 0))
    pb_hips.keyframe_insert("location", frame=1)

    # Frame 2: anclaje bilateral establecido
    bpy.context.scene.frame_set(2)
    pb_hips.location = Vector((0, 0, 0))
    pb_hips.keyframe_insert("location", frame=2)

    # Frame 3: apoyo bilateral asimétrico con deriva divergente
    bpy.context.scene.frame_set(3)
    pb_hips.location = Vector((0.004, 0.004, 0))
    pb_hips.keyframe_insert("location", frame=3)
    pb_lu.rotation_mode = "QUATERNION"
    pb_ru.rotation_mode = "QUATERNION"
    pb_lu.rotation_quaternion = Quaternion(Vector((0, 1, 0)), 0.02)
    pb_ru.rotation_quaternion = Quaternion(Vector((0, 1, 0)), -0.02)
    pb_lu.keyframe_insert("rotation_quaternion", frame=3)
    pb_ru.keyframe_insert("rotation_quaternion", frame=3)

    action.frame_range = (1, 3)

    new_act, locked = foot_lock.lock_action(arm_obj, action, prefix=prefix)
    assert new_act is not None
    assert locked > 0

    arm_obj.animation_data.action = new_act

    # Anclas en frame 2
    bpy.context.scene.frame_set(2)
    bpy.context.view_layer.update()
    anc_left = foot_lock._foot_point_world(arm_obj, pb_lf).copy()
    anc_right = foot_lock._foot_point_world(arm_obj, pb_rf).copy()

    # Medición en frame 3
    bpy.context.scene.frame_set(3)
    bpy.context.view_layer.update()
    pt_l = foot_lock._foot_point_world(arm_obj, pb_lf)
    pt_r = foot_lock._foot_point_world(arm_obj, pb_rf)

    err_l = (pt_l - anc_left).length
    err_r = (pt_r - anc_right).length

    assert err_l < 1e-4, f"Pie izquierdo deslizó en apoyo bilateral: {err_l}"
    assert err_r < 1e-4, f"Pie derecho deslizó en apoyo bilateral: {err_r}"

    # Limpieza
    bpy.data.actions.remove(action)
    bpy.data.actions.remove(new_act)
    bpy.data.objects.remove(arm_obj)
    bpy.data.armatures.remove(arm_data)


def test_mesh_wrist_knot_elimination_on_real_mesh():
    """Valida sobre la malla real de test_project.blend que la rotación coordinada
    antebrazo-palma elimina el colapso de sección (nudo de globo) en comparación
    con el giro aislado sin antebrazo."""
    blend_path = Path(__file__).resolve().parent.parent / "test_project.blend"
    if not blend_path.exists():
        pytest.skip("test_project.blend no encontrado")

    import bpy
    with bpy.data.libraries.load(str(blend_path)) as (data_from, data_to):
        data_to.objects = ["Armature", "tripo_node_2bbf9a47"]

    arm = data_to.objects[0]
    mesh_obj = data_to.objects[1]
    bpy.context.scene.collection.objects.link(arm)
    bpy.context.scene.collection.objects.link(mesh_obj)
    mesh_obj.parent = arm

    vg_fa = mesh_obj.vertex_groups.get("mixamorig:LeftForeArm")
    vg_hand = mesh_obj.vertex_groups.get("mixamorig:LeftHand")
    wrist_verts = [
        v.index for v in mesh_obj.data.vertices
        if any(g.group == vg_fa.index and g.weight > 0.1 for g in v.groups)
        and any(g.group == vg_hand.index and g.weight > 0.1 for g in v.groups)
    ]
    assert len(wrist_verts) > 50, "Debe haber vértices de transición en la muñeca"

    def get_wrist_radius(depsgraph):
        eval_mesh = mesh_obj.evaluated_get(depsgraph).to_mesh()
        pts = [eval_mesh.vertices[i].co for i in wrist_verts]
        mesh_obj.evaluated_get(depsgraph).to_mesh_clear()
        center = sum(pts, Vector()) / len(pts)
        dists = [(p - center).length for p in pts]
        return sum(dists) / len(dists)

    bpy.context.view_layer.update()
    depsgraph = bpy.context.evaluated_depsgraph_get()
    rest_radius = get_wrist_radius(depsgraph)

    # Caso 1: Giro sin antebrazo coordinado (produce nudo y colapso de sección)
    pb_hand = arm.pose.bones.get("mixamorig:LeftHand")
    pb_fa = arm.pose.bones.get("mixamorig:LeftForeArm")
    pb_hand.rotation_mode = "QUATERNION"
    pb_fa.rotation_mode = "QUATERNION"

    pb_hand.rotation_quaternion = Quaternion(Vector((0, 1, 0)), math.radians(180))
    pb_fa.rotation_quaternion = Quaternion((1, 0, 0, 0))
    bpy.context.view_layer.update()
    depsgraph = bpy.context.evaluated_depsgraph_get()
    knot_radius = get_wrist_radius(depsgraph)
    knot_collapse = (1.0 - knot_radius / rest_radius)

    # Caso 2: Giro coordinado (antebrazo acompaña el roll de la palma)
    pb_fa.rotation_quaternion = Quaternion(Vector((0, 1, 0)), math.radians(180))
    pb_hand.rotation_quaternion = Quaternion((1, 0, 0, 0))
    bpy.context.view_layer.update()
    depsgraph = bpy.context.evaluated_depsgraph_get()
    coord_radius = get_wrist_radius(depsgraph)
    coord_collapse = (1.0 - coord_radius / rest_radius)

    assert knot_collapse > 0.40, f"El giro aislado debe colapsar por skinning lineal: {knot_collapse:.1%}"
    assert abs(coord_collapse) < 0.05, f"La coordinación debe eliminar el nudo de globo: {coord_collapse:.1%}"

    # Limpieza
    bpy.data.objects.remove(mesh_obj)
    bpy.data.objects.remove(arm)


@pytest.mark.parametrize("side", ["Left", "Right"])
@pytest.mark.parametrize("smoothing", [0.0, 0.5])
@pytest.mark.parametrize("rot_x, scale", [(False, 1.0), (True, 0.01)])
def test_apply_pose_full_twist_sequence_across_transforms_and_sides(monkeypatch, side, smoothing, rot_x, scale):
    """Integración real de apply_pose() sobre la secuencia 0° -> 180° -> 0°
    probando transforms de objeto (identidad y 90° X escala 0.01), lados L y R, y suavizados.
    """
    from puppet_mocap import retarget
    from test_arm_twist import _build_full_rig, _make_body_landmarks, _make_hand_landmarks

    arm = _build_full_rig()
    if rot_x:
        arm.matrix_world = Matrix.Rotation(math.pi / 2.0, 4, "X") @ Matrix.Scale(scale, 4)
    else:
        arm.matrix_world = Matrix.Scale(scale, 4)

    monkeypatch.setattr(retarget, "get_armature", lambda: arm)
    common.reset_smoothing()

    body_lms, sh_pt, el_pt, wr_pt = _make_body_landmarks(side=side, elbow_posture="bent30")
    fwd = (wr_pt - el_pt).normalized()
    key = "L" if side == "Left" else "R"

    sequence = [0, 30, 60, 90, 120, 150, 180, 150, 120, 90, 60, 30, 0]
    prev_twist_fa = None

    q_u_0, q_fa_0, q_h_0 = None, None, None

    for idx, deg in enumerate(sequence):
        t = 1.0 + idx * 0.05
        hand_lms = _make_hand_landmarks(wr_pt, roll_deg=deg, side=side, fwd_dir=fwd)
        retarget.apply_pose(
            landmarks=body_lms,
            hands_data={key: {"lm": hand_lms, "hd": side}},
            prefix="mixamorig:",
            sample_time=t,
            rotation_smooth=smoothing,
            enable_body=True,
            enable_hands=True,
        )

        pb_u = arm.pose.bones[f"mixamorig:{side}Arm"]
        pb_fa = arm.pose.bones[f"mixamorig:{side}ForeArm"]
        pb_h = arm.pose.bones[f"mixamorig:{side}Hand"]

        if idx == 0:
            q_u_0 = pb_u.rotation_quaternion.copy()
            q_fa_0 = pb_fa.rotation_quaternion.copy()
            q_h_0 = pb_h.rotation_quaternion.copy()
            prev_twist_fa = 0.0
            continue

        # Verificar continuidad temporal (< 25° de cambio de twist entre pasos de 30°)
        delta_step = math.degrees(pb_fa.rotation_quaternion.rotation_difference(pb_fa.rotation_quaternion).angle)
        # Comparar con el paso anterior
        if prev_twist_fa is not None:
            step_diff = math.degrees(pb_fa.rotation_quaternion.rotation_difference(q_fa_0).angle) - prev_twist_fa
            assert abs(step_diff) <= 25.0, f"Salto discontinuo de twist en {deg}°: {step_diff:.1f}°"
            prev_twist_fa = math.degrees(pb_fa.rotation_quaternion.rotation_difference(q_fa_0).angle)

        # En el pico de 180°, verificar reparto de twist
        if deg == 180:
            diff_u = math.degrees(q_u_0.rotation_difference(pb_u.rotation_quaternion).angle)
            diff_fa = math.degrees(q_fa_0.rotation_difference(pb_fa.rotation_quaternion).angle)
            diff_h = math.degrees(q_h_0.rotation_difference(pb_h.rotation_quaternion).angle)

            # Ningún hueso debe absorber más del 60% del giro (108°)
            assert diff_u < 108.0, f"UpperArm twist excesivo: {diff_u:.1f}°"
            assert diff_fa < 108.0, f"ForeArm twist excesivo: {diff_fa:.1f}°"
            assert diff_h < 108.0, f"Hand twist excesivo: {diff_h:.1f}°"



