"""'Corregir pies' (PUPPET_OT_lock_current_take) sobre el registro de tomas:
debe operar sobre la toma SELECCIONADA (no la action activa a secas), crear
una toma NUEVA enlazada a la original vía 'corrects', y NUNCA tocar ni
borrar la original (brief de Fase 3: "mantener copia original al corregir
tomas").
"""
import bpy
from mathutils import Quaternion, Vector

from puppet_mocap import operators, properties


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


def _create_test_armature(name_prefix: str = "LockRig"):
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


def _bake_original_take(arm_obj, prefix, take_id="T_orig", label="Toma original"):
    action = bpy.data.actions.new("Action_LockOrig")
    action.use_fake_user = True
    arm_obj.animation_data_create()
    arm_obj.animation_data.action = action

    pb_hips = arm_obj.pose.bones[f"{prefix}Hips"]
    pb_rf = arm_obj.pose.bones[f"{prefix}RightFoot"]
    for f in range(1, 4):
        bpy.context.scene.frame_set(f)
        pb_hips.location = Vector((0.002 * (f - 1), 0.003 * (f - 1), 0.0))
        pb_hips.keyframe_insert("location", frame=f)
        pb_rf.rotation_mode = "QUATERNION"
        pb_rf.rotation_quaternion = Quaternion(Vector((1.0, 0.0, 0.0)), 0.1 * (f - 1))
        pb_rf.keyframe_insert("rotation_quaternion", frame=f)
    action.frame_range = (1, 3)
    arm_obj.animation_data.action = None

    action["puppet_mocap_take"] = True
    action["puppet_mocap_owner"] = arm_obj.name
    action["puppet_mocap_take_id"] = take_id
    action["puppet_mocap_take_label"] = label
    action["puppet_mocap_take_kind"] = "body"
    action["puppet_mocap_take_source"] = "capture"
    return action


def test_lock_current_take_preserves_original_and_links_correction():
    _register_props_once()
    _register_operator_once(operators.PUPPET_OT_lock_current_take)
    arm_obj, prefix = _create_test_armature()
    try:
        from puppet_mocap import retarget
        retarget.set_target_armature(arm_obj.name)
        original = _bake_original_take(arm_obj, prefix)
        original_frame_range = tuple(original.frame_range)

        props = bpy.context.scene.puppet_mocap
        props.target_armature = arm_obj
        props.bone_prefix = prefix
        props.selected_take_id = "T_orig"

        result = bpy.ops.puppet_mocap.lock_current_take()

        assert result == {"FINISHED"}
        # La original sigue existiendo, sin tocar.
        still_there = bpy.data.actions.get("Action_LockOrig")
        assert still_there is not None
        assert tuple(still_there.frame_range) == original_frame_range
        assert still_there.get("puppet_mocap_take_id") == "T_orig"

        # selected_take_id avanzó a una toma NUEVA que corrige la original.
        new_id = props.selected_take_id
        assert new_id != "T_orig"
        new_take = operators.find_take(arm_obj.name, new_id)
        assert new_take is not None
        assert new_take["corrects"] == "T_orig"
        assert "corregidos" in new_take["label"]

        # Ambas tomas conviven en el registro (no se perdió ninguna).
        all_ids = {t["take_id"] for t in operators.list_takes(arm_obj.name)}
        assert {"T_orig", new_id} <= all_ids
    finally:
        if bpy.context.screen.is_animation_playing:
            bpy.ops.screen.animation_cancel(restore_frame=False)
        retarget.set_target_armature(None)
        _cleanup(arm_obj)
        for a in list(bpy.data.actions):
            if a.get("puppet_mocap_owner") == "LockRig_Obj" or a.name == "Action_LockOrig":
                bpy.data.actions.remove(a)


def test_lock_current_take_without_selection_fails_cleanly():
    _register_props_once()
    _register_operator_once(operators.PUPPET_OT_lock_current_take)
    arm_obj, prefix = _create_test_armature("LockRigNoSel")
    try:
        from puppet_mocap import retarget
        retarget.set_target_armature(arm_obj.name)
        props = bpy.context.scene.puppet_mocap
        props.target_armature = arm_obj
        props.bone_prefix = prefix
        props.selected_take_id = ""

        import pytest
        with pytest.raises(RuntimeError, match="canal de cuerpo"):
            bpy.ops.puppet_mocap.lock_current_take()
    finally:
        retarget.set_target_armature(None)
        _cleanup(arm_obj)
