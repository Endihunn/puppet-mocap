"""Tests de integración en Blender para 'Reproducir toma' (P1, hallazgo #3):
antes solo asignaba la action al rig, nunca llamaba a reproducir. Verificado:
incluso en modo --background Blender expone screen/window y
bpy.ops.screen.animation_play() cambia is_animation_playing de verdad, así
que esto prueba la reproducción real, no solo la asignación.
"""
import bpy
from mathutils import Vector

from puppet_mocap import operators, retarget


def _create_test_armature(name_prefix: str = "PlaybackRig"):
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


def test_set_take_playback_true_assigns_action_and_starts_playing():
    assert not bpy.context.screen.is_animation_playing
    arm = _create_test_armature()
    try:
        retarget.set_target_armature(arm.name)
        action = bpy.data.actions.new("PuppetTake_playback")
        action.use_fake_user = True
        operators._baked_state["body"] = action.name

        operators.set_take_playback(True)

        assert arm.animation_data.action is not None
        assert arm.animation_data.action.name == action.name
        assert bpy.context.screen.is_animation_playing, (
            "set_take_playback(True) debe iniciar la reproducción real, "
            "no solo asignar la action (hallazgo #3)"
        )

        operators.set_take_playback(False)
        assert arm.animation_data.action is None
        assert not bpy.context.screen.is_animation_playing
    finally:
        if bpy.context.screen.is_animation_playing:
            bpy.ops.screen.animation_cancel(restore_frame=False)
        operators._baked_state["body"] = None
        retarget.set_target_armature(None)
        _cleanup(arm)
        leftover = bpy.data.actions.get("PuppetTake_playback")
        if leftover is not None:
            bpy.data.actions.remove(leftover)


def test_set_take_playback_true_is_idempotent_does_not_pause():
    """animation_play() es un TOGGLE — llamarlo dos veces sin guardia pausaría
    en vez de mantener reproduciendo. set_take_playback(True) dos veces seguidas
    debe seguir reproduciendo."""
    arm = _create_test_armature("PlaybackIdem")
    try:
        retarget.set_target_armature(arm.name)
        action = bpy.data.actions.new("PuppetTake_idem")
        action.use_fake_user = True
        operators._baked_state["body"] = action.name

        operators.set_take_playback(True)
        assert bpy.context.screen.is_animation_playing
        operators.set_take_playback(True)
        assert bpy.context.screen.is_animation_playing, (
            "una segunda llamada con play=True no debe pausar la reproducción"
        )
    finally:
        if bpy.context.screen.is_animation_playing:
            bpy.ops.screen.animation_cancel(restore_frame=False)
        operators._baked_state["body"] = None
        retarget.set_target_armature(None)
        _cleanup(arm)
        leftover = bpy.data.actions.get("PuppetTake_idem")
        if leftover is not None:
            bpy.data.actions.remove(leftover)
