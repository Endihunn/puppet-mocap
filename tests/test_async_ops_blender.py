"""Tests para el refactor async (P1): check_deps, check_kimodo_deps,
autodetect_kimodo_python y download_models bloqueaban el hilo principal con
subprocess.run(timeout=30/60) o urllib directamente en execute(). Ahora usan
run_async() (hilo de fondo + bpy.app.timers) y devuelven FINISHED de inmediato.

Nota de entorno: Blender en --background no bombea su propio bucle de eventos
dentro de un script síncrono, así que bpy.app.timers.register() nunca dispara
solo — no hay forma headless de "esperar a que el timer corra de verdad".
Por eso:
1. run_async() se prueba completo como unidad aislada (el hilo SÍ corre de
   verdad; el poll se invoca a mano para simular lo que Blender haría).
2. Los 4 operadores se prueban con run_async monkeypatcheado por un spy que
   captura (work_fn, on_done) sin lanzar hilos — así se verifica la lógica
   real de cada on_done (qué escribe en las props para cada resultado
   posible) sin depender del bombeo de timers ni de subprocess/red reales.
"""
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import bpy

from puppet_mocap import operators, properties


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
        pass  # ya registrada por otro test de este archivo en la misma sesión


def test_capture_python_uses_bundled_runtime_for_default_and_honors_override(
        tmp_path, monkeypatch):
    bundled = tmp_path / "python.exe"
    bundled.write_bytes(b"test runtime placeholder")
    monkeypatch.setattr(operators, "_bundled_capture_python_path", lambda: bundled)

    props = SimpleNamespace(python_path="py")
    assert operators._capture_python_path(props) == str(bundled)
    assert operators._using_bundled_capture_python(props)

    alternate = tmp_path / "alternate-python.exe"
    props.python_path = str(alternate)
    assert operators._capture_python_path(props) == str(alternate)
    assert not operators._using_bundled_capture_python(props)


# --- run_async como unidad aislada ------------------------------------------

def test_run_async_runs_work_on_a_background_thread():
    seen_thread = {}

    def work():
        seen_thread["thread"] = threading.current_thread()
        return "ok"

    done = {}
    operators.run_async(work, lambda result, error: done.update(result=result, error=error))

    for _ in range(50):  # hasta ~1s, sin bpy.app.timers -- espera cruda del hilo
        if "thread" in seen_thread:
            break
        time.sleep(0.02)

    assert seen_thread.get("thread") is not None
    assert seen_thread["thread"] is not threading.current_thread()
    assert seen_thread["thread"] is not threading.main_thread()


def test_run_async_returns_the_poll_callable_for_manual_pumping():
    calls = []

    def work():
        time.sleep(0.05)
        return "resultado"

    def on_done(result, error):
        calls.append((result, error))

    poll = operators.run_async(work, on_done)
    assert callable(poll)

    # Mientras el hilo sigue corriendo, el poll debe pedir que lo vuelvan a
    # llamar (float), sin invocar on_done todavía.
    first = poll()
    assert isinstance(first, float)
    assert calls == []

    # Esperar a que el hilo de verdad termine, luego simular el siguiente tick.
    time.sleep(0.3)
    second = poll()
    assert second is None  # señal de "desregístrame"
    assert calls == [("resultado", None)]


def test_run_async_propagates_exceptions_to_on_done():
    def work():
        raise ValueError("boom")

    calls = []
    poll = operators.run_async(work, lambda result, error: calls.append((result, error)))
    time.sleep(0.3)
    poll()
    assert len(calls) == 1
    result, error = calls[0]
    assert result is None
    assert isinstance(error, ValueError)


# --- Los 4 operadores: no bloquean + on_done escribe lo correcto -----------

class _RunAsyncSpy:
    """Sustituye a operators.run_async: no lanza hilos, solo guarda
    (work_fn, on_done) para que el test decida cuándo y con qué invocar
    on_done -- así se prueba la lógica real sin timers ni subprocess/red."""
    def __init__(self):
        self.calls = []

    def __call__(self, work_fn, on_done, poll_interval=0.2):
        self.calls.append((work_fn, on_done))
        return lambda: None


def _swap_run_async(monkeypatch_holder):
    spy = _RunAsyncSpy()
    monkeypatch_holder["orig"] = operators.run_async
    operators.run_async = spy
    return spy


def _restore_run_async(monkeypatch_holder):
    operators.run_async = monkeypatch_holder["orig"]


def test_check_deps_does_not_block_and_on_done_reports_ok():
    _register_props_once()
    holder = {}
    spy = _swap_run_async(holder)
    try:
        _register_operator_once(operators.PUPPET_OT_check_deps)
        props = bpy.context.scene.puppet_mocap
        props.python_path = "py"
        props.deps_checking = False

        t0 = time.perf_counter()
        bpy.ops.puppet_mocap.check_deps()
        elapsed = time.perf_counter() - t0

        assert elapsed < 1.0, "execute() debe volver de inmediato, no esperar el subprocess"
        assert props.deps_checking is True
        assert len(spy.calls) == 1
        _work_fn, on_done = spy.calls[0]

        on_done({"kind": "ran", "returncode": 0,
                  "stdout": "0.10.9|4.9.0|1.26.4", "stderr": ""}, None)
        assert props.deps_checking is False
        assert props.deps_status.startswith("OK")
    finally:
        _restore_run_async(holder)


def test_check_deps_second_click_while_running_is_rejected():
    _register_props_once()
    holder = {}
    spy = _swap_run_async(holder)
    props = bpy.context.scene.puppet_mocap
    try:
        _register_operator_once(operators.PUPPET_OT_check_deps)
        props.deps_checking = True  # ya hay una verificación en curso

        result = bpy.ops.puppet_mocap.check_deps()

        assert result == {"CANCELLED"}
        assert len(spy.calls) == 0, "no debe lanzar una segunda verificación en paralelo"
    finally:
        props.deps_checking = False
        _restore_run_async(holder)


def test_check_kimodo_deps_on_done_reports_missing():
    _register_props_once()
    holder = {}
    spy = _swap_run_async(holder)
    try:
        _register_operator_once(operators.PUPPET_OT_check_kimodo_deps)
        props = bpy.context.scene.puppet_mocap
        props.kimodo_python_path = "py"
        props.kimodo_deps_checking = False

        t0 = time.perf_counter()
        bpy.ops.puppet_mocap.check_kimodo_deps()
        elapsed = time.perf_counter() - t0
        assert elapsed < 1.0
        assert props.kimodo_deps_checking is True

        _work_fn, on_done = spy.calls[0]
        on_done({"kind": "ran", "returncode": 1, "stdout": "", "stderr": "ModuleNotFoundError\n"},
                None)
        assert props.kimodo_deps_checking is False
        assert "FALTAN" in props.kimodo_status
    finally:
        _restore_run_async(holder)


def test_autodetect_kimodo_python_button_on_done_sets_path():
    _register_props_once()
    holder = {}
    spy = _swap_run_async(holder)
    try:
        _register_operator_once(operators.PUPPET_OT_autodetect_kimodo_python)
        props = bpy.context.scene.puppet_mocap
        props.kimodo_python_checking = False
        props.kimodo_python_path = ""

        t0 = time.perf_counter()
        bpy.ops.puppet_mocap.autodetect_kimodo_python()
        elapsed = time.perf_counter() - t0
        assert elapsed < 1.0
        assert props.kimodo_python_checking is True

        _work_fn, on_done = spy.calls[0]
        on_done("C:/fake/python.exe", None)
        assert props.kimodo_python_checking is False
        assert props.kimodo_python_path == "C:/fake/python.exe"
    finally:
        _restore_run_async(holder)


def test_download_models_progress_and_on_done(tmp_path):
    _register_props_once()
    holder = {}
    spy = _swap_run_async(holder)
    try:
        _register_operator_once(operators.PUPPET_OT_download_models)
        props = bpy.context.scene.puppet_mocap
        props.models_downloading = False

        orig_addon_dir = operators._addon_dir
        # Redirige a un directorio vacío para que TODOS los modelos "falten"
        # sin depender de qué haya instalado de verdad en este equipo.
        operators._addon_dir = lambda: tmp_path
        try:
            t0 = time.perf_counter()
            bpy.ops.puppet_mocap.download_models()
            elapsed = time.perf_counter() - t0
        finally:
            operators._addon_dir = orig_addon_dir

        assert elapsed < 1.0, "no debe bloquear en la descarga real"
        assert props.models_downloading is True
        assert len(spy.calls) == 1
        _work_fn, on_done = spy.calls[0]

        fake_result = [(name, True, "1.0 MB") for name in operators.MODEL_URLS]
        on_done(fake_result, None)
        assert props.models_downloading is False
        assert "✓" in props.models_download_status
    finally:
        _restore_run_async(holder)
