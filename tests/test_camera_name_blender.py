"""Nombre real de cámara (brief de Fase 3: 'Mostrar nombre real si está
disponible; no presentar un índice como un dispositivo identificado').

_detect_cameras_work() usa PowerShell/WMI (Get-PnpDevice) porque OpenCV no
da nombres de dispositivo por índice. Su orden de enumeración NO está
garantizado que coincida con el índice real de OpenCV -- por eso
camera_display_label() solo afirma una correspondencia exacta cuando hay
UNA sola cámara detectada; con varias, lo dice explícitamente en vez de
prometer una correspondencia que no se puede verificar.
"""
import subprocess

from puppet_mocap import operators


def _reset_camera_cache():
    operators._CAMERA_NAMES.update(names=None, checking=False, t=0.0)


def test_detect_cameras_work_parses_single_camera_json(monkeypatch):
    class _FakeResult:
        returncode = 0
        stdout = '"USB2.0 HD UVC WebCam"'
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeResult())
    names = operators._detect_cameras_work()
    assert names == ["USB2.0 HD UVC WebCam"]


def test_detect_cameras_work_parses_multiple_cameras_json_array(monkeypatch):
    class _FakeResult:
        returncode = 0
        stdout = '["Cam A", "Cam B"]'
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeResult())
    names = operators._detect_cameras_work()
    assert names == ["Cam A", "Cam B"]


def test_detect_cameras_work_returns_empty_on_failure(monkeypatch):
    def _raise(*a, **k):
        raise subprocess.TimeoutExpired(cmd="powershell", timeout=10)

    monkeypatch.setattr(subprocess, "run", _raise)
    assert operators._detect_cameras_work() == []


def test_detect_cameras_work_returns_empty_on_empty_stdout(monkeypatch):
    class _FakeResult:
        returncode = 0
        stdout = "   "
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeResult())
    assert operators._detect_cameras_work() == []


def test_camera_display_label_before_detection_shows_honest_placeholder():
    _reset_camera_cache()
    try:
        label = operators.camera_display_label(0)
        assert "#0" in label
        assert "detectando" in label
    finally:
        _reset_camera_cache()


def test_camera_display_label_with_no_names_detected_never_claims_a_name():
    _reset_camera_cache()
    operators._CAMERA_NAMES["names"] = []
    try:
        label = operators.camera_display_label(0)
        assert "#0" in label
        assert "no detectado" in label
    finally:
        _reset_camera_cache()


def test_camera_display_label_with_exactly_one_camera_shows_its_real_name():
    _reset_camera_cache()
    operators._CAMERA_NAMES["names"] = ["USB2.0 HD UVC WebCam"]
    try:
        label = operators.camera_display_label(0)
        assert label == "USB2.0 HD UVC WebCam"
        assert "#" not in label, "con una sola cámara no debe quedar rastro del índice desnudo"
    finally:
        _reset_camera_cache()


def test_camera_display_label_with_multiple_cameras_flags_unreliable_order():
    _reset_camera_cache()
    operators._CAMERA_NAMES["names"] = ["Cam A", "Cam B"]
    try:
        label = operators.camera_display_label(1)
        assert "Cam B" in label
        assert "no garantizado" in label, (
            "con más de una cámara, el emparejamiento nombre<->índice no está "
            "verificado y el texto debe decirlo, no prometerlo en silencio"
        )
    finally:
        _reset_camera_cache()


def test_camera_display_label_index_out_of_range_is_honest_not_a_crash():
    _reset_camera_cache()
    operators._CAMERA_NAMES["names"] = ["Cam A"]
    try:
        label = operators.camera_display_label(5)
        assert "#5" in label
        assert "no detectado" in label
    finally:
        _reset_camera_cache()
