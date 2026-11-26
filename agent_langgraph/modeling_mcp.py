"""Run-bound MCP tools for drawing inspection and native modeling."""
import asyncio
import base64
import json
import math
import re
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Literal

from scripts.pipeline.stepped_bore import expand_stepped_bore

from .pipeline import PIPELINE, digest, read_json, run_script, write_json

MODEL_VIEWS = ("axonometric", "back", "bottom", "end_section", "front", "side", "top")


def create_server(workspace, filename):
    import fitz
    from mcp.server.fastmcp import FastMCP

    server = FastMCP("zemax-modeling")
    server.tool()(expand_stepped_bore)
    pdf = (workspace / "input" / filename).resolve()
    if pdf.parent != (workspace / "input").resolve():
        raise ValueError("invalid staged PDF")

    def attempt_dir(attempt):
        if not re.fullmatch(r"attempt_[0-9]{2,}", attempt):
            raise ValueError("invalid attempt identifier")
        path = (workspace / "attempts" / attempt).resolve()
        if path.parent != workspace / "attempts" or not path.is_dir():
            raise ValueError("attempt does not belong to this run")
        return path

    @server.tool()
    def inspect_drawing(page: int = 0, region: list[float] | None = None) -> dict:
        """Read a page or zoom into a normalized [left, top, right, bottom] rectangle."""
        with fitz.open(pdf) as document:
            if page == 0 and region is None:
                return {"page_count": len(document)}
            if not 1 <= page <= len(document):
                raise ValueError("page outside drawing")
            source = document[page - 1]
            clip = source.rect
            if region is not None:
                if (len(region) != 4 or any(not math.isfinite(n) or not 0 <= n <= 1 for n in region)
                        or region[0] >= region[2] or region[1] >= region[3]):
                    raise ValueError("region must be a nonempty normalized rectangle")
                l, t, r, b = region
                clip = fitz.Rect(clip.x0 + l * clip.width, clip.y0 + t * clip.height,
                                 clip.x0 + r * clip.width, clip.y0 + b * clip.height)
            dpi = 130 if region is None else min(400, max(130, int(140000 / max(clip.width, clip.height))))
            image = source.get_pixmap(dpi=dpi, clip=clip).tobytes("png")
            return {"page": page, "text": source.get_text(clip=clip),
                    "png": base64.b64encode(image).decode("ascii")}

    @server.tool()
    def validate_plan() -> dict:
        """Validate this run's declarative geometry plan."""
        from .responses import validate_spec

        validate_spec(workspace / "geometry_spec.json", filename)
        return {"valid": True}

    @server.tool()
    def build_model(attempt: str) -> dict:
        """Build, save and read back a native model using the fixed adapter."""
        path = attempt_dir(attempt)
        run_script(PIPELINE / "zos_runner.py", path / "request.json", path / "agent.log")
        return read_json(path / "native_result.json")

    @server.tool()
    def measure_model(attempt: str) -> dict:
        """Extract geometry evidence and generate views in one pass from the reopened model."""
        path = attempt_dir(attempt)
        result = read_json(path / "native_result.json")
        model_hash = digest(path / "model.zos")
        if model_hash != result["model_sha256"]:
            raise ValueError("model changed after build")
        command = [sys.executable, str(PIPELINE / "collect_native_views.py"),
                   str(path / "model.zos"), str(result["model"]["final_object_id"]), str(path)]
        with (path / "agent.log").open("a", encoding="utf-8") as log:
            native = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, timeout=60)
        evidence_path = path / "native_geometry_evidence.json"
        evidence = read_json(evidence_path) if native.returncode == 0 else None
        if evidence is None or not evidence.get("watertight") or evidence.get("volume_mm3") is None:
            import trimesh
            from scripts.pipeline.make_views import render_views, section_evidence

            stl = path / "model.stl"
            if not result.get("stl_sha256") or not stl.is_file() or digest(stl) != result["stl_sha256"]:
                if evidence is not None:
                    return evidence
                raise RuntimeError("native geometry unavailable; see agent.log")
            mesh = trimesh.load_mesh(stl, force="mesh")
            if mesh.is_empty or mesh.bounds is None:
                if evidence is not None:
                    return evidence
                raise RuntimeError("native geometry unavailable and STL contains no geometry")
            mesh.fix_normals()
            if evidence is not None and not mesh.is_volume:
                return evidence
            views = render_views(mesh, path / "native_views", face_views=True)
            write_json(evidence_path, {
                "model_sha256": model_hash,
                "source": "verified_stl",
                "bounds_mm": mesh.bounds.tolist(),
                "extents_mm": mesh.extents.tolist(),
                "watertight": bool(mesh.is_volume),
                "volume_mm3": float(mesh.volume) if mesh.is_volume else None,
                "sections": section_evidence(mesh),
                "views": {file.name: digest(file) for file in views},
            })
        return read_json(path / "native_geometry_evidence.json")

    @server.tool()
    def measure_section(attempt: str, axis: Literal["X", "Y", "Z"], coordinate: float) -> dict:
        """Measure a section normal to the given axis at a model coordinate in mm."""
        if not math.isfinite(coordinate):
            raise ValueError("Non-finite section coordinate")
        path = attempt_dir(attempt)
        result = read_json(path / "native_result.json")
        model = path / "model.zos"
        model_hash = result["model_sha256"]
        if digest(model) != model_hash:
            raise ValueError("model changed after build")
        output = path / f"section_{uuid.uuid4().hex}.json"
        command = [sys.executable, str(PIPELINE / "collect_native_views.py"), str(model),
                   str(result["model"]["final_object_id"]), str(output),
                   "--section", axis, str(coordinate)]
        with (path / "agent.log").open("a", encoding="utf-8") as log:
            subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, timeout=60, check=True)
        evidence = read_json(output)
        if evidence["model_sha256"] != model_hash:
            raise ValueError("Section evidence does not match the built model")
        image = output.with_suffix(".png")
        if image.is_file():
            evidence["png"] = base64.b64encode(image.read_bytes()).decode("ascii")
        return evidence

    @server.tool()
    def render_model_views(attempt: str) -> dict:
        """Return views generated from the same geometry used by measure_model."""
        path = attempt_dir(attempt)
        evidence = read_json(path / "native_geometry_evidence.json")
        if digest(path / "model.zos") != evidence["model_sha256"]:
            raise ValueError("model changed after measurement")
        images = {}
        for name in MODEL_VIEWS:
            file = path / "native_views" / f"{name}.png"
            if digest(file) != evidence["views"][file.name]:
                raise ValueError(f"view changed after measurement: {name}")
            images[name] = base64.b64encode(file.read_bytes()).decode("ascii")
        return images

    return server


async def _call(workspace, filename, tool, arguments):
    from mcp.shared.memory import create_connected_server_and_client_session

    server = create_server(workspace.resolve(), filename)
    async with create_connected_server_and_client_session(server) as session:
        if tool is None:
            return [entry.name for entry in (await session.list_tools()).tools]
        result = await session.call_tool(tool, arguments)
    if result.isError:
        raise ValueError(f"{tool}: {result.content[0].text}")
    if result.structuredContent is not None:
        return result.structuredContent
    return json.loads(result.content[0].text)


def call(workspace, filename, tool, **arguments):
    return asyncio.run(_call(workspace, filename, tool, arguments))


if __name__ == "__main__":
    create_server(Path(sys.argv[1]).resolve(), sys.argv[2]).run(transport="stdio")
