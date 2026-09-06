"""Corre la suite de tests dentro del Python de Blender (mathutils es un módulo
builtin del binario de Blender, no importable desde el python.exe embebido).

Uso:
    blender --background --factory-startup --python tests/run_in_blender.py -- [args de pytest]

`scripts/run_tests.ps1` lo envuelve. numpy + pytest se toman del venv de tests
(.venv-test), cuya versión de Python debe coincidir con la de Blender.
"""
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# venv de tests: numpy + pytest (mathutils viene del builtin de Blender)
_site = REPO / ".venv-test" / "Lib" / "site-packages"
if _site.is_dir():
    sys.path.insert(0, str(_site))
sys.path.insert(0, str(REPO))

try:
    import pytest
except ImportError:
    sys.stderr.write(
        f"pytest no encontrado. Crea el venv de tests:\n"
        f"  uv venv --python 3.13 {REPO / '.venv-test'}\n"
        f"  uv pip install --python {REPO / '.venv-test' / 'Scripts' / 'python.exe'} numpy pytest\n"
    )
    os._exit(2)

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
if not argv:
    argv = ["tests/", "-v"]

code = pytest.main(argv)
sys.stdout.flush()
sys.stderr.flush()
# os._exit para saltarse el atexit de Blender (que puede tragar el returncode)
os._exit(int(code))
