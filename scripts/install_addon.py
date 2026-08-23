"""Instala puppet_mocap.zip en Blender de forma headless.

Uso:
    blender --background --python scripts/install_addon.py -- --zip addon/puppet_mocap.zip
"""
from pathlib import Path
import sys

import bpy


def _arg_after(name):
    argv = sys.argv
    if "--" in argv:
        argv = argv[argv.index("--") + 1:]
    if name in argv:
        i = argv.index(name)
        if i + 1 < len(argv):
            return argv[i + 1]
    return None


def main():
    zip_arg = _arg_after("--zip")
    zip_path = Path(zip_arg) if zip_arg else (
        Path(__file__).resolve().parent.parent / "addon" / "puppet_mocap.zip")
    if not zip_path.exists():
        raise SystemExit("no existe el zip; corre primero: py scripts/pack_addon.py")
    bpy.ops.preferences.addon_install(filepath=str(zip_path), overwrite=True)
    bpy.ops.preferences.addon_enable(module="puppet_mocap")
    bpy.ops.wm.save_userpref()
    print(f"instalado y habilitado: {zip_path}")


if __name__ == "__main__":
    main()
