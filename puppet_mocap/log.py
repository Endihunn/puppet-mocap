"""Logger persistente del addon Puppet Mocap.

Escribe a %TEMP%/puppet_mocap.log con timestamp y traceback completo en
excepciones. La idea: cuando el addon o el subprocess se rompe, el usuario
abre ese archivo y ve qué pasó sin tener que correr Blender desde consola.

Uso:
    from . import log
    log.info("mensaje")
    log.error("algo salió mal")
    try:
        ...
    except Exception:
        log.exception("descripción del contexto")  # añade traceback
"""
from __future__ import annotations

import logging
import os
import sys
import tempfile
import time
from pathlib import Path

LOG_PATH = Path(tempfile.gettempdir()) / "puppet_mocap.log"
_MAX_LOG_BYTES = 5 * 1024 * 1024  # rota si supera 5 MB

_logger: logging.Logger | None = None


def get_log_path() -> str:
    return str(LOG_PATH)


class _FlushingFileHandler(logging.FileHandler):
    """FileHandler que hace flush después de CADA registro, y fsync throttled.

    Crítico para diagnóstico de crashes: un crash duro de Blender (segfault o
    abort de C++) no flushea buffers de Python. Sin fsync, las últimas líneas
    — exactamente donde está la pista del crash — se pierden.

    fsync en cada registro costaba 1-10 ms de FlushFileBuffers en el hilo
    principal (peor con ESET filtrando escrituras). Ahora: flush siempre,
    fsync solo para ERROR+ o máximo una vez por segundo.
    """

    _FSYNC_INTERVAL = 1.0

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._last_fsync = 0.0

    def emit(self, record):
        super().emit(record)
        try:
            if self.stream is not None:
                self.stream.flush()
                now = time.monotonic()
                if (record.levelno >= logging.ERROR
                        or now - self._last_fsync >= self._FSYNC_INTERVAL):
                    self._last_fsync = now
                    if hasattr(self.stream, "fileno"):
                        os.fsync(self.stream.fileno())
        except (OSError, ValueError):
            pass


def _setup() -> logging.Logger:
    global _logger
    if _logger is not None:
        return _logger

    lg = logging.getLogger("puppet_mocap")
    lg.setLevel(logging.DEBUG)
    lg.propagate = False

    # Truncar al iniciar si excede el tamaño max — evita archivos infinitos
    try:
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > _MAX_LOG_BYTES:
            LOG_PATH.unlink()
    except OSError:
        pass

    fmt = logging.Formatter(
        "%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    try:
        fh = _FlushingFileHandler(str(LOG_PATH), mode="a", encoding="utf-8")
        fh.setFormatter(fmt)
        lg.addHandler(fh)
    except OSError as e:
        sys.stderr.write(f"[puppet_mocap] No pude abrir log {LOG_PATH}: {e}\n")

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    lg.addHandler(sh)

    _logger = lg
    # Marcar que el logger ya está armado, en log y en consola
    lg.info(f"=== logger inicializado, escribiendo a {LOG_PATH} ===")
    return lg


def info(msg: str) -> None:
    _setup().info(msg)


def warn(msg: str) -> None:
    _setup().warning(msg)


def error(msg: str) -> None:
    _setup().error(msg)


def exception(msg: str) -> None:
    """Loguea un mensaje + el traceback completo del except actual."""
    _setup().exception(msg)


def debug(msg: str) -> None:
    _setup().debug(msg)


def banner(msg: str) -> None:
    """Línea destacada para marcar inicio/fin de sesiones de captura."""
    lg = _setup()
    lg.info("=" * 60)
    lg.info(msg)
    lg.info("=" * 60)


def clear() -> bool:
    """Trunca el log EN SITIO. En Windows unlink() falla mientras el
    FileHandler (o el subprocess de captura) tenga el archivo abierto —
    truncar a través del handle propio siempre funciona."""
    lg = _setup()
    for h in lg.handlers:
        if isinstance(h, logging.FileHandler):
            try:
                h.acquire()
                try:
                    if h.stream is not None:
                        h.stream.seek(0)
                        h.stream.truncate()
                finally:
                    h.release()
                return True
            except (OSError, ValueError):
                pass
    # Fallback: truncar con un handle nuevo (modo 'w' comparte con 'a')
    try:
        with open(LOG_PATH, "w", encoding="utf-8"):
            pass
        return True
    except OSError:
        return False
