"""Collect display triangles from a reopened OpticStudio model."""
import argparse
from pathlib import Path

import numpy as np
import trimesh

from make_views import render_views, section_evidence
from zos_runner import digest, load_api, new_application, write_json


def read_native_mesh(model, object_id):
    api = load_api()
    app = new_application(api)
    try:
        system = app.PrimarySystem
        if not system.LoadFile(str(model), False):
            raise RuntimeError("native_model_reopen_failed")
        if not 1 <= object_id <= system.NCE.NumberOfObjects:
            raise ValueError("invalid_native_object")
        tool = system.Tools.OpenShadedModelVisualizationExport()
        if tool is None:
            raise RuntimeError("native_visualization_unavailable")
        try:
            tool.Object = object_id
            tool.RunAndWaitForCompletion()
            results = tool.GetResults()
            vertices = []
            orders = (api.Tools.VertexOrder.First, api.Tools.VertexOrder.Second,
                      api.Tools.VertexOrder.Third)
            for triangle in range(results.NumberOfTriangles):
                for order in orders:
                    ok, x, y, z, _, _, _, _, actual_object = results.GetVertex(
                        triangle, order, 0., 0., 0., 0., 0., 0., 0, 0)
                    if not ok or actual_object != object_id:
                        raise RuntimeError("native_triangle_object_mismatch")
                    vertices.append((x, y, z))
        finally:
            tool.Close()
    finally:
        app.CloseApplication()

    if not vertices or not np.isfinite(vertices).all():
        raise RuntimeError(f"native_triangles_unavailable:object={object_id}")
    mesh = trimesh.Trimesh(vertices=vertices, faces=np.arange(len(vertices)).reshape(-1, 3))
    mesh.fix_normals()
    if not np.all(mesh.extents > 0) or not np.any(mesh.area_faces > 0):
        raise RuntimeError("native_mesh_degenerate")
    return mesh


def collect(model, object_id, output):
    mesh = read_native_mesh(model, object_id)
    output.mkdir(parents=True, exist_ok=True)
    paths = render_views(mesh, output / "native_views", face_views=True)
    write_json(output / "native_geometry_evidence.json", {
        "model_sha256": digest(model), "object_id": object_id,
        "bounds_mm": mesh.bounds.tolist(), "extents_mm": mesh.extents.tolist(),
        "triangle_count": len(mesh.faces), "watertight": bool(mesh.is_watertight),
        "volume_mm3": float(mesh.volume) if mesh.is_volume else None,
        "sections": section_evidence(mesh),
        "views": {path.name: digest(path) for path in paths},
    })


def collect_section(model, object_id, output, axis, coordinate):
    from matplotlib import pyplot as plt

    model_hash = digest(model)
    mesh = read_native_mesh(model, object_id)
    if digest(model) != model_hash:
        raise ValueError("Model changed during section measurement")
    normal = np.eye(3)["XYZ".index(axis)]
    section = mesh.section(plane_origin=normal * coordinate, plane_normal=normal)
    axes = [i for i, name in enumerate("XYZ") if name != axis]
    loops = [] if section is None else [path[:, axes] for path in section.discrete]
    status = "empty" if section is None else "open" if not section.is_closed or not loops else "closed"
    if loops:
        figure, plot = plt.subplots()
        try:
            for loop in loops:
                plot.plot(*loop.T)
            plot.set_aspect("equal")
            plot.set_xlabel("XYZ"[axes[0]] + " (mm)")
            plot.set_ylabel("XYZ"[axes[1]] + " (mm)")
            figure.savefig(output.with_suffix(".png"), dpi=140)
        finally:
            plt.close(figure)
    write_json(output, {"model_sha256": model_hash, "status": status, "axis": axis,
                        "coordinate_mm": coordinate, "outline_axes": "".join("XYZ"[i] for i in axes),
                        "outline_points_mm": [loop.round(6).tolist() for loop in loops]})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model", type=Path)
    parser.add_argument("object_id", type=int)
    parser.add_argument("output", type=Path)
    parser.add_argument("--section", nargs=2, metavar=("AXIS", "COORDINATE"))
    args = parser.parse_args()
    if args.section:
        axis, coordinate = args.section
        collect_section(args.model.resolve(), args.object_id, args.output.resolve(), axis, float(coordinate))
    else:
        collect(args.model.resolve(), args.object_id, args.output.resolve())
