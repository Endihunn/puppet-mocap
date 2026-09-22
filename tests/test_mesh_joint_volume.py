"""Tests de conservación de volumen de articulaciones (hombro, codo, muñeca)
sobre la malla real en test_project.blend a través de apply_pose().

Valida los criterios de aceptación del brief:
1. Área transversal conservada entre 80% y 120% del valor neutral en hombro, codo y muñeca
   a lo largo de toda la secuencia 0° -> 180° -> 0°.
2. Demostración de falla del comportamiento previo (concentración del 100% del roll en ForeArm),
   el cual colapsa la sección del codo a menos del 25% en skinning lineal.
"""
import math
import sys
from pathlib import Path

import numpy as np
import pytest

mathutils = pytest.importorskip("mathutils")
from mathutils import Matrix, Quaternion, Vector

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from puppet_mocap import retarget
from puppet_mocap.retarget import common, hands


def _load_real_mesh_fixture():
    blend_path = Path(__file__).resolve().parent.parent / "test_project.blend"
    if not blend_path.exists():
        pytest.skip("test_project.blend no encontrado")

    import bpy
    with bpy.data.libraries.load(str(blend_path)) as (data_from, data_to):
        data_to.objects = ["Armature", "tripo_node_2bbf9a47"]

    arm = data_to.objects[0]
    mesh_obj = data_to.objects[1]
    bpy.context.scene.collection.objects.link(arm)
    bpy.context.scene.collection.objects.link(mesh_obj)
    mesh_obj.parent = arm
    return arm, mesh_obj


def _cleanup_fixture(arm, mesh_obj):
    import bpy
    bpy.data.objects.remove(mesh_obj)
    bpy.data.objects.remove(arm)


def _get_joint_vertices(mesh_obj, vg1, vg2, center, max_dist):
    verts = []
    for v in mesh_obj.data.vertices:
        w1 = next((g.weight for g in v.groups if g.group == vg1.index), 0.0)
        w2 = next((g.weight for g in v.groups if g.group == vg2.index), 0.0)
        if w1 > 0.1 and w2 > 0.1:
            if (v.co - center).length < max_dist:
                verts.append(v.index)
    return verts


def _compute_section_area(mesh_obj, verts, limb_axis):
    import bpy
    bpy.context.view_layer.update()
    depsgraph = bpy.context.evaluated_depsgraph_get()
    eval_mesh = mesh_obj.evaluated_get(depsgraph).to_mesh()
    mw = mesh_obj.matrix_world
    pts = [mw @ eval_mesh.vertices[i].co.copy() for i in verts]
    mesh_obj.evaluated_get(depsgraph).to_mesh_clear()

    axis = limb_axis.normalized()
    up = Vector((0, 0, 1))
    u_axis = axis.cross(up)
    if u_axis.length < 1e-4:
        u_axis = Vector((0, 1, 0))
    u_axis.normalize()
    v_axis = axis.cross(u_axis).normalized()

    pts_2d = np.array([[p.dot(u_axis), p.dot(v_axis)] for p in pts])
    cov = np.cov(pts_2d, rowvar=False)
    det = np.linalg.det(cov)
    return math.pi * math.sqrt(max(det, 1e-12))


def _make_body_and_hand_landmarks(arm, sh_pos, el_pos, wr_pos, roll_deg):
    body_lms = [[0.0, 0.0, 0.0, 1.0] for _ in range(33)]
    sh_c = common.canonical_to_armature_space(arm, sh_pos)
    el_c = common.canonical_to_armature_space(arm, el_pos)
    wr_c = common.canonical_to_armature_space(arm, wr_pos)

    def to_mp(v):
        return [v.x, -v.z, v.y, 1.0]

    body_lms[11] = to_mp(sh_c)
    body_lms[12] = to_mp(Vector((-sh_c.x, sh_c.y, sh_c.z)))
    body_lms[13] = to_mp(el_c)
    body_lms[14] = to_mp(Vector((-el_c.x, el_c.y, el_c.z)))
    body_lms[15] = to_mp(wr_c)
    body_lms[16] = to_mp(Vector((-wr_c.x, wr_c.y, wr_c.z)))

    fwd_axis = (wr_c - el_c).normalized()
    up_ref = Vector((0, 0, 1))
    lat = fwd_axis.cross(up_ref).normalized()
    up = lat.cross(fwd_axis).normalized()

    rad = math.radians(roll_deg)
    R = Matrix.Rotation(rad, 3, fwd_axis)
    cur_lat = R @ lat
    cur_up = R @ up

    hand_pts = []
    base_offsets = [Vector((0, 0, 0)) for _ in range(21)]
    base_offsets[9] = 0.08 * fwd_axis
    base_offsets[5] = 0.06 * fwd_axis + 0.04 * cur_lat
    base_offsets[17] = 0.06 * fwd_axis - 0.04 * cur_lat
    base_offsets[1] = 0.02 * fwd_axis + 0.03 * cur_lat + 0.01 * cur_up

    for o in base_offsets:
        pt = wr_c + o
        hand_pts.append(to_mp(pt))

    return body_lms, hand_pts


def test_real_mesh_joint_volume_preservation_under_twist_sequence(monkeypatch):
    """Valida que hombro, codo y muñeca conservan entre 80% y 120% del área neutral
    durante la secuencia completa 0° -> 180° -> 0° a través de apply_pose().
    """
    arm, mesh_obj = _load_real_mesh_fixture()
    monkeypatch.setattr(retarget, "get_armature", lambda: arm)

    try:
        # Activar Preserve Volume recomendado
        for m in mesh_obj.modifiers:
            if m.type == "ARMATURE":
                m.use_deform_preserve_volume = True

        vg_sh = mesh_obj.vertex_groups.get("mixamorig:LeftShoulder")
        vg_u = mesh_obj.vertex_groups.get("mixamorig:LeftArm")
        vg_fa = mesh_obj.vertex_groups.get("mixamorig:LeftForeArm")
        vg_h = mesh_obj.vertex_groups.get("mixamorig:LeftHand")

        b_u = arm.data.bones["mixamorig:LeftArm"]
        b_fa = arm.data.bones["mixamorig:LeftForeArm"]
        b_h = arm.data.bones["mixamorig:LeftHand"]

        sh_pos = b_u.head_local
        el_pos = b_fa.head_local
        wr_pos = b_h.head_local

        sh_verts = _get_joint_vertices(mesh_obj, vg_sh, vg_u, sh_pos, max_dist=0.04)
        el_verts = _get_joint_vertices(mesh_obj, vg_u, vg_fa, el_pos, max_dist=0.03)
        wr_verts = _get_joint_vertices(mesh_obj, vg_fa, vg_h, wr_pos, max_dist=0.03)

        assert len(sh_verts) >= 30, "Debe haber vértices de hombro suficientes"
        assert len(el_verts) >= 20, "Debe haber vértices de codo suficientes"
        assert len(wr_verts) >= 50, "Debe haber vértices de muñeca suficientes"

        axis_u = (el_pos - sh_pos).normalized()
        axis_fa = (wr_pos - el_pos).normalized()

        common.reset_smoothing()
        body_0, hand_0 = _make_body_and_hand_landmarks(arm, sh_pos, el_pos, wr_pos, 0.0)
        retarget.apply_pose(
            landmarks=body_0,
            hands_data={"L": {"lm": hand_0, "hd": "Left"}},
            prefix="mixamorig:",
            sample_time=1.0,
            rotation_smooth=0.0,
            enable_body=True,
            enable_hands=True,
        )

        sh_0 = _compute_section_area(mesh_obj, sh_verts, axis_u)
        el_0 = _compute_section_area(mesh_obj, el_verts, axis_fa)
        wr_0 = _compute_section_area(mesh_obj, wr_verts, axis_fa)

        assert sh_0 > 1e-6 and el_0 > 1e-6 and wr_0 > 1e-6

        sequence = [0, 30, 60, 90, 120, 150, 180, 150, 120, 90, 60, 30, 0]
        for idx, deg in enumerate(sequence):
            t = 1.0 + (idx + 1) * 0.05
            b_lms, h_lms = _make_body_and_hand_landmarks(arm, sh_pos, el_pos, wr_pos, deg)
            retarget.apply_pose(
                landmarks=b_lms,
                hands_data={"L": {"lm": h_lms, "hd": "Left"}},
                prefix="mixamorig:",
                sample_time=t,
                rotation_smooth=0.0,
                enable_body=True,
                enable_hands=True,
            )

            sh_a = _compute_section_area(mesh_obj, sh_verts, axis_u)
            el_a = _compute_section_area(mesh_obj, el_verts, axis_fa)
            wr_a = _compute_section_area(mesh_obj, wr_verts, axis_fa)

            sh_ratio = sh_a / sh_0
            el_ratio = el_a / el_0
            wr_ratio = wr_a / wr_0

            assert 0.80 <= sh_ratio <= 1.20, f"Hombro fuera de rango en {deg}°: {sh_ratio:.1%}"
            assert 0.80 <= el_ratio <= 1.20, f"Codo fuera de rango en {deg}°: {el_ratio:.1%}"
            assert 0.80 <= wr_ratio <= 1.20, f"Muñeca fuera de rango en {deg}°: {wr_ratio:.1%}"

    finally:
        _cleanup_fixture(arm, mesh_obj)


def test_demonstrate_failure_with_old_isolated_forearm_roll():
    """Demuestra que concentrar el 100% del roll en ForeArm bajo Linear Blend Skinning
    destruye el área del codo (colapso a < 25%).
    """
    arm, mesh_obj = _load_real_mesh_fixture()

    try:
        # Linear blend skinning por defecto (sin Preserve Volume)
        for m in mesh_obj.modifiers:
            if m.type == "ARMATURE":
                m.use_deform_preserve_volume = False

        vg_u = mesh_obj.vertex_groups.get("mixamorig:LeftArm")
        vg_fa = mesh_obj.vertex_groups.get("mixamorig:LeftForeArm")
        b_fa = arm.data.bones["mixamorig:LeftForeArm"]
        el_pos = b_fa.head_local

        el_verts = _get_joint_vertices(mesh_obj, vg_u, vg_fa, el_pos, max_dist=0.03)
        b_h = arm.data.bones["mixamorig:LeftHand"]
        axis_fa = (b_h.head_local - el_pos).normalized()

        # Pose neutral
        for pb in arm.pose.bones:
            pb.rotation_quaternion = Quaternion((1, 0, 0, 0))
        el_0 = _compute_section_area(mesh_obj, el_verts, axis_fa)

        # Simular viejo comportamiento: 100% de 180° en ForeArm aislado
        pb_fa = arm.pose.bones["mixamorig:LeftForeArm"]
        pb_fa.rotation_quaternion = Quaternion(Vector((0, 1, 0)), math.radians(180))

        el_collapse = _compute_section_area(mesh_obj, el_verts, axis_fa)
        collapse_ratio = el_collapse / el_0

        # Debe colapsar severamente por la discontinuidad de 180° en el empalme
        assert collapse_ratio < 0.25, (
            f"El comportamiento previo debió colapsar el codo: {collapse_ratio:.1%}"
        )

    finally:
        _cleanup_fixture(arm, mesh_obj)
