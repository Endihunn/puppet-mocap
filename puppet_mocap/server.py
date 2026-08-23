"""Servidor TCP en background thread. Recibe JSON line-delimited del subprocess
de captura y los pone en una queue que el main thread drena vía bpy.app.timers.
"""
import json
import queue
import socket
import sys
import threading
import time

from . import log

POSE_QUEUE = queue.Queue(maxsize=200)
_state = {
    "thread": None,
    "stop_event": None,
    "client_connected": False,
    "port": None,
}


def is_running():
    th = _state["thread"]
    return th is not None and th.is_alive()


def is_client_connected():
    return _state["client_connected"]


def get_port():
    return _state["port"]


def _server_loop(stop_event, sock, port):
    """El socket llega ya bind+listen desde start() — un puerto ocupado se
    reporta al operador ANTES de lanzar el subprocess, no en un log tardío."""
    sock.settimeout(0.5)
    log.info(f"server escuchando en 127.0.0.1:{port}")

    bad_json = 0
    while not stop_event.is_set():
        try:
            conn, addr = sock.accept()
        except socket.timeout:
            continue
        except OSError as e:
            log.warn(f"server accept OSError: {e}")
            break
        log.info(f"cliente conectado desde {addr}")
        _state["client_connected"] = True
        conn.settimeout(0.5)
        buf = bytearray()
        try:
            while not stop_event.is_set():
                try:
                    chunk = conn.recv(65536)
                except socket.timeout:
                    continue
                except (ConnectionResetError, OSError) as e:
                    log.info(f"recv terminó: {e}")
                    break
                if not chunk:
                    break
                buf.extend(chunk)
                while True:
                    nl = buf.find(b"\n")
                    if nl < 0:
                        break
                    line = bytes(buf[:nl])
                    del buf[: nl + 1]
                    if not line.strip():
                        continue
                    try:
                        msg = json.loads(line.decode("utf-8"))
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        bad_json += 1
                        if bad_json <= 5 or bad_json % 100 == 0:
                            log.warn(f"JSON inválido descartado (#{bad_json})")
                        continue
                    # Sello de llegada: al grabar, el mapeo tiempo→frame usa
                    # este instante (no el del drain) para que un backlog no
                    # colapse varios mensajes en el mismo frame.
                    msg["_rx"] = time.time()
                    try:
                        POSE_QUEUE.put_nowait(msg)
                    except queue.Full:
                        try:
                            POSE_QUEUE.get_nowait()
                            POSE_QUEUE.put_nowait(msg)
                        except queue.Empty:
                            pass
        except Exception:
            log.exception("loop de recepción de cliente")
        finally:
            try:
                conn.close()
            except OSError:
                pass
            _state["client_connected"] = False
            log.info("cliente desconectado")
    try:
        sock.close()
    except OSError:
        pass
    log.info("server detenido")


def start(port: int) -> tuple[bool, str]:
    """Devuelve (ok, error_msg). Bind + listen ocurren AQUÍ (hilo llamador)
    para que un puerto ocupado cancele el arranque en vez de dejar un
    subprocess huérfano conectando a nada."""
    if is_running():
        return False, "el server ya está corriendo"
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if sys.platform == "win32":
        # Windows: SO_REUSEADDR permite a otro socket secuestrar un puerto con
        # listener activo (un bind wildcard se cuela sobre 127.0.0.1 y la
        # entrega de conexiones queda indefinida). SO_EXCLUSIVEADDRUSE falla
        # limpio si el puerto ya está tomado.
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        except OSError:
            pass
    else:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("127.0.0.1", port))
        sock.listen(1)
    except OSError as e:
        try:
            sock.close()
        except OSError:
            pass
        log.error(f"server bind falló en puerto {port}: {e}")
        return False, f"puerto {port} ocupado o inválido ({e})"
    stop_event = threading.Event()
    th = threading.Thread(target=_server_loop, args=(stop_event, sock, port),
                          daemon=True)
    th.start()
    _state["thread"] = th
    _state["stop_event"] = stop_event
    _state["port"] = port
    return True, ""


def stop() -> bool:
    """Devuelve True si el thread terminó y el estado quedó limpio; False si
    el join expiró (el thread sigue vivo y `is_running()` debe seguir
    devolviendo True para no lanzar un segundo server encima del zombi)."""
    ev = _state["stop_event"]
    if ev is not None:
        ev.set()
    th = _state["thread"]
    if th is not None:
        th.join(timeout=2.0)
        if th.is_alive():
            log.warn("server thread no terminó tras join(2s); estado NO limpiado")
            return False
    _state["thread"] = None
    _state["stop_event"] = None
    _state["client_connected"] = False
    while not POSE_QUEUE.empty():
        try:
            POSE_QUEUE.get_nowait()
        except queue.Empty:
            break
    return True
