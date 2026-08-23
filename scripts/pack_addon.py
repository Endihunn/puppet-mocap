"""Empaqueta puppet_mocap en un zip reproducible para instalar en Blender.

Uso:
    py scripts/pack_addon.py

Genera addon/puppet_mocap.zip con el paquete puppet_mocap/. Se excluyen los
modelos MediaPipe (*.task, descargables on demand) y __pycache__. Reproducible:
orden de archivos determinista + timestamps fijos.
"""
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parent.parent
ADDON_DIR = ROOT / "puppet_mocap"
OUT_DIR = ROOT / "addon"
OUT_ZIP = OUT_DIR / "puppet_mocap.zip"

FIXED_DATE = (1980, 1, 1, 0, 0, 0)

EXCLUDED_SUFFIXES = {".task", ".pyc", ".pyo", ".tmp"}


def collect_files():
    files = []
    for path in sorted(ADDON_DIR.rglob("*")):
        if not path.is_file():
            continue
        if "__pycache__" in path.parts:
            continue
        if path.suffix.lower() in EXCLUDED_SUFFIXES:
            continue
        rel = path.relative_to(ROOT).as_posix()
        files.append((rel, path))
    return files


def main():
    files = collect_files()
    if not files:
        raise SystemExit("no hay archivos para empaquetar")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if OUT_ZIP.exists():
        OUT_ZIP.unlink()
    with zipfile.ZipFile(OUT_ZIP, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for rel, path in files:
            zi = zipfile.ZipInfo(rel, date_time=FIXED_DATE)
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.external_attr = 0o644 << 16
            zf.writestr(zi, path.read_bytes())
    print(f"generado {OUT_ZIP} ({len(files)} archivos, {OUT_ZIP.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
