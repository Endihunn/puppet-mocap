"""Tests del conversor Kimodo → Mixamo (K2-T1).

Corren bajo el Python de Blender (mathutils + numpy reales). Fuera de Blender
se saltan con importorskip (mathutils no tiene wheel de Windows).
Reportar POR EJE, nunca por magnitud (lo que enmascaró el bug de T1).
"""
import math
import pathlib
import sys

import pytest

np = pytest.importorskip("numpy")
mathutils = pytest.importorskip("mathutils")
from mathutils import Matrix, Quaternion, Vector  # noqa: E402

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from puppet_mocap.kimodo.convert import (  # noqa: E402
    SOMA77_TO_MIXAMO, YUP_TO_ZUP, convert_motion, is_finger_bone,
    joint_order, mapping_for, prefixed_mapping)

DATA = pathlib.Path(__file__).resolve().parent / "data"

ZUP = YUP_TO_ZUP


def _assert_close(a, b, tol=1e-5):
    assert abs(a - b) < tol, f"{a} != {b}"


def test_yup_to_zup_axes():
    # Kimodo → Blender: +Y(up)→+Z, +Z(forward)→−Y, +X(left)→+X
    expect = {
        (1, 0, 0): (1, 0, 0),   # X = left
        (0, 1, 0): (0, 0, 1),   # Y = up
        (0, 0, 1): (0, -1, 0),  # Z = forward
    }
    for k, e in expect.items():
        v = ZUP @ Vector(k)
        assert (v - Vector(e)).length < 1e-6


def test_yup_to_zup_rotation_axis():
    # rot 90° sobre Kimodo +Y (up) → rot 90° sobre Blender +Z (up)
    R_k = Matrix.Rotation(math.pi / 2, 3, "Y")
    R_b = ZUP @ R_k @ ZUP.transposed()
    axis = R_b.to_quaternion().axis
    assert abs(axis.z) > 0.99   # gira sobre +Z (up), no sobre X/Y


def test_input_to_armature_rotation_is_applied_after_yup_to_zup():
    """Kimodo también debe respetar la rotación del objeto Armature."""
    motion = {
        "posed_joints": np.array([[[0.0, 0.0, 0.0],
                                   [0.0, 1.0, 0.0]]]),  # +Y Kimodo = +Z canónico
        "root_positions": np.zeros((1, 3)),
    }
    names = ["Hips", "Spine1"]
    rest3 = {"Hips": Matrix.Identity(3)}
    rest_pos = {"Hips": Vector((0, 0, 0))}
    parent_of = {"Hips": None}
    mapping = {"Hips": "Hips"}
    input_to_armature = Matrix.Rotation(-math.pi / 2.0, 3, "X")

    rotations, _ = convert_motion(
        motion, names, rest3, rest_pos, parent_of, mapping,
        apply_root=False, root_scale=1.0,
        input_to_armature=input_to_armature,
    )
    q = Quaternion(rotations["Hips"][0][1])
    assert q.angle < 1e-6  # +Z canónico → +Y local, la rest ya apunta +Y


def test_aim_points_bone_at_child_joint():
    """El retarget es POR DIRECCION: el eje Y del hueso debe quedar apuntando
    al joint hijo de Kimodo.

    Antes se componian las global_rot_mats sobre la rest de Mixamo. Medido
    sobre walk_wave eso daba 14.1 grados de error angular medio (max 49.2), y
    usarlas como orientacion absoluta —lo que hacia el codigo original— daba
    95.3 (max 159.1): el personaje salia retorcido. Apuntar da 0.2 (max 3.0).
    """
    # Rest realista: un Hips de Mixamo apunta HACIA ARRIBA, o sea que su eje Y
    # local va al +Z del armature. Esa rest es justamente YUP_TO_ZUP.
    rest3 = {"Hips": ZUP, "Spine": ZUP}
    rest_pos = {"Hips": Vector((0, 0, 0)), "Spine": Vector((0, 0, 1))}
    parent_of = {"Hips": None, "Spine": "Hips"}
    names = ["Hips", "Spine1"]
    mapping = {"Hips": "Hips", "Spine1": "Spine"}

    def _ang(child_kimodo):
        pj = np.zeros((1, 2, 3))
        pj[0, 1] = child_kimodo
        motion = {"posed_joints": pj, "root_positions": np.zeros((1, 3)),
                  "fps": 30.0}
        rot, _ = convert_motion(motion, names, rest3, rest_pos, parent_of,
                                mapping, fps=30.0, root_scale=1.0)
        q = Quaternion(rot["Hips"][0][1])
        if q.w < 0:
            q = Quaternion((-q.w, -q.x, -q.y, -q.z))
        return math.degrees(q.angle)

    # Hijo hacia ARRIBA en Kimodo (+Y) = +Z de Blender = la propia rest.
    assert _ang((0.0, 1.0, 0.0)) < 1e-3, "apuntar a la rest deberia dar identidad"
    # Hijo hacia ADELANTE en Kimodo (+Z) = -Y de Blender: 90 grados desde +Z.
    a = _ang((0.0, 0.0, 1.0))
    assert abs(a - 90.0) < 1e-3, f"esperaba 90 grados, dio {a}"
    # Hijo hacia ABAJO en Kimodo (-Y) = -Z de Blender: 180 grados.
    a = _ang((0.0, -1.0, 0.0))
    assert abs(a - 180.0) < 1e-3, f"esperaba 180 grados, dio {a}"


def test_aim_child_map_covers_every_mapped_bone():
    """Todo hueso Mixamo mapeado debe tener un joint hijo al que apuntar; si no,
    se queda en rest y el rig sale a medio animar."""
    from puppet_mocap.kimodo.convert import SOMA_AIM_CHILD
    m = mapping_for(77)
    faltan = [k for k in m if k not in SOMA_AIM_CHILD]
    assert not faltan, f"sin joint hijo para apuntar: {faltan}"
    # y el hijo debe existir en el esqueleto de 77
    orden = set(joint_order(77))
    malos = [(k, v) for k, v in SOMA_AIM_CHILD.items()
             if k in m and v not in orden]
    assert not malos, f"hijo inexistente en SOMA-77: {malos}"


def test_root_is_relative_to_first_frame():
    """La traslación de raíz es RELATIVA al primer frame, no absoluta.

    Kimodo canonicaliza su root a XZ=(0,0) con la cadera a ~1 m, que no tiene
    relación con dónde está el Hips del rig. Usando la posición absoluta, sobre
    un Mixamo de escala centimétrica el personaje se hundía 52 unidades bajo el
    suelo en el frame 1.
    """
    rest3 = {"Hips": Matrix.Identity(3)}
    rest_pos = {"Hips": Vector((0, 0, 0))}
    parent_of = {"Hips": None}
    rp = np.array([[0.0, 0.9, 0.0], [0.0, 0.9, 0.0]])
    motion = {"posed_joints": np.zeros((len(rp), 77, 3)),
              "root_positions": rp, "fps": 30.0}
    _rot, root = convert_motion(motion, ["Hips"], rest3, rest_pos, parent_of,
                                {"Hips": "Hips"}, fps=30.0, root_scale=1.0)
    for i in range(3):
        _assert_close(root["Hips"][0][1][i], 0.0, 1e-6)


def test_root_axes_per_axis():
    """Delta de Kimodo → delta de Blender, EJE POR EJE (nunca magnitud).
    Kimodo es Y-up: +Y=arriba, +Z=adelante, +X=lateral.
    """
    rest3 = {"Hips": Matrix.Identity(3)}
    rest_pos = {"Hips": Vector((0, 0, 0))}
    parent_of = {"Hips": None}
    # frame 0 = origen; frame 1 = +0.5 en cada eje de Kimodo, por separado
    cases = {
        "arriba  (Kimodo +Y)": ([0.0, 0.5, 0.0], (0.0, 0.0, 0.5)),
        "adelante(Kimodo +Z)": ([0.0, 0.0, 0.5], (0.0, -0.5, 0.0)),
        "lateral (Kimodo +X)": ([0.5, 0.0, 0.0], (0.5, 0.0, 0.0)),
    }
    for label, (kimodo_delta, expected) in cases.items():
        rp = np.array([[0.0, 0.0, 0.0], kimodo_delta])
        motion = {"posed_joints": np.zeros((2, 77, 3)),
                  "root_positions": rp, "fps": 30.0}
        _rot, root = convert_motion(motion, ["Hips"], rest3, rest_pos, parent_of,
                                    {"Hips": "Hips"}, fps=30.0, root_scale=1.0)
        got = root["Hips"][1][1]
        for i, ax in enumerate("XYZ"):
            assert abs(got[i] - expected[i]) < 1e-6, (
                f"{label}: eje {ax} dio {got[i]:+.3f}, esperaba {expected[i]:+.3f}")


def test_root_scale_from_torso():
    """Kimodo emite metros; un Mixamo típico está en centímetros (~100 unidades
    de alto). Sin escalar, una caminata de 6 m avanzaba 6 unidades sobre un
    cuerpo de 100 — el personaje caminaba en el sitio.
    """
    from puppet_mocap.kimodo.convert import torso_scale
    # rig 100x: torso de 18 unidades; Kimodo: torso de 0.18 m
    rest_pos = {"Hips": Vector((0, 0, 0)), "Spine2": Vector((0, 0, 18.0))}
    pj = np.zeros((1, 2, 3), dtype=np.float32)
    pj[0, 1] = (0.0, 0.18, 0.0)
    motion = {"posed_joints": pj}
    sc = torso_scale(motion, ["Hips", "Chest"], {"Hips": "Hips", "Chest": "Spine2"},
                     rest_pos, "Hips")
    _assert_close(sc, 100.0, 1e-3)
    # sin datos suficientes cae a 1.0 en vez de reventar
    assert torso_scale({}, [], {}, {}, "Hips") == 1.0


def test_mapping_for_joint_counts():
    # 22 huesos de cuerpo: Hips, 3 spine, Neck, Head, 2x(Shoulder/Arm/ForeArm/
    # Hand), 2x(UpLeg/Leg/Foot/ToeBase). Sin dedos (ver convert.py).
    assert len(mapping_for(77)) == 22
    assert len(mapping_for(22)) == 22
    assert len(joint_order(77)) == 77
    assert len(joint_order(22)) == 22


def test_leg_naming_trap():
    """SOMA llama 'Leg' al MUSLO y 'Shin' a la pierna. Mapear por nombre
    cablea las piernas invertidas y medio rig sigue pareciendo correcto.
    Si alguien 'arregla' el mapa por nombre, este test falla."""
    m = mapping_for(77)
    assert m["LeftLeg"] == "LeftUpLeg"
    assert m["LeftShin"] == "LeftLeg"
    assert m["RightLeg"] == "RightUpLeg"
    assert m["RightShin"] == "RightLeg"
    # La columna va desplazada un puesto
    assert m["Spine1"] == "Spine"
    assert m["Spine2"] == "Spine1"
    assert m["Chest"] == "Spine2"
    assert m["Neck1"] == "Neck"
    # Identidad donde SÍ coinciden (no romper lo que ya está bien)
    for b in ("Hips", "Head", "LeftArm", "LeftForeArm", "LeftHand",
              "LeftShoulder", "LeftFoot", "LeftToeBase"):
        assert m[b] == b


def test_no_finger_channels():
    """Kimodo rellena las falanges con pose estática; mapearlas metía 120
    fcurves constantes que pisan las manos capturadas."""
    assert not any(is_finger_bone(v) for v in SOMA77_TO_MIXAMO.values())
    assert not any(is_finger_bone(v) for v in mapping_for(77).values())
    assert is_finger_bone("LeftHandThumb1")      # el detector funciona
    assert not is_finger_bone("LeftHand")


def _mixamo_rest_from_npz_neutral():
    """Rig Mixamo sintético (cuerpo + dedos) para ejercitar el filtrado."""
    spec = [("Hips", None), ("Spine", "Hips"), ("Spine1", "Spine"),
            ("Spine2", "Spine1"), ("Neck", "Spine2"), ("Head", "Neck")]
    for s_ in ("Left", "Right"):
        spec += [(s_ + "Shoulder", "Spine2"), (s_ + "Arm", s_ + "Shoulder"),
                 (s_ + "ForeArm", s_ + "Arm"), (s_ + "Hand", s_ + "ForeArm")]
        for f in ("Thumb", "Index", "Middle", "Ring", "Pinky"):
            for i in (1, 2, 3):
                par = s_ + "Hand" if i == 1 else f"{s_}Hand{f}{i - 1}"
                spec.append((f"{s_}Hand{f}{i}", par))
        spec += [(s_ + "UpLeg", "Hips"), (s_ + "Leg", s_ + "UpLeg"),
                 (s_ + "Foot", s_ + "Leg"), (s_ + "ToeBase", s_ + "Foot")]
    rest3, rest_pos, parent_of = {}, {}, {}
    for i, (n, par) in enumerate(spec):
        rest3[n] = Matrix.Identity(3)
        rest_pos[n] = Vector((0.0, 0.0, 1.0 + i * 0.02))
        parent_of[n] = par
    return rest3, rest_pos, parent_of


@pytest.mark.parametrize("fixture", ["synthetic_77.npz", "kimodo_walk_wave.npz"])
def test_real_npz_fixture(fixture):
    """Carga un .npz real (no una matriz sintética a mano), convierte y
    comprueba el recuento de canales. Es el test que destapa un mapeo que
    emite de más."""
    path = DATA / fixture
    with np.load(path, allow_pickle=False) as z:
        motion = {k: np.asarray(z[k]) for k in z.files}

    for key, ndim in (("posed_joints", 3), ("global_rot_mats", 4),
                      ("root_positions", 2)):
        assert key in motion, f"falta {key} en {fixture}"
        assert motion[key].ndim == ndim

    j = int(motion["posed_joints"].shape[1])
    assert j == 77
    t = int(motion["posed_joints"].shape[0])

    rest3, rest_pos, parent_of = _mixamo_rest_from_npz_neutral()
    rotations, root = convert_motion(
        motion, joint_order(j), rest3, rest_pos, parent_of, mapping_for(j),
        fps=30.0)

    # 22 huesos de cuerpo, CERO dedos — aunque el rig sí tiene dedos.
    assert len(rotations) == 22, sorted(rotations)
    assert not [b for b in rotations if is_finger_bone(b)]
    assert "Hips" in root and len(root["Hips"]) == t
    for pts in rotations.values():
        assert len(pts) == t

    # 22 huesos x 4 componentes + 3 de Hips.location
    assert len(rotations) * 4 + 3 == 91


def test_sibling_branches_do_not_contaminate():
    """Regresion: `parent_world` era una variable unica arrastrada sobre una
    lista ordenada por profundidad, asi que al pasar de una rama a otra del
    mismo nivel el segundo hermano heredaba la matriz del primero.

    Se prueban TODOS los pares (i, j) de hermanos: mover el hermano i no puede
    alterar al hermano j. Asi no depende del orden de recorrido.
    """
    names = joint_order(77)
    m = mapping_for(77)
    siblings = [("Spine1", "Spine"), ("LeftLeg", "LeftUpLeg"),
                ("RightLeg", "RightUpLeg")]
    rx = Matrix.Rotation(math.radians(37.0), 3, "X")
    rz = Matrix.Rotation(math.radians(-52.0), 3, "Z")
    rest3 = {"Hips": rx, "Spine": rz, "LeftUpLeg": rx @ rz, "RightUpLeg": rz @ rx,
             "Spine1": Matrix.Identity(3), "LeftLeg": rx, "RightLeg": rz}
    rest_pos = {b: Vector((0.0, 0.0, 1.0)) for b in rest3}
    parent_of = {"Hips": None, "Spine": "Hips", "LeftUpLeg": "Hips",
                 "RightUpLeg": "Hips", "Spine1": "Spine", "LeftLeg": "LeftUpLeg",
                 "RightLeg": "RightUpLeg"}

    def _run(moved, ang):
        pj = np.zeros((1, 77, 3))
        for k in names:
            pj[0, names.index(k)] = (0.0, 1.0, 0.0)   # todos apuntando arriba
        if moved:
            child = __import__("puppet_mocap.kimodo.convert", fromlist=["x"]).SOMA_AIM_CHILD[moved]
            pj[0, names.index(child)] = (math.sin(math.radians(ang)),
                                         math.cos(math.radians(ang)), 0.0)
        motion = {"posed_joints": pj, "root_positions": np.zeros((1, 3)), "fps": 30.0}
        rots, _ = convert_motion(motion, names, rest3, rest_pos, parent_of, m,
                                 fps=30.0, apply_root=False, root_scale=1.0)
        return {b: pts[0][1] for b, pts in rots.items()}

    base = _run(None, 0.0)
    for moved_k, moved_mx in siblings:
        got = _run(moved_k, 70.0)
        for _k, other_mx in siblings:
            if other_mx == moved_mx or other_mx not in base or other_mx not in got:
                continue
            for i, (va, vb) in enumerate(zip(base[other_mx], got[other_mx])):
                assert abs(va - vb) < 1e-6, (
                    f"mover {moved_mx} altero {other_mx} (componente {i})")


def test_bone_prefix_is_applied():
    """Un rig Mixamo real usa 'mixamorig:Hips', pero mapping_for() devuelve
    'Hips'. Sin prefijar, NINGÚN hueso casaba: la generación no movía nada
    (y antes del fix del bucle, horneaba identidad sobre todo el rig).
    """
    m = prefixed_mapping(mapping_for(77), "mixamorig:")
    assert m["Hips"] == "mixamorig:Hips"
    assert m["LeftLeg"] == "mixamorig:LeftUpLeg"
    assert len(m) == len(mapping_for(77))
    # prefijo vacío = identidad
    assert prefixed_mapping(mapping_for(77), "") == mapping_for(77)


def test_end_to_end_with_prefixed_rig():
    """El caso REAL: rig Mixamo con prefijo y dedos, npz generado de verdad.
    Debe mover los 22 huesos de cuerpo, cero dedos, y trasladar la raíz."""
    prefix = "mixamorig:"
    with np.load(DATA / "kimodo_walk_wave.npz", allow_pickle=False) as z:
        motion = {k: np.asarray(z[k]) for k in z.files}
    j = int(motion["posed_joints"].shape[1])

    rest3, rest_pos, parent_of = _mixamo_rest_from_npz_neutral()
    rest3 = {prefix + k: v for k, v in rest3.items()}
    rest_pos = {prefix + k: v for k, v in rest_pos.items()}
    parent_of = {prefix + k: (prefix + v if v else None)
                 for k, v in parent_of.items()}

    mapping = prefixed_mapping(mapping_for(j), prefix)
    rotations, root = convert_motion(motion, joint_order(j), rest3, rest_pos,
                                     parent_of, mapping, fps=30.0)

    assert len(rotations) == 22, sorted(rotations)
    assert all(b.startswith(prefix) for b in rotations)
    assert not [b for b in rotations if is_finger_bone(b)]
    assert prefix + "Hips" in root

    # La raíz se mueve de verdad (no es una curva plana)
    xs = [p[1][0] for p in root[prefix + "Hips"]]
    assert max(xs) - min(xs) > 1e-4, "la traslación de raíz salió plana"

    # Y el cuerpo también: alguna rotación debe variar entre frames
    moved = False
    for pts in rotations.values():
        if max(abs(pts[i][1][k] - pts[0][1][k])
               for i in range(len(pts)) for k in range(4)) > 1e-4:
            moved = True
            break
    assert moved, "ninguna rotación varía: el rig saldría congelado"


def test_pelvis_sigue_el_yaw_de_la_fuente():
    """REGRESION: apuntar la pelvis (casi vertical) a su hijo deja el giro
    alrededor de la vertical indeterminado, asi que el personaje NUNCA giraba.

    Medido antes del fix, rotando la fuente: 45 grados -> 48 de error;
    90 -> 94; 180 -> 179. O sea el rig se quedaba encarando siempre igual.
    """
    from puppet_mocap.kimodo.convert import TWO_VEC_ORIENT  # noqa: F401
    path = DATA / "kimodo_walk_wave.npz"
    with np.load(path, allow_pickle=False) as z:
        base = {k: np.asarray(z[k]) for k in z.files}
    j = int(base["posed_joints"].shape[1])
    names = joint_order(j)
    jidx = {n: i for i, n in enumerate(names)}

    bones = ("Hips", "Spine", "LeftUpLeg", "RightUpLeg", "Spine1", "Spine2", "Neck")
    rest3 = {b: Matrix.Identity(3) for b in bones}
    rest_pos = {b: Vector((0, 0, 1.0)) for b in bones}
    parent_of = {"Hips": None, "Spine": "Hips", "LeftUpLeg": "Hips",
                 "RightUpLeg": "Hips", "Spine1": "Spine", "Spine2": "Spine1",
                 "Neck": "Spine2"}
    mapping = {k: v for k, v in mapping_for(j).items() if v in rest3}

    for deg in (45.0, 90.0, 180.0):
        th = math.radians(deg)
        # giro alrededor del eje Y de Kimodo (up)
        R = np.array([[math.cos(th), 0, math.sin(th)],
                      [0, 1, 0],
                      [-math.sin(th), 0, math.cos(th)]])
        motion = dict(base)
        motion["posed_joints"] = base["posed_joints"] @ R.T
        pj = motion["posed_joints"]
        rot, _ = convert_motion(motion, names, rest3, rest_pos, parent_of,
                                mapping, fps=30.0, apply_root=False, root_scale=1.0)
        f, q = rot["Hips"][0]
        lat = ZUP @ Vector(tuple(float(pj[f, jidx["LeftLeg"], k]
                                       - pj[f, jidx["RightLeg"], k]) for k in range(3)))
        lat.normalize()
        src = math.degrees(math.atan2(lat.y, lat.x))
        m = Quaternion(q).to_matrix()
        xa = Vector((m[0][0], m[1][0], m[2][0]))   # eje X del hueso = lateral
        rig = math.degrees(math.atan2(xa.y, xa.x))
        err = abs((rig - src + 180) % 360 - 180)
        assert err < 2.0, f"giro {deg}: el rig da {rig:.1f} y la fuente {src:.1f} ({err:.1f} de error)"
