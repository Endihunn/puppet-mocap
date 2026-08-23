"""Retarget de manos: orientación del wrist + 15 huesos de dedos por lado."""
from __future__ import annotations

import time

from .. import log
from .common import _state, aim, chained_world_3x3, mp_to_arm, orient_yz

# Tolerancia para flips de la palm normal: cuántos frames consecutivos con
# normal invertida toleramos antes de aceptarla como rotación real. A 20 fps,
# 3 frames ≈ 150 ms — suficiente para descartar flips espurios del modelo
# (<2 frames) sin bloquear giros de muñeca legítimos del usuario.
PALM_FLIP_TOLERANCE = 3

# Índices MediaPipe Pose para codo/muñeca por lado anatómico
_BODY_ELBOW_WRIST = {"Left": (13, 15), "Right": (14, 16)}

# --- MediaPipe Hand landmark indices --------------------------------------
HL_WRIST = 0
HL_THUMB_CMC, HL_THUMB_MCP, HL_THUMB_IP, HL_THUMB_TIP = 1, 2, 3, 4
HL_INDEX_MCP, HL_INDEX_PIP, HL_INDEX_DIP, HL_INDEX_TIP = 5, 6, 7, 8
HL_MIDDLE_MCP, HL_MIDDLE_PIP, HL_MIDDLE_DIP, HL_MIDDLE_TIP = 9, 10, 11, 12
HL_RING_MCP, HL_RING_PIP, HL_RING_DIP, HL_RING_TIP = 13, 14, 15, 16
HL_PINKY_MCP, HL_PINKY_PIP, HL_PINKY_DIP, HL_PINKY_TIP = 17, 18, 19, 20

FINGER_VECTORS = [
    ("HandThumb1",  HL_THUMB_CMC,   HL_THUMB_MCP),
    ("HandThumb2",  HL_THUMB_MCP,   HL_THUMB_IP),
    ("HandThumb3",  HL_THUMB_IP,    HL_THUMB_TIP),
    ("HandIndex1",  HL_INDEX_MCP,   HL_INDEX_PIP),
    ("HandIndex2",  HL_INDEX_PIP,   HL_INDEX_DIP),
    ("HandIndex3",  HL_INDEX_DIP,   HL_INDEX_TIP),
    ("HandMiddle1", HL_MIDDLE_MCP,  HL_MIDDLE_PIP),
    ("HandMiddle2", HL_MIDDLE_PIP,  HL_MIDDLE_DIP),
    ("HandMiddle3", HL_MIDDLE_DIP,  HL_MIDDLE_TIP),
    ("HandRing1",   HL_RING_MCP,    HL_RING_PIP),
    ("HandRing2",   HL_RING_PIP,    HL_RING_DIP),
    ("HandRing3",   HL_RING_DIP,    HL_RING_TIP),
    ("HandPinky1",  HL_PINKY_MCP,   HL_PINKY_PIP),
    ("HandPinky2",  HL_PINKY_PIP,   HL_PINKY_DIP),
    ("HandPinky3",  HL_PINKY_DIP,   HL_PINKY_TIP),
]

# Cadenas padre→hijo. Procesamos cada cadena propagando la matriz world 3x3
# manualmente para que los hijos vean la rotación recién asignada del padre,
# no la stale del depsgraph.
FINGER_CHAINS = (
    ("HandThumb1",  "HandThumb2",  "HandThumb3"),
    ("HandIndex1",  "HandIndex2",  "HandIndex3"),
    ("HandMiddle1", "HandMiddle2", "HandMiddle3"),
    ("HandRing1",   "HandRing2",   "HandRing3"),
    ("HandPinky1",  "HandPinky2",  "HandPinky3"),
)
_FINGER_VEC_BY_NAME = {n: (a, b) for n, a, b in FINGER_VECTORS}


def bone_names(prefix: str = "mixamorig:") -> list[str]:
    out = []
    for side in ("Left", "Right"):
        out.append(f"{prefix}{side}Hand")
        for suffix, _, _ in FINGER_VECTORS:
            out.append(f"{prefix}{side}{suffix}")
    return out


def _palm_basis(side: str, pts, flip_normal: bool = False, handedness: str | None = None):
    """Devuelve (palm_forward, palm_axis) en arm-space.
        palm_forward = wrist → middle MCP   (= Y local del bone Hand de Mixamo)
        palm_axis    = "hacia la palma"     (= Z local del bone Hand de Mixamo)

    Convención del rig Mixamo: Z local del Hand y del ForeArm apunta hacia la
    PALMA (no al dorso). Verificado con `_dump_hand_axes_once`: en T-pose con
    palmas abajo, el Z local sale a −Z arm = abajo = dirección palmar.

    Disambiguación palm/dorso: el cross-product (idx-wrist) × (pinky-wrist)
    es perpendicular al plano palmar, pero su SIGNO depende de qué cara ve
    MediaPipe — cuando la palma está casi paralela al rayo óptico, la
    inferencia de profundidad alterna entre "veo palma" y "veo dorso" y el
    sign del cross salta 180°. Una regla constante por chiralidad ("if Left:
    negate") sólo funciona en una orientación y rompe en la contraria.

    Solución: usar la posición anatómica del THUMB_CMC (landmark 1). El
    carpometacarpiano del pulgar articula con el trapecio, que está en la
    cara palmar del wrist — independiente de la chiralidad y de la pose de
    los dedos. Si normal.dot(thumb_CMC - wrist) < 0, el cross apunta al
    dorso → lo invertimos. Esto da una palma_normal consistente sin importar
    qué cara ve la cámara.

    El toggle `flip_palm_normal` queda como escape hatch para rigs no-Mixamo
    cuya convención del Z local sea opuesta (Z hacia el dorso).
    """
    wrist     = pts[0]
    thumb_cmc = pts[1]
    idx       = pts[5]
    mid       = pts[9]
    pinky     = pts[17]

    fwd = mid - wrist
    if fwd.length < 1e-6:
        return None
    fwd.normalize()

    normal = (idx - wrist).cross(pinky - wrist)
    if normal.length < 1e-6:
        return None
    normal.normalize()

    # Signo palma/dorso. PRIMARIO: handedness de MediaPipe. Se infiere del
    # aspecto 2D de la mano, NO de la profundidad (z) — por eso NO se voltea
    # cuando el dorso encara la cámara, que es justo donde el cross-product y
    # el thumb_CMC fallan (ambos dependen de la z y se invierten juntos al
    # volverse ambigua). El cross apunta a un lado fijo según la chiralidad;
    # negamos para "Right" para que ambas manos queden con el mismo sentido.
    # Si el signo GLOBAL sale al revés, `flip_palm_normal` lo corrige — y
    # entonces queda bien en TODA la rotación, no sólo con la palma a cámara.
    if handedness in ("Left", "Right"):
        if handedness == "Right":
            normal = -normal
    else:
        # FALLBACK sin handedness (capture viejo): heurístico thumb_CMC. El
        # thumb está en el semiespacio palmar; dot<0 ⇒ el cross apunta al
        # dorso ⇒ invertimos. Sólo confiable con la palma hacia la cámara.
        thumb_offset = thumb_cmc - wrist
        if thumb_offset.length > 1e-5 and normal.dot(thumb_offset) < 0.0:
            normal = -normal

    # Histéresis: si la normal cruda flippeó respecto al frame anterior, lo
    # tratamos como ruido del modelo (palma "de canto" → cross product cambia
    # de signo aunque la mano física no rotó). Tras PALM_FLIP_TOLERANCE
    # rechazos consecutivos aceptamos el flip como rotación real. La compara-
    # ción se hace ANTES de `flip_normal` (toggle del usuario) para que su
    # decisión no se cancele con la histéresis.
    cache = _state.setdefault("palm_normal_prev", {})
    prev = cache.get(side)
    flip_count = 0 if prev is None else prev.get("flip_count", 0)
    if prev is not None:
        if normal.dot(prev["normal"]) < 0.0:
            flip_count += 1
            if flip_count < PALM_FLIP_TOLERANCE:
                normal = -normal
            else:
                flip_count = 0
        else:
            flip_count = 0
    cache[side] = {"normal": normal.copy(), "flip_count": flip_count}

    if flip_normal:
        normal = -normal

    raw_normal_post_flip = normal.copy()
    normal = normal - normal.dot(fwd) * fwd
    if normal.length < 1e-4:
        return None
    normal.normalize()

    # Diagnóstico: una vez por lado, volcar los vectores clave en arm-space.
    diag_key = f"palm_diag_{side}"
    if not _state.get(diag_key):
        _state[diag_key] = True
        log.info(
            f"{side} palm_basis: "
            f"fwd=({fwd.x:+.2f},{fwd.y:+.2f},{fwd.z:+.2f}) "
            f"normal_raw=({raw_normal_post_flip.x:+.2f},{raw_normal_post_flip.y:+.2f},{raw_normal_post_flip.z:+.2f}) "
            f"normal=({normal.x:+.2f},{normal.y:+.2f},{normal.z:+.2f}) "
            f"angle_fwd_normal_deg={fwd.angle(normal) * 57.296:.1f}"
        )

    # Diag continuo (throttled ~1.5s/lado): si al rotar la muñeca el handedness
    # se mantiene estable, el signo del normal no parpadea = fix correcto. Si
    # el handedness se voltea aquí, el problema es la clasificación de MediaPipe.
    # P4: tras la property debug_hands (off por defecto) — antes logueaba por
    # lado cada 1.5 s toda la sesión.
    if _state.get("debug_hands"):
        _tk = f"_palm_log_t_{side}"
        _now = time.time()
        if _now - _state.get(_tk, 0.0) > 1.5:
            _state[_tk] = _now
            log.info(f"[palmaV3] {side} hd={handedness} "
                     f"normal=({normal.x:+.2f},{normal.y:+.2f},{normal.z:+.2f})")

    return fwd, normal


def _orient_forearm_from_palm(arm, bone_side: str, body_landmarks,
                              palm_normal_arm, prefix: str):
    """Restringe el roll del forearm orientándolo con orient_yz(elbow→wrist,
    palm_normal). Resuelve la indeterminación del aim() de body.apply, que
    deja el roll libre y multiplica el efecto de los flips de la palma.

    Devuelve el world 3x3 recién asignado al forearm para que el Hand hijo
    lo use como `parent_world_3x3` y no lea la matriz stale del depsgraph.
    Retorna None si no pudo orientar (debe caerse al fallback de body.apply).
    """
    fa = arm.pose.bones.get(f"{prefix}{bone_side}ForeArm")
    if fa is None:
        return None
    elbow_idx, wrist_idx = _BODY_ELBOW_WRIST[bone_side]
    if len(body_landmarks) <= max(elbow_idx, wrist_idx):
        return None
    elbow = mp_to_arm(body_landmarks[elbow_idx])
    wrist = mp_to_arm(body_landmarks[wrist_idx])
    y = wrist - elbow
    if y.length < 1e-4:
        return None
    y.normalize()
    # Matriz fresca del brazo (la acaba de calcular body.apply este mismo
    # frame) — sin ella el ForeArm se orienta contra el depsgraph stale.
    arm_world = _state.get("arm_world_3x3", {}).get(bone_side)
    q_fa = orient_yz(fa, y, palm_normal_arm, parent_world_3x3=arm_world)
    if q_fa is None:
        return None
    return chained_world_3x3(fa, q_fa, parent_world_3x3=arm_world)


def _hand_matches_body_side(lms, body_landmarks, bone_side) -> bool:
    """True si la muñeca de la mano está más cerca de la muñeca del cuerpo de
    `bone_side` que de la opuesta — i.e. los landmarks del cuerpo corresponden
    al mismo brazo que esta mano. Sustituye al viejo `data_side == bone_side`,
    que se apagaba en silencio tras el auto-swap correctivo (P1-5)."""
    wrist_idx = _BODY_ELBOW_WRIST[bone_side][1]  # 15 Left / 16 Right
    other_idx = 16 if wrist_idx == 15 else 15
    if not lms or len(body_landmarks) <= other_idx:
        return False
    hand_wrist = mp_to_arm(lms[0])
    b_wrist = mp_to_arm(body_landmarks[wrist_idx])
    b_other = mp_to_arm(body_landmarks[other_idx])
    return (hand_wrist - b_wrist).length < (hand_wrist - b_other).length


def _dump_hand_axes_once(arm, prefix: str):
    """Loguea los ejes locales del Hand al primer apply para debug."""
    if _state.get("last_axis_dump"):
        return
    _state["last_axis_dump"] = True
    for side in ("Left", "Right"):
        pb = arm.pose.bones.get(f"{prefix}{side}Hand")
        if pb is None:
            log.warn(f"hueso {prefix}{side}Hand no encontrado en el rig")
            continue
        rest = pb.bone.matrix_local.to_3x3()
        cols = [rest.col[i] for i in range(3)]
        log.info(
            f"{side}Hand rest axes: "
            f"X=({cols[0][0]:+.2f},{cols[0][1]:+.2f},{cols[0][2]:+.2f}) "
            f"Y=({cols[1][0]:+.2f},{cols[1][1]:+.2f},{cols[1][2]:+.2f}) "
            f"Z=({cols[2][0]:+.2f},{cols[2][1]:+.2f},{cols[2][2]:+.2f})"
        )


def _apply_one_hand(arm, data_side: str, bone_side: str, lms,
                    prefix: str, flip_normal: bool,
                    body_landmarks=None, handedness: str | None = None) -> int:
    """Aplica una mano al rig.

    `data_side` = clave del cache de histéresis por lado en `_palm_basis` (el
        signo palma/dorso lo decide `handedness`, no este parámetro). Debe
        seguir a la entrada para no mezclar el estado de histéresis entre manos.
    `bone_side` = a qué huesos del rig se aplica (LeftHand* o RightHand*).
        Es el que cambia con swap.
    `body_landmarks` = lms 33 del pose corporal (opcional). Si está dado y la
        muñeca de la mano corresponde al mismo lado del cuerpo que `bone_side`
        (por proximidad, no por data_side), sobrescribe la orientación del
        ForeArm con orient_yz(elbow→wrist, palm_normal) para restringir el roll
        y eliminar la indeterminación que multiplica los flips de muñeca.
    """
    if not lms or len(lms) < 21:
        return 0

    pts = [mp_to_arm(lm) for lm in lms]
    moved = 0

    pb_hand = arm.pose.bones.get(f"{prefix}{bone_side}Hand")
    if pb_hand is None:
        return 0

    basis = _palm_basis(data_side, pts, flip_normal=flip_normal, handedness=handedness)
    if basis is None:
        return 0
    fwd, normal = basis

    # Fix A: orientar el forearm con la palm normal cuando los landmarks del
    # cuerpo corresponden al MISMO brazo que esta mano (por proximidad de
    # muñeca, no por data_side — P1-5). Si la mano se aplica al lado contrario
    # (swap manual), el body ya orientó con aim() y dejamos ese roll libre.
    forearm_world_3x3 = None
    if body_landmarks and len(body_landmarks) >= 17 and \
            _hand_matches_body_side(lms, body_landmarks, bone_side):
        forearm_world_3x3 = _orient_forearm_from_palm(
            arm, bone_side, body_landmarks, normal, prefix,
        )

    # Hand: si orientamos el forearm en este frame, pasarle el 3x3 fresh para
    # que no lea la matriz stale del depsgraph. Si no, fallback al comporta-
    # miento anterior (parent.matrix del frame previo).
    q_hand = orient_yz(pb_hand, fwd, normal, parent_world_3x3=forearm_world_3x3)
    if q_hand is None:
        # No pudimos orientar el Hand, no tiene sentido seguir con dedos.
        return 0
    moved += 1

    # World 3x3 del Hand con su rotación recién asignada — clave para que los
    # dedos no hereden una matriz stale del depsgraph. Sin esto, el thumb
    # termina apuntando a `R_hand · target` en lugar de `target` (R_hand ≈ 90°
    # cuando la palma rota al frente), lo que se ve como "el pulgar va al revés".
    hand_world_3x3 = chained_world_3x3(pb_hand, q_hand,
                                        parent_world_3x3=forearm_world_3x3)

    for chain in FINGER_CHAINS:
        parent_3x3 = hand_world_3x3
        for suffix in chain:
            pb = arm.pose.bones.get(f"{prefix}{bone_side}{suffix}")
            if pb is None:
                break
            idx = _FINGER_VEC_BY_NAME.get(suffix)
            if idx is None:
                continue
            p_idx, c_idx = idx
            v = pts[c_idx] - pts[p_idx]
            q_assigned = None
            if v.length > 1e-5:
                if "Thumb" in suffix:
                    q_assigned = orient_yz(pb, v.normalized(), normal, parent_world_3x3=parent_3x3)
                else:
                    q_assigned = aim(pb, v.normalized(), parent_world_3x3=parent_3x3)
                if q_assigned is not None:
                    moved += 1
            # Propagar al siguiente hijo de la cadena. Si aim falló, usamos la
            # rotación actual del bone (no la matrix stale) para mantener la
            # consistencia.
            q_for_chain = q_assigned if q_assigned is not None else pb.rotation_quaternion
            parent_3x3 = chained_world_3x3(pb, q_for_chain, parent_world_3x3=parent_3x3)

    return moved


def _hand_landmarks(side_data):
    """Acepta lista cruda de 21 lms o {'lm': [...]} del nuevo formato."""
    if side_data is None:
        return None
    if isinstance(side_data, dict):
        return side_data.get("lm")
    return side_data


def _hand_field(side_data, key):
    """Extrae un campo del dict de una mano (p.ej. 'hd'=handedness). None si
    el payload es la lista cruda vieja o el campo no existe."""
    if isinstance(side_data, dict):
        return side_data.get(key)
    return None


def apply(arm, hands, landmarks=None, prefix: str = "mixamorig:",
          swap: bool = False, flip_normal: bool = False) -> int:
    """Aplica orientación de manos + dedos a partir del payload `hands`."""
    if not hands:
        return 0
    l_lms = _hand_landmarks(hands.get("L"))
    r_lms = _hand_landmarks(hands.get("R"))
    # handedness de MediaPipe por lado (viaja con el dato; se usa para el signo
    # palma/dorso). Sigue al dato en el auto-swap de abajo.
    l_hd = _hand_field(hands.get("L"), "hd")
    r_hd = _hand_field(hands.get("R"), "hd")

    # data_side = chiralidad anatómica del lado físico que produjo el dato.
    # Lo trackeamos por entrada para que tanto el auto-swap como el manual
    # actualicen consistentemente; pasarle la data_side equivocada a
    # `_palm_basis` invierte la normal de la palma → muñeca rota 180°.
    l_data_side = "Left"
    r_data_side = "Right"

    # Red de seguridad: el capture_runner ya empareja L/R por proximidad
    # image-space; esto ataja casos raros (mano fuera del frame al momento
    # de la pose, swap por handedness en versiones viejas, etc). Margen del
    # 20% para no oscilar cuando las muñecas se cruzan al aplaudir.
    if landmarks and len(landmarks) >= 17 and l_lms and r_lms:
        try:
            body_l_wrist = mp_to_arm(landmarks[15])
            body_r_wrist = mp_to_arm(landmarks[16])
            hand_l_wrist = mp_to_arm(l_lms[0])
            hand_r_wrist = mp_to_arm(r_lms[0])

            dist_keep = ((body_l_wrist - hand_l_wrist).length
                         + (body_r_wrist - hand_r_wrist).length)
            dist_swap = ((body_l_wrist - hand_r_wrist).length
                         + (body_r_wrist - hand_l_wrist).length)

            did_swap = dist_swap < dist_keep * 0.8
            if did_swap:
                l_lms, r_lms = r_lms, l_lms
                # Tras intercambiar las listas, l_lms contiene datos del lado
                # físico contrario; data_side y handedness deben seguirlas.
                l_data_side, r_data_side = r_data_side, l_data_side
                l_hd, r_hd = r_hd, l_hd
            # Log solo en TRANSICIÓN — antes esto disparaba un log (con
            # flush a disco) por cada frame mientras la condición persistía.
            if did_swap != _state.get("auto_swap_active", False):
                _state["auto_swap_active"] = did_swap
                if did_swap:
                    log.info("hands: auto-swap correctivo activado (capture asignó al revés)")
                else:
                    log.info("hands: auto-swap correctivo desactivado")
        except Exception as e:
            log.warn(f"Error en auto-swap de manos: {e}")

    # Loguear el primer frame con manos para diagnóstico (qué nos llega exactamente)
    if not _state.get("first_hand_logged"):
        _state["first_hand_logged"] = True
        sample = l_lms or r_lms
        if sample and len(sample) >= 21:
            wrist = sample[0]
            mid = sample[9]
            log.info(
                f"primer frame de manos: L={'sí' if l_lms else 'no'} R={'sí' if r_lms else 'no'} "
                f"swap={swap} wrist={wrist} mid_mcp={mid}"
            )

    # `bone_side` = a qué huesos del rig va. swap solo cambia el bone_side;
    # data_side se mantiene atado a la chiralidad física del dato para que
    # `_palm_basis` calcule la normal hacia el dorso correctamente.
    moved = 0
    if l_lms:
        moved += _apply_one_hand(
            arm, data_side=l_data_side,
            bone_side=("Right" if swap else "Left"),
            lms=l_lms, prefix=prefix, flip_normal=flip_normal,
            body_landmarks=landmarks, handedness=l_hd,
        )
    if r_lms:
        moved += _apply_one_hand(
            arm, data_side=r_data_side,
            bone_side=("Left" if swap else "Right"),
            lms=r_lms, prefix=prefix, flip_normal=flip_normal,
            body_landmarks=landmarks, handedness=r_hd,
        )

    _dump_hand_axes_once(arm, prefix)
    return moved
