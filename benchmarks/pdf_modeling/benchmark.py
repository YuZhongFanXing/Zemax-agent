"""Generate dimensioned benchmark PDFs and score native modeling runs."""
import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import fitz


CASES = {
    "step_block": ([(0, 0), (12, 0), (12, 3), (7, 3), (7, 5), (0, 5)], 6,
                   "Overall length 12.00. Left plateau length 7.00 and height 5.00.",
                   "Right plateau length 5.00 and height 3.00."),
    "chamfer_block": ([(0, 0), (10, 0), (10, 4), (8, 6), (0, 6)], 4,
                      "Overall length 10.00 and height 6.00.",
                      "Upper right chamfer 2.00 horizontal x 2.00 vertical."),
    "trapezoid_block": ([(0, 0), (14, 0), (11, 5), (3, 5)], 7,
                        "Bottom length 14.00. Top length 8.00. Height 5.00.",
                        "Top edge centered, with 3.00 inset at each end."),
}


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def generate(root):
    drawings = root / "drawings"
    drawings.mkdir(parents=True)
    answers = {}
    for name, (profile, width, first_note, second_note) in CASES.items():
        length = max(x for x, _ in profile)
        height = max(y for _, y in profile)
        area = abs(sum(x * v - y * u for (x, y), (u, v) in
                       zip(profile, profile[1:] + profile[:1]))) / 2
        with fitz.open() as document:
            page = document.new_page(width=660, height=380)
            for y, label in [
                (38, name.replace("_", " ").title()),
                (66, "Mechanical model. All dimensions in mm. General linear tolerance +/-0.05 mm."),
                (102, "PROFILE VIEW"), (257, first_note), (279, second_note),
                (306, f"Constant solid profile through full width {width:.2f} mm."),
                (328, "Side faces perpendicular to the profile. Sharp edges except as shown."),
            ]:
                page.insert_text((40, y), label, fontsize=13)
            page.insert_text((375, 102), "TOP VIEW", fontsize=13)
            outline = [(40 + 18*x, 232 - 18*y) for x, y in profile]
            page.draw_polyline(outline + outline[:1])
            page.draw_rect((375, 118, 375 + 18*length, 118 + 16*width))
            for x in sorted({x for x, _ in profile if 0 < x < length}):
                page.draw_line((375 + 18*x, 118), (375 + 18*x, 118 + 16*width))
            page.get_pixmap().save(drawings / f"{name}.png")
            document.save(drawings / f"{name}.pdf")
        answers[name] = {"extents_mm": sorted([length, height, width]),
                         "volume_mm3": area * width}
    write_json(root / "reference.json", answers)


def score(workspace, reference):
    reports = sorted((workspace / "attempts").glob("*/verification.json"))
    final = workspace / "final" / "verification.json"
    report = final if final.is_file() else reports[-1] if reports else None
    if report is None:
        return {"pass": False, "reason": "no verification report"}
    verification = json.loads(report.read_text(encoding="utf-8"))
    evidence = json.loads((report.parent / "native_geometry_evidence.json").read_text(encoding="utf-8"))
    model = report.parent / "model.zos"
    provenance = hashlib.sha256(model.read_bytes()).hexdigest() == evidence["model_sha256"]
    actual = sorted(evidence["extents_mm"])
    dimensions_ok = all(abs(a - b) <= .05 for a, b in zip(actual, reference["extents_mm"], strict=True))
    volume = evidence.get("volume_mm3")
    volume_ok = volume is not None and abs(volume - reference["volume_mm3"]) <= .02 * reference["volume_mm3"]
    checks = {**verification["gates"], "evidence_hash": provenance,
              "dimensions": dimensions_ok, "volume": volume_ok}
    if "hole_count" in reference:
        sections = evidence["sections"]
        axes = "XYZ"
        checks["through_hole_count"] = any(
            abs(evidence["extents_mm"][axes.index(axis)] - reference["extrusion_mm"]) <= .05
            and all(len(section["outline_points_mm"]) == reference["hole_count"] + 1
                    for section in axial_sections)
            for axis in axes
            if len(axial_sections := [section for section in sections
                                      if section["normal_axis"] == axis]) >= 3
        )
        if "hole_diameters_mm" in reference:
            checks["hole_diameters"] = any(
                abs(evidence["extents_mm"][axes.index(axis)] - reference["extrusion_mm"]) <= .05
                and all(section_diameters_match(section, reference["hole_diameters_mm"])
                        for section in axial_sections)
                for axis in axes
                if len(axial_sections := [section for section in sections
                                          if section["normal_axis"] == axis]) >= 3
            )
    return {"pass": all(checks.values()), "checks": checks, "actual_extents_mm": actual,
            "actual_volume_mm3": volume, "reference": reference, "report": str(report)}


def section_diameters_match(section, expected):
    spans = sorted(
        (tuple(max(coords) - min(coords) for coords in zip(*loop, strict=True))
         for loop in section["outline_points_mm"]),
        key=lambda span: span[0] * span[1],
    )
    holes = spans[:-1]
    return len(holes) == len(expected) and all(
        abs(width - diameter) <= .05 and abs(height - diameter) <= .05
        for (width, height), diameter in zip(holes, expected)
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--extended", action="store_true")
    parser.add_argument("--generate-only", action="store_true")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    root = args.output.resolve()
    if not args.resume:
        if args.extended:
            from extended_drawings import generate_extended
            write_json(root / "reference.json", generate_extended(root))
        else:
            generate(root)
    if args.generate_only:
        return
    answers = json.loads((root / "reference.json").read_text(encoding="utf-8"))
    results_file = root / "results.json"
    results = json.loads(results_file.read_text(encoding="utf-8")) if results_file.exists() else {}
    for name, reference in answers.items():
        if name in results:
            continue
        workspace = root / "runs" / name
        if workspace.exists():
            number = 2
            while (root / "runs" / f"{name}_run{number}").exists():
                number += 1
            workspace = root / "runs" / f"{name}_run{number}"
        command = [sys.executable, "-m", "agent_langgraph.runner", "--responses",
                   "--pdf", str(root / "drawings" / f"{name}.pdf"), "--workspace",
                   str(workspace), "--max-attempts", "2", "--max-minutes", "10"]
        with (root / f"{name}.log").open("w", encoding="utf-8") as log:
            process = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT)
        try:
            result = score(workspace, reference)
        except (ValueError, KeyError, OSError) as exc:
            result = {"pass": False, "reason": f"scoring error: {exc}"}
        results[name] = {**result, "agent_exit": process.returncode, "workspace": str(workspace)}
        write_json(root / "results.json", results)
        print(f"{name}: {'PASS' if results[name]['pass'] else 'FAIL'}", flush=True)


if __name__ == "__main__":
    main()
