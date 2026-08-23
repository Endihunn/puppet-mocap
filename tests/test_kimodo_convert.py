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


def test_identity_kimodo_gives_rest_mixamo():
    # Cadena sintética Hips→LeftArm con rest matrices NO triviales.
    rest3 = {
        "Hips": Matrix.Rotation(math.radians(20), 3, "X"),
        "LeftArm": Matrix.Rotation(math.radians(-30), 3, "Y")
                   @ Matrix.Rotation(math.radians(10), 3, "Z"),
    }
    rest_pos = {"Hips": Vector((0, 0, 0)), "LeftArm": Vector((0, 1, 0))}
    parent_of = {"Hips": None, "LeftArm": "Hips"}
    joint_order = ["Hips", "LeftArm"]
    mapping = {"Hips": "Hips", "LeftArm": "LeftArm"}

    # Kimodo global_rot_mats que reproduzcan la rest Mixamo (en mundo, Z-up):
    #   r_k = YUP^T @ rest3[bone] @ YUP
    T = 3
    g = np.zeros((T, 2, 3, 3))
    rp = np.zeros((T, 3))
    for f in range(T):
        for i, jname in enumerate(joint_order):
            bone = mapping[jname]
            g[f, i] = ZUP.transposed() @ rest3[bone] @ ZUP
    motion = {"global_rot_mats": g, "root_positions": rp, "fps": 30.0}

    rot, _root = convert_motion(motion, joint_order, rest3, rest_pos,
                                parent_of, mapping, fps=30.0)

    # Con el motion correcto, TODOS los huesos deben quedar en rest (basis=identity)
    for bone, pts in rot.items():
        for f, (w, x, y, z) in pts:
            q = Quaternion((w, x, y, z))
            if q.w < 0:
                q = Quaternion((-q.w, -q.x, -q.y, -q.z))
            assert q.angle < 1e-4, f"{bone} frame {f} no quedó en rest (angle={q.angle})"


def test_root_yup_to_zup_and_rest_basis():
    # Hips.location está en el REST BASIS del hueso (bug T1). Con rest3[Hips]=I
    # y head en el origen, un root en Kimodo (0,0.9,0) debe dar location=(0,0,0.9)
    # (Y-up → Z-up) — POR EJE, no magnitud.
    rest3 = {"Hips": Matrix.Identity(3)}
    rest_pos = {"Hips": Vector((0, 0, 0))}
    parent_of = {"Hips": None}
    g = np.eye(3).reshape(1, 1, 3, 3)
    rp = np.array([[0.0, 0.9, 0.0]])
    motion = {"global_rot_mats": g, "root_positions": rp, "fps": 30.0}
    _rot, root = convert_motion(motion, ["Hips"], rest3, rest_pos,
                                parent_of, {"Hips": "Hips"}, fps=30.0)
    loc = root["Hips"][0][1]
    _assert_close(loc[0], 0.0, 1e-5)   # X
    _assert_close(loc[1], 0.0, 1e-5)   # Y (armature)
    _assert_close(loc[2], 0.9, 1e-5)   # Z (up) — Kimodo +Y → Blender +Z


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
    """Regresión: `parent_world` era una sola variable arrastrada sobre una
    lista ordenada por profundidad, así que al pasar de una rama a otra del
    mismo nivel el segundo hermano heredaba la matriz del primero en vez de la
    de su padre.

    El test prueba TODOS los pares (i, j) de hermanos: mover el hermano i no
    puede alterar al hermano j. Así no depende del orden en que se recorran
    los huesos — con el bug, el hermano procesado después de uno rotado
    siempre se desvía, sea cual sea ese orden.
    """
    names = joint_order(77)
    m = mapping_for(77)
    # Tres hermanos, todos hijos directos de Hips en Mixamo.
    siblings = [("Spine1", "Spine"), ("LeftLeg", "LeftUpLeg"),
                ("RightLeg", "RightUpLeg")]
    for kimodo_name, mixamo_name in siblings:
        assert m[kimodo_name] == mixamo_name

    # Rest matrices NO identidad y DISTINTAS entre sí: con identidad el bug
    # es invisible porque toda la cadena colapsa.
    rx = Matrix.Rotation(math.radians(37.0), 3, "X")
    rz = Matrix.Rotation(math.radians(-52.0), 3, "Z")
    rest3 = {"Hips": rx, "Spine": rz, "LeftUpLeg": rx @ rz,
             "RightUpLeg": rz @ rx}
    rest_pos = {b: Vector((0.0, 0.0, 1.0)) for b in rest3}
    parent_of = {"Hips": None, "Spine": "Hips", "LeftUpLeg": "Hips",
                 "RightUpLeg": "Hips"}

    def _run(rotated_joint, angle_deg):
        g = np.zeros((1, 77, 3, 3), dtype=np.float32)
        g[:, :] = np.eye(3, dtype=np.float32)
        if rotated_joint is not None:
            r = Matrix.Rotation(math.radians(angle_deg), 3, "Y")
            g[0, names.index(rotated_joint)] = np.array(
                [tuple(r[i]) for i in range(3)], dtype=np.float32)
        motion = {
            "global_rot_mats": g,
            "posed_joints": np.zeros((1, 77, 3), dtype=np.float32),
            "root_positions": np.zeros((1, 3), dtype=np.float32),
        }
        rots, _ = convert_motion(motion, names, rest3, rest_pos, parent_of, m,
                                 fps=30.0, apply_root=False)
        return {b: pts[0][1] for b, pts in rots.items()}

    base = _run(None, 0.0)
    for moved_k, moved_mx in siblings:
        got = _run(moved_k, 80.0)
        for _other_k, other_mx in siblings:
            if other_mx == moved_mx:
                continue
            for i, (va, vb) in enumerate(zip(base[other_mx], got[other_mx])):
                assert abs(va - vb) < 1e-6, (
                    f"mover {moved_mx} alteró {other_mx} "
                    f"(componente {i}: {va} -> {vb})")


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
