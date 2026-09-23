"""Registro real de tomas (Fase 3): list_takes()/find_take() agrupan
cuerpo+cara por take_id, y remove_selected_take() borra SOLO la toma
seleccionada -- antes 'Eliminar esta toma' recorría todas las tomas propias
del rig, demasiado amplio en cuanto hay más de una toma para elegir.
"""
import bpy
from mathutils import Vector

from puppet_mocap import operators, properties


def _register_props_once():
    if not hasattr(bpy.types.Scene, "puppet_mocap"):
        bpy.utils.register_class(properties.PuppetMocapProperties)
        bpy.types.Scene.puppet_mocap = bpy.props.PointerProperty(
            type=properties.PuppetMocapProperties
        )


def _create_test_armature(name_prefix: str = "RegistryRig"):
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


def _keyed_action(name, arm):
    """Una action con al menos 1 keyframe -- frame_range solo es fiable con
    datos reales, una action vacía da (0,0) o falla según la versión."""
    action = bpy.data.actions.new(name)
    action.use_fake_user = True
    ad = arm.animation_data_create()
    prev = ad.action
    ad.action = action
    pb = arm.pose.bones["mixamorig:Hips"]
    pb.rotation_mode = "QUATERNION"
    for f in (1, 10):
        pb.keyframe_insert("rotation_quaternion", frame=f)
    ad.action = prev
    return action


def _tag(action, arm, take_id, label, kind, source="capture", corrects=None):
    action["puppet_mocap_take"] = True
    action["puppet_mocap_owner"] = arm.name
    action["puppet_mocap_take_id"] = take_id
    action["puppet_mocap_take_label"] = label
    action["puppet_mocap_take_kind"] = kind
    action["puppet_mocap_take_source"] = source
    if corrects:
        action["puppet_mocap_corrects"] = corrects


def test_list_takes_groups_body_and_face_by_take_id():
    arm = _create_test_armature()
    try:
        body = _keyed_action("Body1", arm)
        _tag(body, arm, "T1", "Toma 1", "body")
        face = bpy.data.actions.new("Face1")
        face.use_fake_user = True
        _tag(face, arm, "T1", "Toma 1", "face")

        takes = operators.list_takes(arm.name)

        assert len(takes) == 1
        t = takes[0]
        assert t["body_action"] == "Body1"
        assert t["face_action"] == "Face1"
        assert t["label"] == "Toma 1"
    finally:
        _cleanup(arm)
        for n in ("Body1", "Face1"):
            a = bpy.data.actions.get(n)
            if a is not None:
                bpy.data.actions.remove(a)


def test_list_takes_orders_newest_first_and_ignores_other_rig():
    arm = _create_test_armature("Multi")
    other = _create_test_armature("MultiOther")
    try:
        a1 = _keyed_action("Take_old", arm)
        _tag(a1, arm, "20260101_000000_001", "Vieja", "body")
        a2 = _keyed_action("Take_new", arm)
        _tag(a2, arm, "20260922_000000_001", "Nueva", "body")
        a3 = _keyed_action("Take_other_rig", other)
        _tag(a3, other, "20260922_000000_002", "De otro rig", "body")

        takes = operators.list_takes(arm.name)

        assert [t["label"] for t in takes] == ["Nueva", "Vieja"]
    finally:
        _cleanup(arm, other)
        for n in ("Take_old", "Take_new", "Take_other_rig"):
            a = bpy.data.actions.get(n)
            if a is not None:
                bpy.data.actions.remove(a)


def test_remove_selected_take_only_deletes_the_selected_one():
    """El caso central del brief: con VARIAS tomas del mismo rig, borrar solo
    debe afectar a la seleccionada."""
    _register_props_once()
    arm = _create_test_armature("DeleteScoped")
    try:
        a1 = _keyed_action("Keep1", arm)
        _tag(a1, arm, "T_keep", "Conservar", "body")
        a2 = _keyed_action("Delete1", arm)
        _tag(a2, arm, "T_delete", "Borrar", "body")

        props = bpy.context.scene.puppet_mocap
        props.selected_take_id = "T_delete"

        ok, msg = operators.remove_selected_take(props)

        assert ok is True
        assert bpy.data.actions.get("Delete1") is None
        assert bpy.data.actions.get("Keep1") is not None, (
            "una toma NO seleccionada del mismo rig no debe tocarse"
        )
        remaining_ids = [t["take_id"] for t in operators.list_takes(arm.name)]
        assert remaining_ids == ["T_keep"]
        assert props.selected_take_id == "T_keep", (
            "tras borrar, selected_take_id debe moverse a otra toma existente"
        )
    finally:
        _cleanup(arm)
        for n in ("Keep1", "Delete1"):
            a = bpy.data.actions.get(n)
            if a is not None:
                bpy.data.actions.remove(a)


def test_remove_selected_take_clears_selection_when_none_left():
    _register_props_once()
    arm = _create_test_armature("DeleteLast")
    try:
        a1 = _keyed_action("Only1", arm)
        _tag(a1, arm, "T_only", "Única", "body")
        props = bpy.context.scene.puppet_mocap
        props.selected_take_id = "T_only"

        ok, _ = operators.remove_selected_take(props)

        assert ok is True
        assert props.selected_take_id == ""
    finally:
        _cleanup(arm)
        a = bpy.data.actions.get("Only1")
        if a is not None:
            bpy.data.actions.remove(a)


def test_remove_selected_take_with_no_selection_fails_cleanly():
    _register_props_once()
    arm = _create_test_armature("DeleteNoSel")
    try:
        props = bpy.context.scene.puppet_mocap
        props.selected_take_id = ""
        ok, msg = operators.remove_selected_take(props)
        assert ok is False
    finally:
        _cleanup(arm)


def test_remove_selected_take_detaches_active_slot_before_removing():
    """Si la toma seleccionada está ACTIVA en el rig (se estaba reproduciendo),
    borrarla debe soltar el slot antes de eliminar el datablock."""
    _register_props_once()
    arm = _create_test_armature("DeleteActive")
    try:
        action = _keyed_action("ActiveTake", arm)
        _tag(action, arm, "T_active", "Activa", "body")
        arm.animation_data.action = action  # queda asignada, como si se reprodujera

        props = bpy.context.scene.puppet_mocap
        props.selected_take_id = "T_active"

        ok, _ = operators.remove_selected_take(props)

        assert ok is True
        assert arm.animation_data.action is None
        assert bpy.data.actions.get("ActiveTake") is None
    finally:
        _cleanup(arm)
        a = bpy.data.actions.get("ActiveTake")
        if a is not None:
            bpy.data.actions.remove(a)
