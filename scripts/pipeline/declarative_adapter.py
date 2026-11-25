"""Interpret validated modeling operations with fixed ZOSAPI mappings."""
import json
import math
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from model_adapter_template import configure_extruded, user_aperture_dir, write_contour_uda


def operations(spec, selected):
    plan = next((item for item in spec["representation_candidates"] if item["id"] == selected), None)
    if not plan or not isinstance(plan.get("operations"), list) or not 1 <= len(plan["operations"]) <= 200:
        raise ValueError("candidate requires 1–200 operations")
    expanded = []
    for entry in plan["operations"]:
        count = entry.get("count", 1)
        if type(count) is not int or not 1 <= count <= 200 or len(expanded) + count > 200:
            raise ValueError("array exceeds 200 objects")
        for index in range(count):
            item = dict(entry)
            if count > 1:
                item["id"] = f"{entry['id']}_{index}"
                item["position"] = [a + index*b for a,b in zip(entry.get("position", [0,0,0]), entry["step"], strict=True)]
            expanded.append(item)
    identifiers = set()
    def vector(value, size, positive=False):
        if (not isinstance(value, list) or len(value) != size
                or any(type(x) not in (int,float) or not math.isfinite(x)
                       or abs(x) > 1e6 or (positive and x <= 0) for x in value)):
            raise ValueError(f"expected {size} finite dimensions")
    for item in expanded:
        identifier = item["id"]
        if not isinstance(identifier,str) or not identifier or identifier in identifiers:
            raise ValueError("duplicate or invalid id")
        vector(item.get("position",[0,0,0]),3)
        vector(item.get("rotation",[0,0,0]),3)
        kind = item["kind"]
        if kind == "box":
            vector(item["size"],3,True)
        elif kind == "cylinder":
            vector([item["radius"],item["length"]],2,True)
        elif kind == "extrude":
            vector([item["length"],item.get("rear_scale_y",1)],2,True)
            if not 3 <= len(item["points"]) <= 256:
                raise ValueError("extrude needs 3–256 vertices")
            for point in item["points"]:
                vector(point,2)
            holes = item.get("holes", [])
            if not isinstance(holes, list) or len(holes) > 200:
                raise ValueError("extrude supports up to 200 through holes")
            for hole in holes:
                vector(hole, 3)
                if hole[2] <= 0:
                    raise ValueError("through-hole radius must be positive")
        elif kind in ("union","subtract"):
            parents = item.get("parents")
            if (not isinstance(parents, list) or len(parents) != 2
                    or any(not isinstance(parent, str) or parent not in identifiers for parent in parents)
                    or parents[0] == parents[1]):
                raise ValueError(f"{identifier}: {kind} requires parents=[first_prior_id, second_prior_id]")
        else:
            raise ValueError(f"unsupported operation: {kind}")
        identifiers.add(identifier)
    return expanded


def build(system, api, request):
    spec = json.loads(Path(request["geometry_spec"]).read_text(encoding="utf-8"))
    nce = system.NCE
    types = api.Editors.NCE.ObjectType
    columns = api.Editors.NCE.ObjectColumn
    records = {}
    aperture_files = []
    for index, item in enumerate(operations(spec, request["representation_strategy"]), 1):
        if index > nce.NumberOfObjects:
            nce.InsertNewObjectAt(index)
        row = nce.GetObjectAt(index)
        kind = item["kind"]
        names = {"box":"RectangularVolume","cylinder":"CylinderVolume","extrude":"Extruded",
                 "union":"BooleanNative","subtract":"BooleanNative"}
        params = {}
        if kind == "extrude":
            filename = f"lg_{uuid.uuid4().hex}.uda"
            path = write_contour_uda(user_aperture_dir() / filename, item["points"],
                                     item.get("holes", []))
            aperture_files.append(path.name)
            configure_extruded(row, api, filename, item["length"], rear_scale_y=item.get("rear_scale_y",1))
        else:
            if not row.ChangeType(row.GetObjectTypeSettings(getattr(types,names[kind]))):
                raise RuntimeError(f"object type change failed: {names[kind]}")
            if kind == "box":
                x,y,z = item["size"]
                params.update(Par1=x/2, Par2=y/2, Par3=z)
            elif kind == "cylinder":
                # CylinderVolume uses independent front and back radii.
                params.update(Par1=item["radius"], Par2=item["length"], Par3=item["radius"])
            else:
                row.Comment = "A+B" if kind == "union" else "A-B"
                params.update(Par5=records[item["parents"][0]]["index"],
                              Par6=records[item["parents"][1]]["index"])
                for parent in item["parents"]:
                    nce.GetObjectAt(records[parent]["index"]).DrawData.DoNotDrawObject = True
            for column,value in params.items():
                cell = row.GetObjectCell(getattr(columns,column))
                try:
                    if type(value) is int and kind in ("union","subtract"):
                        cell.IntegerValue = value
                    else:
                        cell.DoubleValue = float(value)
                except Exception as exc:
                    raise RuntimeError(f"{names[kind]} {column}: {exc}") from exc
        position = item.get("position",[0,0,0])
        row.XPosition,row.YPosition,row.ZPosition = map(float,position)
        rotation = item.get("rotation", [0, 0, 0])
        row.TiltAboutX,row.TiltAboutY,row.TiltAboutZ = map(float, rotation)
        if item.get("material"):
            row.Material = item["material"]
        records[item["id"]] = {"index":index,"type":names[kind],"params":params,
                                 "expression":str(row.Comment),"position":position,
                                 "rotation":rotation}
    if aperture_files:
        import shutil
        output = Path(request["output_dir"]) / "apertures"
        output.mkdir(exist_ok=True)
        for filename in aperture_files:
            shutil.copy2(user_aperture_dir()/filename,output/filename)
    return {"objects":list(records.values()),"final_object_id":index}


def readback(system, api, request, build_metadata):
    columns = api.Editors.NCE.ObjectColumn
    checks = []
    for expected in build_metadata["objects"]:
        row = system.NCE.GetObjectAt(expected["index"])
        actual_type = str(row.Type)
        valid = actual_type == expected["type"] and str(row.Comment) == expected["expression"]
        valid &= all(math.isclose(actual, target, abs_tol=1e-8) for actual,target in
                     zip((row.XPosition,row.YPosition,row.ZPosition),expected["position"]))
        if "rotation" in expected:
            actual_rotation = (row.TiltAboutX, row.TiltAboutY, row.TiltAboutZ)
            valid &= all(abs((actual - target + 180) % 360 - 180) <= 1e-8
                         for actual, target in zip(actual_rotation, expected["rotation"]))
        for column,value in expected["params"].items():
            cell = row.GetObjectCell(getattr(columns,column))
            actual = cell.IntegerValue if actual_type == "BooleanNative" else cell.DoubleValue
            valid &= math.isclose(actual,value,abs_tol=1e-8)
        checks.append({"index":expected["index"],"type":actual_type,"pass":bool(valid)})
    return {"pass":all(item["pass"] for item in checks),"objects":checks,
            "object_type":checks[-1]["type"],"final_object_id":build_metadata["final_object_id"]}
