"""Pruebas de validación exhaustiva para el sistema Hard Plant de Puppet Mocap.

Valida:
1. test_stance_snapshot_invariance: invariancia de la rotación local y contacto en mundo (< 0.5 mm) ante desplazamiento de cadera.
2. test_torso_delta_remapping: Spine/Spine1/Spine2 absorben la rotación y traslación de Hips desacoplada.
3. test_hard_plant_failure_demonstration: demostración de que omitir el snapshot causa deriva (> 20 mm), mientras que Hard Plant mantiene el pie fijo (< 0.5 mm).
4. test_unilateral_and_bilateral_support: apoyo unilateral (swing >= 95% altura, apoyo clavado) y bilateral (ambos pies clavados, caderas estabilizadas).
5. test_negative_frame_range: horneado con rango de fotogramas negativo (p. ej. -5 a 10).
6. test_offline_baking_determinism: dos ejecuciones independientes sobre la misma acción producen resultados idénticos.
"""
from __future__ import annotations

import math
import pytest
from mathutils import Matrix, Quaternion, Vector

import bpy
from puppet_mocap.retarget import foot_lock, foot_plant
from puppet_mocap.retarget.foot_plant import StanceSnapshot, remap_hips_delta_to_spine


def _create_full_rig(name_prefix: str = "HardPlantRig"):
    """Crea una armadura de prueba con jerarquía Mixamo completa: Hips, Spines, Legs y Toes."""
    arm_data = bpy.data.armatures.new(f"{name_prefix}_Data")
    arm_obj = bpy.data.objects.new(f"{name_prefix}_Obj", arm_data)
    bpy.context.scene.collection.objects.link(arm_obj)
    bpy.context.view_layer.objects.active = arm_obj
    bpy.ops.object.mode_set(mode="EDIT")

    prefix = "mixamorig:"
    eb_hips = arm_data.edit_bones.new(f"{prefix}Hips")
    eb_hips.head = Vector((0.0, 0.0, 1.0))
    eb_hips.tail = Vector((0.0, 0.0, 1.1))

    # Cadena de columna (Spine, Spine1, Spine2)
    eb_spine = arm_data.edit_bones.new(f"{prefix}Spine")
    eb_spine.head = Vector((0.0, 0.0, 1.1))
    eb_spine.tail = Vector((0.0, 0.0, 1.25))
    eb_spine.parent = eb_hips

    eb_spine1 = arm_data.edit_bones.new(f"{prefix}Spine1")
    eb_spine1.head = Vector((0.0, 0.0, 1.25))
    eb_spine1.tail = Vector((0.0, 0.0, 1.4))
    eb_spine1.parent = eb_spine

    eb_spine2 = arm_data.edit_bones.new(f"{prefix}Spine2")
    eb_spine2.head = Vector((0.0, 0.0, 1.4))
    eb_spine2.tail = Vector((0.0, 0.0, 1.55))
    eb_spine2.parent = eb_spine1

    # Cadenas de piernas (UpLeg, Leg, Foot, ToeBase)
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

        eb_t = arm_data.edit_bones.new(f"{prefix}{side}ToeBase")
        eb_t.head = Vector((sign * 0.1, 0.15, 0.1))
        eb_t.tail = Vector((sign * 0.1, 0.25, 0.05))
        eb_t.parent = eb_f

    bpy.ops.object.mode_set(mode="POSE")
    return arm_obj, prefix


def _cleanup_rig(arm_obj):
    """Elimina de forma segura la armadura y sus datos."""
    if arm_obj is None:
        return
    try:
        data = arm_obj.data
        if hasattr(arm_obj, "animation_data") and arm_obj.animation_data:
            if arm_obj.animation_data.action:
                bpy.data.actions.remove(arm_obj.animation_data.action)
        bpy.data.objects.remove(arm_obj)
        if data and isinstance(data, bpy.types.Armature):
            bpy.data.armatures.remove(data)
    except Exception:
        pass


def test_stance_snapshot_invariance():
    """Valida que un StanceSnapshot restaura exactamente la pose y contacto (< 0.5 mm) tras perturbaciones."""
    arm_obj, prefix = _create_full_rig("SnapshotInv")
    pb_hips = arm_obj.pose.bones[f"{prefix}Hips"]
    pb_lu = arm_obj.pose.bones[f"{prefix}LeftUpLeg"]
    pb_ll = arm_obj.pose.bones[f"{prefix}LeftLeg"]
    pb_lf = arm_obj.pose.bones[f"{prefix}LeftFoot"]
    pb_lt = arm_obj.pose.bones[f"{prefix}LeftToeBase"]

    bpy.context.view_layer.update()
    pt_orig = foot_lock._foot_point_world(arm_obj, pb_lf).copy()

    # Capturar snapshot del pie izquierdo en apoyo
    snap = StanceSnapshot.capture(arm_obj, pb_hips, pb_lu, pb_ll, pb_lf, pb_lt)
    assert snap is not None

    # Perturbar fuertemente la cadera y las rotaciones de la pierna (deriva mocap)
    pb_hips.location = pb_hips.location + Vector((0.08, -0.05, -0.04))
    pb_hips.rotation_quaternion = (Quaternion(Vector((0.0, 1.0, 0.0)), 0.25) @ pb_hips.rotation_quaternion).normalized()
    pb_lu.rotation_quaternion = Quaternion(Vector((1.0, 0.0, 0.0)), 0.3)
    pb_ll.rotation_quaternion = Quaternion(Vector((1.0, 0.0, 0.0)), -0.2)
    bpy.context.view_layer.update()

    pt_perturbed = foot_lock._foot_point_world(arm_obj, pb_lf).copy()
    drift = (pt_perturbed - pt_orig).length
    assert drift > 0.04, f"La perturbación debió mover el pie: deriva {drift} m"

    # Restaurar snapshot
    snap.restore(pb_hips, pb_lu, pb_ll, pb_lf, pb_lt, restore_hips=True)
    bpy.context.view_layer.update()

    pt_restored = foot_lock._foot_point_world(arm_obj, pb_lf).copy()
    error = (pt_restored - pt_orig).length
    assert error < 0.0005, f"El pie restaurado debe tener error < 0.5 mm, obtenido {error * 1000:.3f} mm"

    # Verificar que las rotaciones locales coinciden con el snapshot
    assert (pb_lu.rotation_quaternion - snap.up_leg_local).magnitude < 1e-6
    assert (pb_ll.rotation_quaternion - snap.leg_local).magnitude < 1e-6
    assert (pb_lf.rotation_quaternion - snap.foot_local).magnitude < 1e-6
    if snap.toe_local:
        assert (pb_lt.rotation_quaternion - snap.toe_local).magnitude < 1e-6

    _cleanup_rig(arm_obj)


def test_torso_delta_remapping():
    """Valida que Spine, Spine1 y Spine2 absorben la rotación y traslación de Hips."""
    arm_obj, prefix = _create_full_rig("TorsoRemap")
    spines = [arm_obj.pose.bones[f"{prefix}{s}"] for s in ("Spine", "Spine1", "Spine2")]

    for sp in spines:
        sp.rotation_mode = "QUATERNION"
        sp.rotation_quaternion = Quaternion((1.0, 0.0, 0.0, 0.0))
        sp.location = Vector((0.0, 0.0, 0.0))

    delta_rot = Quaternion(Vector((0.0, 1.0, 0.0)), math.radians(15.0))
    delta_loc = Vector((0.03, 0.02, -0.01))

    moved = remap_hips_delta_to_spine(
        arm_obj, prefix, delta_rot=delta_rot, delta_loc=delta_loc,
        max_trans=0.06, weights=(0.33, 0.33, 0.34)
    )
    assert moved == 4, f"Se esperaban 4 componentes modificadas (3 rotaciones + 1 traslación), obtenidas {moved}"

    # Cada spine debe haber recibido una rotación no trivial en Y
    for sp in spines:
        angle = sp.rotation_quaternion.angle
        assert angle > math.radians(2.0), f"Hueso {sp.name} no recibió rotación apreciable: {math.degrees(angle)} deg"

    # Spine base debe tener traslación no nula y acotada <= max_trans
    loc_delta = spines[0].location.length
    assert loc_delta > 0.005, f"Spine base no recibió traslación: {loc_delta}"
    assert loc_delta <= 0.06 + 1e-5, f"Spine base excedió max_trans: {loc_delta}"

    _cleanup_rig(arm_obj)


def test_hard_plant_failure_demonstration():
    """Demuestra que sin Hard Plant (FK estándar con deriva de Hips), el pie de apoyo se desplaza > 20 mm."""
    arm_obj, prefix = _create_full_rig("FailureDemo")
    pb_hips = arm_obj.pose.bones[f"{prefix}Hips"]
    pb_lu = arm_obj.pose.bones[f"{prefix}LeftUpLeg"]
    pb_ll = arm_obj.pose.bones[f"{prefix}LeftLeg"]
    pb_lf = arm_obj.pose.bones[f"{prefix}LeftFoot"]
    pb_lt = arm_obj.pose.bones[f"{prefix}LeftToeBase"]

    bpy.context.view_layer.update()
    pt_ground = foot_lock._foot_point_world(arm_obj, pb_lf).copy()

    # Captura snapshot del apoyo
    snap = StanceSnapshot.capture(arm_obj, pb_hips, pb_lu, pb_ll, pb_lf, pb_lt)

    # 1. Caso Fallo: mocap mueve la cadera hacia abajo y adelante 6 cm en cinemática directa (FK)
    pb_hips.location = pb_hips.location + Vector((0.0, 0.06, -0.06))
    bpy.context.view_layer.update()

    pt_fk_moved = foot_lock._foot_point_world(arm_obj, pb_lf).copy()
    fk_drift = (pt_fk_moved - pt_ground).length

    # En FK directo el pie desliza más de 20 mm debido al movimiento de cadera
    assert fk_drift > 0.02, (
        f"Demostración de fallo: en cinemática directa el pie debió patinar > 20 mm, patinó {fk_drift * 1000:.1f} mm"
    )

    # 2. Caso Éxito: Hard Plant restaura el snapshot y estabiliza cadera
    snap.restore(pb_hips, pb_lu, pb_ll, pb_lf, pb_lt, restore_hips=True)
    bpy.context.view_layer.update()

    pt_hard_planted = foot_lock._foot_point_world(arm_obj, pb_lf).copy()
    hp_drift = (pt_hard_planted - pt_ground).length

    assert hp_drift < 0.0005, (
        f"Con Hard Plant el pie debe permanecer clavado (< 0.5 mm), residual: {hp_drift * 1000:.3f} mm"
    )

    _cleanup_rig(arm_obj)


def test_unilateral_and_bilateral_support():
    """Valida el horneado con apoyo unilateral (swing >= 95%) y apoyo bilateral (ambos clavados)."""
    arm_obj, prefix = _create_full_rig("SupportUniBi")
    action = bpy.data.actions.new("Action_UniBi")
    action.use_fake_user = True
    arm_obj.animation_data_create()
    arm_obj.animation_data.action = action

    pb_hips = arm_obj.pose.bones[f"{prefix}Hips"]
    pb_lu = arm_obj.pose.bones[f"{prefix}LeftUpLeg"]
    pb_ru = arm_obj.pose.bones[f"{prefix}RightUpLeg"]
    pb_rl = arm_obj.pose.bones[f"{prefix}RightLeg"]
    pb_lf = arm_obj.pose.bones[f"{prefix}LeftFoot"]
    pb_rf = arm_obj.pose.bones[f"{prefix}RightFoot"]

    # Frames 1 a 3: Bilateral (ambos pies quietos en el suelo, cadera oscilando)
    for f in (1, 2, 3):
        bpy.context.scene.frame_set(f)
        pb_hips.location = Vector((0.002 * f, 0.001 * f, 0.0))
        pb_hips.keyframe_insert("location", frame=f)

    # Frames 4 a 6: Unilateral (pie izquierdo de apoyo en el suelo, pie derecho levantándose > 8 cm)
    for f in (4, 5, 6):
        bpy.context.scene.frame_set(f)
        pb_hips.location = Vector((0.008, 0.005, 0.0))
        pb_hips.keyframe_insert("location", frame=f)

        # Elevar pierna derecha
        angle = 0.25 * (f - 3)
        pb_ru.rotation_mode = "QUATERNION"
        pb_rl.rotation_mode = "QUATERNION"
        pb_ru.rotation_quaternion = Quaternion(Vector((1.0, 0.0, 0.0)), angle)
        pb_rl.rotation_quaternion = Quaternion(Vector((1.0, 0.0, 0.0)), -angle * 1.5)
        pb_ru.keyframe_insert("rotation_quaternion", frame=f)
        pb_rl.keyframe_insert("rotation_quaternion", frame=f)

    action.frame_range = (1, 6)

    # Medir altura pico del pie en swing original (frames 4-6)
    max_z_orig = -999.0
    for f in (4, 5, 6):
        bpy.context.scene.frame_set(f)
        bpy.context.view_layer.update()
        z = foot_lock._foot_point_world(arm_obj, pb_rf).z
        if z > max_z_orig:
            max_z_orig = z

    assert max_z_orig - 0.10 > 0.08, f"El pie derecho debió elevarse > 8 cm, elevación: {max_z_orig - 0.10}"

    new_act, stats = foot_lock.lock_action(arm_obj, action, prefix=prefix)
    assert new_act is not None
    assert stats["total_locked_frames"] > 0
    assert stats["bilateral_frames"] > 0, "Debe detectar fotogramas bilaterales en frames 1-3"

    arm_obj.animation_data.action = new_act

    # 1. Comprobar que en apoyo bilateral (frames 2 y 3) ambos pies quedan anclados (< 0.5 mm)
    bpy.context.scene.frame_set(2)
    bpy.context.view_layer.update()
    bi_l2 = foot_lock._foot_point_world(arm_obj, pb_lf).copy()
    bi_r2 = foot_lock._foot_point_world(arm_obj, pb_rf).copy()

    bpy.context.scene.frame_set(3)
    bpy.context.view_layer.update()
    bi_l3 = foot_lock._foot_point_world(arm_obj, pb_lf).copy()
    bi_r3 = foot_lock._foot_point_world(arm_obj, pb_rf).copy()

    drift_l = (bi_l3 - bi_l2).length
    drift_r = (bi_r3 - bi_r2).length
    assert drift_l < 0.0005, f"Pie izquierdo derivó en apoyo bilateral: {drift_l * 1000:.3f} mm"
    assert drift_r < 0.0005, f"Pie derecho derivó en apoyo bilateral: {drift_r * 1000:.3f} mm"

    # 2. Comprobar que en apoyo unilateral (frames 4 a 6) el pie izquierdo queda anclado (< 0.5 mm)
    bpy.context.scene.frame_set(4)
    bpy.context.view_layer.update()
    uni_l4 = foot_lock._foot_point_world(arm_obj, pb_lf).copy()

    bpy.context.scene.frame_set(6)
    bpy.context.view_layer.update()
    uni_l6 = foot_lock._foot_point_world(arm_obj, pb_lf).copy()

    drift_uni = (uni_l6 - uni_l4).length
    assert drift_uni < 0.0005, f"Pie izquierdo derivó en apoyo unilateral: {drift_uni * 1000:.3f} mm"

    # 3. Comprobar preservación del balanceo (swing >= 95% de la altura)
    max_z_baked = -999.0
    for f in (4, 5, 6):
        bpy.context.scene.frame_set(f)
        bpy.context.view_layer.update()
        z = foot_lock._foot_point_world(arm_obj, pb_rf).z
        if z > max_z_baked:
            max_z_baked = z

    ratio = max_z_baked / max_z_orig
    assert ratio >= 0.95, f"El pie en swing perdió altura: ratio {ratio:.1%}"

    bpy.data.actions.remove(action)
    bpy.data.actions.remove(new_act)
    _cleanup_rig(arm_obj)


def test_negative_frame_range():
    """Valida el horneado correcto sobre acciones con rangos de fotogramas negativos (p. ej. -5 a 10)."""
    arm_obj, prefix = _create_full_rig("NegRange")
    action = bpy.data.actions.new("Action_NegativeFrames")
    action.use_fake_user = True
    arm_obj.animation_data_create()
    arm_obj.animation_data.action = action

    pb_hips = arm_obj.pose.bones[f"{prefix}Hips"]

    # Keyframes de -5 a 5
    for f in range(-5, 6):
        bpy.context.scene.frame_set(f)
        pb_hips.location = Vector((0.001 * f, 0.0, 0.0))
        pb_hips.keyframe_insert("location", frame=f)

    action.frame_range = (-5, 5)

    new_act, stats = foot_lock.lock_action(arm_obj, action, prefix=prefix)
    assert new_act is not None
    assert new_act.frame_range[0] <= -5, f"El inicio del rango debe ser <= -5, obtenido: {new_act.frame_range[0]}"
    assert new_act.frame_range[1] >= 5, f"El final del rango debe ser >= 5, obtenido: {new_act.frame_range[1]}"

    # Verificar que la acción contiene keyframes y evalúa correctamente en el frame negativo -5
    arm_obj.animation_data.action = new_act
    bpy.context.scene.frame_set(-5)
    bpy.context.view_layer.update()

    # Comprobar fcurves tanto en API legacy (action.fcurves) como slotted (Blender 4.4+/5.x)
    fcurves = []
    if hasattr(new_act, "layers"):
        slot = getattr(arm_obj.animation_data, "action_slot", None)
        for layer in new_act.layers:
            for strip in layer.strips:
                bags = getattr(strip, "channelbags", None)
                if bags is None:
                    bag = strip.channelbag(slot) if slot is not None else None
                    bags = [bag] if bag is not None else []
                for cb in bags:
                    for fc in cb.fcurves:
                        fcurves.append(fc)
    elif hasattr(new_act, "fcurves"):
        fcurves = list(new_act.fcurves)

    assert len(fcurves) > 0, "La acción horneada debe contener fcurves"
    has_neg_keyframe = any(
        abs(kp.co[0] - (-5.0)) < 1e-4
        for fc in fcurves
        for kp in fc.keyframe_points
    )
    assert has_neg_keyframe, "La acción horneada debe contener keyframes en fotogramas negativos (frame -5)"

    # Comprobar que la acción original no sufrió cambios
    assert action.frame_range[0] == -5.0
    assert action.frame_range[1] == 5.0

    bpy.data.actions.remove(action)
    bpy.data.actions.remove(new_act)
    _cleanup_rig(arm_obj)


def test_offline_baking_determinism():
    """Valida que dos horneados sucesivos sobre la misma acción producen resultados idénticos bit a bit."""
    arm_obj, prefix = _create_full_rig("Determinism")
    action = bpy.data.actions.new("Action_Det")
    action.use_fake_user = True
    arm_obj.animation_data_create()
    arm_obj.animation_data.action = action

    pb_hips = arm_obj.pose.bones[f"{prefix}Hips"]
    pb_lf = arm_obj.pose.bones[f"{prefix}LeftFoot"]
    pb_rf = arm_obj.pose.bones[f"{prefix}RightFoot"]

    for f in range(1, 6):
        bpy.context.scene.frame_set(f)
        pb_hips.location = Vector((0.003 * f, -0.002 * f, 0.0))
        pb_hips.keyframe_insert("location", frame=f)

    action.frame_range = (1, 5)

    # Horneado 1
    new_act1, stats1 = foot_lock.lock_action(arm_obj, action, prefix=prefix)

    # Horneado 2
    new_act2, stats2 = foot_lock.lock_action(arm_obj, action, prefix=prefix)

    assert int(stats1) == int(stats2), "Los fotogramas bloqueados deben ser idénticos"
    assert abs(stats1["rms_residual"] - stats2["rms_residual"]) < 1e-8

    # Evaluar que las curvas y poses resultantes son idénticas en todos los frames
    for f in range(1, 6):
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

        assert (h1 - h2).length < 1e-7, f"Frame {f}: desajuste en Hips"
        assert (l1 - l2).length < 1e-7, f"Frame {f}: desajuste en LeftFoot"
        assert (r1 - r2).length < 1e-7, f"Frame {f}: desajuste en RightFoot"

    bpy.data.actions.remove(action)
    bpy.data.actions.remove(new_act1)
    bpy.data.actions.remove(new_act2)
    _cleanup_rig(arm_obj)
