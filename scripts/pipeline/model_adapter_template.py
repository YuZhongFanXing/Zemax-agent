"""Task-specific model adapter template.

The blind-session model fills this file from the permitted source document.
The generic runner owns application lifecycle, save/reopen, export and result
recording. This file owns only object creation and object readback.

ZOSAPI construction rules:
- ChangeType requires settings: row.ChangeType(row.GetObjectTypeSettings(ObjectType.X)).
- Common native names include RectangularVolume and CylinderVolume; do not invent
  enum names such as ObjectType.Cylinder.
- After building, readback must verify the object count, final object type, parent
  IDs and operation parameters before export.
"""

from __future__ import annotations

import math
from pathlib import Path


def user_aperture_dir() -> Path:
    """Return the OpticStudio user aperture directory used by Extruded UDA objects."""
    path = Path.home() / "Documents" / "Zemax" / "Objects" / "Apertures"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _signed_area(points: list[tuple[float, float]]) -> float:
    return 0.5 * sum(x0 * y1 - x1 * y0 for (x0, y0), (x1, y1) in zip(points, points[1:] + points[:1]))


def _winding(points: list[tuple[float, float]], ccw: bool) -> list[tuple[float, float]]:
    """Return a contour with the requested UDA winding."""
    if (_signed_area(points) > 0) is ccw:
        return points
    return list(reversed(points))


def write_contour_uda(path: Path, outer: list[tuple[float, float]], holes: list[tuple[float, float, float]], samples: int = 48) -> Path:
    """Write one outer contour plus circular inner contours in Zemax UDA format.

    UDA uses opposite winding for islands and holes. Normalizing it here avoids
    the common failure where the exported mesh has the right outline but the
    apparent holes become filled material.
    """
    outer = _winding(list(outer), ccw=True)
    lines = [*(f"LIN {x:.9f} {y:.9f}" for x, y in outer), "BRK"]
    for cx, cy, radius in holes:
        points = [(cx + radius * math.cos(2 * math.pi * i / samples),
                   cy + radius * math.sin(2 * math.pi * i / samples)) for i in range(samples)]
        points = _winding(points, ccw=False)
        lines.extend(f"LIN {x:.9f} {y:.9f}" for x, y in points)
        lines.append("BRK")
    path.write_text("\n".join(lines) + "\n", encoding="ascii")
    return path


def configure_extruded(row, api, uda_name: str, length_mm: float, z_mm: float = 0.0,
                       rear_scale_y: float = 1.0) -> None:
    """Bind a UDA and configure the verified Extruded parameter cells."""
    obj_type = api.Editors.NCE.ObjectType.Extruded
    settings = row.GetObjectTypeSettings(obj_type)
    settings.FileName1 = uda_name
    if not row.ChangeType(settings):
        raise RuntimeError("extruded_type_change_failed")
    row.GetCellAt(1).set_Value(uda_name)
    if row.GetCellAt(1).Value != uda_name:
        raise RuntimeError(f"uda_filename_not_accepted:{row.GetCellAt(1).Value}")
    row.GetCellAt(4).DoubleValue = 0.0
    row.GetCellAt(5).DoubleValue = 0.0
    row.GetCellAt(6).DoubleValue = float(z_mm)
    row.GetCellAt(11).DoubleValue = float(length_mm)
    row.GetCellAt(12).DoubleValue = 1.0
    row.GetCellAt(13).DoubleValue = 1.0
    row.GetCellAt(14).DoubleValue = 1.0
    row.GetCellAt(15).DoubleValue = float(rear_scale_y)


def configure_boolean_union(row, api, parent_ids: list[int], material: str | None = None) -> None:
    """Configure the verified A+B+... BooleanNative parent layout."""
    if not 1 <= len(parent_ids) <= 3:
        raise ValueError("BooleanNative template supports one to three parents")
    settings = row.GetObjectTypeSettings(api.Editors.NCE.ObjectType.BooleanNative)
    if not row.ChangeType(settings):
        raise RuntimeError("boolean_native_change_type_failed")
    row.Comment = "+".join(chr(65 + i) for i in range(len(parent_ids)))
    for index, parent_id in enumerate(parent_ids, start=5):
        row.GetCellAt(index).IntegerValue = int(parent_id)
    if material:
        row.Material = material


def build(system, api, request, app=None):
    """Create the candidate named by request['representation_strategy']."""
    raise NotImplementedError("implement build() from the permitted source")


def readback(system, api, request, build_metadata):
    """Read back objects from the reopened system and return final_object_id."""
    raise NotImplementedError("implement readback() from the permitted source")


def validate_export(mesh, request):
    """Measure actual STL geometry and return pass, numeric_pass and metrics."""
    raise NotImplementedError("implement independent geometry checks from the permitted source")
