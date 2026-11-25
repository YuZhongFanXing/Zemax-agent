import argparse
import hashlib
import json
from pathlib import Path
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.collections import LineCollection, PolyCollection
from mpl_toolkits.mplot3d.art3d import Poly3DCollection
import trimesh
import numpy as np


def section_evidence(mesh):
    """Return actual section outlines in model coordinates."""
    lower, upper = mesh.bounds
    sections = []
    for axis, name in enumerate("XYZ"):
        normal = np.eye(3)[axis]
        axes = [index for index in range(3) if index != axis]
        for fraction in (0.1, 0.5, 0.9):
            coordinate = float(lower[axis] + fraction * (upper[axis] - lower[axis]))
            section = mesh.section(plane_origin=normal * coordinate, plane_normal=normal)
            loops = [] if section is None else [
                np.round(loop[:, axes], 6).tolist() for loop in section.discrete
            ]
            sections.append({"normal_axis": name, "coordinate_mm": coordinate,
                             "outline_axes": "".join("XYZ"[index] for index in axes),
                             "outline_points_mm": loops})
    return sections


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--request", required=True); args = ap.parse_args()
    request_path = Path(args.request).resolve()
    request = json.loads(request_path.read_text(encoding="utf-8"))
    out = Path(request["output_dir"]).resolve()
    stl = out / request["stl_filename"]; mesh = trimesh.load_mesh(stl, force="mesh")
    if isinstance(mesh, trimesh.Scene): mesh = trimesh.util.concatenate(tuple(mesh.geometry.values()))
    paths = render_views(mesh, out / "export_views")
    manifest = {"source": str(stl), "views": [{"path": str(p), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for p in paths]}; (out / "view_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def render_views(mesh, view_dir, face_views=False):
    v = mesh.vertices; e = mesh.edges_unique; lo, hi = mesh.bounds; view_dir.mkdir(exist_ok=True)
    paths = []
    def ortho(name, axes, labels, reverse=False):
        fig, ax = plt.subplots(figsize=(8, 6), dpi=160); ax.add_collection(LineCollection(v[e][:, :, list(axes)], linewidths=0.3, color="#315b7d")); ax.set_xlim(lo[axes[0]], hi[axes[0]]); ax.set_ylim(lo[axes[1]], hi[axes[1]]); ax.set_aspect("equal", adjustable="box"); ax.set_xlabel(labels[0]); ax.set_ylabel(labels[1])
        if reverse:
            ax.invert_xaxis()
        fig.tight_layout(); p = view_dir / name; fig.savefig(p); plt.close(fig); return p
    paths += [ortho("front.png", (0, 2), ("x", "z")), ortho("back.png", (0, 2), ("x", "z"), reverse=True), ortho("side.png", (1, 2), ("y", "z"))]
    if face_views:
        triangles = v[mesh.faces]
        for name, sign in (("top", 1), ("bottom", -1)):
            order = np.argsort(sign * triangles.mean(axis=1)[:, 2])
            fig, ax = plt.subplots(figsize=(8, 6), dpi=160)
            xy = triangles[order, :, :2].copy()
            xy[:, :, 0] *= sign
            shading = plt.cm.Blues(0.35 + 0.45 * np.abs(mesh.face_normals[order, 2]))
            ax.add_collection(PolyCollection(xy, facecolors=shading,
                                             edgecolors=shading, linewidths=0.15))
            ax.autoscale()
            ax.set_aspect("equal")
            path = view_dir / f"{name}.png"
            fig.savefig(path, bbox_inches="tight")
            plt.close(fig)
            paths.append(path)
    section = mesh.section(plane_origin=[0, 0, (lo[2] + hi[2]) / 2], plane_normal=[0, 0, 1]); fig, ax = plt.subplots(figsize=(8, 3), dpi=160)
    if section is not None:
        planar, _ = section.to_2D()
        for entity in planar.entities: xy = planar.vertices[entity.points]; ax.plot(xy[:, 0], xy[:, 1], color="#315b7d", linewidth=0.8)
    ax.set_aspect("equal", adjustable="box"); fig.tight_layout(); p = view_dir / "end_section.png"; fig.savefig(p); plt.close(fig); paths.append(p)
    fig = plt.figure(figsize=(8, 6), dpi=160); ax = fig.add_subplot(111, projection="3d"); ax.add_collection3d(Poly3DCollection([v[f] for f in mesh.faces], facecolor="#b7cee6", edgecolor="#315b7d", linewidths=0.12)); ax.set_xlim(lo[0], hi[0]); ax.set_ylim(lo[1], hi[1]); ax.set_zlim(lo[2], hi[2]); ax.set_box_aspect(mesh.extents.copy()); ax.view_init(24, 118); fig.tight_layout(); p = view_dir / "axonometric.png"; fig.savefig(p); plt.close(fig); paths.append(p)
    return paths


if __name__ == "__main__": main()
