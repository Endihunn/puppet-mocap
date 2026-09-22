"""_maybe_autodetect_kimodo (P1, hallazgo #4): llamada desde Panel.draw()
cuando la ruta de Kimodo está vacía. Antes escaneaba candidatos con
subprocess.run(timeout=60) DIRECTO en draw() -- podía colgar la UI hasta 60s
si un candidato existía pero 'import kimodo' se colgaba. Ahora usa run_async,
igual que el botón 'Detectar'.
"""
import time

from puppet_mocap import operators


class _RunAsyncSpy:
    def __init__(self):
        self.calls = []

    def __call__(self, work_fn, on_done, poll_interval=0.2):
        self.calls.append((work_fn, on_done))
        return lambda: None


def _reset_autodetect_cache():
    operators._KIMODO_AUTODETECT["done"] = False
    operators._KIMODO_AUTODETECT["t"] = 0.0


def test_maybe_autodetect_never_calls_the_blocking_scan_from_draw():
    """Aserción central: el chequeo bloqueante _kimodo_python_valid /
    subprocess.run no debe ejecutarse dentro de la llamada síncrona que hace
    Panel.draw() -- solo dentro del work_fn que run_async manda a otro hilo."""
    import types
    _reset_autodetect_cache()
    orig_run_async = operators.run_async
    spy = _RunAsyncSpy()
    operators.run_async = spy
    try:
        props = types.SimpleNamespace(kimodo_python_path="", kimodo_status="")

        t0 = time.perf_counter()
        operators._maybe_autodetect_kimodo(props)
        elapsed = time.perf_counter() - t0

        assert elapsed < 0.5, "_maybe_autodetect_kimodo no debe bloquear draw()"
        assert len(spy.calls) == 1
        assert props.kimodo_python_path == ""  # nada se escribió todavía (async)
    finally:
        operators.run_async = orig_run_async
        _reset_autodetect_cache()


def test_maybe_autodetect_on_done_sets_path_when_found():
    import types
    _reset_autodetect_cache()
    orig_run_async = operators.run_async
    spy = _RunAsyncSpy()
    operators.run_async = spy
    try:
        props = types.SimpleNamespace(kimodo_python_path="", kimodo_status="")
        operators._maybe_autodetect_kimodo(props)
        _work_fn, on_done = spy.calls[0]

        on_done("C:/fake/python.exe", None)

        assert props.kimodo_python_path == "C:/fake/python.exe"
        assert "automáticamente" in props.kimodo_status
    finally:
        operators.run_async = orig_run_async
        _reset_autodetect_cache()


def test_maybe_autodetect_only_scans_once_per_session_even_if_called_again():
    """El guard 'done' ya existía antes del fix; confirmamos que sigue
    funcionando ahora que el escaneo real es async (una segunda llamada
    mientras la primera todavía no resolvió no debe lanzar un segundo hilo)."""
    import types
    _reset_autodetect_cache()
    orig_run_async = operators.run_async
    spy = _RunAsyncSpy()
    operators.run_async = spy
    try:
        props = types.SimpleNamespace(kimodo_python_path="", kimodo_status="")
        operators._maybe_autodetect_kimodo(props)
        operators._maybe_autodetect_kimodo(props)
        operators._maybe_autodetect_kimodo(props)
        assert len(spy.calls) == 1
    finally:
        operators.run_async = orig_run_async
        _reset_autodetect_cache()
