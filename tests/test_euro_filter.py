"""Tests del One Euro Filter (capture/euro_filter.py) — sin dependencias."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from puppet_mocap.capture.euro_filter import OneEuro, OneEuroVec  # noqa: E402


def test_first_sample_passthrough():
    f = OneEuro()
    assert f(0.7, 1.0) == 0.7


def test_constant_signal_converges():
    f = OneEuro(min_cutoff=2.0, beta=0.1)
    f(0.0, 0.0)
    prev = 0.0
    for i in range(1, 200):
        prev = f(1.0, i / 30.0)
    assert abs(prev - 1.0) < 1e-3


def test_one_euro_vec_shape_and_visibility():
    f = OneEuroVec(2, freq=30.0, min_cutoff=1.0, beta=0.1)
    lms = [[0.1, 0.2, 0.3, 0.9], [0.4, 0.5, 0.6, 1.0]]
    out = f(lms, 0.0)
    assert len(out) == 2
    assert len(out[0]) == 4
    assert out[0][3] == 0.9  # visibility passthrough


def test_one_euro_smoothes_step():
    # Un salto grande en un instante no se aplica de golpe (filtro, no passthrough)
    f = OneEuro(min_cutoff=1.0, beta=0.0)
    f(0.0, 0.0)
    y = f(1.0, 0.033)
    assert 0.0 < y < 1.0
