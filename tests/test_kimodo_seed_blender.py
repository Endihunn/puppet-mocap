"""Tests de integración en Blender para el gate de semilla de Kimodo (P1 del
brief de auditoría 2026-09-22): 'Resultado repetible' debe cambiar el comando
efectivo, no solo mostrar/ocultar el campo de semilla.
"""
import types

import bpy

from puppet_mocap import operators, properties


def _register_props_once():
    if not hasattr(bpy.types.Scene, "puppet_mocap"):
        bpy.utils.register_class(properties.PuppetMocapProperties)
        bpy.types.Scene.puppet_mocap = bpy.props.PointerProperty(
            type=properties.PuppetMocapProperties
        )


def _fake_props(seed_use: bool, seed: int, postprocess: bool = False):
    return types.SimpleNamespace(
        kimodo_prompt="a person walks forward",
        kimodo_duration=5.0,
        kimodo_model="Kimodo-SOMA-RP-v1.1",
        kimodo_num_transition=5,
        kimodo_seed_use=seed_use,
        kimodo_seed=seed,
        kimodo_postprocess=postprocess,
    )


def test_seed_flag_omitted_when_toggle_off_even_with_valid_seed():
    """Antes: el comando decidía por kimodo_seed >= 0, ignorando el toggle."""
    props = _fake_props(seed_use=False, seed=42)
    cmd = operators.build_kimodo_cmd(props, "py", "runner.py", "stem")
    assert "--seed" not in cmd


def test_seed_flag_sent_when_toggle_on():
    props = _fake_props(seed_use=True, seed=42)
    cmd = operators.build_kimodo_cmd(props, "py", "runner.py", "stem")
    assert "--seed" in cmd
    assert cmd[cmd.index("--seed") + 1] == "42"


def test_postprocess_flag_follows_its_own_bool_independently():
    cmd_off = operators.build_kimodo_cmd(_fake_props(False, -1, postprocess=False),
                                          "py", "runner.py", "stem")
    cmd_on = operators.build_kimodo_cmd(_fake_props(False, -1, postprocess=True),
                                         "py", "runner.py", "stem")
    assert "--postprocess" not in cmd_off
    assert "--postprocess" in cmd_on


def test_enabling_seed_use_autofills_a_valid_seed():
    """Activar el toggle con semilla en -1 (aleatoria) debe fijar una semilla
    concreta de inmediato — si no, 'repetible' seguiría siendo aleatorio."""
    _register_props_once()
    scene = bpy.context.scene
    props = scene.puppet_mocap
    props.kimodo_seed_use = False
    props.kimodo_seed = -1

    props.kimodo_seed_use = True

    assert props.kimodo_seed >= 0


def test_toggling_off_and_on_preserves_a_previously_chosen_seed():
    _register_props_once()
    props = bpy.context.scene.puppet_mocap
    props.kimodo_seed_use = True
    props.kimodo_seed = 777

    props.kimodo_seed_use = False
    props.kimodo_seed_use = True

    assert props.kimodo_seed == 777
