"""Fase 2 pide explícitamente comprobar la coexistencia webcam/Kimodo: ambas
rutas comparten el rig objetivo y _baked_state["body"/"face"], y
_kimodo_bake() reasigna arm.animation_data.action del MISMO rig durante su
postproceso (foot lock / ground snap). Nada prueba que corran a la vez sin
pisarse -- en vez de prometer independencia sin probarla, se bloquean
mutuamente con un mensaje explícito.

Nota: un operador que hace self.report({'ERROR'}, ...) y devuelve
{'CANCELLED'} hace que bpy.ops.<id>() LANCE RuntimeError (no devuelve el set)
-- es el comportamiento estándar de Blender, no un bug de este addon.
"""
import bpy
import pytest

from puppet_mocap import operators, properties, server


class _FakeAliveThread:
    def is_alive(self):
        return True


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


def test_generate_motion_rejects_while_capture_server_running():
    _register_props_once()
    _register_operator_once(operators.PUPPET_OT_generate_motion)
    props = bpy.context.scene.puppet_mocap
    props.kimodo_running = False
    server._state["thread"] = _FakeAliveThread()
    try:
        with pytest.raises(RuntimeError, match="captura en vivo"):
            bpy.ops.puppet_mocap.generate_motion()
    finally:
        server._state["thread"] = None


def test_generate_motion_reaches_later_validation_when_capture_not_running():
    """Confirma que el gate nuevo no rechaza de más: sin captura corriendo,
    el CANCELLED viene de una validación MÁS ADELANTE (Python de Kimodo sin
    configurar), no del gate de coexistencia."""
    _register_props_once()
    _register_operator_once(operators.PUPPET_OT_generate_motion)
    props = bpy.context.scene.puppet_mocap
    props.kimodo_running = False
    props.kimodo_python_path = ""
    server._state["thread"] = None

    with pytest.raises(RuntimeError, match="Sin armature"):
        bpy.ops.puppet_mocap.generate_motion()


def test_start_capture_rejects_while_kimodo_generating():
    _register_props_once()
    _register_operator_once(operators.PUPPET_OT_start_capture)
    props = bpy.context.scene.puppet_mocap
    server._state["thread"] = None
    props.kimodo_running = True
    try:
        with pytest.raises(RuntimeError, match="generación de Kimodo"):
            bpy.ops.puppet_mocap.start_capture()
    finally:
        props.kimodo_running = False


def test_start_capture_reaches_later_validation_when_kimodo_not_running():
    _register_props_once()
    _register_operator_once(operators.PUPPET_OT_start_capture)
    props = bpy.context.scene.puppet_mocap
    server._state["thread"] = None
    props.kimodo_running = False
    props.enable_body = False
    props.enable_hands = False
    props.enable_face = False  # fuerza CANCELLED en una validación posterior

    with pytest.raises(RuntimeError, match="Activa al menos un módulo"):
        bpy.ops.puppet_mocap.start_capture()


def teardown_module(module):
    server._state["thread"] = None
    server._state["client_connected"] = False
