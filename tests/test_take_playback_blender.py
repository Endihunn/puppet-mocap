"""Tests de integración en Blender para 'Reproducir / Pausar' sobre la toma
SELECCIONADA (Fase 3: registro real de tomas, ya no 'la última acción
horneada'). Verificado: incluso en modo --background Blender expone
screen/window y bpy.ops.screen.animation_play() cambia is_animation_playing
de verdad, así que esto prueba reproducción real.

Contrato nuevo (brief de Fase 3): Pausar (play=False) SOLO detiene la
reproducción -- la toma se queda asignada. Volver a la captura en vivo es
una acción aparte (detach_take_for_live_capture), no un efecto lateral de
pausar.
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


def _make_take(arm, name="PuppetTake_playback", take_id="TID_playback"):
    action = bpy.data.actions.new(name)
    action.use_fake_user = True
    action["puppet_mocap_take"] = True
    action["puppet_mocap_owner"] = arm.name
    action["puppet_mocap_take_id"] = take_id
    action["puppet_mocap_take_label"] = "Toma de prueba"
    action["puppet_mocap_take_kind"] = "body"
    return action


class _FakeProps:
    def __init__(self, take_id=""):
        self.selected_take_id = take_id


def test_set_take_playback_true_assigns_selected_take_and_starts_playing():
    if bpy.context.screen.is_animation_playing:  # estado limpio, sin asumir orden de tests
        bpy.ops.screen.animation_cancel(restore_frame=False)
    arm = _create_test_armature()
    try:
        retarget.set_target_armature(arm.name)
        action = _make_take(arm)
        props = _FakeProps(take_id="TID_playback")

        operators.set_take_playback(props, True)

        assert arm.animation_data.action is not None
        assert arm.animation_data.action.name == action.name
        assert bpy.context.screen.is_animation_playing, (
            "set_take_playback(props, True) debe iniciar la reproducción real"
        )

        operators.set_take_playback(props, False)
        assert not bpy.context.screen.is_animation_playing
        assert arm.animation_data.action is not None, (
            "Pausar NO debe desasignar la toma (brief de Fase 3: "
            "'Pausar debe conservar la toma asignada')"
        )
        assert arm.animation_data.action.name == action.name
    finally:
        if bpy.context.screen.is_animation_playing:
            bpy.ops.screen.animation_cancel(restore_frame=False)
        retarget.set_target_armature(None)
        _cleanup(arm)
        leftover = bpy.data.actions.get("PuppetTake_playback")
        if leftover is not None:
            bpy.data.actions.remove(leftover)


def test_set_take_playback_true_is_idempotent_does_not_pause():
    """animation_play() es un TOGGLE — llamarlo dos veces sin guardia pausaría
    en vez de mantener reproduciendo."""
    arm = _create_test_armature("PlaybackIdem")
    try:
        retarget.set_target_armature(arm.name)
        _make_take(arm, name="PuppetTake_idem", take_id="TID_idem")
        props = _FakeProps(take_id="TID_idem")

        operators.set_take_playback(props, True)
        assert bpy.context.screen.is_animation_playing
        operators.set_take_playback(props, True)
        assert bpy.context.screen.is_animation_playing, (
            "una segunda llamada con play=True no debe pausar la reproducción"
        )
    finally:
        if bpy.context.screen.is_animation_playing:
            bpy.ops.screen.animation_cancel(restore_frame=False)
        retarget.set_target_armature(None)
        _cleanup(arm)
        leftover = bpy.data.actions.get("PuppetTake_idem")
        if leftover is not None:
            bpy.data.actions.remove(leftover)


def test_detach_take_for_live_capture_unassigns_and_stops_playback():
    arm = _create_test_armature("PlaybackDetach")
    try:
        retarget.set_target_armature(arm.name)
        _make_take(arm, name="PuppetTake_detach", take_id="TID_detach")
        props = _FakeProps(take_id="TID_detach")
        operators.set_take_playback(props, True)
        assert bpy.context.screen.is_animation_playing
        assert arm.animation_data.action is not None

        operators.detach_take_for_live_capture()

        assert not bpy.context.screen.is_animation_playing
        assert arm.animation_data.action is None
    finally:
        if bpy.context.screen.is_animation_playing:
            bpy.ops.screen.animation_cancel(restore_frame=False)
        retarget.set_target_armature(None)
        _cleanup(arm)
        leftover = bpy.data.actions.get("PuppetTake_detach")
        if leftover is not None:
            bpy.data.actions.remove(leftover)


def test_set_take_playback_with_unknown_selected_take_does_not_crash():
    """selected_take_id vacío o apuntando a una toma ya eliminada: no debe
    reventar, simplemente no asigna nada."""
    arm = _create_test_armature("PlaybackUnknown")
    try:
        retarget.set_target_armature(arm.name)
        props = _FakeProps(take_id="no-existe")

        operators.set_take_playback(props, True)  # no debe lanzar

        assert arm.animation_data is None or arm.animation_data.action is None
    finally:
        if bpy.context.screen.is_animation_playing:
            bpy.ops.screen.animation_cancel(restore_frame=False)
        retarget.set_target_armature(None)
        _cleanup(arm)
