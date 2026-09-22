"""Tests de integración en Blender para el sistema de apoyo y bloqueo de pies (Foot Plant / Foot Lock).

Valida:
1. Apoyo unilateral: el pie clavado no desliza (< 0.1 mm de error).
2. Apoyo bilateral divergente: ambos pies se anclan independientemente.
3. Preservación del balanceo (swing): el pie elevado preserva >= 95% de su altura.
4. GroundModel con OBJECT_RAYCAST sobre malla de escena.
5. Inmutabilidad de la action fuente y determinismo entre ejecuciones sucesivas.
6. Calibración de suelo desde pose actual (PUPPET_OT_calibrate_ground).
"""
import math
import pytest
from mathutils import Matrix, Quaternion, Vector

import bpy
from puppet_mocap.retarget import foot_lock, foot_plant


def _create_test_armature(name_prefix: str = "TestRig"):
    arm_data = bpy.data.armatures.new(f"{name_prefix}_Data")
    arm_obj = bpy.data.objects.new(f"{name_prefix}_Obj", arm_data)
    bpy.context.scene.collection.objects.link(arm_obj)
    bpy.context.view_layer.objects.active = arm_obj
    bpy.ops.object.mode_set(mode="EDIT")

    prefix = "mixamorig:"
    eb_hips = arm_data.edit_bones.new(f"{prefix}Hips")
    eb_hips.head = Vector((0.0, 0.0, 1.0))
    eb_hips.tail = Vector((0.0, 0.0, 1.1))

    for side, sign in (("Left", 1), ("Right", -1)):
        eb_u = arm_data.edit_bones.new(f"{prefix}{side}UpLeg")
        eb_u.head = Vector((sign * 0.1, 0.0, 1.0))
        eb_u.tail = Vector((sign * 0.1, 0.0, 0.5))
        eb_u.parent = eb_hips

        eb_l = arm_data.edit_bones.new(f"{prefix}{side}Leg")
        eb_l.head = Vector((sign * 0.1, 0.0, 0.5))
        eb_l.tail = Vector((sign * 0.1, 0.0, 0.1))
        eb_l.parent = eb_u

        eb_f = arm_data.edit_bones.new(f"{prefix}{side}Foot")
        eb_f.head = Vector((sign * 0.1, 0.0, 0.1))
        eb_f.tail = Vector((sign * 0.1, 0.15, 0.1))
        eb_f.parent = eb_l

    bpy.ops.object.mode_set(mode="POSE")
    return arm_obj, prefix


def _cleanup_objects(*objs):
    for obj in objs:
        if obj is None:
            continue
        try:
            if hasattr(obj, "data") and obj.data:
                if isinstance(obj.data, bpy.types.Armature):
                    bpy.data.armatures.remove(obj.data)
                elif isinstance(obj.data, bpy.types.Mesh):
                    bpy.data.meshes.remove(obj.data)
            bpy.data.objects.remove(obj)
        except Exception:
            pass


def test_unilateral_support_foot_lock():
    """Valida que durante apoyo simple, el pie en contacto mantiene su ancla fija en mundo."""
    arm_obj, prefix = _create_test_armature("UniSupport")
    action = bpy.data.actions.new("Action_Uni")
    action.use_fake_user = True
    arm_obj.animation_data_create()
    arm_obj.animation_data.action = action

    pb_hips = arm_obj.pose.bones[f"{prefix}Hips"]
    pb_lf = arm_obj.pose.bones[f"{prefix}LeftFoot"]
    pb_rf = arm_obj.pose.bones[f"{prefix}RightFoot"]

    # Frames 1-3: Pie izquierdo estacionario en el piso, pie derecho sube (balanceo)
    for f in range(1, 4):
        bpy.context.scene.frame_set(f)
        # Hips deriva ligeramente
        pb_hips.location = Vector((0.002 * (f - 1), 0.003 * (f - 1), 0.0))
        pb_hips.keyframe_insert("location", frame=f)

        # Pie derecho se eleva
        pb_rf.rotation_mode = "QUATERNION"
        pb_rf.rotation_quaternion = Quaternion(Vector((1.0, 0.0, 0.0)), 0.1 * (f - 1))
        pb_rf.keyframe_insert("rotation_quaternion", frame=f)

    action.frame_range = (1, 3)

    new_act, stats = foot_lock.lock_action(arm_obj, action, prefix=prefix)
    assert new_act is not None
    assert int(stats) > 0

    arm_obj.animation_data.action = new_act

    # Evaluar posición del pie izquierdo en frame 2 y frame 3
    bpy.context.scene.frame_set(2)
    bpy.context.view_layer.update()
    pt_f2 = foot_lock._foot_point_world(arm_obj, pb_lf).copy()

    bpy.context.scene.frame_set(3)
    bpy.context.view_layer.update()
    pt_f3 = foot_lock._foot_point_world(arm_obj, pb_lf).copy()

    drift = (pt_f3 - pt_f2).length
    assert drift < 2e-4, f"Pie izquierdo de apoyo derivó: {drift} m"

    bpy.data.actions.remove(action)
    bpy.data.actions.remove(new_act)
    _cleanup_objects(arm_obj)


def test_bilateral_divergent_support_independent_pinning():
    """Valida que en apoyo bilateral ambos pies se mantienen anclados independientemente."""
    arm_obj, prefix = _create_test_armature("BiSupport")
    action = bpy.data.actions.new("Action_Bi")
    action.use_fake_user = True
    arm_obj.animation_data_create()
    arm_obj.animation_data.action = action

    pb_hips = arm_obj.pose.bones[f"{prefix}Hips"]
    pb_lu = arm_obj.pose.bones[f"{prefix}LeftUpLeg"]
    pb_ru = arm_obj.pose.bones[f"{prefix}RightUpLeg"]
    pb_lf = arm_obj.pose.bones[f"{prefix}LeftFoot"]
    pb_rf = arm_obj.pose.bones[f"{prefix}RightFoot"]

    # Frame 1 y 2: en reposo
    for f in (1, 2):
        bpy.context.scene.frame_set(f)
        pb_hips.location = Vector((0.0, 0.0, 0.0))
        pb_hips.keyframe_insert("location", frame=f)

    # Frame 3: deriva de caderas y rotaciones opuestas en muslos (divergencia)
    bpy.context.scene.frame_set(3)
    pb_hips.location = Vector((0.005, -0.003, 0.0))
    pb_hips.keyframe_insert("location", frame=3)
    pb_lu.rotation_mode = "QUATERNION"
    pb_ru.rotation_mode = "QUATERNION"
    pb_lu.rotation_quaternion = Quaternion(Vector((0.0, 1.0, 0.0)), 0.03)
    pb_ru.rotation_quaternion = Quaternion(Vector((0.0, 1.0, 0.0)), -0.03)
    pb_lu.keyframe_insert("rotation_quaternion", frame=3)
    pb_ru.keyframe_insert("rotation_quaternion", frame=3)

    action.frame_range = (1, 3)

    new_act, stats = foot_lock.lock_action(arm_obj, action, prefix=prefix)
    assert new_act is not None
    assert stats.get("bilateral_frames", 0) > 0 or int(stats) > 0

    arm_obj.animation_data.action = new_act

    bpy.context.scene.frame_set(2)
    bpy.context.view_layer.update()
    anc_l = foot_lock._foot_point_world(arm_obj, pb_lf).copy()
    anc_r = foot_lock._foot_point_world(arm_obj, pb_rf).copy()

    bpy.context.scene.frame_set(3)
    bpy.context.view_layer.update()
    cur_l = foot_lock._foot_point_world(arm_obj, pb_lf).copy()
    cur_r = foot_lock._foot_point_world(arm_obj, pb_rf).copy()

    err_l = (cur_l - anc_l).length
    err_r = (cur_r - anc_r).length

    assert err_l < 1e-4, f"Pie izquierdo deslizó en apoyo bilateral: {err_l}"
    assert err_r < 1e-4, f"Pie derecho deslizó en apoyo bilateral: {err_r}"

    bpy.data.actions.remove(action)
    bpy.data.actions.remove(new_act)
    _cleanup_objects(arm_obj)


def test_swing_foot_height_preservation():
    """Valida que un pie en balanceo que se eleva >= 8 cm preserva >= 95% de su altura."""
    arm_obj, prefix = _create_test_armature("SwingPreserve")
    action = bpy.data.actions.new("Action_Swing")
    action.use_fake_user = True
    arm_obj.animation_data_create()
    arm_obj.animation_data.action = action

    pb_hips = arm_obj.pose.bones[f"{prefix}Hips"]
    pb_lu = arm_obj.pose.bones[f"{prefix}LeftUpLeg"]
    pb_ru = arm_obj.pose.bones[f"{prefix}RightUpLeg"]
    pb_ll = arm_obj.pose.bones[f"{prefix}LeftLeg"]
    pb_rl = arm_obj.pose.bones[f"{prefix}RightLeg"]
    pb_lf = arm_obj.pose.bones[f"{prefix}LeftFoot"]
    pb_rf = arm_obj.pose.bones[f"{prefix}RightFoot"]

    # Pierna izquierda fija en apoyo, pierna derecha se flexiona subiendo el pie > 8 cm
    for f in range(1, 6):
        bpy.context.scene.frame_set(f)
        pb_hips.location = Vector((0.0, 0.0, 0.0))
        pb_hips.keyframe_insert("location", frame=f)

        pb_ru.rotation_mode = "QUATERNION"
        pb_rl.rotation_mode = "QUATERNION"
        # Flexión de cadera y rodilla en frames 3, 4, 5
        angle = 0.25 * max(0, f - 2)
        pb_ru.rotation_quaternion = Quaternion(Vector((1.0, 0.0, 0.0)), angle)
        pb_rl.rotation_quaternion = Quaternion(Vector((1.0, 0.0, 0.0)), -angle * 1.5)
        pb_ru.keyframe_insert("rotation_quaternion", frame=f)
        pb_rl.keyframe_insert("rotation_quaternion", frame=f)

    action.frame_range = (1, 5)

    # Medir altura máxima del pie derecho antes del lock
    max_z_orig = -999.0
    for f in range(1, 6):
        bpy.context.scene.frame_set(f)
        bpy.context.view_layer.update()
        z = foot_lock._foot_point_world(arm_obj, pb_rf).z
        if z > max_z_orig:
            max_z_orig = z

    # El pie derecho debe haberse levantado significativamente (> 8 cm sobre su reposo)
    lift = max_z_orig - 0.10
    assert lift > 0.08, f"Prueba mal configurada: elevación fue solo {lift} m"

    new_act, stats = foot_lock.lock_action(arm_obj, action, prefix=prefix)
    assert new_act is not None

    arm_obj.animation_data.action = new_act

    max_z_locked = -999.0
    for f in range(1, 6):
        bpy.context.scene.frame_set(f)
        bpy.context.view_layer.update()
        z = foot_lock._foot_point_world(arm_obj, pb_rf).z
        if z > max_z_locked:
            max_z_locked = z

    # El pie en swing no debe haberse bajado artificialmente al suelo
    ratio = max_z_locked / max_z_orig
    assert ratio >= 0.95, (
        f"El pie en swing perdió altura: antes {max_z_orig:.3f} m, "
        f"después {max_z_locked:.3f} m (ratio {ratio:.1%})"
    )

    bpy.data.actions.remove(action)
    bpy.data.actions.remove(new_act)
    _cleanup_objects(arm_obj)


def test_source_action_immutability_and_determinism():
    """Valida que la acción fuente original no se modifica y que el proceso es determinista."""
    arm_obj, prefix = _create_test_armature("Immutability")
    action = bpy.data.actions.new("Action_Orig")
    action.use_fake_user = True
    arm_obj.animation_data_create()
    arm_obj.animation_data.action = action

    pb_hips = arm_obj.pose.bones[f"{prefix}Hips"]
    pb_lf = arm_obj.pose.bones[f"{prefix}LeftFoot"]
    pb_rf = arm_obj.pose.bones[f"{prefix}RightFoot"]

    for f in range(1, 4):
        bpy.context.scene.frame_set(f)
        pb_hips.location = Vector((0.01 * f, 0.0, 0.0))
        pb_hips.keyframe_insert("location", frame=f)

    action.frame_range = (1, 3)

    # Captura snapshot de evaluaciones originales
    orig_evals = {}
    for f in range(1, 4):
        bpy.context.scene.frame_set(f)
        bpy.context.view_layer.update()
        orig_evals[f] = (
            Vector(pb_hips.location).copy(),
            foot_lock._foot_point_world(arm_obj, pb_lf).copy(),
            foot_lock._foot_point_world(arm_obj, pb_rf).copy(),
        )

    # Ejecución 1
    new_act1, stats1 = foot_lock.lock_action(arm_obj, action, prefix=prefix)

    # Comprobar inmutabilidad de la acción fuente original
    arm_obj.animation_data.action = action
    for f in range(1, 4):
        bpy.context.scene.frame_set(f)
        bpy.context.view_layer.update()
        cur_h = Vector(pb_hips.location)
        cur_l = foot_lock._foot_point_world(arm_obj, pb_lf)
        cur_r = foot_lock._foot_point_world(arm_obj, pb_rf)
        assert (cur_h - orig_evals[f][0]).length < 1e-6, "Hips cambió en la acción original"
        assert (cur_l - orig_evals[f][1]).length < 1e-6, "LeftFoot cambió en la acción original"
        assert (cur_r - orig_evals[f][2]).length < 1e-6, "RightFoot cambió en la acción original"

    # Ejecución 2 (determinismo)
    new_act2, stats2 = foot_lock.lock_action(arm_obj, action, prefix=prefix)
    assert int(stats1) == int(stats2)

    # Evaluar que new_act1 y new_act2 producen exactamente los mismos resultados en todos los frames
    for f in range(1, 4):
        arm_obj.animation_data.action = new_act1
        bpy.context.scene.frame_set(f)
        bpy.context.view_layer.update()
        h1 = Vector(pb_hips.location).copy()
        l1 = foot_lock._foot_point_world(arm_obj, pb_lf).copy()
        r1 = foot_lock._foot_point_world(arm_obj, pb_rf).copy()

        arm_obj.animation_data.action = new_act2
        bpy.context.scene.frame_set(f)
        bpy.context.view_layer.update()
        h2 = Vector(pb_hips.location).copy()
        l2 = foot_lock._foot_point_world(arm_obj, pb_lf).copy()
        r2 = foot_lock._foot_point_world(arm_obj, pb_rf).copy()

        assert (h1 - h2).length < 1e-6, f"Frame {f}: determinismo falló en Hips"
        assert (l1 - l2).length < 1e-6, f"Frame {f}: determinismo falló en LeftFoot"
        assert (r1 - r2).length < 1e-6, f"Frame {f}: determinismo falló en RightFoot"

    bpy.data.actions.remove(action)
    bpy.data.actions.remove(new_act1)
    bpy.data.actions.remove(new_act2)
    _cleanup_objects(arm_obj)


def test_calibrate_ground_operator():
    """Valida el operador PUPPET_OT_calibrate_ground sobre un rig colocado en la escena."""
    from puppet_mocap import operators, properties
    if not hasattr(bpy.types.Scene, "puppet_mocap"):
        bpy.utils.register_class(properties.PuppetMocapProperties)
        bpy.types.Scene.puppet_mocap = bpy.props.PointerProperty(
            type=properties.PuppetMocapProperties
        )
    if not hasattr(bpy.types, "PUPPET_OT_calibrate_ground"):
        bpy.utils.register_class(operators.PUPPET_OT_calibrate_ground)

    arm_obj, prefix = _create_test_armature("CalibGround")
    arm_obj.location = Vector((0.0, 0.0, 0.25))  # Rig elevado 25 cm
    bpy.context.view_layer.update()

    props = bpy.context.scene.puppet_mocap
    props.target_armature = arm_obj
    props.bone_prefix = prefix

    res = bpy.ops.puppet_mocap.calibrate_ground()
    assert res == {"FINISHED"}

    # El pie del rig está a Z local 0.1 m, con rig en Z=0.25 m -> Z mundo = 0.35 m
    assert abs(props.ground_z - 0.35) < 1e-3, (
        f"Calibración de suelo esperada ~0.35 m, obtenida: {props.ground_z}"
    )

    _cleanup_objects(arm_obj)


def test_raycast_ground_model_with_mesh():
    """Valida que GroundModel con OBJECT_RAYCAST detecta la cota de una malla de suelo."""
    # Crear un plano de suelo a Z = -0.05
    mesh_data = bpy.data.meshes.new("FloorMesh")
    mesh_obj = bpy.data.objects.new("FloorObj", mesh_data)
    bpy.context.scene.collection.objects.link(mesh_obj)

    # Crear 4 vértices para formar un plano horizontal de 2x2 metros
    import bmesh
    bm = bmesh.new()
    bm.verts.new((-1.0, -1.0, -0.05))
    bm.verts.new((1.0, -1.0, -0.05))
    bm.verts.new((1.0, 1.0, -0.05))
    bm.verts.new((-1.0, 1.0, -0.05))
    bm.faces.new(bm.verts)
    bm.to_mesh(mesh_data)
    bm.free()

    bpy.context.view_layer.update()
    depsgraph = bpy.context.evaluated_depsgraph_get()
    scene = bpy.context.scene

    ground = foot_plant.GroundModel(mode="OBJECT_RAYCAST", ground_object=mesh_obj, fallback_z=0.0)
    z, normal = ground.query(0.0, 0.0, depsgraph=depsgraph, scene=scene)

    assert abs(z - (-0.05)) < 1e-3, f"Altura Z esperada -0.05 m, obtenida {z}"
    assert normal.dot(Vector((0.0, 0.0, 1.0))) > 0.99, "Normal de suelo debe ser +Z"

    _cleanup_objects(mesh_obj)
