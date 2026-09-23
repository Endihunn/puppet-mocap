"""Brief de Fase 3: "Comprobar cancelación y limpieza de trabajos asíncronos".
Dos gaps reales:
1. unregister_handlers()/on_load_pre no mataban el subprocess de Kimodo
   (torch) si el addon se deshabilitaba o el .blend cambiaba a media
   generación -- quedaba huérfano corriendo en segundo plano.
2. run_async()'s poll no atrapaba errores de on_done -- si el addon se
   desregistra o el archivo cambia mientras un check corre, on_done escribe
   en props que ya no existen y eso reventaba con una traceback en consola.
"""
import time

from puppet_mocap import operators


class _FakeProc:
    def __init__(self):
        self.terminated = False
        self.killed = False
        self._alive = True

    def poll(self):
        return None if self._alive else 0

    def terminate(self):
        self.terminated = True
        self._alive = False

    def wait(self, timeout=None):
        if self._alive:
            raise Exception("no debería bloquear si terminate() ya marcó muerto")

    def kill(self):
        self.killed = True
        self._alive = False


def test_unregister_handlers_kills_orphaned_kimodo_process():
    proc = _FakeProc()
    operators._KIMODO_STATE["proc"] = proc
    try:
        operators.unregister_handlers()
        assert proc.terminated is True
        assert operators._KIMODO_STATE["proc"] is None
    finally:
        operators._KIMODO_STATE["proc"] = None
        operators.register_handlers()  # deja el estado del addon como estaba


def test_on_load_pre_kills_orphaned_kimodo_process():
    proc = _FakeProc()
    operators._KIMODO_STATE["proc"] = proc
    try:
        operators._on_load_pre()
        assert proc.terminated is True
        assert operators._KIMODO_STATE["proc"] is None
    finally:
        operators._KIMODO_STATE["proc"] = None


def test_run_async_poll_swallows_on_done_exceptions():
    """Si on_done falla (p.ej. props ya no existen tras desregistrar el
    addon), el poll no debe propagar la excepción -- Blender ejecuta
    timers en su bucle principal; una excepción sin atrapar ahí se ve como
    traceback en consola por un job que el usuario ya ni puede ver."""
    def _boom(result, error):
        raise RuntimeError("props ya no existen")

    poll = operators.run_async(lambda: "ok", _boom)
    time.sleep(0.2)

    result = poll()  # NO debe lanzar

    assert result is None  # sigue señalizando "desregístrame" pese al error
