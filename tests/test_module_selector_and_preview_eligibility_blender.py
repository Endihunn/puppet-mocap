"""Brief de Fase 3: 'un selector por módulo en modo básico' (Cuerpo/Manos/
Cara) que graba lo seleccionado, con la separación captura/grabación movida
a Avanzado (para superponer manos/cara sobre una animación ya grabada); y
'Sin configurar: indicar el requisito faltante y ofrecer la acción para
resolverlo' para 'Iniciar vista previa' (can_start_preview, mismo patrón
que can_start_recording).
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


def _create_test_armature(name_prefix: str = "PreviewEligRig"):
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


# --- Selector único por módulo (básico) vs separado (avanzado) ------------

def test_basic_mode_toggling_enable_off_also_stops_recording_it():
    _register_props_once()
    props = bpy.context.scene.puppet_mocap
    props.modulos_advanced = False
    props.enable_hands = True
    props.record_hands = True

    props.enable_hands = False

    assert props.record_hands is False


def test_basic_mode_toggling_enable_on_also_records_it():
    _register_props_once()
    props = bpy.context.scene.puppet_mocap
    props.modulos_advanced = False
    props.enable_face = False
    props.record_face = False

    props.enable_face = True

    assert props.record_face is True


def test_advanced_mode_does_not_resync_record_when_enable_changes():
    """El caso de uso que el brief pide conservar: superponer manos/cara
    sobre una animación de cuerpo YA grabada, sin re-grabar el cuerpo."""
    _register_props_once()
    props = bpy.context.scene.puppet_mocap
    props.modulos_advanced = True
    props.enable_body = True
    props.record_body = True

    props.enable_body = False  # sigue viendo el cuerpo en vivo pero no lo graba de nuevo

    assert props.record_body is True, (
        "en Avanzado, apagar 'ver' un módulo no debe forzar apagar 'grabarlo'"
    )


# --- can_start_preview: elegibilidad de 'Iniciar vista previa' ------------

def test_can_start_preview_rejects_with_no_modules_enabled():
    _register_props_once()
    props = bpy.context.scene.puppet_mocap
    props.enable_body = False
    props.enable_hands = False
    props.enable_face = False
    ok, msg = operators.can_start_preview(props)
    assert ok is False
    assert "módulo" in msg.lower()


def test_can_start_preview_rejects_with_no_armature():
    _register_props_once()
    props = bpy.context.scene.puppet_mocap
    props.enable_body = True
    props.target_armature = None
    ok, msg = operators.can_start_preview(props)
    assert ok is False
    assert "armature" in msg.lower()


def test_can_start_preview_rejects_without_python_path():
    _register_props_once()
    arm = _create_test_armature()
    try:
        from puppet_mocap import retarget
        props = bpy.context.scene.puppet_mocap
        props.enable_body = True
        props.enable_hands = False
        props.enable_face = False
        props.target_armature = arm
        props.bone_prefix = "mixamorig:"
        props.python_path = ""

        ok, msg = operators.can_start_preview(props)

        assert ok is False
        assert "python" in msg.lower()
    finally:
        retarget.set_target_armature(None)
        _cleanup(arm)


def test_can_start_preview_allows_when_everything_is_ready():
    _register_props_once()
    arm = _create_test_armature("PreviewEligOk")
    try:
        from puppet_mocap import retarget
        props = bpy.context.scene.puppet_mocap
        props.enable_body = True
        props.enable_hands = False
        props.enable_face = False
        props.target_armature = arm
        props.bone_prefix = "mixamorig:"
        props.python_path = "py"

        ok, msg = operators.can_start_preview(props)

        assert ok is True
        assert msg == ""
    finally:
        retarget.set_target_armature(None)
        _cleanup(arm)
