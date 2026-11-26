"""Translate a stepped bore into native cylindrical cuts."""
import math
import re


ROTATIONS = {
    "+X": [0, 90, 0], "-X": [0, -90, 0],
    "+Y": [-90, 0, 0], "-Y": [90, 0, 0],
    "+Z": [0, 0, 0], "-Z": [180, 0, 0],
}


def expand_stepped_bore(target, result_id, entrance, direction, segments, exit_distance=None):
    """Segments run from the mouth inward, with decreasing diameters and cumulative depths."""
    if any(not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", name)
           for name in (target, result_id)) or target == result_id:
        raise ValueError("Use distinct valid target and result IDs")
    if direction not in ROTATIONS or len(entrance) != 3 or not all(map(math.isfinite, entrance)):
        raise ValueError("Provide a finite XYZ entrance and a signed coordinate axis")
    if not 1 <= len(segments) <= 16:
        raise ValueError("Provide between 1 and 16 bore segments")
    axis = "XYZ".index(direction[1])
    sign = 1 if direction[0] == "+" else -1
    start = 0
    previous_diameter = math.inf
    operations = []
    for index, segment in enumerate(segments, 1):
        diameter, depth = segment["diameter"], segment["depth"]
        if not (math.isfinite(diameter) and math.isfinite(depth)
                and 0 < diameter < previous_diameter and depth > start):
            raise ValueError("Diameters must decrease and cumulative depths must increase")
        last = index == len(segments)
        if last and exit_distance is not None and not math.isclose(depth, exit_distance, abs_tol=1e-8):
            raise ValueError("Final cumulative depth must equal the through-hole exit distance")
        # Each cutter starts outside the mouth. Overlapping cuts avoid coincident shoulders.
        position = list(entrance)
        position[axis] -= sign * 0.001
        cutter = f"{result_id}__cutter_{index}"
        output = result_id if last else f"{result_id}__cut_{index}"
        operations.extend([
            {"id": cutter, "kind": "cylinder", "radius": diameter / 2,
             "length": depth + (0.002 if last and exit_distance is not None else 0.001),
             "position": position, "rotation": ROTATIONS[direction]},
            {"id": output, "kind": "subtract", "parents": [target, cutter]},
        ])
        target, start, previous_diameter = output, depth, diameter
    return {"operations": operations}
