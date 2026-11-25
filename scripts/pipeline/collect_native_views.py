"""Collect display triangles from a reopened OpticStudio model."""
import argparse
from pathlib import Path

import numpy as np
import trimesh

from make_views import render_views, section_evidence
from zos_runner import digest, load_api, new_application, write_json


def collect(model, object_id, output):
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


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model", type=Path)
    parser.add_argument("object_id", type=int)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    collect(args.model.resolve(), args.object_id, args.output.resolve())
