"""Puppet Mocap — webcam motion capture for Mixamo armatures.

Instalación: Edit > Preferences > Add-ons > Install from Disk → puppet_mocap.zip
Uso: View3D > N panel > pestaña Puppet Mocap
"""
bl_info = {
    "name": "Puppet Mocap",
    "author": "darth",
    "version": (0, 3, 1),
    "blender": (4, 4, 0),
    "location": "View3D > N Panel > Puppet Mocap",
    "description": "Mocap de webcam → armature Mixamo (cuerpo con twist de torso y pies + manos + cara vía MediaPipe en proceso externo; grabación buffer→bake; calibración de postura)",
    "category": "Animation",
}

import bpy

from . import properties, operators, panel, server


def register():
    bpy.utils.register_class(properties.PuppetMocapProperties)
    bpy.types.Scene.puppet_mocap = bpy.props.PointerProperty(
        type=properties.PuppetMocapProperties
    )
    for cls in operators.CLASSES:
        bpy.utils.register_class(cls)
    for cls in panel.CLASSES:
        bpy.utils.register_class(cls)
    operators.register_handlers()


def unregister():
    # Apagar TODO: antes el subprocess quedaba huérfano con la webcam tomada
    # al deshabilitar el addon o cerrar Blender a media captura.
    operators.unregister_handlers()
    try:
        operators._cleanup_capture(None)
    except Exception:
        pass

    for cls in reversed(panel.CLASSES):
        bpy.utils.unregister_class(cls)
    for cls in reversed(operators.CLASSES):
        bpy.utils.unregister_class(cls)
    if hasattr(bpy.types.Scene, "puppet_mocap"):
        del bpy.types.Scene.puppet_mocap
    bpy.utils.unregister_class(properties.PuppetMocapProperties)
