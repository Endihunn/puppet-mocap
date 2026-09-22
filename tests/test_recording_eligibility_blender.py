"""Tests para can_start_recording() (P1, hallazgo #6): antes 'Grabar' se
dibujaba siempre disponible y toggle_record() solo exigía server.is_running(),
sin cliente conectado ni ningún canal grabable activo.
"""
import types

from puppet_mocap import operators, server


class _FakeAliveThread:
    def is_alive(self):
        return True


def _set_server_state(running: bool, connected: bool):
    server._state["thread"] = _FakeAliveThread() if running else None
    server._state["client_connected"] = connected


def _fake_props(enable_body=True, record_body=True,
                enable_hands=False, record_hands=True,
                enable_face=False, record_face=True):
    return types.SimpleNamespace(
        enable_body=enable_body, record_body=record_body,
        enable_hands=enable_hands, record_hands=record_hands,
        enable_face=enable_face, record_face=record_face,
    )


def test_rejects_when_server_not_running():
    _set_server_state(running=False, connected=False)
    ok, msg = operators.can_start_recording(_fake_props())
    assert ok is False
    assert "captura" in msg.lower()


def test_rejects_when_running_but_no_client_connected():
    _set_server_state(running=True, connected=False)
    ok, msg = operators.can_start_recording(_fake_props())
    assert ok is False
    assert "cliente" in msg.lower() or "cámara" in msg.lower()


def test_rejects_when_connected_but_no_recordable_channel():
    _set_server_state(running=True, connected=True)
    props = _fake_props(enable_body=False, enable_hands=False, enable_face=False)
    ok, msg = operators.can_start_recording(props)
    assert ok is False
    assert "canal" in msg.lower()


def test_rejects_when_channels_enabled_but_record_flags_off():
    """enable_body=True pero record_body=False (el usuario apagó grabar ese
    canal aunque siga viéndolo en vivo) tampoco cuenta como grabable."""
    _set_server_state(running=True, connected=True)
    props = _fake_props(enable_body=True, record_body=False,
                         enable_hands=False, enable_face=False)
    ok, msg = operators.can_start_recording(props)
    assert ok is False


def test_allows_when_connected_and_body_channel_active():
    _set_server_state(running=True, connected=True)
    ok, msg = operators.can_start_recording(_fake_props())
    assert ok is True
    assert msg == ""


def teardown_module(module):
    server._state["thread"] = None
    server._state["client_connected"] = False
