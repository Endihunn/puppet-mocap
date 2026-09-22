"""Tests para PUPPET_OT_reset_rig / fix_orientation(force=True) (P2: "el reset
también pone a cero la rotación del objeto, además de la pose" —
Fase 2 la nombra explícitamente como 'preservación de la colocación del rig').

Antes: 'Reset Rig' siempre zeroaba arm.rotation_euler/rotation_quaternion,
borrando cualquier colocación deliberada (p.ej. el +90° X típico de un rig
Mixamo). Ahora por default solo resetea la POSE; el transform del objeto
requiere reset_object_transform=True explícito.
"""
import math

import bpy
from mathutils import Euler, Vector

from puppet_mocap import operators, properties
from puppet_mocap.retarget import common as retarget_common


def _register_props_once():
    if not hasattr(bpy.types.Scene, "puppet_mocap"):
        bpy.utils.register_class(properties.PuppetMocapProperties)
        bpy.types.Scene.puppet_mocap = bpy.props.PointerProperty(
            type=properties.PuppetMocapProperties
        )


def _register_operator_once(cls):
    try:
        bpy.utils.register_class(cls)
    except ValueError:
        pass


def _create_test_armature(name_prefix: str = "ResetRig"):
    arm_data = bpy.data.armatures.new(f"{name_prefix}_Data")
    arm_obj = bpy.data.objects.new(f"{name_prefix}_Obj", arm_data)
    bpy.context.scene.collection.objects.link(arm_obj)
    bpy.context.view_layer.objects.active = arm_obj
    bpy.ops.object.mode_set(mode="EDIT")
    eb = arm_data.edit_bones.new("mixamorig:Hips")
    eb.head = Vector((0.0, 0.0, 1.0))
    eb.tail = Vector((0.0, 0.0, 1.1))
    bpy.ops.object.mode_set(mode="POSE")
    return arm_obj


def _cleanup(*objs):
    for obj in objs:
        if obj is None:
            continue
        try:
            if hasattr(obj, "data") and obj.data is not None:
                bpy.data.armatures.remove(obj.data)
            bpy.data.objects.remove(obj)
        except Exception:
            pass


def _deform_pose(arm):
    pb = arm.pose.bones["mixamorig:Hips"]
    pb.rotation_mode = "QUATERNION"
    pb.rotation_quaternion = (0.9, 0.1, 0.1, 0.1)
    pb.location = (0.1, 0.2, 0.3)


def test_reset_rig_default_preserves_object_placement():
    """El +90 X típico de un rig Mixamo (colocación deliberada) debe sobrevivir
    a un reset de pose normal."""
    arm = _create_test_armature()
    try:
        from puppet_mocap import retarget
        retarget.set_target_armature(arm.name)
        arm.rotation_mode = "XYZ"
        arm.rotation_euler = Euler((math.radians(90), 0.0, 0.0), "XYZ")
        _deform_pose(arm)

        ok = retarget_common.fix_orientation(force=True, prefix="mixamorig:")

        assert ok is True
        assert math.isclose(arm.rotation_euler.x, math.radians(90), abs_tol=1e-6), (
            "reset_object_transform=False no debe tocar la rotación del objeto"
        )
        pb = arm.pose.bones["mixamorig:Hips"]
        assert tuple(pb.rotation_quaternion) == (1.0, 0.0, 0.0, 0.0)
        assert tuple(pb.location) == (0.0, 0.0, 0.0)
    finally:
        retarget.set_target_armature(None)
        _cleanup(arm)


def test_reset_rig_explicit_opt_in_also_resets_object_transform():
    arm = _create_test_armature("ResetRigOptIn")
    try:
        from puppet_mocap import retarget
        retarget.set_target_armature(arm.name)
        arm.rotation_mode = "XYZ"
        arm.rotation_euler = Euler((math.radians(90), 0.0, 0.0), "XYZ")
        _deform_pose(arm)

        ok = retarget_common.fix_orientation(force=True, prefix="mixamorig:",
                                              reset_object_transform=True)

        assert ok is True
        assert math.isclose(arm.rotation_euler.x, 0.0, abs_tol=1e-6)
        assert tuple(arm.rotation_quaternion) == (1.0, 0.0, 0.0, 0.0)
    finally:
        retarget.set_target_armature(None)
        _cleanup(arm)


def test_reset_rig_operator_default_property_is_false():
    """El botón normal del panel (sin tocar F6) NO debe tocar el objeto."""
    _register_props_once()
    _register_operator_once(operators.PUPPET_OT_reset_rig)
    arm = _create_test_armature("ResetRigOp")
    try:
        from puppet_mocap import retarget
        bpy.context.scene.puppet_mocap.target_armature = arm
        retarget.set_target_armature(arm.name)
        arm.rotation_mode = "XYZ"
        arm.rotation_euler = Euler((math.radians(90), 0.0, 0.0), "XYZ")
        _deform_pose(arm)

        result = bpy.ops.puppet_mocap.reset_rig()

        assert result == {"FINISHED"}
        assert math.isclose(arm.rotation_euler.x, math.radians(90), abs_tol=1e-6)
    finally:
        bpy.context.scene.puppet_mocap.target_armature = None
        retarget.set_target_armature(None)
        _cleanup(arm)
