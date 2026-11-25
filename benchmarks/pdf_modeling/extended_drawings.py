"""Generate fully dimensioned profile and through-hole benchmark drawings."""
import math

import fitz


PROFILES = {
    "double_step": [(0, 0), (24, 0), (24, 10), (17, 10), (17, 13), (8, 13), (8, 16), (0, 16)],
    "asymmetric_bevel": [(0, 0), (24, 0), (24, 9), (18, 15), (4, 15), (0, 11)],
    "edge_slot": [(0, 0), (24, 0), (24, 15), (15, 15), (15, 9), (10, 9), (10, 15), (0, 15)],
    "side_notch": [(0, 0), (24, 0), (24, 6), (19, 6), (19, 11), (24, 11), (24, 16), (0, 16)],
    "dovetail": [(0, 0), (24, 0), (24, 7), (20, 7), (23, 15), (1, 15), (4, 7), (0, 7)],
    "offset_shoulder": [(0, 0), (24, 0), (24, 8), (19, 8), (19, 16), (6, 16), (6, 12), (0, 12)],
    "offset_trapezoid": [(0, 0), (24, 0), (22, 15), (7, 15)],
    "corner_reliefs": [(0, 0), (21, 0), (24, 3), (24, 13), (21, 16), (4, 16), (0, 12)],
    "sloped_crown": [(0, 0), (24, 0), (24, 10), (15, 16), (5, 16), (0, 12)],
    "twin_slots": [(0, 0), (24, 0), (24, 15), (18, 15), (18, 9), (15, 9),
                   (15, 15), (9, 15), (9, 9), (6, 9), (6, 15), (0, 15)],
}

HOLES = [
    [(8, 4, 1.5), (16, 4, 1.0)],
    [(7, 4, 1.0), (12, 4, 1.2), (17, 4, 1.0)],
    [(7, 3, 1.1), (12, 6.5, 1.4), (17, 3, 1.1)],
    [(7, 3, 1.0), (11, 6.5, 1.0), (15, 3, 1.0), (18, 6.5, 1.0)],
    [(6, 4, 0.8), (9, 4, 0.8), (12, 4, 1.25), (15, 4, 0.8), (18, 4, 0.8)],
]


def generate_extended(root):
    drawings = root / "drawings"
    drawings.mkdir(parents=True)
    references = {}
    for family, outline in PROFILES.items():
        for index, holes in enumerate(HOLES, 1):
            case = f"{family}_{index:02d}"
            width = 4 + index
            height = max(y for _, y in outline)
            area = abs(sum(x * v - y * u for (x, y), (u, v) in
                           zip(outline, outline[1:] + outline[:1]))) / 2
            references[case] = {
                "extents_mm": sorted([24, height, width]),
                "volume_mm3": width * (area - sum(math.pi * r*r for _, _, r in holes)),
                "extrusion_mm": width,
                "hole_count": len(holes),
                "hole_diameters_mm": sorted(2 * radius for _, _, radius in holes),
            }
            with fitz.open() as document:
                page = document.new_page(width=720, height=620)
                page.insert_text((38, 35), case.replace("_", " ").title(), fontsize=18)
                page.insert_text((38, 62), "Millimetres. Linear tolerance +/-0.05 mm.", fontsize=12)
                page.insert_text((38, 96), "SECTION NORMAL TO THROUGH HOLES", fontsize=12)
                transform = lambda x, y: fitz.Point(48 + 13*x, 327 - 13*y)
                vertices = [transform(x, y) for x, y in outline]
                page.draw_polyline(vertices + vertices[:1])
                page.insert_text((38, 347), "Datum (0, 0) at lower left. +X right, +Y up.", fontsize=11)
                for number, (x, y, radius) in enumerate(holes, 1):
                    center = transform(x, y)
                    page.draw_circle(center, radius*13)
                    page.insert_text((center.x - 6, center.y + 3), str(number), fontsize=9)
                page.insert_text((400, 96), "OUTER VERTICES (X, Y)", fontsize=12)
                for number, (x, y) in enumerate(outline, 1):
                    page.insert_text((400, 98 + 17*number),
                                     f"{number:02d}     {x:5.2f}     {y:5.2f}", fontsize=11)
                page.insert_text((400, 324), "Join in order and close the profile.", fontsize=11)
                page.insert_text((38, 380), "THROUGH HOLES (NO., X, Y, DIAMETER)", fontsize=12)
                for number, (x, y, radius) in enumerate(holes, 1):
                    page.insert_text((38, 384 + 19*number),
                                     f"{number}     {x:.2f}     {y:.2f}     {2*radius:.2f}", fontsize=11)
                page.insert_text((38, 510), f"Constant section for full extrusion length {width:.2f} mm.",
                                 fontsize=12)
                page.insert_text((38, 534), "All holes cylindrical and through the full length.", fontsize=12)
                page.insert_text((38, 558), "Sharp edges. No further features.", fontsize=12)
                document.save(drawings / f"{case}.pdf")
    return references
