"""Cliente externo: webcam + MediaPipe → JSON line-delimited al addon.

Lo lanza el addon vía subprocess.Popen. NO uses MCP — el addon expone su propio
puerto TCP y nosotros enviamos un JSON por línea.

Uso (ejecutado por el addon):
    py capture_runner.py --addon-port 9878 --cam 0 --fps 20 \
                         --smooth-min-cutoff 1.5 --smooth-beta 0.05 \
                         --model /path/to/pose_landmarker_lite.task

Salir: ESC o Q en la ventana de webcam, o cerrarla con la X (el addon también
hace terminate).

Diseño v0.3:
  * Hilo lector de cámara con patrón "última frame": la inferencia siempre
    consume la frame más reciente — el buffering interno del driver ya no
    acumula 100-300 ms de lag cuando la inferencia va lenta.
  * El loop corre al ritmo de --fps (lo que se envía): antes se inferían
    los 30 fps de la cámara para tirar un tercio a la basura.
  * Los 3 landmarkers (pose/manos/cara) corren EN PARALELO en un
    ThreadPoolExecutor: latencia = max(inferencias), no la suma.
  * Timestamps monotónicos (perf_counter) — time.time() puede retroceder con
    NTP y detect_for_video exige timestamps estrictamente crecientes.
  * TCP_NODELAY (Nagle metía jitter de hasta 40 ms en paquetes chicos).
  * Payload compacto: floats a 5 decimales (~60% menos bytes; el ruido de
    MediaPipe es órdenes de magnitud mayor que 1e-5 m).
  * Manos sin pose corporal: fallback por handedness — antes manos-solo
    enviaba exactamente nada, en silencio.
"""
from __future__ import annotations

import argparse
import json
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import cv2
import mediapipe as mp
from mediapipe.tasks import python as mp_python
from mediapipe.tasks.python import vision as mp_vision

sys.path.insert(0, str(Path(__file__).resolve().parent))
from euro_filter import OneEuroVec  # noqa: E402

WIN_NAME = "Puppet Mocap (Q/ESC para salir)"

POSE_CONNECTIONS = [
    (11, 12), (11, 13), (13, 15), (12, 14), (14, 16),
    (11, 23), (12, 24), (23, 24),
    (23, 25), (25, 27), (24, 26), (26, 28),
    (27, 29), (27, 31), (29, 31),
    (28, 30), (28, 32), (30, 32),
    (0, 2), (0, 5), (2, 7), (5, 8),
]

HAND_CONNECTIONS = [
    (0, 1), (1, 2), (2, 3), (3, 4),
    (0, 5), (5, 6), (6, 7), (7, 8),
    (0, 9), (9, 10), (10, 11), (11, 12),
    (0, 13), (13, 14), (14, 15), (15, 16),
    (0, 17), (17, 18), (18, 19), (19, 20),
    (5, 9), (9, 13), (13, 17)
]


class CameraReader(threading.Thread):
    """Lee la cámara tan rápido como entrega y guarda solo la frame más
    reciente. Acota la latencia cámara→inferencia a ~1 frame sin importar el
    buffering del backend."""

    def __init__(self, cap):
        super().__init__(daemon=True)
        self.cap = cap
        self.lock = threading.Lock()
        self.frame = None
        self.stopped = False
        self.dead = False

    def run(self):
        failures = 0
        while not self.stopped:
            ok, frame = self.cap.read()
            if not ok:
                failures += 1
                if failures > 30:
                    self.dead = True
                    return
                time.sleep(0.05)
                continue
            failures = 0
            with self.lock:
                self.frame = frame

    def latest(self):
        with self.lock:
            return self.frame

    def stop(self):
        self.stopped = True


class AddonClient:
    """Conexión TCP persistente al server del addon. Reconecta si se cae."""

    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self.sock: socket.socket | None = None
        self.last_connect_attempt = 0.0

    def connect(self) -> bool:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(2.0)
            s.connect((self.host, self.port))
            s.settimeout(None)
            # Nagle retiene paquetes chicos esperando ACKs → jitter
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.sock = s
            return True
        except OSError:
            self.sock = None
            return False

    def send_json(self, msg: dict) -> bool:
        if self.sock is None:
            now = time.monotonic()
            if now - self.last_connect_attempt < 1.0:
                return False
            self.last_connect_attempt = now
            if not self.connect():
                return False
        payload = (json.dumps(msg, separators=(",", ":")) + "\n").encode("utf-8")
        try:
            self.sock.sendall(payload)
            return True
        except OSError:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None
            return False

    def close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None


def _r5(v: float) -> float:
    return round(float(v), 5)


def world_landmarks_to_list(world_lms) -> list:
    return [[_r5(lm.x), _r5(lm.y), _r5(lm.z),
             _r5(getattr(lm, "visibility", 1.0))] for lm in world_lms]


def hand_to_absolute(hand_world_lms, pose_wrist) -> list:
    """Suma la posición de la muñeca del Pose para obtener coords en el mismo
    sistema que pose_world_landmarks (origen en mid-hips). Sin pose corporal,
    pose_wrist=(0,0,0): el retarget de manos solo usa vectores relativos
    dentro de la mano, así que la orientación sigue siendo correcta."""
    wx, wy, wz = pose_wrist
    return [[_r5(lm.x + wx), _r5(lm.y + wy), _r5(lm.z + wz)]
            for lm in hand_world_lms]


def detect_hands_categorized(h_result, pose_image_lms, pose_world_lms):
    """Devuelve dict {'L': {'lm': [21 lms], 'hd': str}, 'R': {...}}.

    CON pose: empareja cada mano con pose[15]=LEFT_WRIST / pose[16]=RIGHT_WRIST
    por PROXIMIDAD en image-space. NO usa `handedness` para el L/R: la
    convención "viewer's perspective" de la Tasks API cambia con el mirror de
    la imagen y algunos drivers espejan sin avisar; la proximidad a las
    muñecas anatómicas del Pose es estable.

    SIN pose (captura manos-solo o cuerpo perdido): fallback por handedness.
    Los labels de MediaPipe asumen imagen ESPEJADA; nosotros inferimos sin
    espejar, así que label 'Right' = mano físicamente izquierda → 'L'.
    """
    out = {}
    if not h_result.hand_landmarks or not h_result.hand_world_landmarks:
        return out

    def _hd(idx):
        try:
            return h_result.handedness[idx][0].category_name
        except (IndexError, AttributeError, TypeError):
            return None

    if pose_image_lms is None or pose_world_lms is None:
        # Fallback sin ancla corporal
        for i, _ in enumerate(h_result.hand_landmarks):
            hd = _hd(i)
            side = "L" if hd == "Right" else "R"
            if side in out:
                side = "R" if side == "L" else "L"
                if side in out:
                    continue
            out[side] = {
                "lm": hand_to_absolute(h_result.hand_world_landmarks[i],
                                       (0.0, 0.0, 0.0)),
                "hd": hd,
            }
        return out

    pose_l_xy = (pose_image_lms[15].x, pose_image_lms[15].y)
    pose_r_xy = (pose_image_lms[16].x, pose_image_lms[16].y)

    def dist2(a, b):
        return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2

    # Cada mano detectada: (idx, dist a L, dist a R) en image-space
    candidates = []
    for i, hand_img in enumerate(h_result.hand_landmarks):
        h_wrist_xy = (hand_img[0].x, hand_img[0].y)
        candidates.append((i, dist2(h_wrist_xy, pose_l_xy), dist2(h_wrist_xy, pose_r_xy)))

    # Asignar primero la mano con mayor |d_L - d_R| (la decisión más confiable).
    candidates.sort(key=lambda c: -abs(c[1] - c[2]))

    used = set()
    for idx, d_l, d_r in candidates:
        side = "L" if d_l < d_r else "R"
        if side in used:
            side = "R" if side == "L" else "L"
            if side in used:
                continue
        used.add(side)

        if side == "L":
            wrist_pose = (pose_world_lms[15].x, pose_world_lms[15].y, pose_world_lms[15].z)
        else:
            wrist_pose = (pose_world_lms[16].x, pose_world_lms[16].y, pose_world_lms[16].z)

        out[side] = {
            "lm": hand_to_absolute(h_result.hand_world_landmarks[idx], wrist_pose),
            "hd": _hd(idx),
        }

    return out


def draw_overlay(frame, image_lms, connections=None, color=(0, 255, 255)):
    h, w = frame.shape[:2]
    pts = [(int(lm.x * w), int(lm.y * h)) for lm in image_lms]
    conn = connections or POSE_CONNECTIONS
    for a, b in conn:
        if a < len(pts) and b < len(pts):
            cv2.line(frame, pts[a], pts[b], color, 2)
    for p in pts:
        cv2.circle(frame, p, 3, (0, 0, 255), -1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--addon-port", type=int, required=True)
    ap.add_argument("--addon-host", default="127.0.0.1")
    ap.add_argument("--cam", type=int, default=0)
    ap.add_argument("--fps", type=float, default=20.0,
                    help="Ritmo del loop: inferencia + envío por segundo.")
    ap.add_argument("--smooth-min-cutoff", type=float, default=1.5)
    ap.add_argument("--smooth-beta", type=float, default=0.05)
    ap.add_argument("--model", type=str, default="",
                    help="Ruta a pose_landmarker_lite.task. Vacío = sin pose corporal.")
    ap.add_argument("--hand-model", type=str, default="",
                    help="Ruta a hand_landmarker.task. Vacío = sin captura de manos.")
    ap.add_argument("--face-model", type=str, default="",
                    help="Ruta a face_landmarker.task. Vacío = sin captura de cara.")
    ap.add_argument("--width", type=int, default=640)
    ap.add_argument("--height", type=int, default=480)
    args = ap.parse_args()

    if args.model and not Path(args.model).exists():
        print(f"[capture] pose model not found: {args.model}", file=sys.stderr)
        return 1
    if not (args.model or args.hand_model or args.face_model):
        print("[capture] ningún módulo activo (ni pose, ni manos, ni cara)", file=sys.stderr)
        return 1

    client = AddonClient(args.addon_host, args.addon_port)
    print(f"[capture] connecting to addon at {args.addon_host}:{args.addon_port}")
    for _ in range(10):
        if client.connect():
            break
        time.sleep(0.3)
    if client.sock is None:
        print("[capture] could not connect to addon (will retry while running)")

    landmarker = None
    if args.model:
        base_opts = mp_python.BaseOptions(model_asset_path=args.model)
        options = mp_vision.PoseLandmarkerOptions(
            base_options=base_opts,
            running_mode=mp_vision.RunningMode.VIDEO,
            num_poses=1,
            min_pose_detection_confidence=0.5,
            min_pose_presence_confidence=0.5,
            min_tracking_confidence=0.5,
        )
        landmarker = mp_vision.PoseLandmarker.create_from_options(options)
        print("[capture] body pose detection enabled")

    hand_landmarker = None
    if args.hand_model and Path(args.hand_model).exists():
        h_base = mp_python.BaseOptions(model_asset_path=args.hand_model)
        h_opts = mp_vision.HandLandmarkerOptions(
            base_options=h_base,
            running_mode=mp_vision.RunningMode.VIDEO,
            num_hands=2,
            min_hand_detection_confidence=0.5,
            min_hand_presence_confidence=0.5,
            min_tracking_confidence=0.7,
        )
        hand_landmarker = mp_vision.HandLandmarker.create_from_options(h_opts)
        print("[capture] hand detection enabled")
    elif args.hand_model:
        print(f"[capture] hand model NOT found: {args.hand_model}", file=sys.stderr)

    face_landmarker = None
    if args.face_model and Path(args.face_model).exists():
        f_base = mp_python.BaseOptions(model_asset_path=args.face_model)
        f_opts = mp_vision.FaceLandmarkerOptions(
            base_options=f_base,
            running_mode=mp_vision.RunningMode.VIDEO,
            num_faces=1,  # 1 cara = suavizado interno gratis de MediaPipe
            output_face_blendshapes=True,
            output_facial_transformation_matrixes=False,
            min_face_detection_confidence=0.5,
            min_face_presence_confidence=0.5,
            min_tracking_confidence=0.5,
        )
        face_landmarker = mp_vision.FaceLandmarker.create_from_options(f_opts)
        print("[capture] face detection enabled")
    elif args.face_model:
        print(f"[capture] face model NOT found: {args.face_model}", file=sys.stderr)

    cap = cv2.VideoCapture(args.cam, cv2.CAP_DSHOW)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, args.width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, args.height)
    # MJPG evita que muchas cámaras UVC caigan a YUY2 lento; buffersize es
    # best-effort (DSHOW suele ignorarlo — por eso también existe CameraReader)
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    cap.set(cv2.CAP_PROP_FPS, 30)
    if not cap.isOpened():
        print(f"[capture] could not open webcam {args.cam}", file=sys.stderr)
        return 2

    reader = CameraReader(cap)
    reader.start()

    n_active = sum(1 for x in (landmarker, hand_landmarker, face_landmarker)
                   if x is not None)
    executor = ThreadPoolExecutor(max_workers=3) if n_active > 1 else None

    # Euro filters
    pose_euro = OneEuroVec(33, freq=30.0, min_cutoff=args.smooth_min_cutoff,
                           beta=args.smooth_beta, d_cutoff=1.0)
    hand_lm_euros = {
        "L": OneEuroVec(21, freq=30.0, min_cutoff=args.smooth_min_cutoff,
                        beta=args.smooth_beta, d_cutoff=1.0),
        "R": OneEuroVec(21, freq=30.0, min_cutoff=args.smooth_min_cutoff,
                        beta=args.smooth_beta, d_cutoff=1.0),
    }
    face_euros: dict = {}

    def smooth_blendshape(name: str, val: float, t: float) -> float:
        f = face_euros.get(name)
        if f is None:
            f = OneEuroVec(1, freq=30.0, min_cutoff=args.smooth_min_cutoff,
                           beta=args.smooth_beta, d_cutoff=1.0)
            face_euros[name] = f
        out = f([[val, 0.0, 0.0]], t)
        return out[0][0]

    interval = 1.0 / max(1.0, args.fps)
    n_frames = 0
    n_sent = 0
    t0 = time.perf_counter()
    last_ts_ms = -1
    print("[capture] running. ESC/Q o cerrar la ventana para salir.")

    rc = 0
    try:
        while True:
            loop_start = time.perf_counter()
            if reader.dead:
                print("[capture] camera stopped delivering frames", file=sys.stderr)
                rc = 2
                break
            frame = reader.latest()
            if frame is None:
                if cv2.waitKey(20) & 0xFF in (27, ord("q")):
                    break
                continue
            frame = frame.copy()  # el reader puede reemplazar la suya

            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
            # Timestamps: monotónicos y estrictamente crecientes o MediaPipe
            # revienta el grafo (y no se recupera). Mismo ts para las 3 tasks.
            ts_ms = int((time.perf_counter() - t0) * 1000)
            if ts_ms <= last_ts_ms:
                ts_ms = last_ts_ms + 1
            last_ts_ms = ts_ms
            now = time.monotonic()
            n_frames += 1

            # ---- Inferencia (paralela si hay 2+ landmarkers) ----
            result = h_result = f_result = None
            if executor is not None:
                fut_p = (executor.submit(landmarker.detect_for_video, mp_image, ts_ms)
                         if landmarker else None)
                fut_h = (executor.submit(hand_landmarker.detect_for_video, mp_image, ts_ms)
                         if hand_landmarker else None)
                fut_f = (executor.submit(face_landmarker.detect_for_video, mp_image, ts_ms)
                         if face_landmarker else None)
                result = fut_p.result() if fut_p else None
                h_result = fut_h.result() if fut_h else None
                f_result = fut_f.result() if fut_f else None
            else:
                if landmarker is not None:
                    result = landmarker.detect_for_video(mp_image, ts_ms)
                if hand_landmarker is not None:
                    h_result = hand_landmarker.detect_for_video(mp_image, ts_ms)
                if face_landmarker is not None:
                    f_result = face_landmarker.detect_for_video(mp_image, ts_ms)

            has_pose = bool(result and result.pose_landmarks)

            # ---- Overlay ----
            if has_pose:
                draw_overlay(frame, result.pose_landmarks[0])
            if h_result and h_result.hand_landmarks:
                for idx, hand_lms in enumerate(h_result.hand_landmarks):
                    try:
                        handedness = h_result.handedness[idx][0].category_name
                    except (IndexError, AttributeError, TypeError):
                        handedness = None
                    color = (255, 0, 255) if handedness == "Right" else (0, 255, 0)
                    draw_overlay(frame, hand_lms, connections=HAND_CONNECTIONS, color=color)
            if f_result and f_result.face_landmarks:
                fl = f_result.face_landmarks[0]
                h, w = frame.shape[:2]
                for lm in fl[::8]:  # downsample para no saturar
                    cv2.circle(frame, (int(lm.x * w), int(lm.y * h)), 1, (255, 255, 0), -1)

            # ---- Payload ----
            payload: dict = {"t": "pose"}

            if has_pose and result.pose_world_landmarks:
                lms = world_landmarks_to_list(result.pose_world_landmarks[0])
                payload["lm"] = [[_r5(x), _r5(y), _r5(z), _r5(v)]
                                 for x, y, z, v in pose_euro(lms, now)]

            if h_result and h_result.hand_world_landmarks:
                if has_pose and result.pose_world_landmarks:
                    raw_hands = detect_hands_categorized(
                        h_result, result.pose_landmarks[0],
                        result.pose_world_landmarks[0])
                else:
                    raw_hands = detect_hands_categorized(h_result, None, None)
                hands_data = {}
                for side, data in raw_hands.items():
                    lm_smooth = hand_lm_euros[side](data["lm"], now)
                    hands_data[side] = {
                        "lm": [[_r5(x), _r5(y), _r5(z)] for x, y, z, _ in lm_smooth],
                        "hd": data.get("hd"),
                    }
                if hands_data:
                    payload["hands"] = hands_data

            if f_result and f_result.face_blendshapes:
                bs = {}
                for cat in f_result.face_blendshapes[0]:
                    if cat.category_name == "_neutral":
                        continue
                    bs[cat.category_name] = _r5(smooth_blendshape(
                        cat.category_name, float(cat.score), now))
                if bs:
                    payload["face"] = {"blendshapes": bs}

            if "lm" in payload or "hands" in payload or "face" in payload:
                if client.send_json(payload):
                    n_sent += 1

            # ---- HUD + ventana ----
            display = cv2.flip(frame, 1)
            connected = client.sock is not None
            color = (0, 255, 0) if connected else (0, 0, 255)
            cv2.putText(
                display,
                f"f={n_frames} sent={n_sent} {'[CONNECTED]' if connected else '[DISCONNECTED]'}",
                (10, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2,
            )
            modules = []
            if landmarker is not None:
                modules.append("BODY" if has_pose else "body")
            if hand_landmarker is not None:
                modules.append("HANDS" if (h_result and h_result.hand_landmarks) else "hands")
            if face_landmarker is not None:
                modules.append("FACE" if (f_result and f_result.face_landmarks) else "face")
            cv2.putText(display, " ".join(modules), (10, 60),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.55, (200, 200, 0), 2)

            cv2.imshow(WIN_NAME, display)
            k = cv2.waitKey(1) & 0xFF
            if k in (27, ord("q")):
                break
            # Cerrar con la X de la ventana antes la hacía reaparecer
            try:
                if cv2.getWindowProperty(WIN_NAME, cv2.WND_PROP_VISIBLE) < 1:
                    break
            except cv2.error:
                break

            # ---- Pacing al ritmo de envío ----
            remaining = interval - (time.perf_counter() - loop_start)
            if remaining > 0.002:
                time.sleep(remaining - 0.001)

    finally:
        reader.stop()
        # P0-2: el reader puede estar bloqueado dentro de cap.read() cuando
        # soltamos el VideoCapture por debajo (uso-después-de-liberar en el lado
        # C++ de OpenCV). join() lo deja salir limpio antes de release().
        reader.join(timeout=2.0)
        cap.release()
        cv2.destroyAllWindows()
        if executor is not None:
            executor.shutdown(wait=False)
        if landmarker is not None:
            landmarker.close()
        if hand_landmarker is not None:
            hand_landmarker.close()
        if face_landmarker is not None:
            face_landmarker.close()
        client.close()
        elapsed = time.perf_counter() - t0
        print(f"[capture] done: {n_frames} frames captured, {n_sent} sent in {elapsed:.1f}s")
    return rc


if __name__ == "__main__":
    sys.exit(main())
