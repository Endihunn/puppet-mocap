"""One Euro Filter (Casiez et al. 2012) — suavizado adaptativo de baja latencia."""
import math


class _LowPass:
    def __init__(self, alpha: float):
        self.alpha = alpha
        self.y = None

    def __call__(self, x):
        self.y = x if self.y is None else self.alpha * x + (1.0 - self.alpha) * self.y
        return self.y


def _alpha(cutoff: float, dt: float) -> float:
    tau = 1.0 / (2.0 * math.pi * cutoff)
    return 1.0 / (1.0 + tau / dt)


class OneEuro:
    def __init__(self, freq=30.0, min_cutoff=1.0, beta=0.1, d_cutoff=1.0):
        self.freq = freq
        self.min_cutoff = min_cutoff
        self.beta = beta
        self.d_cutoff = d_cutoff
        self.x_filter = _LowPass(_alpha(min_cutoff, 1.0 / freq))
        self.dx_filter = _LowPass(_alpha(d_cutoff, 1.0 / freq))
        self.last_t = None

    def __call__(self, x: float, t: float) -> float:
        if self.last_t is None:
            self.last_t = t
            self.x_filter.y = x
            self.dx_filter.y = 0.0
            return x
        dt = max(t - self.last_t, 1e-3)
        self.last_t = t
        prev = self.x_filter.y if self.x_filter.y is not None else x
        dx = (x - prev) / dt
        # El alpha del filtro de derivada debe corresponder al dt ACTUAL antes
        # de suavizar dx; si no, la velocidad estimada usa el intervalo del
        # frame anterior y el cutoff adaptativo reacciona mal tras un hiccup.
        self.dx_filter.alpha = _alpha(self.d_cutoff, dt)
        dx_smooth = self.dx_filter(dx) if self.dx_filter.y is not None else dx
        cutoff = self.min_cutoff + self.beta * abs(dx_smooth)
        self.x_filter.alpha = _alpha(cutoff, dt)
        return self.x_filter(x)


class OneEuroVec:
    """One Euro Filter para una secuencia de N landmarks 3D."""

    def __init__(self, n: int, **kw):
        self.filters = [[OneEuro(**kw) for _ in range(3)] for _ in range(n)]

    def __call__(self, landmarks, t: float):
        out = []
        for i, lm in enumerate(landmarks):
            x = self.filters[i][0](lm[0], t)
            y = self.filters[i][1](lm[1], t)
            z = self.filters[i][2](lm[2], t)
            vis = lm[3] if len(lm) > 3 else 1.0
            out.append([x, y, z, vis])
        return out
