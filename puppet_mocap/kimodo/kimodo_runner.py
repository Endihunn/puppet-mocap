"""Cliente externo de Kimodo: texto → .npz.

Lo lanza el addon (K2-T3) vía subprocess.Popen con un venv PROPIO (torch+CUDA,
separado de MediaPipe). Espejo estructural de capture/capture_runner.py pero
NO importa nada de capture/ — es un ejecutable independiente.

Códigos de retorno:
    0 ok
    1 faltan dependencias o el modelo no se puede descargar/acceder (gated)
    2 falló la generación
    3 OOM de VRAM (usar TEXT_ENCODER_DEVICE=cpu)
"""
from __future__ import annotations

import sys
import traceback

# --- helpers ----------------------------------------------------------------

_GPU_OOM_MARKERS = ("OutOfMemoryError", "CUDA out of memory", "out of memory")
_MODEL_DEP_MARKERS = ("Gated repo", "401", "403", "not found in", "No module named",
                      "Cannot access gated", "gated repo", "download")


def _import_deps():
    """Devuelve (ok, msg). Verifica torch/transformers/kimodo."""
    try:
        import torch  # noqa: F401
        import transformers  # noqa: F401
        import kimodo  # noqa: F401
        return True, ""
    except ImportError as e:
        return False, f"faltan dependencias: {e}"


def _banner(msg):
    print(f"[kimodo_runner] {msg}", flush=True)


def main():
    # --prompt, --duration, --model, --out, --seed, --no-postprocess, --num_transition_frames
    argv = sys.argv
    def _val(name, default=None):
        if name in argv:
            i = argv.index(name)
            if i + 1 < len(argv):
                return argv[i + 1]
        return default

    prompt = _val("--prompt")
    duration = _val("--duration", "5.0")
    model = _val("--model", "kimodo-soma-rp")
    out = _val("--out")
    seed = _val("--seed")
    no_pp = "--no-postprocess" in argv
    ntf = _val("--num_transition_frames", "5")

    if not prompt or not out:
        print("[kimodo_runner] uso: --prompt <txt> --out <file.npz> [--model m] "
              "[--dur n] [--seed s]", file=sys.stderr, flush=True)
        return 1

    ok, msg = _import_deps()
    if not ok:
        print(f"[kimodo_runner] {msg}", file=sys.stderr, flush=True)
        return 1

    # El text encoder debe ir a CPU (VRAM <3 GB); en la 4080 con GPU pide ~17 GB.
    import os
    os.environ["TEXT_ENCODER_DEVICE"] = os.environ.get("TEXT_ENCODER_DEVICE", "cpu")

    import torch
    if not torch.cuda.is_available():
        print("[kimodo_runner] CUDA no disponible; generación requiere GPU.", file=sys.stderr, flush=True)
        return 1

    _banner(f"generando: '{prompt}' ({duration}s) model={model} out={out}")

    # Ejecutar el CLI de kimodo en-proceso (reutiliza su lógica de output/CFG).
    import kimodo.scripts.generate as gen
    sys.argv = ["kimodo_gen", prompt,
                "--model", model,
                "--duration", duration,
                "--output", out,
                "--num_transition_frames", str(ntf)]
    if seed is not None:
        sys.argv += ["--seed", str(seed)]
    if no_pp:
        sys.argv += ["--no-postprocess"]

    try:
        gen.main()
    except torch.cuda.OutOfMemoryError:
        traceback.print_exc(file=sys.stderr)
        print("[kimodo_runner] OOM de VRAM. Asegúrate de TEXT_ENCODER_DEVICE=cpu.", file=sys.stderr, flush=True)
        return 3
    except Exception as e:
        msg = str(e)
        traceback.print_exc(file=sys.stderr)
        if any(m in msg for m in _MODEL_DEP_MARKERS):
            print("[kimodo_runner] problema de modelo/dependencias (¿access gated?).", file=sys.stderr, flush=True)
            return 1
        print(f"[kimodo_runner] generación falló: {msg}", file=sys.stderr, flush=True)
        return 2

    _banner("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
