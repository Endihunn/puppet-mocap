"""_finish_recording()/_bake_take() deben, tras hornear, dejar la toma nueva
disponible en el registro Y seleccionada -- así "Última toma" en el panel
siempre refleja lo que se acaba de grabar sin que el usuario tenga que
elegirla a mano.
"""
import bpy
from mathutils import Vector

from puppet_mocap import operators, properties, retarget


def _register_props_once():
    if not hasattr(bpy.types.Scene, "puppet_mocap"):
        bpy.utils.register_class(properties.PuppetMocapProperties)
        bpy.types.Scene.puppet_mocap = bpy.props.PointerProperty(
            type=properties.PuppetMocapProperties
        )


def _create_test_armature(name_prefix: str = "FinishRecRig"):
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


def test_finish_recording_auto_selects_the_new_take():
    _register_props_once()
    arm = _create_test_armature()
    try:
        retarget.set_target_armature(arm.name)
        scene = bpy.context.scene
        props = scene.puppet_mocap
        props.target_armature = arm
        props.selected_take_id = "algo-viejo-que-ya-no-existe"

        operators._record_state.update(
            active=True,
            pending=False,
            samples=[
                (0.0, {"mixamorig:Hips": (1.0, 0.0, 0.0, 0.0)}, {}, {}),
                (0.1, {"mixamorig:Hips": (0.99, 0.01, 0.0, 0.0)}, {}, {}),
            ],
            start_frame=1,
            fps=30.0,
        )
        props.is_recording = True

        result = operators._finish_recording(scene, props)

        assert result is not None
        take_id, _frames = result
        assert props.selected_take_id == take_id
        take = operators.find_take(arm.name, take_id)
        assert take is not None
        assert take["body_action"] is not None
    finally:
        operators._record_state.update(active=False, pending=False, samples=[])
        retarget.set_target_armature(None)
        for a in list(bpy.data.actions):
            if a.get("puppet_mocap_owner") == arm.name:
                bpy.data.actions.remove(a)
        _cleanup(arm)
