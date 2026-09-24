"""Empaqueta la beta autocontenida de Puppet Mocap para Blender en Windows x64.

Uso:
    py scripts/pack_addon.py

Además del código, exige los modelos y el runtime portable en
addon/bundle_resources/. Así el ZIP no se genera accidentalmente incompleto.
"""
from pathlib import Path
import os
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parent.parent
ADDON_DIR = ROOT / "puppet_mocap"
OUT_DIR = ROOT / "addon"
OUT_ZIP = OUT_DIR / "puppet_mocap.zip"
RESOURCE_DIR = OUT_DIR / "bundle_resources"
RUNTIME_DIR = RESOURCE_DIR / "runtime"
MODELS_DIR = RESOURCE_DIR / "models"

FIXED_DATE = (1980, 1, 1, 0, 0, 0)
EXCLUDED_SUFFIXES = {".pyc", ".pyo", ".tmp"}
REQUIRED_MODELS = {
    "pose_landmarker_lite.task",
    "hand_landmarker.task",
    "face_landmarker.task",
}
PACKAGE_README = """Puppet Mocap 0.4.2 beta — Windows x64

Instala este ZIP desde Blender: Editar > Preferencias > Complementos > Instalar
desde disco. Blender 4.4 o posterior debe estar instalado.

La captura por webcam (cuerpo, manos y cara) incluye CPython 3.13.2, MediaPipe,
OpenCV, NumPy y los tres modelos. No requiere pip, instalaciones de Python ni
descargas adicionales. Al dejar «Python de captura» en su valor por defecto,
el complemento usa el runtime incluido. El ZIP está preparado para Windows
x64; no se debe instalar en macOS o Linux.

«Generar (Kimodo)» es una función opcional aparte y conserva sus requisitos de
modelo/runtime propios.
"""


def validate_bundle():
    python_exe = RUNTIME_DIR / "python.exe"
    if not python_exe.is_file():
        raise SystemExit(f"falta runtime portable: {python_exe}")
    for model in sorted(REQUIRED_MODELS):
        path = MODELS_DIR / model
        if not path.is_file() or path.stat().st_size < 1024 * 1024:
            raise SystemExit(f"falta modelo o parece incompleto: {path}")

    env = os.environ.copy()
    env["PATH"] = os.pathsep.join((
        str(RUNTIME_DIR),
        str(RUNTIME_DIR / "DLLs"),
        env.get("PATH", ""),
    ))
    smoke = subprocess.run(
        [str(python_exe), "-s", "-c",
         "import mediapipe, cv2, numpy; "
         "print(mediapipe.__version__ + '|' + cv2.__version__ + '|' + numpy.__version__)"] ,
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(RUNTIME_DIR),
        env=env,
    )
    if smoke.returncode:
        raise SystemExit(
            "runtime portable no supera la prueba de imports:\n" + smoke.stderr[-4000:]
        )
    print(f"runtime verificado: {smoke.stdout.strip()}")


def collect_files():
    files = []
    for path in sorted(ADDON_DIR.rglob("*")):
        if not path.is_file():
            continue
        if "__pycache__" in path.parts:
            continue
        # Los modelos incluidos se toman de bundle_resources para que exista
        # una sola copia canónica y se validen antes de crear el ZIP.
        if path.suffix.lower() == ".task":
            continue
        if path.suffix.lower() in EXCLUDED_SUFFIXES:
            continue
        files.append((path.relative_to(ROOT).as_posix(), path))

    for root, prefix in ((RUNTIME_DIR, "puppet_mocap/runtime"),
                         (MODELS_DIR, "puppet_mocap/models")):
        for path in sorted(root.rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            if path.suffix.lower() in EXCLUDED_SUFFIXES:
                continue
            rel = f"{prefix}/{path.relative_to(root).as_posix()}"
            files.append((rel, path))

    files.append(("puppet_mocap/LEEME_BETA.txt", None))
    files.sort(key=lambda item: item[0])
    names = [rel for rel, _ in files]
    if len(names) != len(set(names)):
        raise SystemExit("rutas duplicadas en el paquete")
    return files


def main():
    validate_bundle()
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
            if path is not None and path.suffix.lower() in {".dll", ".pyd"}:
                zi._compresslevel = 1
            elif path is not None and path.suffix.lower() in {".task", ".zip"}:
                zi.compress_type = zipfile.ZIP_STORED
            zi.external_attr = 0o644 << 16
            payload = PACKAGE_README.encode("utf-8") if path is None else path.read_bytes()
            zf.writestr(zi, payload)
    print(f"generado {OUT_ZIP} ({len(files)} archivos, {OUT_ZIP.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
