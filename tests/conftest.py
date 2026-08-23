"""Stubs de bpy/puppet_mocap para testear la lógica pura de retarget sin
arrancar el addon completo (que registra classes en bpy). Bajo Blender, bpy y
mathutils son los reales; fuera de Blender se usa mathutils de pip (Linux) y un
stub de bpy."""
import pathlib
import sys
import types


def _stub_bpy():
    if "bpy" in sys.modules:
        return
    try:
        import bpy  # noqa: F401  (disponible dentro de Blender)
        return
    except ImportError:
        pass
    bpy = types.ModuleType("bpy")
    bpy.app = types.SimpleNamespace()
    bpy.data = types.SimpleNamespace()
    bpy.context = types.SimpleNamespace()
    bpy.types = types.SimpleNamespace()
    bpy.props = types.SimpleNamespace()
    bpy.utils = types.SimpleNamespace()
    sys.modules["bpy"] = bpy


def _stub_puppet_package():
    """Evita ejecutar puppet_mocap/__init__.py (registro de operators/panel) y
    deja que retarget.common importe solo lo que necesita."""
    root = pathlib.Path(__file__).resolve().parent.parent
    pm = sys.modules.get("puppet_mocap")
    if pm is None:
        pm = types.ModuleType("puppet_mocap")
        pm.__path__ = [str(root / "puppet_mocap")]
        pm.__package__ = "puppet_mocap"
        sys.modules["puppet_mocap"] = pm
    if "puppet_mocap.log" not in sys.modules:
        logm = types.ModuleType("puppet_mocap.log")
        for n in ("info", "warn", "error", "exception", "debug", "banner"):
            setattr(logm, n, lambda *a, **k: None)
        sys.modules["puppet_mocap.log"] = logm


_stub_bpy()
_stub_puppet_package()
