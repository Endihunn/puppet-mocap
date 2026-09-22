"""Tests de integración en Blender para 'Eliminar esta toma' (antes 'Borrar
Keyframes', P0 del brief de auditoría 2026-09-22).

Valida el contrato de borrado seguro en retarget/common.py y retarget/face.py:
1. Una toma propia (marcador puppet_mocap_take/owner) activa en el armature
   objetivo SÍ se borra.
2. Una action ajena (sin marcador, asignada a mano por el usuario) activa en
   el armature objetivo NUNCA se toca.
3. Una toma propia que sigue compartida con otro objeto (otro usuario real,
   no solo fake_user) se desasigna de este armature pero el datablock se
   conserva (no se fuerza do_unlink) porque el otro objeto todavía la usa.
4. Las tomas horneadas y marcadas de OTRO rig del archivo sobreviven (T3).
5. Lo mismo para la animación facial (shape keys): propia se borra, ajena se
   conserva.
"""
import bpy
from mathutils import Vector

from puppet_mocap.retarget import common, face


def _create_test_armature(name_prefix: str = "ClearKF"):
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
                if isinstance(obj.data, bpy.types.Armature):
                    bpy.data.armatures.remove(obj.data)
                elif isinstance(obj.data, bpy.types.Mesh):
                    bpy.data.meshes.remove(obj.data)
            bpy.data.objects.remove(obj)
        except Exception:
            pass


def _own_take_action(name: str, owner_arm_name: str):
    action = bpy.data.actions.new(name)
    action["puppet_mocap_take"] = True
    action["puppet_mocap_owner"] = owner_arm_name
    return action


def test_own_active_take_is_removed():
    arm = _create_test_armature("OwnActive")
    try:
        action = _own_take_action("PuppetTake_own", arm.name)
        action.use_fake_user = True
        arm.animation_data_create().action = action
        action_name = action.name

        assert common.clear_all_keyframes() is True

        assert arm.animation_data.action is None
        assert bpy.data.actions.get(action_name) is None
    finally:
        _cleanup(arm)


def test_foreign_active_action_is_preserved():
    """Action sin marcador (el usuario la asignó a mano): no se toca."""
    arm = _create_test_armature("ForeignActive")
    try:
        foreign = bpy.data.actions.new("MiAnimacionPropia")
        arm.animation_data_create().action = foreign
        foreign_name = foreign.name

        assert common.clear_all_keyframes() is True

        assert arm.animation_data.action is not None
        assert arm.animation_data.action.name == foreign_name
        assert bpy.data.actions.get(foreign_name) is not None
    finally:
        _cleanup(arm)
        action = bpy.data.actions.get("MiAnimacionPropia")
        if action is not None:
            bpy.data.actions.remove(action)


def test_shared_own_take_is_detached_not_deleted():
    """Toma propia compartida con OTRO objeto: se suelta de este armature,
    pero el datablock sobrevive porque el otro objeto sigue apuntándolo."""
    arm = _create_test_armature("SharedA")
    other = _create_test_armature("SharedB")
    try:
        action = _own_take_action("PuppetTake_shared", arm.name)
        arm.animation_data_create().action = action
        other.animation_data_create().action = action
        action_name = action.name
        assert bpy.data.actions.get(action_name).users >= 2

        assert common.clear_all_keyframes() is True

        assert arm.animation_data.action is None
        surviving = bpy.data.actions.get(action_name)
        assert surviving is not None, "la action compartida no debía borrarse"
        assert other.animation_data.action is not None
        assert other.animation_data.action.name == action_name
    finally:
        _cleanup(arm, other)
        leftover = bpy.data.actions.get("PuppetTake_shared")
        if leftover is not None:
            bpy.data.actions.remove(leftover)


def test_other_rig_own_take_survives():
    """Una toma marcada de OTRO rig del archivo no debe tocarse (T3)."""
    target = _create_test_armature("T3Target")
    other_rig = _create_test_armature("T3Other")
    try:
        other_take = _own_take_action("PuppetTake_other_rig", other_rig.name)
        other_take.use_fake_user = True
        other_take_name = other_take.name
        # other_take queda desasignada (como cualquier toma horneada real tras bake).

        assert common.clear_all_keyframes() is True  # target no tiene action activa

        assert bpy.data.actions.get(other_take_name) is not None
    finally:
        _cleanup(target, other_rig)
        leftover = bpy.data.actions.get("PuppetTake_other_rig")
        if leftover is not None:
            bpy.data.actions.remove(leftover)


def _mesh_with_shape_key(name_prefix: str):
    mesh_data = bpy.data.meshes.new(f"{name_prefix}_Mesh")
    mesh_obj = bpy.data.objects.new(f"{name_prefix}_MeshObj", mesh_data)
    bpy.context.scene.collection.objects.link(mesh_obj)
    mesh_data.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [], [(0, 1, 2)])
    mesh_obj.shape_key_add(name="Basis")
    mesh_obj.shape_key_add(name="jawOpen")
    return mesh_obj


def test_own_face_take_removed_foreign_preserved():
    arm = _create_test_armature("FaceClear")
    mesh_obj = _mesh_with_shape_key("FaceClear")
    try:
        face._state["face_mesh_name"] = mesh_obj.name  # bypass find_face_mesh scan

        own_action = _own_take_action("PuppetTake_cara_own", arm.name)
        own_action.use_fake_user = True
        mesh_obj.data.shape_keys.animation_data_create().action = own_action
        own_name = own_action.name

        face.clear_face_animation(arm)

        assert mesh_obj.data.shape_keys.animation_data.action is None
        assert bpy.data.actions.get(own_name) is None

        foreign = bpy.data.actions.new("MiExpresionPropia")
        mesh_obj.data.shape_keys.animation_data.action = foreign
        foreign_name = foreign.name

        face.clear_face_animation(arm)

        assert mesh_obj.data.shape_keys.animation_data.action is not None
        assert mesh_obj.data.shape_keys.animation_data.action.name == foreign_name
    finally:
        face._state["face_mesh_name"] = None
        _cleanup(arm, mesh_obj)
        leftover = bpy.data.actions.get("MiExpresionPropia")
        if leftover is not None:
            bpy.data.actions.remove(leftover)
